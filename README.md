# disc-to-jellyfin

Automatic DVD/Blu-ray ripping pipeline for Windows. Insert a disc, walk away — it rips, compresses, transfers to your Jellyfin server, and ejects the disc.

```
Disc in drive → MakeMKV rip → HandBrake H.265 → SCP to Jellyfin → Library refresh → Eject
```

## Features

- **Automatic disc detection** — polls the drive, starts when a disc is inserted
- **TMDb lookup** — gets the correct movie title and year (e.g. `FROZEN_II` → `Frozen 2 (2019)`)
- **H.265 compression** — configurable quality (default RF 18)
- **Audio/subtitle filtering** — keep only the languages you want, with surround passthrough
- **Duplicate title handling** — Disney/studio copy protection with 50+ fake titles? Keeps only the real one
- **SCP transfer** — sends finished files to your Jellyfin server over SSH
- **Jellyfin API refresh** — triggers a library scan automatically
- **Auto eject** — disc ejects when done
- **Windows toast notifications** — get notified when a movie is finished
- **Progress bar** — visual progress in console and window title bar
- **Zero dependencies** — pure Python stdlib, no pip install needed

## Requirements

- **Windows 10/11**
- **Python 3.10+** — [python.org](https://www.python.org/downloads/)
- **MakeMKV** — [makemkv.com](https://www.makemkv.com/download/) (free during beta)
- **HandBrakeCLI** — [handbrake.fr](https://handbrake.fr/downloads2.php) (download the CLI version)
- **OpenSSH** — for SCP transfer (built into Windows 10/11)
- **SSH key access** — to your Jellyfin server (password-less login)

## Setup

1. **Clone the repo:**
   ```
   git clone https://github.com/SergentISAF/disc-to-jellyfin.git
   cd disc-to-jellyfin
   ```

2. **Download HandBrakeCLI** from [handbrake.fr](https://handbrake.fr/downloads2.php) and place `HandBrakeCLI.exe` in the folder (or update the path in config).

3. **Copy the example config:**
   ```
   copy config.example.json config.json
   ```

4. **Edit `config.json`** with your settings:
   - `drive_letter` — your optical drive letter
   - `makemkv_path` — path to `makemkvcon64.exe`
   - `handbrake_path` — path to `HandBrakeCLI.exe`
   - `jellyfin_host` / `jellyfin_api_key` — your Jellyfin server details
   - `scp_user` / `scp_host` / `scp_dest` — SSH transfer destination
   - `tmdb_api_key` — free API key from [themoviedb.org](https://www.themoviedb.org/settings/api) (optional but recommended)
   - `audio_languages` / `subtitle_languages` — ISO 639-2 codes, comma-separated (e.g. `dan,eng`)

5. **Run it:**
   Double-click `START_AUTORIP.bat` or run:
   ```
   python auto_rip.py
   ```

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `drive_letter` | `H` | Optical drive letter |
| `poll_interval_seconds` | `10` | How often to check for a disc |
| `handbrake_quality` | `18` | RF value (lower = better quality, bigger file) |
| `audio_languages` | `dan,eng` | Audio tracks to keep (ISO 639-2) |
| `subtitle_languages` | `dan,eng` | Subtitle tracks to keep (ISO 639-2) |
| `min_title_seconds` | `3600` | Minimum title length to rip (filters trailers/extras) |
| `delete_raw_after_compress` | `true` | Delete raw MKV after HandBrake compression |
| `delete_done_after_transfer` | `false` | Delete compressed file after SCP transfer |
| `tmdb_api_key` | `""` | TMDb API key for movie name lookup |

## How it works

1. **Poll** — checks the drive every 10 seconds for a disc
2. **TMDb** — looks up the disc label to get the proper movie title and year
3. **MakeMKV** — rips all titles longer than `min_title_seconds`, then keeps only the largest file (handles studio duplicate protection)
4. **HandBrake** — compresses to H.265 MKV with configured quality, audio passthrough (keeps surround), and selected language subtitles
5. **SCP** — transfers to your Jellyfin server via SSH
6. **Jellyfin** — triggers a library refresh via API
7. **Eject** — opens the disc tray
8. **Notify** — Windows toast notification with movie name and elapsed time
9. **Wait** — ready for the next disc

## Notes

- MakeMKV requires a license key (free beta key available at [makemkv.com/forum](https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053))
- SSH key-based login must be configured for SCP transfer to work without prompts
- The script uses only Python standard library — no pip packages required
- Logs are written to `auto_rip.log` in the script directory

## License

[MIT](LICENSE)
