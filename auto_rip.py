"""
Auto-Rip — Automatisk DVD/Blu-ray ripping pipeline
Disc → MakeMKV → HandBrake H.265 → SCP til Jellyfin → Eject

Midlertidig Windows-løsning indtil Proxmox NAS med ARM er klar.
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "auto_rip.log"

# Kø-lock: kun én HandBrake/SCP-proces ad gangen
_encode_lock = threading.Lock()
# Aktive child-processer (til cleanup ved stop)
_active_procs: list = []

# Logging — fil + konsol
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("auto_rip")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_output(proc):
    """Yield linjer fra process stdout, splitter på både \\r og \\n.
    MakeMKV og HandBrake bruger \\r til progress-opdateringer."""
    buf = b""
    while True:
        byte = proc.stdout.read(1)
        if not byte:
            if buf:
                yield buf.decode("utf-8", errors="replace")
            break
        if byte in (b"\r", b"\n"):
            if buf:
                yield buf.decode("utf-8", errors="replace")
                buf = b""
        else:
            buf += byte


def _set_title(text: str):
    """Opdatér konsol-vinduets titellinje."""
    os.system(f"title {text}")


# ---------------------------------------------------------------------------
# Disc-detektion
# ---------------------------------------------------------------------------

def detect_disc(drive_letter: str) -> str | None:
    """Returnerer disc-label hvis der er en disc i drevet, ellers None."""
    root = f"{drive_letter}:\\"
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
    # 5 = DRIVE_CDROM
    if drive_type != 5:
        return None

    vol_name = ctypes.create_unicode_buffer(261)
    serial = ctypes.wintypes.DWORD()
    max_len = ctypes.wintypes.DWORD()
    flags = ctypes.wintypes.DWORD()
    fs_name = ctypes.create_unicode_buffer(261)

    result = ctypes.windll.kernel32.GetVolumeInformationW(
        root,
        vol_name, 261,
        ctypes.byref(serial),
        ctypes.byref(max_len),
        ctypes.byref(flags),
        fs_name, 261,
    )
    if result == 0:
        return None

    label = vol_name.value.strip()
    return label if label else "UNKNOWN_DISC"


# ---------------------------------------------------------------------------
# MakeMKV rip
# ---------------------------------------------------------------------------

def rip_disc(cfg: dict, disc_label: str) -> Path | None:
    """Rip alle titler > min_title_seconds fra disc:0. Returnerer output-mappen."""
    makemkv = cfg["makemkv_path"]
    if not Path(makemkv).exists():
        log.error("MakeMKV ikke fundet: %s", makemkv)
        return None

    # Rens titlen til et mappenavn
    safe_name = sanitize_name(disc_label)
    out_dir = Path(cfg["raw_dir"]) / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)

    min_secs = cfg.get("min_title_seconds", 120)

    log.info("=== MakeMKV rip starter: %s ===", disc_label)
    log.info("Output: %s", out_dir)

    cmd = [
        makemkv,
        "--robot",
        "--progress=-stdout",
        "mkv",
        "disc:0",
        "all",
        str(out_dir),
        "--minlength=%d" % min_secs,
    ]
    log.info("Kommando: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _active_procs.append(proc)
        for line in _iter_output(proc):
            if line.startswith("PRGV:"):
                _print_makemkv_progress(line, disc_label)
            elif line.startswith("PRGT:"):
                parts = line.split(",", 3)
                if len(parts) >= 4:
                    log.info("MakeMKV trin: %s", parts[3].strip('"'))
            elif line.startswith("MSG:"):
                log.info("MakeMKV: %s", line)
        print()
        _set_title("Auto-Rip DVD/Blu-ray")
        proc.wait()
        _active_procs.remove(proc)

        if proc.returncode != 0:
            log.error("MakeMKV fejlede med kode %d", proc.returncode)
            return None

    except Exception as e:
        log.error("MakeMKV fejl: %s", e)
        return None

    # Tjek at der faktisk blev rippet filer
    mkv_files = list(out_dir.glob("*.mkv"))
    if not mkv_files:
        log.error("Ingen MKV-filer produceret i %s", out_dir)
        return None

    log.info("MakeMKV færdig: %d filer i %s", len(mkv_files), out_dir)

    # Disney-beskyttelse: hvis flere titler, behold kun én
    # Mange discs har identiske duplikater (samme størrelse) — indholdet er det samme,
    # så det er ligegyldigt hvilken der beholdes.
    if len(mkv_files) > 1:
        mkv_files.sort(key=lambda f: f.stat().st_size, reverse=True)
        largest = mkv_files[0]
        largest_size = largest.stat().st_size
        largest_gb = largest_size / (1024 ** 3)
        dupes = mkv_files[1:]
        identical = sum(1 for f in dupes if f.stat().st_size == largest_size)

        if identical == len(dupes):
            log.info("Alle %d titler er identiske (%.2f GB) — duplikat-beskyttelse, beholder én",
                     len(mkv_files), largest_gb)
        elif identical > 0:
            log.info("%d titler fundet: %d identiske duplikater + %d andre — beholder største",
                     len(mkv_files), identical + 1, len(dupes) - identical)
        else:
            log.info("%d titler fundet med forskellige størrelser — beholder største (%.2f GB)",
                     len(mkv_files), largest_gb)

        log.info("  Beholder: %s (%.2f GB)", largest.name, largest_gb)
        for dupe in dupes:
            dupe_gb = dupe.stat().st_size / (1024 ** 3)
            log.info("  Sletter:  %s (%.2f GB)", dupe.name, dupe_gb)
            dupe.unlink()

    return out_dir


def _print_makemkv_progress(line: str, disc_label: str):
    """Parse MakeMKV PRGV-linje og vis progress-bar."""
    try:
        vals = line.split(":")[1].split(",")
        if len(vals) >= 3:
            current = int(vals[0])
            maximum = int(vals[2])
            if maximum > 0:
                pct = current * 100 // maximum
                bar_width = 30
                filled = bar_width * current // maximum
                bar = "█" * filled + "░" * (bar_width - filled)
                print(f"\r  RIP  [{bar}] {pct}%", end="", flush=True)
                _set_title(f"Auto-Rip: MakeMKV {pct}% — {disc_label}")
    except (IndexError, ValueError):
        pass


# ---------------------------------------------------------------------------
# HandBrake komprimering
# ---------------------------------------------------------------------------

def compress(cfg: dict, raw_dir: Path) -> list[Path]:
    """Komprimer alle MKV-filer i raw_dir med HandBrake H.265. Returnerer liste af output-filer."""
    handbrake = cfg["handbrake_path"]
    if not Path(handbrake).exists():
        log.error("HandBrakeCLI ikke fundet: %s", handbrake)
        log.error("Download fra https://handbrake.fr/downloads2.php og placér i auto-rip mappen")
        return []

    quality = cfg.get("handbrake_quality", 18)
    audio_langs = cfg.get("audio_languages", "dan,eng")
    sub_langs = cfg.get("subtitle_languages", "dan,eng")
    done_dir = Path(cfg["done_dir"]) / raw_dir.name
    done_dir.mkdir(parents=True, exist_ok=True)

    mkv_files = sorted(raw_dir.glob("*.mkv"))
    output_files = []

    for i, mkv in enumerate(mkv_files, 1):
        out_file = done_dir / mkv.name
        log.info("=== HandBrake %d/%d: %s ===", i, len(mkv_files), mkv.name)

        cmd = [
            handbrake,
            "-i", str(mkv),
            "-o", str(out_file),
            "-e", "x265",
            "-q", str(quality),
            "-f", "av_mkv",
            "--audio-lang-list", audio_langs,
            "--all-audio",
            "--aencoder", "copy",
            "--audio-fallback", "ac3",
            "--subtitle-lang-list", sub_langs,
            "--all-subtitles",
        ]
        log.info("Kommando: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            _active_procs.append(proc)
            for line in _iter_output(proc):
                if "Encoding:" in line and "%" in line:
                    match = re.search(r"(\d+\.\d+)\s*%", line)
                    if match:
                        pct = float(match.group(1))
                        bar_width = 30
                        filled = int(bar_width * pct / 100)
                        bar = "█" * filled + "░" * (bar_width - filled)
                        print(f"\r  H.265 [{bar}] {pct:.1f}%", end="", flush=True)
                        _set_title(f"Auto-Rip: HandBrake {pct:.1f}% — {mkv.name}")
                elif "average encoding speed" in line.lower():
                    print()
                    _set_title("Auto-Rip DVD/Blu-ray")
                    log.info("HandBrake: %s", line)
            print()
            _set_title("Auto-Rip DVD/Blu-ray")
            proc.wait()
            _active_procs.remove(proc)

            if proc.returncode != 0:
                log.error("HandBrake fejlede med kode %d for %s", proc.returncode, mkv.name)
                continue

            if out_file.exists() and out_file.stat().st_size > 0:
                raw_size = mkv.stat().st_size / (1024 ** 3)
                done_size = out_file.stat().st_size / (1024 ** 3)
                log.info("Komprimeret: %.2f GB → %.2f GB (%.0f%% reduktion)",
                         raw_size, done_size, (1 - done_size / raw_size) * 100)
                output_files.append(out_file)
            else:
                log.error("Output-fil mangler eller tom: %s", out_file)

        except Exception as e:
            log.error("HandBrake fejl for %s: %s", mkv.name, e)

    # Slet raw-filer hvis konfigureret
    if output_files and cfg.get("delete_raw_after_compress", True):
        log.info("Sletter raw-mappe: %s", raw_dir)
        shutil.rmtree(raw_dir, ignore_errors=True)

    return output_files


# ---------------------------------------------------------------------------
# SCP overførsel
# ---------------------------------------------------------------------------

def transfer(cfg: dict, files: list[Path], folder_name: str) -> bool:
    """Overfør filer til Jellyfin-server via SCP."""
    user = cfg["scp_user"]
    host = cfg["scp_host"]
    dest = cfg["scp_dest"]
    remote_dir = f"{dest}/{folder_name}"

    log.info("=== SCP overførsel til %s@%s:%s ===", user, host, remote_dir)

    # Opret remote-mappe
    mkdir_cmd = ["ssh", f"{user}@{host}", f"mkdir -p '{remote_dir}'"]
    try:
        result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("Kunne ikke oprette remote-mappe: %s", result.stderr)
            return False
    except Exception as e:
        log.error("SSH fejl: %s", e)
        return False

    all_ok = True
    for f in files:
        remote_path = f"{user}@{host}:{remote_dir}/{f.name}"
        log.info("Overfører: %s → %s", f.name, remote_path)

        scp_cmd = ["scp", "-o", "ConnectTimeout=10", str(f), remote_path]
        try:
            proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=7200)
            if proc.returncode != 0:
                log.error("SCP fejlede for %s: %s", f.name, proc.stderr)
                all_ok = False
            else:
                size_gb = f.stat().st_size / (1024 ** 3)
                log.info("Overført: %s (%.2f GB)", f.name, size_gb)
        except subprocess.TimeoutExpired:
            log.error("SCP timeout for %s (>2 timer)", f.name)
            all_ok = False
        except Exception as e:
            log.error("SCP fejl for %s: %s", f.name, e)
            all_ok = False

    # Slet done-filer hvis konfigureret og alle overført OK
    if all_ok and cfg.get("delete_done_after_transfer", False):
        done_dir = files[0].parent
        log.info("Sletter done-mappe: %s", done_dir)
        shutil.rmtree(done_dir, ignore_errors=True)

    return all_ok


# ---------------------------------------------------------------------------
# Jellyfin refresh
# ---------------------------------------------------------------------------

def refresh_jellyfin(cfg: dict):
    """Trigger Jellyfin library scan via API."""
    host = cfg["jellyfin_host"]
    port = cfg["jellyfin_port"]
    api_key = cfg["jellyfin_api_key"]
    url = f"http://{host}:{port}/Library/Refresh"

    log.info("Jellyfin library refresh...")

    req = urllib.request.Request(url, method="POST")
    req.add_header("X-Emby-Token", api_key)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("Jellyfin refresh OK (status %d)", resp.status)
    except Exception as e:
        log.error("Jellyfin refresh fejl: %s", e)


# ---------------------------------------------------------------------------
# Disc eject
# ---------------------------------------------------------------------------

def eject_disc():
    """Skub disc ud via Windows MCI."""
    log.info("Skubber disc ud...")
    winmm = ctypes.windll.winmm
    buf = ctypes.create_unicode_buffer(256)

    winmm.mciSendStringW("open cdaudio alias cd", buf, 256, None)
    winmm.mciSendStringW("set cd door open", buf, 256, None)
    winmm.mciSendStringW("close cd", buf, 256, None)

    log.info("Disc skubbet ud")


# ---------------------------------------------------------------------------
# Windows-notifikation
# ---------------------------------------------------------------------------

def notify(title: str, msg: str):
    """Vis Windows toast-notifikation. Fallback til MessageBox."""
    log.info("Notifikation: %s — %s", title, msg)

    try:
        # Prøv PowerShell toast først
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] > $null; "
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::"
            "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$text = $template.GetElementsByTagName('text'); "
            f"$text.Item(0).AppendChild($template.CreateTextNode('{_escape_ps(title)}')) > $null; "
            f"$text.Item(1).AppendChild($template.CreateTextNode('{_escape_ps(msg)}')) > $null; "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Auto-Rip')"
            ".Show($toast)"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, timeout=10,
        )
    except Exception:
        # Fallback: MessageBox (blokerer ikke — kører i ny tråd ville kræve threading)
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
        except Exception:
            pass


def _escape_ps(s: str) -> str:
    """Escape enkelt-anførselstegn til PowerShell."""
    return s.replace("'", "''")


def push_notify(cfg: dict, title: str, msg: str):
    """Send push-notifikation via Ntfy til mobil."""
    topic = cfg.get("ntfy_topic", "")
    if not topic:
        return

    base = cfg.get("ntfy_url", "https://ntfy.sh")
    url = f"{base}/{topic}"
    data = msg.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", "cd,movie_camera")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("Ntfy push sendt (status %d)", resp.status)
    except Exception as e:
        log.warning("Ntfy push fejlede: %s", e)


# ---------------------------------------------------------------------------
# TMDb titel-opslag
# ---------------------------------------------------------------------------

def _disc_label_to_query(label: str) -> str:
    """Konvertér disc-label til søgeord. F.eks. 'FROZEN_II' → 'Frozen II'."""
    # Fjern kendte prefixer (studios lægger dem ofte foran)
    prefixes = ["DISNEY_", "PIXAR_", "MARVEL_", "DC_", "FOX_", "WARNER_", "SONY_", "UNIVERSAL_"]
    query = label.upper()
    for prefix in prefixes:
        if query.startswith(prefix):
            query = query[len(prefix):]
            break
    # Underscores og bindestreger til mellemrum
    query = query.replace("_", " ").replace("-", " ")
    # Indsæt mellemrum mellem bogstaver og tal (TERMINATOR3 → TERMINATOR 3)
    query = re.sub(r"([A-Za-z])(\d)", r"\1 \2", query)
    query = re.sub(r"(\d)([A-Za-z])", r"\1 \2", query)
    # Fjern ekstra mellemrum og titlecase
    query = " ".join(query.split()).title()
    return query


def lookup_tmdb(cfg: dict, disc_label: str) -> str | None:
    """Slå disc-label op i TMDb og returnér 'Titel (År)' eller None."""
    api_key = cfg.get("tmdb_api_key", "")
    if not api_key:
        log.debug("TMDb API-nøgle ikke sat — springer opslag over")
        return None

    query = _disc_label_to_query(disc_label)
    log.info("TMDb søgning: \"%s\" (fra disc-label: %s)", query, disc_label)

    params = urllib.parse.urlencode({"api_key": api_key, "query": query, "language": "da-DK"})
    url = f"https://api.themoviedb.org/3/search/movie?{params}"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = data.get("results", [])
        if not results:
            # Prøv igen på engelsk hvis dansk ikke gav resultater
            params = urllib.parse.urlencode({"api_key": api_key, "query": query, "language": "en-US"})
            url = f"https://api.themoviedb.org/3/search/movie?{params}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])

        if not results:
            log.warning("TMDb: Ingen resultater for \"%s\"", query)
            return None

        # Brug første resultat (højest relevans)
        movie = results[0]
        title = movie.get("title", "")
        release = movie.get("release_date", "")
        year = release[:4] if release else ""

        if title and year:
            tmdb_name = f"{title} ({year})"
        elif title:
            tmdb_name = title
        else:
            return None

        log.info("TMDb fundet: \"%s\"", tmdb_name)
        return tmdb_name

    except Exception as e:
        log.warning("TMDb opslag fejlede: %s", e)
        return None


# ---------------------------------------------------------------------------
# Hjælpefunktioner
# ---------------------------------------------------------------------------

def _wait_for_handbrake():
    """Vent på at en evt. eksisterende HandBrake-proces afslutter (fra tidligere kørsel)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq HandBrakeCLI.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        if "HandBrakeCLI.exe" in result.stdout:
            log.info("HandBrake kører allerede (fra tidligere session) — venter...")
            push_notify(load_config(), "Auto-Rip", "Venter på igangværende HandBrake encode")
            while True:
                time.sleep(30)
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq HandBrakeCLI.exe", "/NH"],
                    capture_output=True, text=True, timeout=10,
                )
                if "HandBrakeCLI.exe" not in result.stdout:
                    log.info("Forrige HandBrake encode afsluttet")
                    break
    except Exception:
        pass


