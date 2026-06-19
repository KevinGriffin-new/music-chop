#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
engine.py — headless core for the DV-footage → music-video pipeline.

This is the single source of truth that BOTH front ends sit on top of:

    web (FastAPI + <video>)  ─┐
                              ├─▶  engine.py  ─▶  proven CLI scripts
    Tkinter (classic look)  ─┘                    (scenedetect / clip_features /
                                                   track_analyze / sync_clips / ffmpeg)

Design goals
------------
* One workflow, written once. The UIs are thin clients — they only collect
  parameters, spawn a stage, and render the ProgressEvents it yields.
* Don't fork the logic. The existing scripts already work and are the source of
  truth, so each stage runs them as a subprocess and parses their output into
  structured progress. (If you later want to drop the subprocess hop, the
  TODOs below point at the exact functions to inline — but you do not need to.)
* No global state, no printing. Stages take explicit paths and a callback;
  they raise StageError on failure. That makes them equally callable from a
  FastAPI background task or a Tkinter worker thread.

Each stage is a generator-style function:

    for ev in engine.analyze(audio, out_dir):
        ui.show(ev)            # ev.frac in 0..1 (or None), ev.message, ev.done

or with an explicit callback via run_stage(). See __main__ for a smoke test.

Requires (inherited from the scripts): scenedetect, opencv-python, librosa,
numpy, scipy, scikit-learn, and ffmpeg/ffprobe on PATH.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from glob import glob
from typing import Callable, Iterator, Optional

# ── project layout ─────────────────────────────────────────────────────────
# engine.py lives at the project root; the vendored pipeline scripts live in
# ./pipeline/. Media (footage, audio, render outputs) lives OUTSIDE the repo:
# set DV2MV_MEDIA to its root (defaults to the current working directory). This
# keeps the code a small, version-controlled project independent of the media.
HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.path.join(HERE, "pipeline")
MEDIA = os.path.abspath(os.environ.get("DV2MV_MEDIA", os.getcwd()))

SCRIPT = {
    "features": os.path.join(PIPELINE, "clip_features.py"),
    "analyze":  os.path.join(PIPELINE, "track_analyze.py"),
    "sync":     os.path.join(PIPELINE, "sync_clips.py"),
}

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".avi", ".dv")

# one-line help for each arrange grid (shared by both UIs so they stay in sync)
GRID_HELP = {
    "sections":  "one cut per song section — calmest, longest takes",
    "downbeats": "one cut per bar — driving, lands on the beat",
    "beats":     "one cut every N beats (beats-per-cut) — fast montage",
    "harmonic":  "one cut at each chord / harmony change",
}


# ── progress / error model ─────────────────────────────────────────────────
@dataclass
class ProgressEvent:
    """One unit of feedback from a running stage."""
    stage: str                       # "detect" | "catalog" | "analyze" | ...
    message: str = ""                # human-readable line for the UI
    frac: Optional[float] = None     # 0.0..1.0, or None when indeterminate
    done: bool = False               # True on the final event
    result: dict = field(default_factory=dict)  # output paths, stats (final ev)


class StageError(RuntimeError):
    """A stage failed; message carries the captured reason."""


ProgressCallback = Callable[[ProgressEvent], None]


