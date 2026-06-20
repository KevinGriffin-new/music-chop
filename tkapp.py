#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
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

File pickers (Add ▸ Music track… / Video footage…), the clip gallery (Gallery…),
the IRIX arrange-options dialog, and projects (New… / Open… — a project scopes a
track + a clip selection + arrange options to its own folder) are all wired.
Clip selection is gallery-based: multi-select thumbnails, then "New project from
selection…". The Export button emits an editable timeline (OTIO + FCPXML) for
finishing in DaVinci Resolve, as an alternative to the baked ffmpeg render. A
running stage is cancellable: the Cancel button sets a threading Event the
worker hands to the engine, which terminates the underlying subprocess (and, for
render, its ffmpeg child group).

Run:  python3 tkapp.py        (Tkinter ships with CPython)
"""
from __future__ import annotations

import gc
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

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
# IRIX uses two distinct fonts:
#  * menus / dialog labels / window titles — a SLANTED (oblique) proportional
#    font; the signature SGI look. Synthesized here as italic Helvetica.
#  * the shell / console — a fixed-width screen font. That's the CC0 "Irix
#    Screen Mono 15" (vendored in assets/fonts/, install into ~/Library/Fonts);
#    we use it for the green log and fall back to a mono family if absent.
IRIX_MENU_FONT = ("Helvetica", 20, "italic")   # slanted menu/label text (try 20–24)
IRIX_FONT_FAMILY = "Irix Screen Mono 15"        # fixed shell font
IRIX_FONT_FALLBACK = "Menlo"            # macOS mono; any mono works
IRIX_FONT_SIZE = -15                    # negative = pixels (crisp for a bitmap font)

GRIDS = ["sections", "downbeats", "beats", "harmonic"]


def pick_irix_font(available) -> tuple:
    """The fixed shell font (SGI mono if installed, else a mono fallback).
    `available` is the set of Tk font families, so this stays pure/testable."""
    fam = IRIX_FONT_FAMILY if IRIX_FONT_FAMILY in available else IRIX_FONT_FALLBACK
    return (fam, IRIX_FONT_SIZE)


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
    font = IRIX_MENU_FONT            # slanted, like IRIX menus/labels
    style.configure(".", background=c["bg"], foreground=c["fg"],
                    font=font, borderwidth=2)
    style.configure("TButton", background=c["bg"], relief="raised",
                    borderwidth=3, padding=4)
    style.map("TButton",
              background=[("active", c["light"]), ("pressed", c["dark"])],
              relief=[("pressed", "sunken"), ("!pressed", "raised")])
    # padding widens the clickable hit box (toggles felt too small to hit)
    style.configure("TRadiobutton", background=c["bg"], indicatorcolor=c["field"],
                    padding=(4, 3))
    style.configure("TCheckbutton", background=c["bg"], indicatorcolor=c["field"],
                    padding=(4, 3))
    style.map("TRadiobutton", background=[("active", c["light"])])
    style.map("TCheckbutton", background=[("active", c["light"])])
    style.configure("TEntry", fieldbackground=c["field"], borderwidth=2)
    style.configure("TSpinbox", fieldbackground=c["field"], arrowsize=12)
    style.configure("TLabel", background=c["bg"])
    style.configure("TFrame", background=c["bg"])
    style.configure("TLabelframe", background=c["bg"], borderwidth=2, relief="ridge")
    style.configure("TLabelframe.Label", background=c["bg"], font=font)
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


def format_arrange_summary(meta: dict) -> str:
    """One-line human summary of an arrange.json (what options/result produced
    the cut). Pure, so it's testable without a window."""
    parts = [f"{meta.get('grid')} grid",
             f"{meta.get('cuts')} cuts",
             f"{meta.get('energy_match_pct')}% energy match",
             f"{meta.get('clips')} clips"]
    if meta.get("allow_reuse"):
        parts.append("reuse")
    if meta.get("grid") == "beats":
        parts.append(f"{meta.get('beats_per_cut')} beats/cut")
    if meta.get("drop_blurry"):
        parts.append(f"drop<{meta.get('drop_blurry')}")
    parts.append(f"clip-from {meta.get('clip_from')}")
    return " · ".join(str(p) for p in parts)

# ── period-correct palette (Motif/CDE grey) ────────────────────────────────
GREY = "#b0b0b0"      # the canonical workstation grey
DARK = "#808080"
LIGHT = "#e0e0e0"
FONT = ("Helvetica", 11)   # bitmap-ish; swap for "Fixed"/"Courier" if installed


def player_command(path: str, player: str, system: str):
    """Build the argv to open `path`, honoring a preferred `player`.

    Set DV2MV_PLAYER to force a specific app (e.g. "VLC") instead of the OS
    default — handy when the default handler for the file type isn't a player
    (a tag editor, say). Returns None on Windows with no override (caller uses
    os.startfile). Kept pure so it's testable without launching anything.
      * macOS:   `open -a <player> <path>`  (player is an app name or path)
      * others:  `<player> <path>`          (player is a command on PATH)
    """
    if system == "Darwin":
        return ["open", "-a", player, path] if player else ["open", path]
    if system == "Windows":
        return [player, path] if player else None
    return [player, path] if player else ["xdg-open", path]


