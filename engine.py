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
import shutil
import signal
import subprocess
import sys
import threading
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


# ── config (remembers the chosen media library across launches) ─────────────
def config_path() -> str:
    """Path to the dv2mv config file (XDG-style; honors XDG_CONFIG_HOME)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "dv2mv", "config.json")


def load_config() -> dict:
    try:
        with open(config_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> str:
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=1)
    return path


def _initial_media() -> tuple:
    """Resolve the media root at import. Precedence: DV2MV_MEDIA env var (for
    CI/scripts) > the remembered choice in the config > the cwd (which the
    media-root guard then catches if it's the checkout). Returns (path, source)."""
    env = os.environ.get("DV2MV_MEDIA")
    if env:
        return os.path.abspath(env), "env"
    saved = load_config().get("media")
    if saved and os.path.isdir(saved):
        return os.path.abspath(saved), "config"
    return os.path.abspath(os.getcwd()), "cwd"


MEDIA, MEDIA_SOURCE = _initial_media()
# Was DV2MV_MEDIA actually set, or did we fall back? The guard uses this to tell
# "you forgot to set it" from "you set it to the repo".
MEDIA_FROM_ENV = MEDIA_SOURCE == "env"


def set_media(path: str, persist: bool = True) -> str:
    """Point the engine at a new media library at runtime (the in-app picker).

    Validates the folder (must exist and not be the code checkout), updates the
    module-level MEDIA, and — unless persist=False — remembers it in the config
    so the next launch starts there. Returns the resolved path; raises StageError
    on a bad choice so the UIs can surface it."""
    global MEDIA, MEDIA_SOURCE
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(p):
        raise StageError(f"Not a folder: {p}")
    check_media_root(p)                      # refuse the code checkout
    MEDIA = p
    MEDIA_SOURCE = "runtime"
    if persist:
        cfg = load_config()
        cfg["media"] = p
        save_config(cfg)
    return p

SCRIPT = {
    "features": os.path.join(PIPELINE, "clip_features.py"),
    "analyze":  os.path.join(PIPELINE, "track_analyze.py"),
    "sync":     os.path.join(PIPELINE, "sync_clips.py"),
    "export":   os.path.join(PIPELINE, "export_timeline.py"),
}

# ── frozen-app dispatch ─────────────────────────────────────────────────────
# When packaged into a single-file macOS .app (PyInstaller), sys.executable is
# the app bootloader, NOT a Python interpreter — so the source-mode pattern of
# re-invoking `[sys.executable, some_script.py]` would relaunch the GUI instead
# of running a stage. The packaging/entry.py dispatcher recognizes the
# --run-pipeline / --run-scenedetect flags below and runs the right code in a
# fresh process (preserving the subprocess isolation that cancellation relies
# on). These helpers emit the correct argv for both frozen and source runs;
# everything is a no-op when running from source.
_FROZEN = getattr(sys, "frozen", False)


def _pipeline_cmd(name: str, *args: str) -> list:
    """argv to run pipeline script `name` (a key in SCRIPT). Frozen: route
    through the app dispatcher; source: run the .py with this interpreter."""
    if _FROZEN:
        return [sys.executable, "--run-pipeline", name, *args]
    return [sys.executable, SCRIPT[name], *args]


def _scenedetect_cmd(*args: str) -> list:
    """argv for the PySceneDetect CLI. Frozen: through the dispatcher, since
    `scenedetect` is a Python console-script (not a bundled native binary);
    source: the bare command resolved on PATH."""
    if _FROZEN:
        return [sys.executable, "--run-scenedetect", *args]
    return ["scenedetect", *args]

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".avi", ".dv")

# one-line help for each arrange grid (shared by both UIs so they stay in sync)
GRID_HELP = {
    "sections":  "one cut per song section — calmest, longest takes",
    "downbeats": "one cut per bar — driving, lands on the beat",
    "beats":     "one cut every N beats (beats-per-cut) — fast montage",
    "harmonic":  "one cut at each chord / harmony change",
}

