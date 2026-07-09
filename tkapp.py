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
import logging
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from logging.handlers import RotatingFileHandler
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

# ── persistent log ──────────────────────────────────────────────────────────
# A windowed .app sends stdout/stderr nowhere, and the in-app console vanishes
# on quit — so a remote tester has nothing to send when a job fails. Tee the
# console + stage output to a rotating file; "send me the log" becomes the whole
# support flow. Never fatal: if the file can't be opened the app still runs.
_LOG = logging.getLogger("dv2mv")
_LOG_PATH = None


def _log_dir() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Logs", "dv2mv")   # macOS convention
    return os.path.join(home, ".local", "state", "dv2mv")       # source/Linux dev


def setup_logging() -> "str | None":
    """Attach a rotating file handler to the dv2mv logger (idempotent). Returns
    the log path, or None if it couldn't be created."""
    global _LOG_PATH
    if _LOG.handlers:
        return _LOG_PATH
    try:
        d = _log_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "dv2mv.log")
        h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5,
                                encoding="utf-8")          # ~2MB × 5 = ~10MB cap
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                         "%Y-%m-%d %H:%M:%S"))
        _LOG.addHandler(h)
        _LOG.setLevel(logging.INFO)
        _LOG.propagate = False
        _LOG_PATH = path
    except OSError:
        _LOG_PATH = None
    return _LOG_PATH


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


def _build_stamp() -> str:
    """Short commit id + date for the banner, so a tester knows which build this
    is. Empty string if it isn't a git checkout (e.g. a packaged copy)."""
    d = engine.HERE
    try:
        sha = subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        date = subprocess.run(
            ["git", "-C", d, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True, text=True, timeout=3).stdout.strip()
        return f"{sha} · {date}" if sha else ""
    except (OSError, subprocess.SubprocessError):
        return ""


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


# ── help (HELP.md rendered in a classic scrollable dialog) ──────────────────
HELP_PATH = os.path.join(engine.HERE, "HELP.md")

_INLINE_MD = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def _inline_spans(s: str):
    """Split a line into (tag, text) spans for **bold** and `code` runs."""
    out = []
    for piece in _INLINE_MD.split(s):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            out.append(("bold", piece[2:-2]))
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            out.append(("code", piece[1:-1]))
        else:
            out.append(("text", piece))
    return out


def _unwrap(md: str):
    """Merge hard-wrapped continuation lines into their paragraph/bullet, so
    a source wrapped at 76 columns reflows instead of breaking mid-thought."""
    out = []
    for line in md.splitlines():
        cont = (line and not line.startswith(("#", "- "))
                and out and out[-1] and not out[-1].startswith("#"))
        if cont:
            out[-1] += " " + line.strip()
        else:
            out.append(line)
    return out


def parse_help(md: str):
    """Markdown-lite -> (tag, text) spans for a tk.Text widget.

    Understands exactly what HELP.md uses: `#`/`##` headings, `- ` bullets,
    inline **bold** and `code`. Anything else is plain text. Pure function so
    the tests can cover it without a display.
    """
    spans = []
    for line in _unwrap(md):
        if line.startswith("## "):
            spans.append(("h2", line[3:] + "\n"))
        elif line.startswith("# "):
            spans.append(("h1", line[2:] + "\n"))
        else:
            body = line
            if line.startswith("- "):
                spans.append(("text", "  • "))
                body = line[2:]
            spans.extend(_inline_spans(body))
            spans.append(("text", "\n"))
    return spans


class HelpWindow(tk.Toplevel):
    """HELP.md in a scrollable read-only text dialog, IRIX-flavored."""

    def __init__(self, master) -> None:
        super().__init__(master)
        self.title("dv2mv Help")
        self.configure(bg=IRIX["bg"])
        self.geometry("640x680")
        try:
            with open(HELP_PATH, encoding="utf-8") as fh:
                md = fh.read()
        except OSError:
            md = ("# dv2mv Help\n\nHELP.md was not found next to the app "
                  "code.\nSee the README at the project page instead.")

        body = ttk.Frame(self, padding=6)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", bg=IRIX["light"], fg=IRIX["fg"],
                       relief="sunken", bd=2, padx=10, pady=8,
                       highlightthickness=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

        self.text = text                      # introspectable (tests)
        base = tkfont.nametofont("TkDefaultFont")
        fam, size = base.actual("family"), base.actual("size")
        text.tag_configure("h1", font=(fam, size + 5, "bold italic"),
                           spacing1=6, spacing3=6)
        text.tag_configure("h2", font=(fam, size + 2, "bold"),
                           spacing1=10, spacing3=3)
        text.tag_configure("bold", font=(fam, size, "bold"))
        text.tag_configure("code", background=IRIX["field"],
                           font=pick_irix_font(set(tkfont.families(self))))
        for tag, chunk in parse_help(md):
            text.insert("end", chunk, tag)
        text.configure(state="disabled")

        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right",
                                                                 padx=4)
        self.bind("<Escape>", lambda e: self.destroy())


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