def sanitize_name(name: str) -> str:
    """Gør et disc-label til et sikkert mappenavn."""
    # Erstat ugyldige tegn
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)
    # Fjern ledende/trailing whitespace og punktummer
    safe = safe.strip(". ")
    return safe if safe else "UNKNOWN"


# ---------------------------------------------------------------------------
# Hovedpipeline
# ---------------------------------------------------------------------------

def run_pipeline(cfg: dict, disc_label: str):
    """Rip disc → eject → start post-processing i baggrunden.
    Returnerer hurtigt så hovedløkken kan polle for ny disc."""
    log.info("=" * 60)
    log.info("PIPELINE START: %s", disc_label)
    log.info("=" * 60)

    # 0. TMDb opslag — find korrekt filmnavn
    tmdb_name = lookup_tmdb(cfg, disc_label)
    if tmdb_name:
        folder_name = sanitize_name(tmdb_name)
        display_name = tmdb_name
    else:
        folder_name = sanitize_name(disc_label)
        display_name = disc_label
        log.info("Bruger disc-label som mappenavn: %s", folder_name)

    # 1. Rip (bruger drevet — blokerer)
    raw_dir = rip_disc(cfg, disc_label)
    if raw_dir is None:
        notify("Auto-Rip Fejl", f"MakeMKV fejlede for {display_name}")
        push_notify(cfg, "Auto-Rip Fejl", f"MakeMKV fejlede for {display_name}")
        return

    # 2. Eject med det samme — drevet er frit til næste disc
    eject_disc()
    push_notify(cfg, "Disc klar", f"{display_name} rippet — indsæt næste disc")

    # 3. Post-processing i baggrundstråd (HandBrake → SCP → Jellyfin)
    thread = threading.Thread(
        target=_post_process,
        args=(cfg, raw_dir, folder_name, display_name),
        daemon=True,
    )
    thread.start()