# the grids compare() sweeps by default (insertion order = display order)
DEFAULT_GRIDS = tuple(GRID_HELP)

# the match strategies compare() sweeps by default (energy yardstick + the two
# anti-fatigue presets); mirrors sync_clips.MATCH_PRESETS keys.
DEFAULT_MATCHES = ("energy", "contrast", "variety")


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


class Cancelled(RuntimeError):
    """A stage was cancelled on request — a clean stop, NOT a failure.

    Kept distinct from StageError so the front ends can tell "the user pressed
    Cancel" (reset the bar, log it) apart from "the stage actually broke" (show
    an error dialog). A cancel token is just a threading.Event: pass one into a
    stage and call .set() from another thread (the UI) to stop it.
    """


# A cancel token is duck-typed as "anything with is_set()/wait()" — i.e. a
# threading.Event. None means "not cancellable" (the historical behavior).
CancelToken = Optional[threading.Event]

ProgressCallback = Callable[[ProgressEvent], None]


def _check_cancel(cancel: CancelToken, stage: str) -> None:
    """Raise Cancelled if the token is set. Call between subprocess invocations
    (e.g. per-file loops) so a multi-step stage stops promptly rather than
    launching the next subprocess after a cancel."""
    if cancel is not None and cancel.is_set():
        raise Cancelled(f"{stage}: cancelled")


# ── media-root guard (the classic "DV2MV_MEDIA unset" footgun) ──────────────
def looks_like_code_checkout(path: str) -> bool:
    """True if `path` is a dv2mv source tree (has engine.py + pipeline/) — i.e.
    almost certainly NOT where media should live."""
    return (os.path.isfile(os.path.join(path, "engine.py"))
            and os.path.isdir(os.path.join(path, "pipeline")))


def check_media_root(media: Optional[str] = None) -> None:
    """Guard the classic DV2MV_MEDIA footgun. Media (footage, audio, render/
    catalog outputs) lives OUTSIDE the repo; if the media root resolved to the
    code checkout, every path points into the source tree — analyze fails with a
    confusing "No such file" and stray catalog/ dirs get written into git.

    Raise an actionable StageError instead of limping on. The front ends call
    this at startup so the message names the real cause, not a downstream symptom.
    """
    media = media if media is not None else MEDIA
    if not looks_like_code_checkout(media):
        return
    how = ("DV2MV_MEDIA is unset, so the media root defaulted to the current "
           "directory") if not MEDIA_FROM_ENV else \
          "DV2MV_MEDIA points at the code checkout"
    raise StageError(
        f"{how}:\n    {media}\n"
        "But media (footage, audio, render/catalog outputs) must live OUTSIDE "
        "the repo. Set it to your media folder — pick one in the app's Media "
        "Library control, or set the env and rerun, e.g.:\n"
        "    export DV2MV_MEDIA=/Volumes/Footage/musicvideo")


# ── subprocess plumbing shared by every stage ──────────────────────────────
def _terminate(proc: subprocess.Popen) -> None:
    """Stop a running subprocess (and its children) promptly.

    On POSIX the child is its own session leader (start_new_session below), so
    we signal the whole process *group* — otherwise cancelling `render` would
    kill the `bash` wrapper but leave its `ffmpeg` child encoding for minutes.
    SIGTERM first for a clean exit, SIGKILL if it ignores us. Windows has no
    process groups here, so fall back to terminate()/kill() on the child.
    """
    try:
        if os.name == "posix":
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except (ProcessLookupError, OSError):
        pass  # already gone


def _cancel_watch(proc: subprocess.Popen, cancel: threading.Event) -> None:
    """Watch a cancel token while `proc` runs; terminate it the moment it fires.

    Runs on a daemon thread so cancellation is prompt even when the subprocess
    is silent (e.g. ffmpeg mid-encode), where polling the output stream alone
    would block. Exits on its own when the process ends naturally."""
    while proc.poll() is None:
        if cancel.wait(0.2):            # set within the timeout → cancel now
            _terminate(proc)
            return


