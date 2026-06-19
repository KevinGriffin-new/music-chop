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

File pickers are wired (Add ▸ Music track… / Video footage…): a track is
analyzed in place; footage is scene-split then cataloged. Still to flesh out:
the grid/reuse parameter controls, a clip gallery (a Canvas of the thumbs/
jpgs), and cancellation.

Run:  python3 tkapp.py        (Tkinter ships with CPython)
"""
from __future__ import annotations

import os
import platform
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import engine

AUDIO_TYPES = [("Audio", "*.mp3 *.wav *.m4a *.flac *.aac *.ogg *.aif *.aiff"),
               ("All files", "*.*")]
VIDEO_TYPES = [("Video", "*.mp4 *.mov *.mkv *.m4v *.avi *.dv"),
               ("All files", "*.*")]

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

        # ── add new media via native file dialogs ──────────────────────────
        src = tk.Frame(self, **bevel)
        src.pack(fill="x", padx=6, pady=(0, 6))
        tk.Label(src, text="Add:", bg=GREY, font=FONT).pack(side="left", padx=4)
        tk.Button(src, text="Music track…", font=FONT, bg=GREY, relief="raised",
                  bd=2, activebackground=LIGHT, padx=8,
                  command=self.add_track).pack(side="left", padx=4, pady=4)
        tk.Button(src, text="Video footage…", font=FONT, bg=GREY, relief="raised",
                  bd=2, activebackground=LIGHT, padx=8,
                  command=self.add_footage).pack(side="left", padx=4, pady=4)

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

        # classic drawn progress bar (no ttk): determinate fill when frac is
        # known, an animated sweep while a stage runs so the UI never looks dead.
        self.pb = tk.Canvas(self, height=16, bg=LIGHT, relief="sunken", bd=2,
                            highlightthickness=0)
        self.pb.pack(fill="x", padx=6, pady=(0, 6))
        self.active = 0          # number of running stages
        self._pb_frac = None     # last known fraction (None => indeterminate)
        self._pb_pos = 0         # sweep position for the indeterminate animation

        self.log = tk.Text(self, height=10, font=("Courier", 10), bg="black",
                           fg="#33ff33", relief="sunken", bd=2)   # green-on-black, naturally
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        self.after(100, self._drain)
        self.after(60, self._pb_tick)

    # ── progress bar (drawn on a Canvas; classic look, always moving) ───────
    def _begin(self) -> None:
        self.active += 1
        self._pb_frac = None

    def _end(self) -> None:
        self.active = max(0, self.active - 1)
        if self.active == 0:
            self._pb_frac = None

    def _pb_tick(self) -> None:
        self.pb.delete("fill")
        w = self.pb.winfo_width()
        h = int(self.pb["height"])
        if self.active and self._pb_frac is not None:                 # determinate
            self.pb.create_rectangle(0, 0, int(w * self._pb_frac), h,
                                     fill="#5b9dff", width=0, tags="fill")
        elif self.active:                                             # indeterminate sweep
            cw = max(40, int(w * 0.22))
            x = (self._pb_pos % (w + cw)) - cw
            self.pb.create_rectangle(x, 0, x + cw, h, fill="#5b9dff",
                                     width=0, tags="fill")
            self._pb_pos += max(6, int(w * 0.02))
        self.after(60, self._pb_tick)

    # ── run any stage generator on a worker thread, funnel into the queue ───
    def _spawn(self, make_gen) -> None:
        """Run make_gen() (a no-arg fn returning a stage generator) off-thread.

        All UI feedback flows through self.q; never touch widgets from here.
        """
        self._begin()
        def work():
            try:
                for ev in make_gen():
                    self.q.put(ev)
            except engine.StageError as exc:
                self.q.put(engine.ProgressEvent("error", f"FAILED: {exc}", done=True))
            finally:
                # one sentinel per spawn ends the bar, even for multi-stage
                # chains (footage = detect THEN catalog, two done events).
                self.q.put(engine.ProgressEvent("__end__", "", done=True))
        threading.Thread(target=work, daemon=True).start()

    # ── kick a stage off from the Track box ─────────────────────────────────
    def launch(self, stage: str) -> None:
        track = self.track.get().strip()
        media = engine.MEDIA          # media root (set DV2MV_MEDIA)
        cat = os.path.join(media, "catalog_audio")
        stem = os.path.splitext(track)[0]
        if stage == "analyze":
            self._spawn(lambda: engine.analyze(
                os.path.join(media, "album-audio", track), cat))
        elif stage == "arrange":
            self._spawn(lambda: engine.arrange(
                os.path.join(cat, f"{stem}.analysis.json"),
                os.path.join(media, "catalog", "manifest.csv"),
                grid="beats", beats_per_cut=2, allow_reuse=True))
        else:
            self._spawn(lambda: engine.render(
                os.path.join(cat, f"render-{stem}.sh")))

    # ── file pickers: bring in new media from anywhere on disk ──────────────
    def add_track(self) -> None:
        """Pick an audio file and analyze it in place (no copy)."""
        path = filedialog.askopenfilename(title="Add a music track",
                                          filetypes=AUDIO_TYPES)
        if not path:
            return
        self.track.delete(0, "end")
        self.track.insert(0, os.path.basename(path))   # so Arrange/Render find it
        cat = os.path.join(engine.MEDIA, "catalog_audio")
        self.status.config(text=f"analyzing {os.path.basename(path)} …")
        self._spawn(lambda: engine.analyze(path, cat, plot=True))

    def add_footage(self) -> None:
        """Pick one or more videos, scene-split them, then (re)build the catalog."""
        paths = filedialog.askopenfilenames(title="Add video footage",
                                            filetypes=VIDEO_TYPES)
        if not paths:
            return
        clips = os.path.join(engine.MEDIA, "clips")
        cat = os.path.join(engine.MEDIA, "catalog")
        sources = list(paths)
        self.status.config(text=f"ingesting {len(sources)} clip(s) …")

        def chain():
            yield from engine.detect(sources, clips)
            yield from engine.catalog(clips, cat)
        self._spawn(chain)

    # ── main-thread UI pump ────────────────────────────────────────────────
    def _drain(self) -> None:
        try:
            while True:
                ev = self.q.get_nowait()
                if ev.stage == "__end__":
                    self._end()                          # spawn finished → stop the bar
                    continue
                if ev.frac is not None:
                    self._pb_frac = ev.frac              # feed the determinate bar
                pct = "" if ev.frac is None else f"{ev.frac*100:3.0f}% "
                self.status.config(text=f"{pct}{ev.message}")
                self.log.insert("end", f"[{ev.stage}] {ev.message}\n")
                self.log.see("end")
                if ev.stage == "error":
                    # the actionable prompt the user asked for (e.g. run Analyze)
                    messagebox.showwarning("dv2mv — can't continue",
                                           ev.message.replace("FAILED: ", ""))
                elif ev.done and ev.result.get("video"):
                    open_in_player(ev.result["video"])   # the Tk preview substitute
        except queue.Empty:
            pass
        self.after(100, self._drain)


if __name__ == "__main__":
    App().mainloop()