def _post_process(cfg: dict, raw_dir, folder_name: str, display_name: str):
    """Compress → transfer → refresh. Kører i baggrundstråd med kø-lock."""
    # Vent på at evt. forrige encode er færdig (kun én HandBrake ad gangen)
    if _encode_lock.locked():
        log.info("HandBrake kø: %s venter på forrige encode...", display_name)
        push_notify(cfg, "Auto-Rip Kø", f"{display_name} venter på forrige encode")
    _encode_lock.acquire()

    try:
        start_time = time.time()
        log.info("=== Post-processing starter: %s ===", display_name)

        # Compress
        done_files = compress(cfg, raw_dir)
        if not done_files:
            notify("Auto-Rip Fejl", f"HandBrake fejlede for {display_name}")
            push_notify(cfg, "Auto-Rip Fejl", f"HandBrake fejlede for {display_name}")
            return

        # Transfer
        transfer_ok = transfer(cfg, done_files, folder_name)
        if not transfer_ok:
            notify("Auto-Rip Advarsel", f"{display_name} overført med fejl — tjek loggen")
            push_notify(cfg, "Auto-Rip Advarsel", f"{display_name} overført med fejl")

        # Jellyfin refresh
        refresh_jellyfin(cfg)

        elapsed = time.time() - start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)

        msg = f"{display_name} færdig på {hours}t {minutes}m"
        if transfer_ok:
            msg += " — overført til Jellyfin"
        else:
            msg += " — filer ligger lokalt (SCP fejl)"

        log.info("PIPELINE FÆRDIG: %s", msg)
        notify("Auto-Rip Færdig", msg)
        push_notify(cfg, "Auto-Rip Færdig", msg)
    finally:
        _encode_lock.release()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Auto-Rip startet")
    log.info("=" * 60)

    cfg = load_config()
    drive = cfg["drive_letter"]
    interval = cfg.get("poll_interval_seconds", 10)

    # Validér at MakeMKV findes
    if not Path(cfg["makemkv_path"]).exists():
        log.error("MakeMKV ikke fundet: %s", cfg["makemkv_path"])
        log.error("Installer MakeMKV eller ret stien i config.json")
        input("Tryk Enter for at lukke...")
        return

    # Advar hvis HandBrake mangler
    if not Path(cfg["handbrake_path"]).exists():
        log.warning("HandBrakeCLI ikke fundet: %s", cfg["handbrake_path"])
        log.warning("Download fra https://handbrake.fr/downloads2.php")
        log.warning("Scriptet fortsætter, men komprimering vil fejle")

    # Tjek om HandBrake allerede kører (fra evt. tidligere kørsel)
    _wait_for_handbrake()

    log.info("Drev: %s:", drive)
    log.info("MakeMKV: %s", cfg["makemkv_path"])
    log.info("HandBrake: %s", cfg["handbrake_path"])
    log.info("Raw-output: %s", cfg["raw_dir"])
    log.info("Done-output: %s", cfg["done_dir"])
    log.info("Jellyfin: %s:%s", cfg["jellyfin_host"], cfg["jellyfin_port"])
    log.info("")
    log.info("Venter på disc i %s:...", drive)

    disc_was_present = False

    try:
        while True:
            label = detect_disc(drive)

            if label and not disc_was_present:
                # Ny disc detekteret
                log.info("Disc fundet: %s", label)
                disc_was_present = True

                # Pause så drevet kan spinne op (Blu-ray kan tage 10+ sek)
                time.sleep(15)

                run_pipeline(cfg, label)

                # Disc blev ejected i run_pipeline — nulstil så næste disc detekteres
                disc_was_present = False
                log.info("")
                log.info("Venter på ny disc i %s:...", drive)

            elif not label and disc_was_present:
                # Disc fjernet manuelt
                disc_was_present = False

            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("")
        log.info("Auto-Rip stopper — afslutter aktive processer...")
        for proc in _active_procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
                log.info("  Stoppet: PID %d", proc.pid)
            except Exception:
                proc.kill()
        log.info("Auto-Rip stoppet af bruger")


if __name__ == "__main__":
    main()