def _stream(cmd: list[str], cwd: str, cancel: CancelToken = None) -> Iterator[str]:
    """Run cmd, yielding combined stdout/stderr lines as they appear.

    Raises StageError(non-zero exit) with the tail of output for context. If a
    `cancel` token is given and fires, the subprocess (and its process group on
    POSIX) is terminated and Cancelled is raised instead. The finally-clause
    also reaps the process if the consumer abandons the generator (e.g. an SSE
    client disconnects), so a stage never leaks a running ffmpeg.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        start_new_session=(os.name == "posix"),   # own group → killable as a tree
    )
    watcher = None
    if cancel is not None:
        watcher = threading.Thread(target=_cancel_watch, args=(proc, cancel),
                                   daemon=True)
        watcher.start()
    tail: list[str] = []
    code = None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail = (tail + [line])[-25:]
            yield line
        code = proc.wait()
    finally:
        if proc.poll() is None:         # consumer bailed (GeneratorExit) — don't leak
            _terminate(proc)
        if watcher is not None:
            watcher.join(timeout=1)
    if cancel is not None and cancel.is_set():
        raise Cancelled(f"{os.path.basename(cmd[0])}: cancelled")
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
    cancel: CancelToken = None,
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
        _check_cancel(cancel, st)
        yield ProgressEvent(st, f"normalizing — {os.path.basename(f)}", i / total)
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", f]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
                "-c:a", "aac", out]
        tail = []
        for line in _stream(cmd, cwd=MEDIA, cancel=cancel):
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
    cancel: CancelToken = None,
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
        _check_cancel(cancel, st)
        base = os.path.basename(f)
        yield ProgressEvent(st, f"detecting scenes — {base}", i / total)
        cmd = _scenedetect_cmd(
            "-i", f, "-o", out_dir,
            "detect-content", "--threshold", str(threshold),
            "--min-scene-len", min_scene_len,
            "split-video", "--rate-factor", str(rate_factor), "--preset", preset,
        )
        for _ in _stream(cmd, cwd=MEDIA, cancel=cancel):
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
    append: bool = False,
    cancel: CancelToken = None,
) -> Iterator[ProgressEvent]:
    """Extract features → out_dir/manifest.csv (+ histograms.npz, thumbs/).

    With append=True the catalog is incremental: only clips not already in the
    manifest are feature-extracted and appended (used by "Add footage" so it
    doesn't re-process the whole library each time). Default re-catalogs the
    whole pile, overwriting the manifest.

    TODO(claude-code): to drop the subprocess, call clip_features.analyze()
    per file directly and write the manifest here. Not required — this works.
    """
    st = "catalog"
    os.makedirs(out_dir, exist_ok=True)
    cmd = _pipeline_cmd("features", clips_dir, "-o", out_dir,
                        "--frames", str(frames), "--width", str(width))
    if recursive:
        cmd.append("--recursive")
    if append:
        cmd.append("--append")
    tail: list[str] = []
    for line in _stream(cmd, cwd=MEDIA, cancel=cancel):
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
    cancel: CancelToken = None,
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

    cmd = _pipeline_cmd("analyze", audio, "-o", out_dir, "--sr", str(sr))
    if plot:
        cmd.append("--plot")
    yield ProgressEvent(st, f"analyzing {track} …", None)
    tail = []
    for line in _stream(cmd, cwd=MEDIA, cancel=cancel):
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


# ── retempo (time-stretch a track to a target BPM, pitch-preserved) ─────────
def _atempo_chain(factor: float) -> list:
    """Split a tempo factor into ffmpeg atempo stages, each within its 0.5..2.0
    range (atempo rejects values outside it, so large stretches must chain)."""
    stages, f = [], factor
    while f > 2.0:
        stages.append(2.0); f /= 2.0
    while f < 0.5:
        stages.append(0.5); f /= 0.5
    stages.append(f)
    return stages


def retempo(
    audio: str,
    target_bpm: float,
    source_bpm: float,
    out_dir: Optional[str] = None,
    cancel: CancelToken = None,
) -> Iterator[ProgressEvent]:
    """Time-stretch a track to target_bpm, preserving pitch → <track>-<bpm>bpm.wav.

    Uses Rubber Band's R3 engine when the `rubberband` CLI is installed (best
    quality on vocals/transients), else falls back to ffmpeg's `atempo` (always
    available; auto-chained so each stage stays in range). `source_bpm` is the
    track's detected tempo (from analyze); the stretch factor is target/source.
    The output is a normal track you then Analyze + Arrange.
    """
    st = "retempo"
    if not os.path.exists(audio):
        raise StageError(f"No such track: {audio}")
    source_bpm, target_bpm = float(source_bpm), float(target_bpm)
    if source_bpm <= 0:
        raise StageError("Source tempo unknown — Analyze the track first.")
    factor = target_bpm / source_bpm
    if abs(factor - 1.0) < 1e-3:
        raise StageError("Target tempo matches the source — nothing to stretch.")
    out_dir = os.path.abspath(out_dir) if out_dir else os.path.dirname(os.path.abspath(audio))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(audio))[0]
    out = os.path.join(out_dir, f"{stem}-{round(target_bpm)}bpm.wav")
    use_rb = shutil.which("rubberband") is not None
    eng = "Rubber Band R3" if use_rb else "ffmpeg atempo"
    yield ProgressEvent(st, f"stretching {stem}: {source_bpm:.0f} → {round(target_bpm)} "
                            f"BPM (×{factor:.3f}, {eng}) …", None)
    tail: list = []
    if use_rb:
        # rubberband reads PCM (libsndfile), not mp3 — decode to a temp wav first.
        tmp = os.path.join(out_dir, f".{stem}.retempo-src.wav")
        for _ in _stream(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                          "-i", audio, "-ar", "44100", tmp], cwd=out_dir, cancel=cancel):
            pass
        cmd = ["rubberband", "-3", "--tempo", f"{source_bpm}:{target_bpm}", tmp, out]
        for line in _stream(cmd, cwd=out_dir, cancel=cancel):
            tail = (tail + [line])[-25:]
        try:
            os.remove(tmp)
        except OSError:
            pass
    else:
        af = ",".join(f"atempo={a:.6f}" for a in _atempo_chain(factor))
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", audio,
               "-filter:a", af, "-ar", "44100", out]
        for line in _stream(cmd, cwd=out_dir, cancel=cancel):
            tail = (tail + [line])[-25:]
    _require(st, {"out": out}, tail)
    yield ProgressEvent(st, f"Stretched → {os.path.basename(out)}  (pitch preserved)",
                        1.0, True, {"output": out, "factor": round(factor, 4),
                                    "engine": "rubberband" if use_rb else "atempo",
                                    "target_bpm": round(target_bpm)})


# ── stage 4: arrange (sync clips to the track's structure) ─────────────────
def arrange_tag(grid: str, match: str = "energy") -> str:
    """The tag identifying an arrangement = grid + match strategy. Both go in the
    output names (render-<track>-<grid>-<match>.sh, cut-…, .arrange.json) so a
    different match on the same grid is its own cut, not an overwrite."""
    return f"{grid}-{match}"


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
    match: str = "energy",             # energy | contrast (clip↔slot weighting)
    tag: Optional[str] = None,         # output suffix; defaults to the grid name
    out_dir: Optional[str] = None,     # where sidecars land; default = analysis dir
    cut_dir: Optional[str] = None,     # where the final cut-*.mp4 lands; default = out_dir
    cancel: CancelToken = None,
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
        tag = arrange_tag(grid, match)
    cmd = _pipeline_cmd("sync", "--analysis", analysis_json,
                        "--manifest", manifest, "--grid", grid,
                        "--beats-per-cut", str(beats_per_cut),
                        "--drop-blurry", str(drop_blurry), "--clip-from", clip_from,
                        "--match", match, "--tag", tag)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        cmd += ["--out", out_dir]
    if cut_dir:
        cmd += ["--cut-dir", cut_dir]
    if allow_reuse:
        cmd.append("--allow-reuse")
    yield ProgressEvent(st, f"syncing clips on the {grid} grid …", None)
    match, tail = None, []
    for line in _stream(cmd, cwd=MEDIA, cancel=cancel):
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


# ── compare: arrange across grids, rank by energy match ─────────────────────
def compare(
    analysis_json: str,
    manifest: str,
    grids: tuple = DEFAULT_GRIDS,
    matches: tuple = DEFAULT_MATCHES,
    beats_per_cut: int = 4,
    allow_reuse: bool = False,
    drop_blurry: float = 0.0,
    clip_from: str = "middle",
    out_dir: Optional[str] = None,
    cut_dir: Optional[str] = None,
    cancel: CancelToken = None,
) -> Iterator[ProgressEvent]:
    """Arrange a track across grid × match-strategy and tabulate the trade-offs.

    Every (grid, match) cell is arranged in turn, tagged "<grid>-<match>" so all
    candidate cuts survive to render/export, and its stats are collected into a
    side-by-side table: the motion-only energy_match_pct is the common yardstick
    (comparable across strategies), with luma_contrast / hue_variety showing what
    the contrast/variety strategies buy. Ranked best-energy-first; a cell that
    fails becomes an error row rather than aborting the sweep.
    """
    st = "compare"
    if not os.path.exists(analysis_json):
        track = os.path.splitext(os.path.basename(analysis_json))[0].replace(".analysis", "")
        raise StageError(f"No analysis for '{track}' yet — run Analyze first.")
    if not os.path.exists(manifest):
        raise StageError("No clip catalog yet — add footage before comparing.")

    rows: list[dict] = []
    combos = [(g, m) for g in grids for m in matches]
    total = len(combos)
    for i, (g, m) in enumerate(combos):
        _check_cancel(cancel, st)
        yield ProgressEvent(st, f"arranging {g} · {m} …", i / total)
        tag = f"{g}-{m}"
        try:
            final: dict = {}
            for ev in arrange(analysis_json, manifest, grid=g, match=m, tag=tag,
                              beats_per_cut=beats_per_cut, allow_reuse=allow_reuse,
                              drop_blurry=drop_blurry, clip_from=clip_from,
                              out_dir=out_dir, cut_dir=cut_dir, cancel=cancel):
                if ev.done:
                    final = ev.result
            s = final.get("summary") or {}
            rows.append({"grid": g, "match": m, "tag": tag,
                         "energy_match_pct": s.get("energy_match_pct"),
                         "luma_contrast": s.get("luma_contrast"),
                         "hue_variety": s.get("hue_variety"),
                         "cuts": s.get("cuts"), "clips": s.get("clips"),
                         "render_sh": final.get("render_sh"),
                         "options": final.get("options")})
        except StageError as exc:
            rows.append({"grid": g, "match": m, "tag": tag,
                         "energy_match_pct": None, "luma_contrast": None,
                         "hue_variety": None, "cuts": None, "clips": None,
                         "error": str(exc)})

    # rank best-energy-first; errored rows (None) sort to the bottom
    ranked = sorted(rows, key=lambda r: (r["energy_match_pct"] is not None,
                                         r["energy_match_pct"] or 0), reverse=True)
    top = ranked[0] if ranked and ranked[0]["energy_match_pct"] is not None else None
    best = top["tag"] if top else None              # a tag suffix Render/Export reuse
    msg = (f"Best energy fit: {top['grid']} · {top['match']} "
           f"({top['energy_match_pct']}%)" if top
           else "Compared (no successful arrangement).")
    yield ProgressEvent(st, msg, 1.0, True,
                        {"comparison": rows, "ranked": ranked, "best": best})


# ── stage 5: render (cut the clips to the grid + lay the music on top) ──────
def render(render_sh: str, cancel: CancelToken = None) -> Iterator[ProgressEvent]:
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
    for line in _stream(["bash", render_sh], cwd=cwd, cancel=cancel):
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


# ── stage 6: export (emit an editable timeline for Resolve finishing) ───────
def find_arrange_json(out_dir: str, track: str,
                      grid: Optional[str] = None) -> Optional[str]:
    """Resolve a track to the arrange.json arrange() wrote (newest, or a specific
    grid's). The export stage reads it; mirrors find_render_script()."""
    stem = os.path.splitext(os.path.basename(track))[0]
    if grid:
        cand = os.path.join(out_dir, f"{stem}{_tag_suffix(grid)}.arrange.json")
        if os.path.exists(cand):
            return cand
    cands = glob(os.path.join(out_dir, f"{stem}*.arrange.json"))
    return max(cands, key=os.path.getmtime) if cands else None


def export(
    arrange_json: str,
    out_dir: Optional[str] = None,
    formats: tuple = ("otio", "fcpxml"),
    cancel: CancelToken = None,
) -> Iterator[ProgressEvent]:
    """Emit an editable timeline (OTIO / FCPXML) from an arrangement.

    dv2mv decides the cut; Resolve finishes it. This reads the arrange.json (and
    its order CSV) and writes a timeline Resolve imports — an alternative to (or
    companion of) the ffmpeg render. Fast: no transcode, just the interchange
    files. Outputs land next to the arrangement unless out_dir is given.
    """
    st = "export"
    if not os.path.exists(arrange_json):
        raise StageError(
            "No arrangement to export yet — run Arrange on the track first "
            f"(missing {os.path.basename(arrange_json)}).")
    cmd = _pipeline_cmd("export", "--arrange", arrange_json,
                        "--formats", ",".join(formats))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        cmd += ["--out", out_dir]
    yield ProgressEvent(st, "exporting editable timeline …", None)
    wrote, tail = [], []
    for line in _stream(cmd, cwd=MEDIA, cancel=cancel):
        line = line.strip()
        if not line:
            continue
        tail = (tail + [line])[-25:]
        if line.startswith("wrote "):
            wrote.append(line[len("wrote "):].strip())
    missing = [p for p in wrote if not os.path.exists(p)]
    if not wrote or missing:
        why = ("reported writing missing file(s): " + ", ".join(missing)) if missing \
            else "produced no timeline files"
        raise StageError(f"export: {why}\n" + "\n".join(tail))
    result: dict = {"outputs": wrote}
    for p in wrote:                                  # also key by extension (otio/fcpxml)
        result[os.path.splitext(p)[1].lstrip(".")] = p
    yield ProgressEvent(
        st, "Exported timeline → " + ", ".join(os.path.basename(p) for p in wrote),
        1.0, True, result)


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
    match: str = "energy"               # energy | contrast (clip↔slot weighting)
    dir: str = ""                       # project folder (holds project.json + outputs)

    def arrange_opts(self) -> dict:
        return {"grid": self.grid, "beats_per_cut": self.beats_per_cut,
                "allow_reuse": self.allow_reuse, "drop_blurry": self.drop_blurry,
                "clip_from": self.clip_from, "match": self.match}

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in
             ("name", "track", "clips", "grid", "beats_per_cut",
              "allow_reuse", "drop_blurry", "clip_from", "match")}
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
                   ("grid", "beats_per_cut", "allow_reuse", "drop_blurry",
                    "clip_from", "match")})
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