class ThumbnailDialog(tk.Toplevel):
    """Options for the thumbnail scout: winners per group + sources to skip.

    The skip regex is remembered in the config — once a private tape is
    excluded it stays excluded on every later run.
    """

    def __init__(self, master, initial_exclude: str, on_ok) -> None:
        super().__init__(master)
        self.title("Thumbnail suggestions")
        self.configure(bg=IRIX["bg"])
        self._on_ok = on_ok
        frm = ttk.Frame(self, padding=8)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Winners per source group:").grid(
            row=0, column=0, sticky="w")
        self.v_per = tk.IntVar(value=8)
        tk.Spinbox(frm, from_=1, to=24, textvariable=self.v_per,
                   width=4).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(frm, text="Skip sources matching:").grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self.v_excl = tk.StringVar(value=initial_exclude)
        ttk.Entry(frm, textvariable=self.v_excl, width=22).grid(
            row=1, column=1, sticky="we", padx=4, pady=(8, 0))
        ttk.Label(frm, text="(a regex on the tape name — remembered, so "
                            "private tapes stay out of the sheet)").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        ttk.Button(bar, text="Scout", command=self._ok).pack(side="right",
                                                             padx=4)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")

    def _ok(self) -> None:
        self._on_ok(int(self.v_per.get()), self.v_excl.get().strip())
        self.destroy()


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
                          ("contrast", "also alternate brightness between cuts"),
                          ("variety", "alternate brightness and colour")):
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