# ── subprocess plumbing shared by every stage ──────────────────────────────
def _stream(cmd: list[str], cwd: str) -> Iterator[str]:
    """Run cmd, yielding combined stdout/stderr lines as they appear.

    Raises StageError(non-zero exit) with the tail of output for context.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        tail = (tail + [line])[-25:]
        yield line
    code = proc.wait()
    if code != 0:
        raise StageError(f"{cmd[0]} ... exited {code}\n" + "\n".join(tail))


_IOFN = re.compile(r"\[(\d+)/(\d+)\]")        # "[3/12] name" -> 3,12
_MATCH = re.compile(r"energy match:\s*~?(\d+)%")
_PROG = re.compile(r"PROG (\d+)/(\d+)\s*(.*)")  # "PROG 2/7 beats" -> 2,7,"beats"


def _require(stage: str, paths: dict, tail: Optional[list[str]] = None) -> dict:
    """Verify every declared output path exists; raise StageError if not.

    Wrapped scripts can exit 0 yet write nothing (e.g. track_analyze.py
    swallows a per-track exception and still returns 0). Without this check a
    stage would report done=True with a result dict pointing at files that were
    never written. We fail loud instead, surfacing the captured output tail so
    the real reason is visible.
    """
    missing = [str(p) for p in paths.values()
               if isinstance(p, str) and (p.endswith((".csv", ".json", ".sh",
                   ".mp4", ".png", ".txt"))) and not os.path.exists(p)]
    if missing:
        why = ("\n--- last output lines ---\n" + "\n".join(tail)) if tail else ""
        raise StageError(
            f"{stage}: completed without error but expected output(s) missing: "
            + ", ".join(missing) + why)
    return paths


def _read_summary(path: str):
    """Read tracks_summary.csv → (fieldnames, {track: row_dict}). Empty if absent."""
    if not os.path.exists(path):
        return None, {}
    with open(path, newline="") as fh:
        r = csv.DictReader(fh)
        rows = {row["track"]: row for row in r if row.get("track")}
        return r.fieldnames, rows


def _merge_summary(path: str, before) -> None:
    """Re-merge the just-written single-track summary back into the snapshot.

    track_analyze.py rewrites tracks_summary.csv with only the track(s) it was
    handed. `before` is the snapshot taken before the run; this inserts/replaces
    the freshly-written row(s) into it and writes the combined table in track
    order, so re-analyzing one track no longer drops the others.
    """
    before_fields, before_rows = before
    new_fields, new_rows = _read_summary(path)
    if not new_rows:
        return  # nothing fresh written (shouldn't happen after _require)
    fields = new_fields or before_fields
    merged = dict(before_rows)
    merged.update(new_rows)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for track in sorted(merged):
            w.writerow(merged[track])


def run_stage(gen: Iterator[ProgressEvent], on_progress: ProgressCallback) -> dict:
    """Drive a stage generator with a callback; return the final result dict.

    Convenience for callers that prefer a callback over iterating. The web app
    iterates directly (to stream SSE); the Tk app uses this from its worker.
    """
    result: dict = {}
    for ev in gen:
        on_progress(ev)
        if ev.done:
            result = ev.result
    return result


# ── stage 0: ingest (normalize mixed sources to one codec/res/fps) ──────────
def ingest(
    src_dir: str,
    out_dir: str,
    crf: int = 18,
    preset: str = "slow",
    fps: Optional[int] = 30,
    scale: Optional[str] = "720:480",
) -> Iterator[ProgressEvent]:
    """Transcode every source in src_dir to a common codec/res/fps in out_dir.

    Optional step. The user already works from imported DV (mp4), so this only
    matters when sources are mixed: re-encoding to one libx264/res/fps profile
    is what keeps the downstream `-c copy` concat valid (mismatched params make
    concat silently corrupt or refuse). No tape/Firewire capture — out of scope.

    Idempotent: a source whose normalized output already exists is skipped.
    """
    st = "ingest"
    if os.path.abspath(src_dir) == os.path.abspath(out_dir):
        raise StageError("ingest: out_dir must differ from src_dir (would overwrite sources)")
    os.makedirs(out_dir, exist_ok=True)
    sources = sorted(
        p for p in glob(os.path.join(src_dir, "*"))
        if p.lower().endswith(VIDEO_EXTS)
    )
    if not sources:
        raise StageError(f"ingest: no source video found in {src_dir}")

    vf = []
    if scale:
        vf.append(f"scale={scale}:force_original_aspect_ratio=decrease")
        vf.append(f"pad={scale}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if fps:
        vf.append(f"fps={fps}")

    todo = [(f, os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + ".mp4"))
            for f in sources]
    todo = [(f, o) for f, o in todo if not os.path.exists(o)]
    if not todo:
        outs = [os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + ".mp4")
                for f in sources]
        yield ProgressEvent(st, "All sources already normalized.", 1.0, True,
                            {"ingest_dir": out_dir, "normalized": 0, "outputs": outs})
        return

    total = len(todo)
    for i, (f, out) in enumerate(todo):
        yield ProgressEvent(st, f"normalizing — {os.path.basename(f)}", i / total)
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", f]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
                "-c:a", "aac", out]
        tail = []
        for line in _stream(cmd, cwd=MEDIA):
            tail = (tail + [line])[-25:]
        _require(st, {"out": out}, tail)
    outs = [os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + ".mp4")
            for f in sources]
    yield ProgressEvent(st, f"Normalized {total} sources → {out_dir}", 1.0, True,
                        {"ingest_dir": out_dir, "normalized": total, "outputs": outs})


# ── stage 1: detect (scene-split sources into a clip pile) ──────────────────
def _list_sources(src) -> list[str]:
    """Resolve `src` (a directory, a single file, or a list of files) to a
    sorted list of existing video files. Lets file pickers/uploads feed
    individual paths while the dir-based callers keep working unchanged.
    """
    if isinstance(src, (list, tuple)):
        cand = [os.path.abspath(p) for p in src]
    elif isinstance(src, str) and os.path.isdir(src):
        cand = glob(os.path.join(src, "*"))
    elif isinstance(src, str):
        cand = [src]                       # a single file path
    else:
        cand = []
    return sorted(p for p in cand
                  if os.path.isfile(p) and p.lower().endswith(VIDEO_EXTS))


def detect(
    src,                                   # dir, single file, or list of files
    out_dir: str,
    threshold: float = 27.0,
    min_scene_len: str = "0.6s",
    rate_factor: int = 18,
    preset: str = "slow",
) -> Iterator[ProgressEvent]:
    """Split source video into scene clips in out_dir.

    `src` may be a directory of sources, a single video file, or a list of
    files (e.g. picked in a file dialog or uploaded). Mirrors fates-end-chop.sh
    but per-source, so we can emit real progress. Skips sources already split
    (idempotent re-runs).
    """
    st = "detect"
    os.makedirs(out_dir, exist_ok=True)
    sources = _list_sources(src)
    if not sources:
        raise StageError(f"detect: no source video found ({src!r})")
    # skip any source whose scene clips already exist
    todo = []
    for f in sources:
        base = os.path.splitext(os.path.basename(f))[0]
        if not glob(os.path.join(out_dir, f"{base}-Scene-*.mp4")):
            todo.append(f)
    if not todo:
        yield ProgressEvent(st, "All sources already split.", 1.0, True,
                            {"clips_dir": out_dir, "split": 0})
        return

    total = len(todo)
    for i, f in enumerate(todo):
        base = os.path.basename(f)
        yield ProgressEvent(st, f"detecting scenes — {base}", i / total)
        cmd = [
            "scenedetect", "-i", f, "-o", out_dir,
            "detect-content", "--threshold", str(threshold),
            "--min-scene-len", min_scene_len,
            "split-video", "--rate-factor", str(rate_factor), "--preset", preset,
        ]
        for _ in _stream(cmd, cwd=MEDIA):
            pass  # scenedetect is noisy; we report per-file granularity
    n = len(glob(os.path.join(out_dir, "*-Scene-*.mp4")))
    if n == 0:
        raise StageError(
            f"detect: scenedetect exited 0 but no *-Scene-*.mp4 clips landed in "
            f"{out_dir} (are the sources valid video?)")
    yield ProgressEvent(st, f"Split {total} sources → {n} clips.", 1.0, True,
                        {"clips_dir": out_dir, "split": total, "clips": n})


# ── stage 2: catalog (per-clip visual features) ────────────────────────────
def catalog(
    clips_dir: str,
    out_dir: str,
    frames: int = 12,
    width: int = 160,
    recursive: bool = False,
) -> Iterator[ProgressEvent]:
    """Extract features → out_dir/manifest.csv (+ histograms.npz, thumbs/).

    TODO(claude-code): to drop the subprocess, call clip_features.analyze()
    per file directly and write the manifest here. Not required — this works.
    """
    st = "catalog"
    os.makedirs(out_dir, exist_ok=True)
    cmd = [sys.executable, SCRIPT["features"], clips_dir, "-o", out_dir,
           "--frames", str(frames), "--width", str(width)]
    if recursive:
        cmd.append("--recursive")
    tail: list[str] = []
    for line in _stream(cmd, cwd=MEDIA):
        tail = (tail + [line])[-25:]
        m = _IOFN.search(line)
        if m:
            i, n = int(m.group(1)), int(m.group(2))
            yield ProgressEvent(st, line, i / n)
    result = {"manifest": os.path.join(out_dir, "manifest.csv"),
              "thumbs": os.path.join(out_dir, "thumbs")}
    _require(st, {"manifest": result["manifest"]}, tail)
    yield ProgressEvent(st, "Catalog complete.", 1.0, True, result)


# ── stage 3: analyze (musical structure of a track) ────────────────────────
def analyze(
    audio: str,
    out_dir: str,
    sr: int = 22050,
    plot: bool = True,
) -> Iterator[ProgressEvent]:
    """Run track_analyze on a single track → out_dir/<track>.analysis.json (+png).

    Note: track_analyze rewrites tracks_summary.csv for whatever it's given. We
    run it on the single file; the UI/caller is responsible for merging the
    summary row if it wants a combined summary (the desktop app should).
    """
    st = "analyze"
    os.makedirs(out_dir, exist_ok=True)
    track = os.path.splitext(os.path.basename(audio))[0]
    analysis = os.path.join(out_dir, f"{track}.analysis.json")
    # Snapshot the combined summary before running: track_analyze rewrites
    # tracks_summary.csv for only the track it's given, so a single-track run
    # would otherwise clobber every other row. We restore + merge below.
    summary_path = os.path.join(out_dir, "tracks_summary.csv")
    before = _read_summary(summary_path)

    cmd = [sys.executable, SCRIPT["analyze"], audio, "-o", out_dir, "--sr", str(sr)]
    if plot:
        cmd.append("--plot")
    yield ProgressEvent(st, f"analyzing {track} …", None)
    tail = []
    for line in _stream(cmd, cwd=MEDIA):
        tail = (tail + [line])[-25:]
        # track_analyze emits "PROG i/n message" per step → a real moving bar
        m = _PROG.search(line)
        if m:
            i, n = int(m.group(1)), int(m.group(2))
            yield ProgressEvent(st, m.group(3).strip() or line, i / n)

    # track_analyze swallows per-track exceptions and still exits 0, so a
    # missing JSON here is a real failure — fail loud with the captured tail.
    _require(st, {"analysis": analysis}, tail)
    _merge_summary(summary_path, before)

    result = {"analysis": analysis}
    if plot and os.path.exists(os.path.join(out_dir, f"{track}.png")):
        result["plot"] = os.path.join(out_dir, f"{track}.png")
    # a clean completion line (not track_analyze's trailing "Next: …" chatter)
    msg = f"Analyzed {track}."
    try:
        with open(analysis) as fh:
            a = json.load(fh)
        msg = (f"Analyzed {track} — {a['tempo_bpm']:.0f} BPM, {a['key']}, "
               f"{len(a.get('sections', []))} sections")
    except (OSError, ValueError, KeyError):
        pass
    yield ProgressEvent(st, msg, 1.0, True, result)


# ── stage 4: arrange (sync clips to the track's structure) ─────────────────
def _tag_suffix(tag: str) -> str:
    """Sanitize a tag into a filename-safe '-tag' suffix (mirrors clip_order.py)."""
    t = re.sub(r"[^A-Za-z0-9._-]+", "-", tag).strip("-")
    return f"-{t}" if t else ""


def find_render_script(out_dir: str, track: str) -> Optional[str]:
    """Resolve a track name to the render script arrange() actually wrote.

    arrange() suffixes outputs by grid/tag (render-<track>-<tag>.sh), but the
    UIs only know the track. Return the newest matching script (so 'Render'
    targets the most recent arrangement), or None if the track hasn't been
    arranged yet. Handles a track passed with or without its audio extension.
    """
    stem = os.path.splitext(os.path.basename(track))[0]
    cands = glob(os.path.join(out_dir, f"render-{stem}*.sh"))
    return max(cands, key=os.path.getmtime) if cands else None


def arrange(
    analysis_json: str,
    manifest: str,
    grid: str = "sections",            # sections | downbeats | beats | harmonic
    beats_per_cut: int = 4,
    allow_reuse: bool = False,
    drop_blurry: float = 0.0,
    clip_from: str = "middle",         # middle | start
    tag: Optional[str] = None,         # output suffix; defaults to the grid name
    out_dir: Optional[str] = None,     # where sidecars land; default = analysis dir
    cut_dir: Optional[str] = None,     # where the final cut-*.mp4 lands; default = out_dir
) -> Iterator[ProgressEvent]:
    """Build the cut: order-sync csv, labels, markers, and render-<track>.sh.

    Returns the energy-match % parsed from sync_clips so the UI can show it.

    Outputs are suffixed with `tag` (default: the grid name) so re-running a
    different grid no longer overwrites a previous cut's sidecars — the user
    compares arrangements, so each must survive. Pass tag="" for the old
    unsuffixed names.
    """
    st = "arrange"
    track = os.path.splitext(os.path.basename(analysis_json))[0].replace(".analysis", "")
    # Prerequisites must exist, else the UI sees a cryptic "can't open file".
    # Surface an actionable prompt instead — these flow to both front ends.
    if not os.path.exists(analysis_json):
        raise StageError(
            f"No analysis for '{track}' yet — run Analyze on the track first.")
    if not os.path.exists(manifest):
        raise StageError(
            "No clip catalog yet — add footage (Upload + analyze footage) to "
            "build the manifest before arranging.")
    if tag is None:
        tag = grid
    cmd = [sys.executable, SCRIPT["sync"], "--analysis", analysis_json,
           "--manifest", manifest, "--grid", grid,
           "--beats-per-cut", str(beats_per_cut),
           "--drop-blurry", str(drop_blurry), "--clip-from", clip_from,
           "--tag", tag]
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        cmd += ["--out", out_dir]
    if cut_dir:
        cmd += ["--cut-dir", cut_dir]
    if allow_reuse:
        cmd.append("--allow-reuse")
    yield ProgressEvent(st, f"syncing clips on the {grid} grid …", None)
    match, tail = None, []
    for line in _stream(cmd, cwd=MEDIA):
        tail = (tail + [line])[-25:]
        m = _MATCH.search(line)
        if m:
            match = int(m.group(1))
    dest = out_dir or os.path.dirname(os.path.abspath(analysis_json))
    sfx = _tag_suffix(tag)
    result = {"order": os.path.join(dest, f"order-sync-{track}{sfx}.csv"),
              "labels": os.path.join(dest, f"{track}{sfx}.labels.txt"),
              "markers": os.path.join(dest, f"{track}{sfx}.markers.csv"),
              "render_sh": os.path.join(dest, f"render-{track}{sfx}.sh"),
              "options": os.path.join(dest, f"{track}{sfx}.arrange.json"),
              "energy_match": match}
    _require(st, {k: result[k] for k in
                  ("order", "labels", "markers", "render_sh", "options")}, tail)
    # carry the recorded options/stats in the event so the UIs can show them
    try:
        with open(result["options"]) as fh:
            result["summary"] = json.load(fh)
    except (OSError, ValueError):
        pass
    yield ProgressEvent(st, f"Arranged ({match}% energy match)." if match is not None
                        else "Arranged.", 1.0, True, result)


# ── stage 5: render (cut the clips to the grid + lay the music on top) ──────
def render(render_sh: str) -> Iterator[ProgressEvent]:
    """Execute the render-<track>.sh produced by arrange() → cut-<track>.mp4.

    The script re-encodes every segment then concats + muxes the audio, so this
    is the long pole. arrange() writes "[seg/total]" markers into the script, so
    render reports a real fractional bar as segments complete.
    """
    st = "render"
    if not os.path.exists(render_sh):
        raise StageError(
            "No render script yet — run Arrange on the track first "
            f"(missing {os.path.basename(render_sh)}).")
    cwd = os.path.dirname(os.path.abspath(render_sh))
    track = os.path.basename(render_sh).replace("render-", "").replace(".sh", "")
    # the script writes the cut wherever arrange put it (e.g. a cuts/ folder) and
    # echoes "wrote <path>"; capture that rather than assuming the location.
    video = None
    yield ProgressEvent(st, "rendering — this is the slow one …", None)
    tail = []
    for line in _stream(["bash", render_sh], cwd=cwd):
        line = line.strip()
        if not line:
            continue
        tail = (tail + [line])[-25:]
        if line.startswith("wrote "):
            video = line[len("wrote "):].strip()
        # arrange() emits "[seg/total]" per segment so the bar is real, not a spinner.
        m = _IOFN.search(line)
        frac = (int(m.group(1)) / int(m.group(2))) if m else None
        yield ProgressEvent(st, line, frac)
    if not video:                       # fallback for older scripts
        video = os.path.join(cwd, f"cut-{track}.mp4")
    _require(st, {"video": video}, tail)
    yield ProgressEvent(st, f"Render complete → {video}", 1.0, True, {"video": video})


# ── projects ────────────────────────────────────────────────────────────────
# A project scopes one music video: a track + a clip selection (a subset of the
# shared library catalog, or "all") + arrange options + its own output folder.
# The library (MEDIA/catalog) stays global; a project just *references* library
# clips, so footage is never copied per project.
PROJECTS_DIRNAME = "projects"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "-", name or "").strip().strip("-") or "untitled"


@dataclass
class Project:
    name: str
    track: str                          # audio filename, e.g. "02 Erased.mp3"
    clips: object = "all"               # list of clip paths, or "all" (whole library)
    grid: str = "sections"
    beats_per_cut: int = 4
    allow_reuse: bool = False
    drop_blurry: float = 0.0
    clip_from: str = "middle"
    dir: str = ""                       # project folder (holds project.json + outputs)

    def arrange_opts(self) -> dict:
        return {"grid": self.grid, "beats_per_cut": self.beats_per_cut,
                "allow_reuse": self.allow_reuse, "drop_blurry": self.drop_blurry,
                "clip_from": self.clip_from}

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in
             ("name", "track", "clips", "grid", "beats_per_cut",
              "allow_reuse", "drop_blurry", "clip_from")}
        return d

    def save(self) -> str:
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "project.json")
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=1)
        return path

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path) as fh:
            d = json.load(fh)
        d["dir"] = os.path.dirname(os.path.abspath(path))
        return cls(**d)


def projects_root(media: str) -> str:
    return os.path.join(media, PROJECTS_DIRNAME)


def list_projects(media: str) -> list[str]:
    """Names of projects (subfolders of MEDIA/projects with a project.json)."""
    root = projects_root(media)
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if os.path.exists(os.path.join(root, d, "project.json")))


def new_project(media: str, name: str, track: str, clips="all", **opts) -> Project:
    """Create + persist a project under MEDIA/projects/<name>/."""
    p = Project(name=name, track=track, clips=clips,
                dir=os.path.join(projects_root(media), _safe_name(name)),
                **{k: v for k, v in opts.items() if k in
                   ("grid", "beats_per_cut", "allow_reuse", "drop_blurry", "clip_from")})
    p.save()
    return p


def load_project(media: str, name: str) -> Project:
    return Project.load(os.path.join(projects_root(media), _safe_name(name), "project.json"))


AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".aif", ".aiff", ".wma")


def list_audio_tracks(media: str) -> list[str]:
    """Audio filenames in <media>/album-audio (the pickable tracks). Sorted."""
    d = os.path.join(media, "album-audio")
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith(AUDIO_EXTS))


def manifest_sources(library_manifest: str) -> dict:
    """Map each source/tape -> [clip paths] from the library manifest, for the
    selection UI. Empty dict if the manifest is absent."""
    out: dict = {}
    if not os.path.exists(library_manifest):
        return out
    with open(library_manifest, newline="") as fh:
        for r in csv.DictReader(fh):
            out.setdefault(r.get("source", "") or "unknown", []).append(r["clip"])
    return out


def write_scoped_manifest(library_manifest: str, clips, out_path: str) -> str:
    """Return a manifest restricted to `clips`. If clips is "all"/empty, the
    library manifest is used as-is; otherwise a subset is written to out_path."""
    if clips == "all" or not clips:
        return library_manifest
    keep = {os.path.abspath(c) for c in clips}
    with open(library_manifest, newline="") as fh:
        r = csv.DictReader(fh)
        rows = [row for row in r if os.path.abspath(row["clip"]) in keep]
        fields = r.fieldnames
    if not rows:
        raise StageError("This project's clip selection matched no library clips.")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return out_path


def arrange_project(project: Project, media: str) -> Iterator[ProgressEvent]:
    """Arrange a project: scoped manifest + the shared analysis -> the project's
    own folder (tag="" so a project owns exactly one current arrangement)."""
    stem = os.path.splitext(project.track)[0]
    analysis = os.path.join(media, "catalog_audio", f"{stem}.analysis.json")
    library = os.path.join(media, "catalog", "manifest.csv")
    manifest = write_scoped_manifest(
        library, project.clips, os.path.join(project.dir, "manifest-scope.csv"))
    # tag by grid so trying different sync schemes accumulates side-by-side
    # variants in the project folder (render-<track>-<grid>.sh / cut-…-<grid>.mp4)
    # to compare, rather than overwriting one cut.
    yield from arrange(analysis, manifest, out_dir=project.dir, tag=project.grid,
                       **project.arrange_opts())


def render_project(project: Project) -> Iterator[ProgressEvent]:
    """Render the project's current grid's arrangement (script in the project)."""
    stem = os.path.splitext(project.track)[0]
    cand = os.path.join(project.dir, f"render-{stem}{_tag_suffix(project.grid)}.sh")
    sh = cand if os.path.exists(cand) else (
        find_render_script(project.dir, project.track) or cand)
    yield from render(sh)


# ── smoke test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Minimal proof the core works: run the analyze stage on a track and print
    # the ProgressEvents. Usage: python3 engine.py "/path/to/02 Erased.mp3" [out_dir]
    audio = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        MEDIA, "album-audio", "02 Erased.mp3")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(MEDIA, "catalog_audio")
    print(f"engine smoke: analyze {audio!r} → {out!r}\n")
    final = run_stage(analyze(audio, out, plot=False),
                      on_progress=lambda e: print(f"  [{e.stage}] "
                          f"{'' if e.frac is None else f'{e.frac*100:4.0f}% '}{e.message}"))
    print("\nresult:", final)