def open_in_player(path: str) -> None:
    """Open a file in the preferred player (the Tk preview substitute).

    Honors DV2MV_PLAYER; falls back to the OS default. On macOS, if the chosen
    app can't open the file, retries with the plain OS default.
    """
    system = platform.system()
    player = os.environ.get("DV2MV_PLAYER", "").strip()
    cmd = player_command(path, player, system)
    if cmd is None:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows default
        return
    proc = subprocess.Popen(cmd)
    if system == "Darwin" and player:
        # `open -a Foo` exits nonzero if the app is missing — fall back cleanly
        if proc.wait() != 0:
            subprocess.Popen(["open", path])


class GalleryWindow(tk.Toplevel):
    """A scrollable thumbnail contact sheet of the cataloged clips.

    The Tk parallel to the web /api/gallery: same data (clip_gallery), a Canvas
    grid of the catalog/thumbs/*.jpg images, click a thumb to open the clip in
    the OS player. Thumbnails decode in .after() chunks so the window stays
    responsive instead of freezing while 300+ JPEGs load.
    """
    COLS = 4
    THUMB = (150, 100)
    SEL = "#5b9dff"            # selection highlight

    def __init__(self, master, manifest_path: str, on_apply=None, preselect=None) -> None:
        super().__init__(master)
        self.title("dv2mv — clip gallery")
        self.configure(bg=IRIX["bg"])
        self.geometry("700x560")
        self._manifest = manifest_path
        self._images = []          # keep PhotoImage refs alive (else they GC away)
        self._cells = {}           # clip path -> its card frame (for select-all/marking)
        self.selected = set(preselect or [])
        self._on_apply = on_apply  # if set, "Use selection" returns clips; else New project

        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        self.count = ttk.Label(bar, text="loading…")
        self.count.pack(side="left", padx=4)
        ttk.Button(bar, text="Clear", command=self.clear_selection).pack(side="right", padx=2)
        ttk.Button(bar, text="Select all", command=self.select_all).pack(side="right", padx=2)
        if on_apply:
            # editing a project: the current clips come up preselected; the three
            # ops interpret the checked set as add/remove/replace against them
            ttk.Button(bar, text="Replace",
                       command=lambda: self._apply("replace")).pack(side="right", padx=2)
            ttk.Button(bar, text="Remove ←",
                       command=lambda: self._apply("remove")).pack(side="right", padx=2)
            ttk.Button(bar, text="Add →",
                       command=lambda: self._apply("add")).pack(side="right", padx=2)
        else:
            ttk.Button(bar, text="New project from selection…",
                       command=lambda: self._apply()).pack(side="right", padx=2)

        body = tk.Frame(self, bg=IRIX["bg"])
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=IRIX["bg"], highlightthickness=0)
        vsb = tk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=IRIX["bg"])
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
                self._update_count()
                return
            i, d = self._queue.pop(0)
            self._add_card(i, d, Image, ImageTk)
        self.count.config(text=f"loading… {self._total - len(self._queue)}/{self._total}")
        self.after(1, self._load_chunk)

    def _add_card(self, i, d, Image, ImageTk) -> None:
        r, c = divmod(i, self.COLS)
        cell = tk.Frame(self.inner, bg=IRIX["bg"], relief="raised", bd=2,
                        highlightthickness=0, highlightbackground=self.SEL)
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
        self._cells[clip] = cell
        thumb = tk.Label(cell, image=img, bg="black", cursor="hand2")
        thumb.pack()
        # single click toggles selection; double-click plays (net selection
        # unchanged: the two Button-1 events of a double-click cancel out)
        thumb.bind("<Button-1>", lambda e, p=clip: self.toggle(p))
        thumb.bind("<Double-Button-1>", lambda e, p=clip: open_in_player(p))
        tk.Label(cell, text=d.get("name", "")[:22], bg=IRIX["bg"], fg=IRIX["fg"],
                 font=("Helvetica", 9)).pack()
        for w in (cell, thumb):
            self._bind_wheel(w)
        if clip in self.selected:
            self._mark(cell, True)

    # ── selection ───────────────────────────────────────────────────────────
    def _mark(self, cell, on: bool) -> None:
        cell.config(highlightthickness=3 if on else 0)

    def toggle(self, clip: str) -> None:
        if clip in self.selected:
            self.selected.discard(clip)
            self._mark(self._cells[clip], False)
        else:
            self.selected.add(clip)
            self._mark(self._cells[clip], True)
        self._update_count()

    def select_all(self) -> None:
        for clip, cell in self._cells.items():
            self.selected.add(clip)
            self._mark(cell, True)
        self._update_count()

    def clear_selection(self) -> None:
        for clip in list(self.selected):
            if clip in self._cells:
                self._mark(self._cells[clip], False)
        self.selected.clear()
        self._update_count()

    def _update_count(self) -> None:
        n = len(self.selected)
        hint = "" if n else " — click to select, double-click to play"
        self.count.config(text=f"{self._total} clips · {n} selected{hint}")

    def _apply(self, op: str = "replace") -> None:
        clips = sorted(self.selected)
        if not clips:
            messagebox.showinfo("dv2mv — gallery",
                                "Select some clips first (click thumbnails).")
            return
        if self._on_apply:
            self._on_apply(clips, op)
            self.destroy()
        else:
            self.destroy()
            self.master.new_project_dialog(preset_clips=clips)

    def destroy(self) -> None:
        # drop the PhotoImage refs while the interpreter is still alive (avoids
        # ImageTk finalizers firing into a dead Tcl interp on GC)
        self._images.clear()
        self._cells.clear()
        super().destroy()