def all_catalog_clips(library_manifest: str) -> list:
    """Flat sorted list of every clip path in the library manifest (empty if
    absent). Used to expand a project's "all" selection for add/remove math."""
    if not os.path.exists(library_manifest):
        return []
    with open(library_manifest, newline="") as fh:
        return sorted(r["clip"] for r in csv.DictReader(fh) if r.get("clip"))


def revise_clip_selection(current, selection, op, library_clips=None):
    """Compute a project's new clip set from a gallery `selection` and an `op`:

      replace  -> the selection itself
      add      -> current ∪ selection
      remove   -> current − selection

    `current` is a list of clip paths or "all" (the whole library); add/remove
    expand "all" via `library_clips`. Returns a sorted list — or "all" when the
    result spans the entire library (kept simple, and so a project tracks new
    footage). Paths are compared by absolute path so picker/manifest forms match.
    """
    sel = {os.path.abspath(c) for c in selection}
    lib = {os.path.abspath(c) for c in (library_clips or [])}
    if op == "replace":
        result = set(sel)
    else:
        base = lib if current == "all" else {os.path.abspath(c) for c in current}
        if op == "add":
            result = base | sel
        elif op == "remove":
            result = base - sel
        else:
            raise StageError(f"unknown selection op: {op!r}")
    if lib and result == lib:
        return "all"
    return sorted(result)


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


