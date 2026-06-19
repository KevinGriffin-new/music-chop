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
analyzed in place; footage is scene-split then cataloged. The clip gallery
(Gallery… → a scrollable Canvas of the thumbs/ jpgs, click to play) is wired
too. Still to flesh out: the grid/reuse parameter controls and cancellation.

Run:  python3 tkapp.py        (Tkinter ships with CPython)
"""
from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import engine

# reuse the gallery data builder (same as the web tier) regardless of cwd
if engine.HERE not in sys.path:
    sys.path.insert(0, engine.HERE)
from pipeline import clip_gallery

AUDIO_TYPES = [("Audio", "*.mp3 *.wav *.m4a *.flac *.aac *.ogg *.aif *.aiff"),
               ("All files", "*.*")]
VIDEO_TYPES = [("Video", "*.mp4 *.mov *.mkv *.m4v *.avi *.dv"),
               ("All files", "*.*")]

# ── IRIX / 4Dwm-flavored theme (SGI gray scheme) ────────────────────────────
# Tweakable to match a real IRIX look. We theme ttk widgets only (buttons/
# radios/checks/entries in the options dialog), which on macOS is the only way
# to escape native Aqua and get genuine beveled Motif/SGI controls. Plain tk
# widgets (the progress Canvas, the green log Text) are left as-is.
IRIX = {
    "bg":     "#9a9a9a",   # SGI gray
    "light":  "#cccccc",   # top/left bevel highlight
    "dark":   "#4f4f4f",   # bottom/right bevel shadow
    "select": "#27408b",   # SGI steel-blue selection
    "fg":     "#000000",
    "field":  "#b6b6b6",   # entry / input field
}
# An SGI-like UI font. Install the real font (macOS: ~/Library/Fonts) and set
# its family name here; falls back to a Motif-ish family until then.
IRIX_FONT_FAMILY = "Helvetica"          # TODO: swap for the uploaded SGI font
IRIX_FONT = (IRIX_FONT_FAMILY, 11)

GRIDS = ["sections", "downbeats", "beats", "harmonic"]


def apply_irix_theme(widget) -> "ttk.Style":
    """Style ttk widgets to read as IRIX 4Dwm (SGI gray, blocky bevels).

    ttk styles are process-global; we don't touch the tk palette, so the main
    window's plain-tk widgets are unaffected.
    """
    style = ttk.Style(widget)
    try:
        style.theme_use("alt")          # flat Motif bevels — closest to 4Dwm
    except tk.TclError:
        style.theme_use("classic")
    c = IRIX
    style.configure(".", background=c["bg"], foreground=c["fg"],
                    font=IRIX_FONT, borderwidth=2)
    style.configure("TButton", background=c["bg"], relief="raised",
                    borderwidth=3, padding=4)
    style.map("TButton",
              background=[("active", c["light"]), ("pressed", c["dark"])],
              relief=[("pressed", "sunken"), ("!pressed", "raised")])
    style.configure("TRadiobutton", background=c["bg"], indicatorcolor=c["field"])
    style.configure("TCheckbutton", background=c["bg"], indicatorcolor=c["field"])
    style.map("TRadiobutton", background=[("active", c["light"])])
    style.map("TCheckbutton", background=[("active", c["light"])])
    style.configure("TEntry", fieldbackground=c["field"], borderwidth=2)
    style.configure("TSpinbox", fieldbackground=c["field"], arrowsize=12)
    style.configure("TLabel", background=c["bg"])
    style.configure("TFrame", background=c["bg"])
    style.configure("TLabelframe", background=c["bg"], borderwidth=2, relief="ridge")
    style.configure("TLabelframe.Label", background=c["bg"], font=IRIX_FONT)
    style.map(".", background=[("selected", c["select"])],
              foreground=[("selected", "#ffffff")])
    return style


def gallery_thumb_path(manifest_path: str, thumb_rel: str) -> str:
    """Filesystem path of a thumb (the manifest stores it relative to its dir)."""
    if not thumb_rel:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(manifest_path)), thumb_rel)


def gallery_clip_path(row: dict) -> str:
    """Filesystem path of a clip from a gallery row (strips the file:// prefix)."""
    c = row.get("clip", "")
    return c[len("file://"):] if c.startswith("file://") else c

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


class GalleryWindow(tk.Toplevel):
    """A scrollable thumbnail contact sheet of the cataloged clips.

    The Tk parallel to the web /api/gallery: same data (clip_gallery), a Canvas
    grid of the catalog/thumbs/*.jpg images, click a thumb to open the clip in
    the OS player. Thumbnails decode in .after() chunks so the window stays
    responsive instead of freezing while 300+ JPEGs load.
    """
    COLS = 4
    THUMB = (150, 100)

    def __init__(self, master, manifest_path: str) -> None:
        super().__init__(master)
        self.title("dv2mv — clip gallery")
        self.configure(bg=GREY)
        self.geometry("680x520")
        self._manifest = manifest_path
        self._images = []          # keep PhotoImage refs alive (else they GC away)

        self.head = tk.Label(self, text="loading…", bg=DARK, fg="white", font=FONT,
                             anchor="w", relief="sunken", bd=2)
        self.head.pack(fill="x")

        body = tk.Frame(self, bg=GREY)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=GREY, highlightthickness=0)
        vsb = tk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=GREY)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self._bind_wheel(self.canvas)

        data = clip_gallery.build_gallery_data(manifest_path)
        self._total = len(data)
        self._queue = list(enumerate(data))
        self.after(1, self._load_chunk)

    def _bind_wheel(self, w) -> None:
        # bind per-widget (not bind_all) so the gallery wheel doesn't hijack the
        # main window's log scroll while it's open
        w.bind("<MouseWheel>", self._wheel)
        w.bind("<Button-4>", self._wheel)
        w.bind("<Button-5>", self._wheel)

    def _wheel(self, e) -> None:
        num = getattr(e, "num", 0)
        step = 1 if num == 5 else -1 if num == 4 else int(-(e.delta or 0) / 120) or 0
        self.canvas.yview_scroll(step, "units")

    def _load_chunk(self) -> None:
        from PIL import Image, ImageTk
        for _ in range(16):
            if not self._queue:
                self.head.config(text=f"{self._total} clips — click a thumb to play")
                return
            i, d = self._queue.pop(0)
            self._add_card(i, d, Image, ImageTk)
        self.head.config(text=f"loading… {self._total - len(self._queue)}/{self._total}")
        self.after(1, self._load_chunk)

    def _add_card(self, i, d, Image, ImageTk) -> None:
        r, c = divmod(i, self.COLS)
        cell = tk.Frame(self.inner, bg=GREY, relief="raised", bd=2)
        cell.grid(row=r, column=c, padx=4, pady=4, sticky="n")
        path = gallery_thumb_path(self._manifest, d.get("thumb", ""))
        try:
            im = Image.open(path)
            im.thumbnail(self.THUMB)
        except Exception:
            im = Image.new("RGB", self.THUMB, "#222")   # missing-thumb placeholder
        img = ImageTk.PhotoImage(im)
        self._images.append(img)
        clip = gallery_clip_path(d)
        thumb = tk.Label(cell, image=img, bg="black", cursor="hand2")
        thumb.pack()
        thumb.bind("<Button-1>", lambda e, p=clip: open_in_player(p))
        tk.Label(cell, text=d.get("name", "")[:22], bg=GREY,
                 font=("Helvetica", 9)).pack()
        for w in (cell, thumb):
            self._bind_wheel(w)