class RetempoDialog(tk.Toplevel):
    """IRIX-styled modal: a BPM slider to time-stretch a track (pitch-preserved).

    Centered on the track's detected tempo, ranges 0.5×..2.0×; on_ok(target_bpm)
    is called only when the target differs from the source — the App then spawns
    engine.retempo followed by engine.analyze on the stretched variant.
    """
    def __init__(self, master, src_bpm, on_ok) -> None:
        super().__init__(master)
        self.title("Retempo")
        self.on_ok = on_ok
        apply_irix_theme(self)
        self.configure(bg=IRIX["bg"])
        self.src = float(src_bpm)
        lo, hi = max(40, round(self.src * 0.5)), round(self.src * 2.0)
        pad = dict(padx=10, pady=6)

        ttk.Label(self, text=f"Source tempo: {self.src:.0f} BPM   "
                             "(stretch is pitch-preserved)").pack(anchor="w", **pad)
        self.readout = ttk.Label(self, text="")
        self.readout.pack(anchor="w", padx=10)
        self.scale = ttk.Scale(self, from_=lo, to=hi, orient="horizontal",
                               command=self._on_move, length=320)
        self.scale.set(round(self.src))
        self.scale.pack(fill="x", **pad)
        rng = ttk.Frame(self)
        rng.pack(fill="x", padx=10)
        ttk.Label(rng, text=f"{lo}").pack(side="left")
        ttk.Label(rng, text=f"{hi}").pack(side="right")

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="Retempo", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")

        self._on_move(round(self.src))
        self.transient(master)
        self.grab_set()
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(20, self.focus_force)

    def _on_move(self, val) -> None:
        bpm = round(float(val))
        pct = (bpm / self.src - 1.0) * 100 if self.src else 0
        same = " — no change" if abs(bpm - self.src) < 1 else ""
        self.readout.config(text=f"Target: {bpm} BPM  ({pct:+.0f}%){same}")

    def _ok(self) -> None:
        bpm = round(float(self.scale.get()))
        self.destroy()
        if abs(bpm - self.src) >= 1:
            self.on_ok(bpm)


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
        self.v_match = tk.StringVar(value=init.get("match", "energy"))

        pad = dict(padx=8, pady=4)
        gf = ttk.LabelFrame(self, text="Cut grid")
        gf.pack(fill="x", **pad)
        for g in GRIDS:
            ttk.Radiobutton(gf, text=f"{g} — {engine.GRID_HELP.get(g, '')}",
                            value=g, variable=self.v_grid,
                            command=self._sync).pack(anchor="w", fill="x", padx=8, pady=1)

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

        mf = ttk.LabelFrame(self, text="Match")
        mf.pack(fill="x", **pad)
        for val, desc in (("energy", "clips track the song's energy"),
                          ("contrast", "also alternate brightness between cuts")):
            ttk.Radiobutton(mf, text=f"{val} — {desc}", value=val,
                            variable=self.v_match).pack(anchor="w", fill="x", padx=8, pady=1)

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="Arrange", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")

        self._sync()
        self.transient(master)
        self.grab_set()
        self.bind("<Return>", lambda e: self._ok())     # Enter arranges
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(20, self.focus_force)

    def _sync(self) -> None:
        # beats-per-cut only applies to the 'beats' grid
        self.sp_bpc.configure(state="normal" if self.v_grid.get() == "beats"
                              else "disabled")

    def params(self) -> dict:
        return {"grid": self.v_grid.get(),
                "beats_per_cut": int(self.v_bpc.get()),
                "allow_reuse": bool(self.v_reuse.get()),
                "drop_blurry": float(self.v_blur.get()),
                "clip_from": self.v_clip.get(),
                "match": self.v_match.get()}

    def _ok(self) -> None:
        p = self.params()
        self.destroy()
        self.on_ok(p)