class PreflightDialog(tk.Toplevel):
    """Required + recommended system tooling (engine.preflight()), IRIX styled.

    Re-run on re-open so installing a missing tool after launch is reflected.
    The header color answers 'can dv2mv run here?' at a glance. A 'Copy install
    command' button copies the canned one-liner for each missing tool onto the
    clipboard (so a source-mode user just pastes it into a terminal); it stays
    disabled when nothing is missing.
    """
    def __init__(self, master) -> None:
        super().__init__(master)
        self.title("Preflight — required + recommended tools")
        apply_irix_theme(self)
        self.configure(bg=IRIX["bg"])
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        self.summary = ttk.Label(body, text="checking…", font=IRIX_MENU_FONT)
        self.summary.pack(anchor="w", pady=(0, 6))
        self.grid = ttk.Frame(body)
        self.grid.pack(fill="both", expand=True)
        # build the bottom bar BEFORE the first render (render toggles copy_btn)
        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        self.copy_btn = ttk.Button(bar, text="Copy install command",
                                   command=self._copy_install, state="disabled")
        self.copy_btn.pack(side="right", padx=4)
        ttk.Button(bar, text="Re-check", command=self._recheck).pack(side="right", padx=4)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self._last = engine.preflight()
        self._render(self._last)
        self.transient(master)
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(20, self.focus_force)

    def _recheck(self) -> None:
        for w in self.grid.winfo_children():
            w.destroy()
        self._last = engine.preflight()
        self._render(self._last)

    def _copy_install(self) -> None:
        cmds = []
        for t in self._last["tools"]:
            if not t["found"] and t["install"]:
                if t["install"] not in cmds:
                    cmds.append(t["install"])
        if not cmds:
            return
        text = "\n".join(cmds)
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("dv2mv — preflight",
                            f"Copied to clipboard:\n\n{text}", parent=self)

    def _render(self, p: dict) -> None:
        self._last = p
        self.summary.config(text=p["summary"],
                            foreground="#1a6a1a" if p["ok"] else "#a33")
        # enable 'Copy install command' only when a missing tool has a canned cmd
        any_cmd = any(not t["found"] and t["install"] for t in p["tools"])
        self.copy_btn.config(state="normal" if any_cmd else "disabled")
        for i, t in enumerate(p["tools"], start=1):
            mark = "✓" if t["found"] else "✗"
            color = "#1a6a1a" if t["found"] else "#a33"
            row = ttk.Frame(self.grid, padding=(0, 2))
            row.grid(row=i, column=0, sticky="we", columnspan=2)
            tk.Label(row, text=mark, width=2, fg=color, bg=IRIX["bg"],
                     font=IRIX_MENU_FONT).pack(side="left")
            label = f"{t['name']}  ({t['kind']})"
            if t.get("bundled"):
                label += "  · bundled"        # frozen .app vendors these
            ttk.Label(row, text=label).pack(side="left", padx=4)
            ttk.Label(row, text=t["why"], foreground="#444").pack(side="left", padx=8)
            if not t["found"] and t["install"]:
                ttk.Label(row, text=t["install"],
                          foreground="#666").pack(side="left", padx=8)