def arrange_project(project: Project, media: str,
                    cancel: CancelToken = None) -> Iterator[ProgressEvent]:
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
    yield from arrange(analysis, manifest, out_dir=project.dir,
                       tag=arrange_tag(project.grid, project.match),
                       cancel=cancel, **project.arrange_opts())


def compare_project(project: Project, media: str, grids: tuple = DEFAULT_GRIDS,
                    matches: tuple = DEFAULT_MATCHES,
                    cancel: CancelToken = None) -> Iterator[ProgressEvent]:
    """Compare a project across grid × match: the project's scoped clips +
    non-grid options, swept, accumulating each variant in the project folder."""
    stem = os.path.splitext(project.track)[0]
    analysis = os.path.join(media, "catalog_audio", f"{stem}.analysis.json")
    library = os.path.join(media, "catalog", "manifest.csv")
    manifest = write_scoped_manifest(
        library, project.clips, os.path.join(project.dir, "manifest-scope.csv"))
    yield from compare(analysis, manifest, grids=grids, matches=matches,
                       out_dir=project.dir, beats_per_cut=project.beats_per_cut,
                       allow_reuse=project.allow_reuse, drop_blurry=project.drop_blurry,
                       clip_from=project.clip_from, cancel=cancel)


def render_project(project: Project,
                   cancel: CancelToken = None) -> Iterator[ProgressEvent]:
    """Render the project's current grid's arrangement (script in the project)."""
    stem = os.path.splitext(project.track)[0]
    tag = arrange_tag(project.grid, project.match)
    cand = os.path.join(project.dir, f"render-{stem}{_tag_suffix(tag)}.sh")
    sh = cand if os.path.exists(cand) else (
        find_render_script(project.dir, project.track) or cand)
    yield from render(sh, cancel=cancel)