class NewProjectDialog(tk.Toplevel):
    """IRIX-styled dialog to create a project: name + track + clip selection.

    Footage is scoped to the shared library — 'all' or a set of source/tapes.
    Arrange options are chosen later (via the Arrange dialog) and saved onto the
    project. on_ok(name, track, clips) does the actual creation.
    """
    def __init__(self, master, media, default_track, on_ok, clips=None) -> None:
        super().__init__(master)
        self.title("New project")
        apply_irix_theme(self)
        self.configure(bg=IRIX["bg"])
        self.media = media
        self.on_ok = on_ok
        self._preset = list(clips) if clips else None
        self.v_name = tk.StringVar()
        self.v_track = tk.StringVar(value=default_track)
        self.v_scope = tk.StringVar(value="selected" if self._preset else "all")
        self._src_vars = {}

        pad = dict(padx=8, pady=4)
        nf = ttk.Frame(self, padding=4); nf.pack(fill="x", **pad)
        ttk.Label(nf, text="Name:").pack(side="left")
        name_entry = ttk.Entry(nf, textvariable=self.v_name)
        name_entry.pack(side="left", fill="x", expand=True, padx=6)
        tf = ttk.Frame(self, padding=4); tf.pack(fill="x", **pad)
        ttk.Label(tf, text="Track:").pack(side="left")
        # dropdown of the tracks actually present (still editable for odd cases)
        tracks = engine.list_audio_tracks(media)
        ttk.Combobox(tf, textvariable=self.v_track, values=tracks).pack(
            side="left", fill="x", expand=True, padx=6)

        sf = ttk.LabelFrame(self, text="Footage")
        sf.pack(fill="x", **pad)            # fill x only, so Create stays on screen
        if self._preset:
            ttk.Radiobutton(sf, text=f"Selected in gallery ({len(self._preset)} clips)",
                            value="selected", variable=self.v_scope,
                            command=self._sync).pack(anchor="w", fill="x", padx=8)
        ttk.Radiobutton(sf, text="All library footage", value="all",
                        variable=self.v_scope, command=self._sync).pack(anchor="w", fill="x", padx=8)
        ttk.Radiobutton(sf, text="By source / tape:", value="sources",
                        variable=self.v_scope, command=self._sync).pack(anchor="w", fill="x", padx=8)

        # the source list can be long — scroll it in a capped-height canvas so
        # the Create/Cancel buttons always stay visible
        wrap = ttk.Frame(sf)
        wrap.pack(fill="x", padx=24, pady=(0, 4))
        cv = tk.Canvas(wrap, height=150, highlightthickness=0, bg=IRIX["bg"])
        sb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="x", expand=True)
        self.srcbox = ttk.Frame(cv)
        cv.create_window((0, 0), window=self.srcbox, anchor="nw")
        self.srcbox.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        self._sources_built = False     # built lazily — see _build_sources()

        bar = ttk.Frame(self, padding=4); bar.pack(fill="x", **pad)
        ttk.Button(bar, text="Create", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        self._sync()
        self.transient(master)
        self.grab_set()
        self.bind("<Return>", lambda e: self._ok())     # Enter creates
        self.bind("<Escape>", lambda e: self.destroy())
        name_entry.focus_set()
        self.after(20, self.focus_force)                # win the first click (macOS)

    def _build_sources(self) -> None:
        # building 100s of checkbuttons is slow, so defer it until "By source"
        # is actually chosen — keeps the dialog snappy to open
        if self._sources_built:
            return
        self._sources_built = True
        for s in sorted(engine.manifest_sources(
                os.path.join(self.media, "catalog", "manifest.csv"))):
            v = tk.BooleanVar()
            self._src_vars[s] = v
            ttk.Checkbutton(self.srcbox, text=s, variable=v).pack(anchor="w", fill="x")
        if not self._src_vars:
            ttk.Label(self.srcbox,
                      text="(no catalog yet — add footage first)").pack(anchor="w")

    def _sync(self) -> None:
        if self.v_scope.get() == "sources":
            self._build_sources()
        state = "normal" if self.v_scope.get() == "sources" else "disabled"
        for w in self.srcbox.winfo_children():
            try:
                w.configure(state=state)
            except tk.TclError:
                pass

    def clips(self):
        scope = self.v_scope.get()
        if scope == "selected" and self._preset:
            return self._preset
        if scope == "all":
            return "all"
        srcmap = engine.manifest_sources(
            os.path.join(self.media, "catalog", "manifest.csv"))
        chosen = []
        for s, v in self._src_vars.items():
            if v.get():
                chosen += srcmap.get(s, [])
        return chosen or "all"

    def _ok(self) -> None:
        name = self.v_name.get().strip()
        if not name:
            messagebox.showwarning("New project", "Please enter a project name.")
            return
        track, clips = self.v_track.get().strip(), self.clips()
        self.destroy()
        self.on_ok(name, track, clips)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("dv2mv")
        apply_irix_theme(self)              # IRIX/4Dwm look for the ttk controls
        self.configure(bg=IRIX["bg"])
        # window/app icon (kino-eye); defensive — never let a bad icon break launch
        self._icon = None
        try:
            self._icon = tk.PhotoImage(
                file=os.path.join(engine.HERE, "assets", "icons", "dv2mv-256.png"))
            self.iconphoto(True, self._icon)
        except Exception:
            pass
        self.q: queue.Queue[engine.ProgressEvent] = queue.Queue()
        self._last_grid = None     # grid chosen in the options dialog; render reuses it
        self.project = None        # active Project (None => whole-library mode)
        self._cancel = None        # cancel token for the in-flight stage (a threading.Event)

        title_font = (IRIX_MENU_FONT[0], IRIX_MENU_FONT[1] + 2, "bold italic")
        ttk.Label(self, text="dv2mv — offline", anchor="center", relief="raised",
                  borderwidth=2, padding=4, font=title_font).pack(fill="x")

        # ── media library (the root all media/outputs live under) ───────────
        lib = ttk.Frame(self, padding=4)
        lib.pack(fill="x", padx=6, pady=(6, 0))
        self.lib_label = ttk.Label(lib, text="Library: …")
        self.lib_label.pack(side="left", padx=4)
        ttk.Button(lib, text="Media library…",
                   command=self.choose_media).pack(side="right", padx=4)

        proj = ttk.Frame(self, padding=4)
        proj.pack(fill="x", padx=6, pady=(6, 0))
        self.proj_label = ttk.Label(proj, text="Project: (none — library mode)")
        self.proj_label.pack(side="left", padx=4)
        ttk.Button(proj, text="Open…", command=self.open_project_dialog).pack(side="right", padx=4)
        ttk.Button(proj, text="New…", command=self.new_project_dialog).pack(side="right", padx=4)

        row = ttk.Frame(self, padding=4)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Track:").pack(side="left", padx=4)
        self.track = ttk.Entry(row)
        self.track.insert(0, "02 Erased.mp3")
        self.track.pack(side="left", fill="x", expand=True, padx=4)

        # ── add new media via native file dialogs ──────────────────────────
        src = ttk.Frame(self, padding=4)
        src.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(src, text="Add:").pack(side="left", padx=4)
        ttk.Button(src, text="Music track…", command=self.add_track).pack(side="left", padx=4)
        ttk.Button(src, text="Video footage…", command=self.add_footage).pack(side="left", padx=4)
        ttk.Button(src, text="Tempo…", command=self.open_retempo).pack(side="left", padx=4)
        ttk.Button(src, text="Gallery…", command=self.open_gallery).pack(side="right", padx=4)

        btns = ttk.Frame(self, padding=(6, 4))
        btns.pack(fill="x", padx=6)
        for label, stage in (("Analyze", "analyze"), ("Arrange", "arrange"),
                             ("Compare", "compare"), ("Render", "render"),
                             ("Export", "export")):
            ttk.Button(btns, text=label,
                       command=lambda s=stage: self.launch(s)).pack(side="left", padx=4)
        # Cancel the running stage — disabled until one is in flight (see _begin/_end)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel_stage,
                                     state="disabled")
        self.cancel_btn.pack(side="right", padx=4)

        self.status = tk.Label(self, text="ready", bg=IRIX["dark"], fg="white",
                               font=IRIX_MENU_FONT, anchor="w", relief="sunken", bd=2)
        self.status.pack(fill="x", padx=6)

        # drawn progress bar (regular tk.Canvas, per request): determinate fill
        # when frac is known, an animated sweep while a stage runs.
        self.pb = tk.Canvas(self, height=16, bg=IRIX["field"], relief="sunken", bd=2,
                            highlightthickness=0)
        self.pb.pack(fill="x", padx=6, pady=(0, 6))
        self.active = 0          # number of running stages
        self._pb_frac = None     # last known fraction (None => indeterminate)
        self._pb_pos = 0         # sweep position for the indeterminate animation

        # the log is the "console" — give it the fixed SGI shell font
        self.log = tk.Text(self, height=10, font=pick_irix_font(set(tkfont.families(self))),
                           bg="black", fg="#33ff33", relief="sunken", bd=2)  # green-on-black
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        self._refresh_lib_label()
        self.after(100, self._drain)
        self.after(60, self._pb_tick)
        self.after(150, self._ensure_media)   # prompt for a library if none is set

    # ── progress bar (drawn on a Canvas; classic look, always moving) ───────
    def _begin(self) -> None:
        self.active += 1
        self._pb_frac = None
        self.cancel_btn.config(state="normal")     # a stage is in flight → armed

    def _end(self) -> None:
        self.active = max(0, self.active - 1)
        if self.active == 0:
            self._pb_frac = None
            self._cancel = None
            self.cancel_btn.config(state="disabled")

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
        """Run make_gen(cancel) (returning a stage generator) off-thread.

        `make_gen` takes the cancel token so it can hand it to the engine stage;
        Cancel (the button) sets it from the main thread. All UI feedback flows
        through self.q; never touch widgets from here.
        """
        cancel = threading.Event()
        self._cancel = cancel
        self._begin()
        def work():
            try:
                for ev in make_gen(cancel):
                    self.q.put(ev)
            except engine.Cancelled:
                # a clean stop, not a failure — no error dialog (see _drain)
                self.q.put(engine.ProgressEvent("cancelled", "cancelled", done=True))
            except engine.StageError as exc:
                self.q.put(engine.ProgressEvent("error", f"FAILED: {exc}", done=True))
            finally:
                # one sentinel per spawn ends the bar, even for multi-stage
                # chains (footage = detect THEN catalog, two done events).
                self.q.put(engine.ProgressEvent("__end__", "", done=True))
        threading.Thread(target=work, daemon=True).start()

    def cancel_stage(self) -> None:
        """Ask the running stage to stop (engine terminates its subprocess)."""
        if self._cancel is not None and self.active:
            self._cancel.set()
            self.cancel_btn.config(state="disabled")    # one press; _drain confirms
            self.status.config(text="cancelling …")

    # ── kick a stage off from the Track box ─────────────────────────────────
    def launch(self, stage: str) -> None:
        track = self.track.get().strip()
        media = engine.MEDIA          # media root (set DV2MV_MEDIA)
        cat = os.path.join(media, "catalog_audio")
        stem = os.path.splitext(track)[0]
        if stage == "analyze":
            self._spawn(lambda c: engine.analyze(
                os.path.join(media, "album-audio", track), cat, cancel=c))
        elif stage == "arrange":
            if self.project:
                self._arrange_project_flow()
            else:
                self._open_arrange_options(track, cat, media)
        elif stage == "compare":
            self._compare_flow(track, cat, media, stem)
        elif stage == "export":
            self._export_flow(track, cat, stem)
        elif self.project:                                   # render the project
            self._spawn(lambda c: engine.render_project(self.project, cancel=c))
        else:
            # library mode: prefer the grid last arranged; else the newest match
            sh = None
            if self._last_grid:
                cand = os.path.join(cat, f"render-{stem}{engine._tag_suffix(self._last_grid)}.sh")
                sh = cand if os.path.exists(cand) else None
            sh = sh or engine.find_render_script(cat, track) or os.path.join(
                cat, f"render-{stem}.sh")
            self._spawn(lambda c: engine.render(sh, cancel=c))

    # ── media library (the root all media/outputs live under) ───────────────
    def _refresh_lib_label(self) -> None:
        ok = not engine.looks_like_code_checkout(engine.MEDIA)
        suffix = "" if ok else "   ⚠ not set — pick a folder"
        self.lib_label.config(text=f"Library: {engine.MEDIA}{suffix}")

    def choose_media(self) -> None:
        """Pick the media library folder (remembered for next launch)."""
        path = filedialog.askdirectory(title="Choose your dv2mv media library",
                                       mustexist=True)
        if not path:
            return
        try:
            engine.set_media(path)            # validates, persists, updates MEDIA
        except engine.StageError as exc:
            messagebox.showwarning("dv2mv — media library", str(exc))
            return
        self._set_project(None)               # the old project belonged to old media
        self._refresh_lib_label()
        self.log.insert("end", f"[media] library set to {engine.MEDIA}\n")
        self.log.see("end")

    def _ensure_media(self) -> None:
        """At startup, if no valid library is set (media resolved to the code
        checkout), prompt for one instead of failing on the first stage."""
        if not engine.looks_like_code_checkout(engine.MEDIA):
            return
        messagebox.showinfo(
            "dv2mv — media library",
            "No media library is set yet. Choose the folder that holds your "
            "footage, audio, and outputs (it's remembered for next time).")
        self.choose_media()

    # ── projects ────────────────────────────────────────────────────────────
    def _set_project(self, p) -> None:
        self.project = p
        if p:
            n = p.clips if p.clips == "all" else f"{len(p.clips)} clips"
            self.proj_label.config(text=f"Project: {p.name} · {p.track} · {n}")
            self.track.delete(0, "end")
            self.track.insert(0, p.track)
        else:
            self.proj_label.config(text="Project: (none — library mode)")

    def new_project_dialog(self, preset_clips=None) -> None:
        def created(name, track, clips):
            p = engine.new_project(engine.MEDIA, name, track, clips=clips)
            self._set_project(p)
            self.log.insert("end", f"[project] created '{name}' → {p.dir}\n")
            self.log.see("end")
        NewProjectDialog(self, engine.MEDIA, self.track.get().strip(),
                         on_ok=created, clips=preset_clips)

    def open_project_dialog(self) -> None:
        names = engine.list_projects(engine.MEDIA)
        if not names:
            messagebox.showinfo("dv2mv — projects",
                                "No projects yet — use New… to create one.")
            return
        top = tk.Toplevel(self)
        top.title("Open project")
        apply_irix_theme(top)
        top.configure(bg=IRIX["bg"])
        ttk.Label(top, text="Project:").pack(side="left", padx=8, pady=8)
        choice = tk.StringVar(value=names[0])
        ttk.OptionMenu(top, choice, names[0], *names).pack(side="left", padx=4, pady=8)

        def pick():
            name = choice.get()
            top.destroy()
            self._set_project(engine.load_project(engine.MEDIA, name))
            self.log.insert("end", f"[project] opened '{name}'\n")
            self.log.see("end")
        ttk.Button(top, text="Open", command=pick).pack(side="left", padx=8, pady=8)
        top.transient(self)
        top.grab_set()
        top.bind("<Return>", lambda e: pick())
        top.bind("<Escape>", lambda e: top.destroy())
        top.after(20, top.focus_force)             # surface it (don't open behind)

    def _arrange_project_flow(self) -> None:
        p = self.project
        # the Track box is the source of truth — (re)point the project to it so
        # Arrange uses the same track Analyze just used (not a stale stored one)
        track = self.track.get().strip()
        if track:
            p.track = track
        stem = os.path.splitext(p.track)[0]
        analysis = os.path.join(engine.MEDIA, "catalog_audio", f"{stem}.analysis.json")
        if not os.path.exists(analysis):
            messagebox.showinfo(
                "dv2mv — arrange",
                f"No analysis for '{stem}' yet — run Analyze on the track first.")
            return

        def run(opts):
            for k, v in opts.items():
                setattr(p, k, v)
            p.save()                              # persists the (re)pointed track + opts
            self._set_project(p)                  # refresh the label's track
            self.status.config(text=f"arranging project '{p.name}' …")
            self._spawn(lambda c: engine.arrange_project(p, engine.MEDIA, cancel=c))
        ArrangeOptions(self, on_ok=run, initial=p.arrange_opts())

    def _open_arrange_options(self, track: str, cat: str, media: str) -> None:
        stem = os.path.splitext(track)[0]
        analysis = os.path.join(cat, f"{stem}.analysis.json")
        if not os.path.exists(analysis):
            messagebox.showinfo(
                "dv2mv — arrange",
                f"No analysis for '{stem}' yet — run Analyze on the track first.")
            return
        manifest = os.path.join(media, "catalog", "manifest.csv")

        cuts = os.path.join(media, "cuts")
        def run(p):
            self._last_grid = p["grid"]
            self.status.config(text=f"arranging on the {p['grid']} grid …")
            self._spawn(lambda c: engine.arrange(analysis, manifest, cut_dir=cuts,
                                                 cancel=c, **p))
        ArrangeOptions(self, on_ok=run,
                       initial={"grid": self._last_grid or "sections"})

    def _compare_flow(self, track: str, cat: str, media: str, stem: str) -> None:
        """Arrange the track on every grid and rank them by energy match, so you
        can pick the scheme that best fits. With a project open, sweeps its
        scoped clips + options; the winning grid is preselected for Render/Export."""
        if self.project:
            self.status.config(text=f"comparing grids for '{self.project.name}' …")
            self._spawn(lambda c: engine.compare_project(self.project, engine.MEDIA,
                                                         cancel=c))
            return
        analysis = os.path.join(cat, f"{stem}.analysis.json")
        if not os.path.exists(analysis):
            messagebox.showinfo(
                "dv2mv — compare",
                f"No analysis for '{stem}' yet — run Analyze on the track first.")
            return
        manifest = os.path.join(media, "catalog", "manifest.csv")
        cuts = os.path.join(media, "cuts")
        self.status.config(text="comparing grids …")
        self._spawn(lambda c: engine.compare(analysis, manifest, cut_dir=cuts, cancel=c))

    def _export_flow(self, track: str, cat: str, stem: str) -> None:
        """Export an arrangement to an editable timeline (OTIO + FCPXML).

        With a project open, export its current grid; else the library
        arrangement for the chosen grid (or the newest one)."""
        if self.project:
            self.status.config(text=f"exporting project '{self.project.name}' …")
            self._spawn(lambda c: engine.export_project(self.project, engine.MEDIA,
                                                        cancel=c))
            return
        arr = None
        if self._last_grid:
            cand = os.path.join(cat, f"{stem}{engine._tag_suffix(self._last_grid)}.arrange.json")
            arr = cand if os.path.exists(cand) else None
        arr = arr or engine.find_arrange_json(cat, track)
        if not arr:
            messagebox.showinfo(
                "dv2mv — export",
                f"No arrangement for '{stem}' yet — run Arrange first.")
            return
        self.status.config(text="exporting editable timeline …")
        self._spawn(lambda c: engine.export(arr, cancel=c))

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
        self._spawn(lambda c: engine.analyze(path, cat, plot=True, cancel=c))

    def open_retempo(self) -> None:
        """Time-stretch the current track to a target BPM (pitch-preserved), then
        analyze the variant so it's ready to Arrange. Needs the track analyzed
        first (we read its detected tempo to stretch from)."""
        track = self.track.get().strip()
        if not track:
            messagebox.showinfo("dv2mv — tempo", "Pick a track first.")
            return
        stem = os.path.splitext(track)[0]
        cat = os.path.join(engine.MEDIA, "catalog_audio")
        try:
            with open(os.path.join(cat, f"{stem}.analysis.json")) as fh:
                src_bpm = float(json.load(fh).get("tempo_bpm") or 0)
        except (OSError, ValueError):
            src_bpm = 0.0
        if src_bpm <= 0:
            messagebox.showinfo(
                "dv2mv — tempo",
                f"Analyze '{stem}' first — I need its detected tempo to stretch from.")
            return

        def go(target_bpm: float) -> None:
            audio = os.path.join(engine.MEDIA, "album-audio", track)
            out_name = f"{stem}-{round(target_bpm)}bpm.wav"
            # point the Track box at the variant now; Analyze writes its sidecar
            self.track.delete(0, "end")
            self.track.insert(0, out_name)
            self.status.config(
                text=f"retempo {round(src_bpm)} → {round(target_bpm)} BPM …")

            def chain(c):
                out = None
                for ev in engine.retempo(audio, target_bpm, src_bpm,
                                         out_dir=os.path.dirname(audio), cancel=c):
                    if ev.done:
                        out = (ev.result or {}).get("output")
                    yield ev
                if out:                                  # analyze the stretched track
                    yield from engine.analyze(out, cat, plot=True, cancel=c)
            self._spawn(chain)

        RetempoDialog(self, src_bpm, go)

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

        def chain(c):
            yield from engine.detect(sources, clips, cancel=c)
            # incremental: only catalog the newly-split clips, append to manifest
            yield from engine.catalog(clips, cat, append=True, cancel=c)
        self._spawn(chain)

    def open_gallery(self) -> None:
        """Open the thumbnail contact sheet for the whole library catalog.

        The gallery always shows the full library (it's the catalog browser).
        With a project open, its clips come up pre-selected (highlighted) and
        "Use selection" updates the project's footage; otherwise the action is
        "New project from selection…".
        """
        manifest = os.path.join(engine.MEDIA, "catalog", "manifest.csv")
        if not os.path.exists(manifest):
            messagebox.showinfo(
                "dv2mv — gallery",
                "No catalog yet — add footage (Video footage…) to build it first.")
            return
        preselect = (self.project.clips if self.project
                     and isinstance(self.project.clips, list) else None)
        on_apply = self._set_project_clips if self.project else None
        GalleryWindow(self, manifest, on_apply=on_apply, preselect=preselect)

    def _set_project_clips(self, clips, op: str = "replace") -> None:
        manifest = os.path.join(engine.MEDIA, "catalog", "manifest.csv")
        lib = engine.all_catalog_clips(manifest)
        self.project.clips = engine.revise_clip_selection(
            self.project.clips, clips, op, lib)
        self.project.save()
        self._set_project(self.project)        # refresh the count in the label
        n = (self.project.clips if self.project.clips == "all"
             else f"{len(self.project.clips)} clips")
        self.log.insert("end",
                        f"[project] {self.project.name}: {op} → {n}\n")
        self.log.see("end")

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
                elif ev.done and ev.result.get("summary"):
                    self._show_summary(ev.result["summary"])
                elif ev.done and ev.result.get("video"):
                    open_in_player(ev.result["video"])   # the Tk preview substitute
                elif ev.done and ev.result.get("outputs"):
                    for p in ev.result["outputs"]:       # exported timeline files
                        self.log.insert("end", f"  ▸ {p}\n")
                    self.log.see("end")
                elif ev.done and ev.result.get("comparison"):
                    self._show_comparison(ev.result)
        except queue.Empty:
            pass
        # cyclic GC is disabled (so it never fires on a worker thread and tears
        # down a Tk object off-thread → Tcl_AsyncDelete abort); reclaim cycles
        # here instead, on the main thread, ~every 5s.
        self._gc_tick = getattr(self, "_gc_tick", 0) + 1
        if self._gc_tick % 50 == 0 and not gc.isenabled():
            gc.collect()
        self.after(100, self._drain)

    def _show_summary(self, meta: dict) -> None:
        """Surface the arrange result/options in the status line and log."""
        line = format_arrange_summary(meta)
        self.status.config(text=f"✓ {meta.get('track', '')}: {line}")
        self.log.insert("end", f"  ▸ {line}\n")
        self.log.see("end")

    def _show_comparison(self, res: dict) -> None:
        """Log the grid comparison best-first and preselect the winning grid so
        Render/Export target it (mirrors the web table)."""
        ranked = res.get("ranked") or res.get("comparison") or []
        best = res.get("best")
        self.log.insert("end", "  grid comparison (best energy match first):\n")
        for r in ranked:
            pct = "—" if r.get("energy_match_pct") is None else f"{r['energy_match_pct']}%"
            star = " ★" if r["grid"] == best else ""
            self.log.insert("end", f"    {r['grid']:<10} {pct:>5}  "
                            f"cuts={r.get('cuts')}  clips={r.get('clips')}{star}\n")
        if best:
            self._last_grid = best       # Render/Export reuse the last grid
            self.status.config(text=f"✓ best fit: {best} — Render/Export will use it")
        self.log.see("end")


if __name__ == "__main__":
    # If the media root isn't set (resolved to the code checkout), the app
    # prompts for a library at startup (App._ensure_media) rather than refusing
    # to launch — so no DV2MV_MEDIA guard/exit here.
    # Disable automatic cyclic GC: with worker threads, a collection firing on a
    # worker would finalize a Tk object off the main thread → Tcl_AsyncDelete
    # abort. We collect on the main thread from the UI pump (_drain) instead.
    gc.disable()
    App().mainloop()