class TourDialog(tk.Toplevel):
    """The interactive 'What does this software do?' walkthrough.

    Steps come from engine.TOUR_STEPS (the same data the web UI uses). Each
    step's `target` names a key in App._tour_targets — the matching widget gets
    a thick outline (Canvas overlay near it) so the eye lands on the right
    control. The card holds title/body/cue + an optional demo line.
    """
    OVERLAY_COLOR = "#5b9dff"

    def __init__(self, master) -> None:
        super().__init__(master)
        self.title("dv2mv tour")
        apply_irix_theme(self)
        self.configure(bg=IRIX["bg"])
        self._steps = engine.TOUR_STEPS
        self._i = 0
        self._overlay = None    # the Canvas rectangle id marking the current target
        # stacked above the root window for the highlight, but below the dialog
        self._marker = tk.Canvas(master, height=0, width=0, highlightthickness=0,
                                 bg=master.cget("bg"))
        self._marker.place(x=0, y=0, relwidth=1, relheight=1)

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        self.title_lbl = ttk.Label(body, font=IRIX_MENU_FONT)
        self.title_lbl.pack(anchor="w")
        pg = ttk.Frame(body)
        pg.pack(fill="x", pady=(2, 6))
        ttk.Label(pg, text="step").pack(side="right")
        self.page_lbl = ttk.Label(pg, text="")
        self.page_lbl.pack(side="right", padx=4)
        self.step_var = tk.IntVar(value=1)
        tk.Spinbox(pg, from_=1, to=len(self._steps), width=4, textvariable=self.step_var,
                   state="readonly", command=self._on_pick).pack(side="right")

        self.body = tk.Text(body, wrap="word", width=52, height=10,
                            bg=IRIX["light"], fg=IRIX["fg"], relief="sunken", bd=2,
                            padx=8, pady=6, highlightthickness=0)
        self.body.pack(fill="both", expand=True)

        self.cue = ttk.Label(body, text="", foreground="#2a6a2a", font=IRIX_MENU_FONT)
        self.cue.pack(anchor="w", pady=(6, 0))
        self.demo = ttk.Label(body, text="", foreground="#444",
                              wraplength=380, justify="left")
        self.demo.pack(anchor="w", pady=(2, 0))

        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        ttk.Button(bar, text="Skip", command=self.close).pack(side="right", padx=4)
        self.back_btn = ttk.Button(bar, text="Prev", command=self.prev)
        self.back_btn.pack(side="right", padx=4)
        self.next_btn = ttk.Button(bar, text="Next", command=self.next)
        self.next_btn.pack(side="right", padx=4)

        self.transient(master)
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(20, self.focus_force)
        # re-position whenever the highlight target or the window moves
        self._move_handle = None
        self.bind("<Configure>", lambda e: self._relayout())
        self._render()

    def _on_pick(self):
        i = self.step_var.get() - 1
        if 0 <= i < len(self._steps):
            self._i = i
            self._clamp_render()

    def _render(self) -> None:
        step = self._steps[self._i]
        self.title_lbl.config(text=step["title"])
        self.page_lbl.config(text=f"of {len(self._steps)}")
        self.step_var.set(self._i + 1)
        self.body.config(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("1.0", step["body"])
        self.body.config(state="disabled")
        self.cue.config(text=step["cue"])
        if step["demo"]:
            self.demo.config(text=("Try: " if not step["demo"].startswith("Try")
                                   else "") + step["demo"])
            self.demo.pack(anchor="w", pady=(2, 0))
        else:
            self.demo.pack_forget()
        self.back_btn.config(state="normal" if self._i > 0 else "disabled")
        self.next_btn.config(text="Done" if self._i == len(self._steps) - 1 else "Next")
        self._relayout()

    def _clamp_render(self):
        if self._i < 0:
            self._i = 0
        elif self._i >= len(self._steps):
            self._i = len(self._steps) - 1
        self._render()

    def next(self):
        if self._i + 1 >= len(self._steps):
            self.close()
            return
        self._i += 1
        self._render()

    def prev(self):
        if self._i > 0:
            self._i -= 1
            self._render()

    def _target_widget(self, name: str):
        return self.master._tour_targets.get(name)

    def _relayout(self) -> None:
        """Draw a thick outline around the current target widget, sized to its
        on-screen geometry. Re-run on each step + window move/configure."""
        try:
            if not self._marker.winfo_exists():
                return
        except tk.TclError:
            return
        step = self._steps[self._i]
        w = self._target_widget(step["target"])
        self._marker.delete("all")
        if w is None or step["target"] == "root":
            return
        try:
            self.update_idletasks()
            x = w.winfo_rootx() - self.master.winfo_rootx()
            y = w.winfo_rooty() - self.master.winfo_rooty()
            ww = w.winfo_width()
            wh = w.winfo_height()
        except tk.TclError:
            return
        if ww <= 1 or wh <= 1:
            return
        self._marker.create_rectangle(x - 4, y - 4, x + ww + 4, y + wh + 4,
                                       outline=self.OVERLAY_COLOR, width=4,
                                       tags="hl")

    def close(self) -> None:
        try:
            self._marker.destroy()
        except tk.TclError:
            pass
        if self.master._tour_win is self:
            self.master._tour_win = None
        self.destroy()


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
        self._last_grid = None     # grid chosen in the options dialog (dialog default)
        self._last_tag = None      # grid-match tag of the last arrangement (Render/Export reuse)
        self.project = None        # active Project (None => whole-library mode)
        self._cancel = None        # cancel token for the in-flight stage (a threading.Event)
        self._tour_targets = {}    # name -> widget, registered as controls are built
        self._help_win = None
        self._tour_win = None
        self._preflight_win = None

        title_font = (IRIX_MENU_FONT[0], IRIX_MENU_FONT[1] + 2, "bold italic")
        stamp = _build_stamp()
        banner = "dv2mv — offline" + (f"   ·   {stamp}" if stamp else "")
        ttk.Label(self, text=banner, anchor="center", relief="raised",
                  borderwidth=2, padding=4, font=title_font).pack(fill="x")

        # ── media library (the root all media/outputs live under) ───────────
        lib = ttk.Frame(self, padding=4)
        lib.pack(fill="x", padx=6, pady=(6, 0))
        self._tour_targets["media-library"] = lib
        self.lib_label = ttk.Label(lib, text="Library: …")
        self.lib_label.pack(side="left", padx=4)
        ttk.Button(lib, text="Help", command=self.open_help).pack(side="right",
                                                                   padx=4)
        ttk.Button(lib, text="Preflight…",
                   command=self.open_preflight).pack(side="right", padx=4)
        ttk.Button(lib, text="Tour…",
                   command=self.open_tour).pack(side="right", padx=4)
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
        # start empty: a hardcoded default made it look like a track was selected
        # (and got inherited as a new project's track) when none had been chosen.
        self.track.pack(side="left", fill="x", expand=True, padx=4)

        # ── add new media via native file dialogs ──────────────────────────
        src = ttk.Frame(self, padding=4)
        src.pack(fill="x", padx=6, pady=(0, 6))
        self._tour_targets["add-media"] = src
        ttk.Label(src, text="Add:").pack(side="left", padx=4)
        b_track = ttk.Button(src, text="Music track…",
                   command=self.add_track)
        b_track.pack(side="left", padx=4)
        self._tour_targets["add-track"] = b_track
        b_foot = ttk.Button(src, text="Video footage…",
                   command=self.add_footage)
        b_foot.pack(side="left", padx=4)
        self._tour_targets["add-footage"] = b_foot
        ttk.Button(src, text="Tempo…", command=self.open_retempo).pack(side="left", padx=4)
        b_gal = ttk.Button(src, text="Gallery…", command=self.open_gallery)
        b_gal.pack(side="right", padx=4)
        self._tour_targets["gallery"] = b_gal
        ttk.Button(src, text="Thumbnails…",
                   command=self.open_thumbnails).pack(side="right", padx=4)

        btns = ttk.Frame(self, padding=(6, 4))
        btns.pack(fill="x", padx=6)
        self._tour_targets["arrange"] = btns
        self._tour_targets["render-export"] = btns
        # labels disambiguate the two outputs: Render bakes a finished .mp4,
        # Export emits an editable timeline (OTIO/FCPXML) for an NLE.
        for label, stage in (("Analyze", "analyze"), ("Arrange", "arrange"),
                             ("Compare", "compare"), ("Render to MP4", "render"),
                             ("Export to editor", "export")):
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
        self._pb_running = False  # is the animation loop currently scheduled?

        # the log is the "console" — give it the fixed SGI shell font
        self.log = tk.Text(self, height=10, font=pick_irix_font(set(tkfont.families(self))),
                           bg="black", fg="#33ff33", relief="sunken", bd=2)  # green-on-black
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        self._refresh_lib_label()
        _LOG.info("Tk %s · media library: %s",
                  self.tk.call("info", "patchlevel"), engine.MEDIA)
        self.after(100, self._drain)
        # the progress animation only runs while a stage is in flight (see
        # _pb_start/_begin) — no idle 60ms canvas churn to compete with input.
        self.after(150, self._ensure_media)   # prompt for a library if none is set

        # macOS: a freshly-foregrounded app window can be the *frontmost* window
        # without being the *key* window, and in that state the first click only
        # arms a ttk.Button (it takes the focus ring) without firing its command
        # — exactly the "buttons need a long/firm press" symptom. The dialogs
        # already counter this with focus_force ("win the first click"); the main
        # window needs it too.
        #
        # Two distinct moments need covering:
        #   • Re-activation (click away + back): macOS Tk sends <Activate> when
        #     the window becomes active again — re-assert key there.
        #   • Cold launch: the window must be key *before* the user reaches the
        #     first button, or that first click is spent activating the window
        #     and its command is lost (this is why the top-most button — Media
        #     library… — felt sticky). A single early after() isn't enough: at
        #     launch the window often isn't mapped yet, so focus_force no-ops.
        #     Retry across a few escalating delays to outlast the packaged app's
        #     slower window mapping.
        self.lift()
        self.bind("<Activate>", self._mac_become_key)
        for _delay in (20, 120, 350, 700):                # win the first click (macOS)
            self.after(_delay, self._mac_become_key)

    def _mac_become_key(self, _event=None) -> None:
        """Make the main window the macOS key window so the first click fires a
        button command instead of just arming it. Skip while a child dialog holds
        the grab — stealing its focus would fight its own focus_force."""
        if self.grab_current():
            return
        self.focus_force()

    # ── progress bar (drawn on a Canvas; classic look, always moving) ───────
    def _begin(self) -> None:
        self.active += 1
        self._pb_frac = None
        self.cancel_btn.config(state="normal")     # a stage is in flight → armed
        self._pb_start()                           # wake the animation loop

    def _end(self) -> None:
        self.active = max(0, self.active - 1)
        if self.active == 0:
            self._pb_frac = None
            self._cancel = None
            self.cancel_btn.config(state="disabled")

    def _pb_start(self) -> None:
        """Start the progress animation if it isn't already running."""
        if not self._pb_running:
            self._pb_running = True
            self._pb_tick()

    def _pb_tick(self) -> None:
        # Idle: clear the bar once and STOP rescheduling — a 60ms canvas redraw
        # loop running while nothing's happening competes with macOS input
        # handling and makes buttons feel like they need a firmer press.
        if not self.active:
            self.pb.delete("fill")
            self._pb_running = False
            return
        self.pb.delete("fill")
        w = self.pb.winfo_width()
        h = int(self.pb["height"])
        if self._pb_frac is not None:                                 # determinate
            self.pb.create_rectangle(0, 0, int(w * self._pb_frac), h,
                                     fill="#5b9dff", width=0, tags="fill")
        else:                                                         # indeterminate sweep
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
            # library mode: prefer the last arrangement (grid-match tag); else the newest
            sh = None
            if self._last_tag:
                cand = os.path.join(cat, f"render-{stem}{engine._tag_suffix(self._last_tag)}.sh")
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
        self._console(f"[media] library set to {engine.MEDIA}\n")

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
            self._console(f"[project] created '{name}' → {p.dir}\n")
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
            self._console(f"[project] opened '{name}'\n")
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
            self._last_tag = engine.arrange_tag(p["grid"], p["match"])  # Render/Export reuse
            self.status.config(text=f"arranging {p['grid']} · {p['match']} …")
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
        if self._last_tag:
            cand = os.path.join(cat, f"{stem}{engine._tag_suffix(self._last_tag)}.arrange.json")
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
        analyze the variant so it's ready to Arrange. Refuses unless there's a
        real, analyzed track — the slider needs the detected tempo AND an actual
        audio file to stretch (the Track box alone can show a stale name)."""
        track = self.track.get().strip()
        if not track:
            messagebox.showinfo("dv2mv — tempo",
                                "No track selected — add one with “Music track…” first.")
            return
        stem = os.path.splitext(track)[0]
        cat = os.path.join(engine.MEDIA, "catalog_audio")
        try:
            with open(os.path.join(cat, f"{stem}.analysis.json")) as fh:
                an = json.load(fh)
        except (OSError, ValueError):
            an = None
        src_bpm = float((an or {}).get("tempo_bpm") or 0)
        if not an or src_bpm <= 0:
            messagebox.showinfo(
                "dv2mv — tempo",
                f"No analyzed track named “{stem}”.\n\n"
                "Add a music track (“Music track…”) and Analyze it before retempo — "
                "the tempo slider needs a real, analyzed audio file to stretch.")
            return
        # the analysis records the actual audio file it was run on; that's what
        # we stretch. If it's absent the Track box is showing a name with no
        # audio behind it — refuse rather than deceptively opening the slider.
        audio = an.get("path") or ""
        if not audio or not os.path.exists(audio):
            messagebox.showinfo(
                "dv2mv — tempo",
                f"The audio file for “{stem}” can’t be found"
                + (f":\n{audio}" if audio else ".")
                + "\n\nAdd and Analyze a music track before retempo.")
            return

        def go(target_bpm: float) -> None:
            out_dir = os.path.join(engine.MEDIA, "album-audio")
            out_name = f"{stem}-{round(target_bpm)}bpm.wav"
            # point the Track box at the variant now; Analyze writes its sidecar
            self.track.delete(0, "end")
            self.track.insert(0, out_name)
            self.status.config(
                text=f"retempo {round(src_bpm)} → {round(target_bpm)} BPM …")

            def chain(c):
                out = None
                for ev in engine.retempo(audio, target_bpm, src_bpm,
                                         out_dir=out_dir, cancel=c):
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

    def open_help(self) -> None:
        """Show HELP.md (singleton window — a second click raises it)."""
        if self._help_win is not None and self._help_win.winfo_exists():
            self._help_win.lift()
            return
        self._help_win = HelpWindow(self)

    def open_preflight(self) -> None:
        """Required + recommended tooling check (singleton)."""
        if self._preflight_win is not None and self._preflight_win.winfo_exists():
            self._preflight_win.lift()
            return
        self._preflight_win = PreflightDialog(self)

    def open_tour(self, step: int = 0) -> None:
        """Interactive walkthrough ('What does this software do?').

        Singleton: opening it again (a second click on Tour…) lifts the existing
        window instead of stacking two markers.
        """
        if self._tour_win is not None and self._tour_win.winfo_exists():
            self._tour_win.lift()
            return
        if self._tour_targets.get("media-library") is None:
            # Tour needs the registered control anchors; if unavailable for any
            # reason, fall back to the help window rather than a no-op click.
            messagebox.showinfo("dv2mv — tour", "Tour anchors missing — opening Help instead.")
            self.open_help()
            return
        self._tour_win = TourDialog(self)
        if step:
            self._tour_win._i = max(0, min(step, len(engine.TOUR_STEPS) - 1))
            self._tour_win._render()

    def open_thumbnails(self) -> None:
        """Scout cover/YouTube thumbnail frames from the catalog; opens the
        contact sheet when done. The skip regex persists in the config."""
        media = engine.MEDIA
        manifest = os.path.join(media, "catalog", "manifest.csv")
        if not os.path.exists(manifest):
            messagebox.showinfo("dv2mv — thumbnails",
                                "No catalog yet — Add video footage first.")
            return
        saved = engine.load_config().get("thumbs_exclude", "")

        def go(per_group: int, exclude: str) -> None:
            if exclude != saved:
                cfg = engine.load_config()
                cfg["thumbs_exclude"] = exclude
                engine.save_config(cfg)
            out = os.path.join(media, "thumbnails")

            def chain(c):
                final = {}
                for ev in engine.thumbnails(manifest, out, per_group=per_group,
                                            exclude_re=exclude, cancel=c):
                    if ev.done:
                        final = ev.result
                    yield ev
                if final.get("contact"):
                    open_in_player(final["contact"])   # show the sheet

            self._spawn(chain)

        ThumbnailDialog(self, saved, on_ok=go)

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
        # Reflect the project's saved scope in the gallery. A clip list comes up
        # highlighted; an "all" scope means the whole library is in scope, so
        # show *everything* selected — otherwise an all-scope project (the
        # default, and what "Add →" leaves you with) opened to an empty-looking
        # gallery, as if the selection had vanished.
        if self.project is None:
            preselect = None
        elif isinstance(self.project.clips, list):
            preselect = self.project.clips
        else:                                       # "all"
            preselect = engine.all_catalog_clips(manifest)
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
        self._console(f"[project] {self.project.name}: {op} → {n}\n")

    def _console(self, text: str) -> None:
        """Append to the in-app console *and* the rotating log file, so the two
        never diverge — support is 'send ~/Library/Logs/dv2mv/dv2mv.log' instead
        of 'screenshot the panel'."""
        self.log.insert("end", text)
        self.log.see("end")
        _LOG.info(text.rstrip("\n"))

    def report_callback_exception(self, exc, val, tb) -> None:
        """Uncaught exception in a Tk callback. In a windowed .app these vanish
        (no stderr); log the full traceback and point the user at the log."""
        import traceback
        _LOG.error("unhandled exception:\n%s",
                   "".join(traceback.format_exception(exc, val, tb)))
        try:
            messagebox.showerror(
                "dv2mv — unexpected error",
                f"{val}\n\nDetails were written to the log:\n"
                f"{_LOG_PATH or '(log file unavailable)'}")
        except Exception:
            pass

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
                self._console(f"[{ev.stage}] {ev.message}\n")
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
                        self._console(f"  ▸ {p}\n")
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
        self._console(f"  ▸ {line}\n")

    def _show_comparison(self, res: dict) -> None:
        """Log the grid × match comparison (best energy first) so you can weigh
        each strategy's brightness/colour alternation against its fit to the song,
        then Arrange the grid + match you pick."""
        ranked = res.get("ranked") or res.get("comparison") or []
        best = res.get("best")                # winning tag, e.g. "downbeats-contrast"
        self._console("  comparison — grid × match  "
                      "(engy = fit to song; luma/hue = brightness/colour "
                      "alternation, higher = punchier):\n")
        self._console(f"    {'grid':<10} {'match':<9} "
                      f"{'engy':>5} {'luma':>5} {'hue':>5}  cuts\n")
        top_grid = None
        for r in ranked:
            pct = "—" if r.get("energy_match_pct") is None else f"{r['energy_match_pct']:.0f}%"
            lc = "—" if r.get("luma_contrast") is None else f"{r['luma_contrast']:.2f}"
            hv = "—" if r.get("hue_variety") is None else f"{r['hue_variety']:.2f}"
            star = " ★" if r.get("tag") == best else ""
            if r.get("tag") == best:
                top_grid = r.get("grid")
            self._console(f"    {r.get('grid',''):<10} {r.get('match',''):<9} "
                          f"{pct:>5} {lc:>5} {hv:>5}  {r.get('cuts')}{star}\n")
        if top_grid:
            self._last_grid = top_grid       # Arrange opens on the best-energy grid
            self._last_tag = best            # Render/Export target the winning grid-match
            self.status.config(
                text="compared — open Arrange to pick a grid + match, then Render")


def main() -> None:
    """Launch the desktop app. Single entry point shared by `python tkapp.py`
    and the packaged macOS .app (packaging/entry.py calls this)."""
    # If the media root isn't set (resolved to the code checkout), the app
    # prompts for a library at startup (App._ensure_media) rather than refusing
    # to launch — so no DV2MV_MEDIA guard/exit here.
    # Disable automatic cyclic GC: with worker threads, a collection firing on a
    # worker would finalize a Tk object off the main thread → Tcl_AsyncDelete
    # abort. We collect on the main thread from the UI pump (_drain) instead.
    gc.disable()

    log_path = setup_logging()
    _LOG.info("─" * 60)
    _LOG.info("dv2mv starting · %s · python %s · %s",
              _build_stamp() or "dev", platform.python_version(), platform.platform())
    if log_path:
        _LOG.info("log file: %s", log_path)

    App().mainloop()


if __name__ == "__main__":
    main()