def export_project(project: Project, media: str, formats: tuple = ("otio", "fcpxml"),
                   cancel: CancelToken = None) -> Iterator[ProgressEvent]:
    """Export the project's current grid's arrangement to an editable timeline
    (into the project folder, next to its render/cut)."""
    stem = os.path.splitext(project.track)[0]
    tag = arrange_tag(project.grid, project.match)
    cand = os.path.join(project.dir, f"{stem}{_tag_suffix(tag)}.arrange.json")
    arr = cand if os.path.exists(cand) else (
        find_arrange_json(project.dir, project.track) or cand)
    yield from export(arr, out_dir=project.dir, formats=formats, cancel=cancel)


# ── smoke test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Minimal proof the core works: run the analyze stage on a track and print
    # the ProgressEvents. Usage: python3 engine.py "/path/to/02 Erased.mp3" [out_dir]
    # Fail loud if the media root is the code checkout (DV2MV_MEDIA unset).
    try:
        check_media_root()
    except StageError as exc:
        sys.exit(str(exc))
    audio = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        MEDIA, "album-audio", "02 Erased.mp3")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(MEDIA, "catalog_audio")
    print(f"engine smoke: analyze {audio!r} → {out!r}\n")
    final = run_stage(analyze(audio, out, plot=False),
                      on_progress=lambda e: print(f"  [{e.stage}] "
                          f"{'' if e.frac is None else f'{e.frac*100:4.0f}% '}{e.message}"))
    print("\nresult:", final)
