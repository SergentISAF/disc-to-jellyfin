"""
Auto-Rip GUI — Tkinter programflade til DVD/Blu-ray ripping pipeline.
Importerer auto_rip.py som motor og viser progress, status og log.
"""

import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import auto_rip

# ──────────────────────────────────────────────
# Custom log handler → tekst-widget
# ──────────────────────────────────────────────

class TextWidgetHandler(logging.Handler):
    def __init__(self, gui):
        super().__init__()
        self.gui = gui

    def emit(self, record):
        msg = self.format(record)
        self.gui._thread_safe(self.gui._append_log, msg)


# ──────────────────────────────────────────────
# Hoved-GUI
# ──────────────────────────────────────────────

class AutoRipGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Auto-Rip DVD/Blu-ray")
        self.root.geometry("620x520")
        self.root.minsize(500, 400)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.cfg = auto_rip.load_config()
        self.running = False
        self.disc_present = False
        self._poll_id = None
        self._gui_queue = queue.Queue()

        self._build_ui()
        self._setup_logging()
        self._register_callbacks()
        self._process_queue()

    # ──────────────────────────────────────────
    # UI opbygning
    # ──────────────────────────────────────────

    def _build_ui(self):
        # Top-frame: status + knap
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="Klar — tryk Start")
        ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 11)).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_start = ttk.Button(top, text="Start", width=10, command=self._start)
        self.btn_start.pack(side=tk.RIGHT, padx=(10, 0))

        # Film-titel
        title_frame = ttk.Frame(self.root, padding=(10, 0, 10, 5))
        title_frame.pack(fill=tk.X)

        ttk.Label(title_frame, text="Film:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        self.title_var = tk.StringVar(value="(ingen)")
        ttk.Label(title_frame, textvariable=self.title_var, font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(5, 0))

        # Progress-bars
        prog_frame = ttk.LabelFrame(self.root, text="Fremskridt", padding=10)
        prog_frame.pack(fill=tk.X, padx=10, pady=5)

        # MakeMKV
        ttk.Label(prog_frame, text="MakeMKV Rip:").grid(row=0, column=0, sticky=tk.W)
        self.rip_pct_var = tk.StringVar(value="0%")
        ttk.Label(prog_frame, textvariable=self.rip_pct_var, width=6).grid(row=0, column=2, padx=(5, 0))
        self.rip_bar = ttk.Progressbar(prog_frame, length=400, mode="determinate")
        self.rip_bar.grid(row=0, column=1, sticky=tk.EW, pady=2)

        # HandBrake
        ttk.Label(prog_frame, text="HandBrake H.265:").grid(row=1, column=0, sticky=tk.W)
        self.encode_pct_var = tk.StringVar(value="0%")
        ttk.Label(prog_frame, textvariable=self.encode_pct_var, width=6).grid(row=1, column=2, padx=(5, 0))
        self.encode_bar = ttk.Progressbar(prog_frame, length=400, mode="determinate")
        self.encode_bar.grid(row=1, column=1, sticky=tk.EW, pady=2)

        prog_frame.columnconfigure(1, weight=1)

        # Log-area
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

        self.log_text = tk.Text(
            log_frame,
            bg="#1e1e1e", fg="#d4d4d4",
            font=("Consolas", 9),
            wrap=tk.WORD,
            state=tk.DISABLED,
            height=12,
        )
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    # ──────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────

    def _setup_logging(self):
        handler = TextWidgetHandler(self)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        auto_rip.log.addHandler(handler)

    def _append_log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ──────────────────────────────────────────
    # Thread-sikker GUI-opdatering
    # ──────────────────────────────────────────

    def _thread_safe(self, func, *args):
        self._gui_queue.put((func, args))

    def _process_queue(self):
        try:
            while True:
                func, args = self._gui_queue.get_nowait()
                func(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._process_queue)

    # ──────────────────────────────────────────
    # Callbacks fra auto_rip
    # ──────────────────────────────────────────

    def _register_callbacks(self):
        auto_rip._on_progress = lambda stage, pct: self._thread_safe(self._update_progress, stage, pct)
        auto_rip._on_status = lambda status: self._thread_safe(self._update_status, status)
        auto_rip._on_title_resolved = lambda title: self._thread_safe(self._update_title, title)
        auto_rip._on_title_input = self._on_title_input

    def _update_progress(self, stage: str, pct: float):
        if stage == "rip":
            self.rip_bar["value"] = pct
            self.rip_pct_var.set(f"{pct:.0f}%")
            self.root.title(f"Auto-Rip: MakeMKV {pct:.0f}%")
        elif stage == "encode":
            self.encode_bar["value"] = pct
            self.encode_pct_var.set(f"{pct:.1f}%")
            self.root.title(f"Auto-Rip: HandBrake {pct:.1f}%")

    def _update_status(self, status: str):
        self.status_var.set(status)

    def _update_title(self, title: str):
        self.title_var.set(title)

    def _on_title_input(self, disc_label, metadata_titles):
        """Vises som popup-dialog. Blokerer worker-tråden via Event."""
        result = {"value": ""}
        event = threading.Event()

        def _show_dialog():
            dialog = tk.Toplevel(self.root)
            dialog.title("Ukendt film")
            dialog.geometry("400x220")
            dialog.transient(self.root)
            dialog.grab_set()
            dialog.resizable(False, False)

            frame = ttk.Frame(dialog, padding=15)
            frame.pack(fill=tk.BOTH, expand=True)

            ttk.Label(frame, text=f"Disc-label: {disc_label}", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
            if metadata_titles:
                ttk.Label(frame, text=f"Metadata: {', '.join(metadata_titles)}").pack(anchor=tk.W, pady=(2, 0))
            ttk.Label(frame, text="TMDb kunne ikke finde filmen automatisk.").pack(anchor=tk.W, pady=(8, 0))
            ttk.Label(frame, text="Indtast filmnavn (tom = brug disc-label):").pack(anchor=tk.W, pady=(8, 0))

            entry = ttk.Entry(frame, width=45)
            entry.pack(fill=tk.X, pady=(5, 0))
            entry.focus_set()

            def _submit(evt=None):
                result["value"] = entry.get().strip()
                dialog.destroy()
                event.set()

            entry.bind("<Return>", _submit)
            ttk.Button(frame, text="OK", command=_submit, width=12).pack(pady=(12, 0))

            dialog.bind("<Escape>", lambda e: _submit())

        self._gui_queue.put((_show_dialog, ()))
        event.wait()
        return result["value"]

    # ──────────────────────────────────────────
    # Start / Stop
    # ──────────────────────────────────────────

    def _start(self):
        self.running = True
        self.disc_present = False
        self.btn_start.config(text="Stop", command=self._stop)
        self._update_status(f"Venter på disc i {self.cfg['drive_letter']}:...")
        self._append_log("Auto-Rip startet")
        auto_rip._wait_for_handbrake()
        self._poll_tick()

    def _stop(self):
        self.running = False
        if self._poll_id:
            self.root.after_cancel(self._poll_id)
            self._poll_id = None
        for proc in auto_rip._active_procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        auto_rip._active_procs.clear()
        self._update_status("Stoppet")
        self.btn_start.config(text="Start", command=self._start)
        self.root.title("Auto-Rip DVD/Blu-ray")

    # ──────────────────────────────────────────
    # Disc polling
    # ──────────────────────────────────────────

    def _poll_tick(self):
        if not self.running:
            return
        threading.Thread(target=self._check_disc, daemon=True).start()
        interval = self.cfg.get("poll_interval_seconds", 10) * 1000
        self._poll_id = self.root.after(interval, self._poll_tick)

    def _check_disc(self):
        label = auto_rip.detect_disc(self.cfg["drive_letter"])
        if label and not self.disc_present:
            self.disc_present = True
            self._thread_safe(self._update_status, f"Disc fundet: {label}")
            time.sleep(15)
            self._run_pipeline(label)
        elif not label and self.disc_present:
            self.disc_present = False

    def _run_pipeline(self, disc_label):
        try:
            # Reset progress
            self._thread_safe(self._update_progress, "rip", 0)
            self._thread_safe(self._update_progress, "encode", 0)
            self._thread_safe(self._update_title, "(ingen)")

            auto_rip.run_pipeline(self.cfg, disc_label)

            # Disc ejected i run_pipeline — klar til næste
            self.disc_present = False
            if self.running:
                self._thread_safe(self._update_status, f"Venter på disc i {self.cfg['drive_letter']}:...")
                self._thread_safe(self._update_progress, "rip", 0)
        except Exception as e:
            auto_rip.log.error("Pipeline fejl: %s", e)
            self._thread_safe(self._update_status, f"Fejl: {e}")

    # ──────────────────────────────────────────
    # Luk vindue
    # ──────────────────────────────────────────

    def _on_close(self):
        if self.running:
            if not messagebox.askokcancel("Afslut", "Pipeline kører stadig. Vil du stoppe og lukke?"):
                return
            self._stop()
        self.root.destroy()


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    root = tk.Tk()
    AutoRipGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