class ArrangeOptions(tk.Toplevel):
    """IRIX-styled modal dialog for the arrange knobs.

    Collects grid / beats-per-cut / allow-reuse / drop-blurry / clip-from, then
    hands them to on_ok(params) — the App spawns engine.arrange(**params).
    """
    def __init__(self, master, on_ok, initial=None) -> None:
        super().__init__(master)
        self.title("Arrange options")
        self.on_ok = on_ok
        apply_irix_theme(self)
        self.configure(bg=IRIX["bg"])
        init = initial or {}
        self.v_grid = tk.StringVar(value=init.get("grid", "sections"))
        self.v_bpc = tk.IntVar(value=init.get("beats_per_cut", 4))
        self.v_reuse = tk.BooleanVar(value=init.get("allow_reuse", False))
        self.v_blur = tk.DoubleVar(value=init.get("drop_blurry", 0.0))
        self.v_clip = tk.StringVar(value=init.get("clip_from", "middle"))

        pad = dict(padx=8, pady=4)
        gf = ttk.LabelFrame(self, text="Cut grid")
        gf.pack(fill="x", **pad)
        for g in GRIDS:
            ttk.Radiobutton(gf, text=g, value=g, variable=self.v_grid,
                            command=self._sync).pack(anchor="w", padx=8, pady=1)

        bf = ttk.Frame(self)
        bf.pack(fill="x", **pad)
        ttk.Label(bf, text="Beats per cut:").pack(side="left")
        self.sp_bpc = ttk.Spinbox(bf, from_=1, to=32, width=5, textvariable=self.v_bpc)
        self.sp_bpc.pack(side="left", padx=6)

        ttk.Checkbutton(self, text="Allow clip reuse",
                        variable=self.v_reuse).pack(anchor="w", **pad)

        df = ttk.Frame(self)
        df.pack(fill="x", **pad)
        ttk.Label(df, text="Drop blurry below:").pack(side="left")
        ttk.Entry(df, width=7, textvariable=self.v_blur).pack(side="left", padx=6)

        cf = ttk.LabelFrame(self, text="Clip piece from")
        cf.pack(fill="x", **pad)
        for v in ("middle", "start"):
            ttk.Radiobutton(cf, text=v, value=v,
                            variable=self.v_clip).pack(side="left", padx=8, pady=1)

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="Arrange", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")

        self._sync()
        self.transient(master)
        self.grab_set()

    def _sync(self) -> None:
        # beats-per-cut only applies to the 'beats' grid
        self.sp_bpc.configure(state="normal" if self.v_grid.get() == "beats"
                              else "disabled")

    def params(self) -> dict:
        return {"grid": self.v_grid.get(),
                "beats_per_cut": int(self.v_bpc.get()),
                "allow_reuse": bool(self.v_reuse.get()),
                "drop_blurry": float(self.v_blur.get()),
                "clip_from": self.v_clip.get()}

    def _ok(self) -> None:
        p = self.params()
        self.destroy()
        self.on_ok(p)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("dv2mv")
        self.configure(bg=GREY)
        self.q: queue.Queue[engine.ProgressEvent] = queue.Queue()
        self._last_grid = None     # grid chosen in the options dialog; render reuses it

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
        tk.Button(src, text="Gallery…", font=FONT, bg=GREY, relief="raised",
                  bd=2, activebackground=LIGHT, padx=8,
                  command=self.open_gallery).pack(side="right", padx=4, pady=4)

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
            self._open_arrange_options(track, cat, media)
        else:
            # prefer the script for the grid last arranged; else the newest match
            sh = None
            if self._last_grid:
                cand = os.path.join(cat, f"render-{stem}{engine._tag_suffix(self._last_grid)}.sh")
                sh = cand if os.path.exists(cand) else None
            sh = sh or engine.find_render_script(cat, track) or os.path.join(
                cat, f"render-{stem}.sh")
            self._spawn(lambda: engine.render(sh))

    def _open_arrange_options(self, track: str, cat: str, media: str) -> None:
        stem = os.path.splitext(track)[0]
        analysis = os.path.join(cat, f"{stem}.analysis.json")
        if not os.path.exists(analysis):
            messagebox.showinfo(
                "dv2mv — arrange",
                f"No analysis for '{stem}' yet — run Analyze on the track first.")
            return
        manifest = os.path.join(media, "catalog", "manifest.csv")

        def run(p):
            self._last_grid = p["grid"]
            self.status.config(text=f"arranging on the {p['grid']} grid …")
            self._spawn(lambda: engine.arrange(analysis, manifest, **p))
        ArrangeOptions(self, on_ok=run,
                       initial={"grid": self._last_grid or "sections"})

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

    def open_gallery(self) -> None:
        """Open the thumbnail contact sheet for the current catalog."""
        manifest = os.path.join(engine.MEDIA, "catalog", "manifest.csv")
        if not os.path.exists(manifest):
            messagebox.showinfo(
                "dv2mv — gallery",
                "No catalog yet — add footage (Video footage…) to build it first.")
            return
        GalleryWindow(self, manifest)

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
