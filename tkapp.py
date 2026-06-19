#!/usr/bin/env python3
"""
tkapp.py — STUB: the offline / browserless front end (Tkinter, classic look).

Thin client over engine.py for environments with no browser. Demonstrates the
two patterns the Tk side hinges on:

  1. CONCURRENCY: a stage runs on a worker *thread*; it can't touch widgets, so
     it pushes ProgressEvents into a queue.Queue. The Tk main loop drains the
     queue every 100 ms via .after() and updates the UI. This keeps the window
     responsive during long stages without any async framework.

  2. THE THROWBACK LOOK: we force the *classic* (non-themed) Tk widgets and a
     period-correct grey palette + sunken/raised bevels. Classic Tk is from the
     Motif/CDE era, so this genuinely reads as 1990s X11 rather than pastiche.
     (Do NOT switch these to ttk.* — ttk is the modern look we're avoiding.)

  3. PREVIEW: Tk has no embedded video widget. On "Render complete" we hand the
     mp4 to the OS player (open / xdg-open / start). That's the deliberate
     trade vs. the web tier's inline <video>.

Claude Code should flesh out: file pickers for source/track, the grid/reuse
parameter controls, a clip gallery (a Canvas of the thumbs/ jpgs), cancellation,
and wiring the ingest+detect+catalog stages.

Run:  python3 tkapp.py        (Tkinter ships with CPython)
"""
from __future__ import annotations

import os
import platform
import queue
import subprocess
import threading
import tkinter as tk

import engine

# ── period-correct palette (Motif/CDE grey) ────────────────────────────────
GREY = "#b0b0b0"      # the canonical workstation grey
DARK = "#808080"
LIGHT = "#e0e0e0"
FONT = ("Helvetica", 11)   # bitmap-ish; swap for "Fixed"/"Courier" if installed


def open_in_player(path: str) -> None:
    """Hand a file to the OS default player (the Tk preview substitute)."""
    sysname = platform.system()
    if sysname == "Darwin":
        subprocess.Popen(["open", path])
    elif sysname == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("dv2mv")
        self.configure(bg=GREY)
        self.q: queue.Queue[engine.ProgressEvent] = queue.Queue()

        bevel = dict(bg=GREY, relief="raised", bd=2)
        tk.Label(self, text="dv2mv — offline", bg=GREY, font=("Helvetica", 13, "bold"),
                 relief="raised", bd=2, pady=4).pack(fill="x")

        row = tk.Frame(self, **bevel)
        row.pack(fill="x", padx=6, pady=6)
        tk.Label(row, text="Track:", bg=GREY, font=FONT).pack(side="left", padx=4)
        self.track = tk.Entry(row, font=FONT, relief="sunken", bd=2, bg=LIGHT)
        self.track.insert(0, "02 Erased.mp3")
        self.track.pack(side="left", fill="x", expand=True, padx=4, pady=4)

        btns = tk.Frame(self, bg=GREY)
        btns.pack(fill="x", padx=6)
        for label, stage in (("Analyze", "analyze"), ("Arrange", "arrange"),
                             ("Render", "render")):
            tk.Button(btns, text=label, font=FONT, bg=GREY, relief="raised", bd=2,
                      activebackground=LIGHT, padx=10,
                      command=lambda s=stage: self.launch(s)).pack(side="left", padx=4, pady=6)

        self.status = tk.Label(self, text="ready", bg=DARK, fg="white", font=FONT,
                               anchor="w", relief="sunken", bd=2)
        self.status.pack(fill="x", padx=6)
        self.log = tk.Text(self, height=10, font=("Courier", 10), bg="black",
                           fg="#33ff33", relief="sunken", bd=2)   # green-on-black, naturally
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        self.after(100, self._drain)

    # ── kick a stage off on a worker thread ────────────────────────────────
    def launch(self, stage: str) -> None:
        track = self.track.get().strip()
        threading.Thread(target=self._work, args=(stage, track), daemon=True).start()

    def _work(self, stage: str, track: str) -> None:
        media = engine.MEDIA          # media root (set DV2MV_MEDIA)
        cat = os.path.join(media, "catalog_audio")
        stem = os.path.splitext(track)[0]
        try:
            if stage == "analyze":
                gen = engine.analyze(os.path.join(media, "album-audio", track), cat)
            elif stage == "arrange":
                gen = engine.arrange(os.path.join(cat, f"{stem}.analysis.json"),
                                     os.path.join(media, "catalog", "manifest.csv"),
                                     grid="beats", beats_per_cut=2, allow_reuse=True)
            else:
                gen = engine.render(os.path.join(cat, f"render-{stem}.sh"))
            for ev in gen:
                self.q.put(ev)
        except engine.StageError as exc:
            self.q.put(engine.ProgressEvent(stage, f"FAILED: {exc}", done=True))

    # ── main-thread UI pump ────────────────────────────────────────────────
    def _drain(self) -> None:
        try:
            while True:
                ev = self.q.get_nowait()
                pct = "" if ev.frac is None else f"{ev.frac*100:3.0f}% "
                self.status.config(text=f"{pct}{ev.message}")
                self.log.insert("end", f"[{ev.stage}] {ev.message}\n")
                self.log.see("end")
                if ev.done and ev.result.get("video"):
                    open_in_player(ev.result["video"])   # the Tk preview substitute
        except queue.Empty:
            pass
        self.after(100, self._drain)


if __name__ == "__main__":
    App().mainloop()
