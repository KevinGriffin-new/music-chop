# dv2mv — handoff for Claude Code

Turn the existing DV-footage → music-video script pipeline into a desktop app.
Two front ends, **one shared engine**: a quick web UI (rich inline preview) and a
browserless Tkinter UI (classic Motif/CDE look) for offline use.

This folder is a **scaffold + working core**, not a finished app. The load-bearing
piece (`engine.py`) runs today; the two UIs are deliberately minimal stubs that
establish the patterns. Your job is to flesh them out and package the result.

## The one idea

Don't build the workflow twice. Everything sits on one headless core:

```
  web (FastAPI + <video>, SSE)  ─┐
                                 ├─▶  engine.py  ─▶  proven scripts + ffmpeg
  Tkinter (classic, threads)   ─┘
```

`engine.py` exposes the six pipeline stages as importable, **progress-yielding**
functions. Each yields `ProgressEvent(stage, message, frac, done, result)` and
raises `StageError` on failure — no printing, no globals, no UI assumptions. That
single contract is what lets the same code drive an SSE stream and a Tk worker
thread.

## The pipeline (vendored scripts in `pipeline/`)

The pipeline scripts now live in `pipeline/` inside this repo (vendored from the
original media folder). Media — footage, audio, render outputs — lives OUTSIDE
the repo; point at it with `DV2MV_MEDIA` (see README). `engine.MEDIA` resolves it.

| stage     | engine fn   | wraps                         | output |
|-----------|-------------|-------------------------------|--------|
| ingest    | `ingest()`  | ffmpeg transcode              | normalized source mp4s |
| detect    | `detect()`  | `scenedetect` (per source)    | `clips/*-Scene-*.mp4` |
| catalog   | `catalog()` | `clip_features.py`            | `catalog/manifest.csv`, `histograms.npz`, `thumbs/` |
| analyze   | `analyze()` | `track_analyze.py`            | `catalog_audio/<track>.analysis.json` (+ `.png`) |
| arrange   | `arrange()` | `sync_clips.py`               | `order-sync-*.csv`, `.labels.txt`, `.markers.csv`, `render-*.sh` |
| render    | `render()`  | the generated `render-*.sh` (ffmpeg) | `cut-<track>.mp4` |

Ingest is the one stage with no script yet — the user works from **already-imported
DV** (mp4 files), so ingest is just an optional transcode-to-common-codec step
(`-c:v libx264 -crf 18`, consistent fps/scale) so the final `-c copy` concat path
stays valid. No Firewire/tape capture — explicitly out of scope.

## Why subprocess, not inlined logic

`engine.py` runs each script as a subprocess and parses its stdout into
`ProgressEvent`s. This is intentional for v1: the scripts are the proven source of
truth, and wrapping them avoids forking the logic. The `clip_features` /
`track_analyze` / `sync_clips` modules are importable, so if you later want to drop
the subprocess hop, the `TODO(claude-code)` comments mark exactly where to inline
the per-item functions. **Not required** — measure first; the subprocess cost is
trivial next to the CV/librosa/ffmpeg work.

## What's here

```
dv2mv/                (project root = git repo)
  engine.py        the headless core — six stages, ProgressEvent, StageError  ✅ runs
  webapp.py        STUB FastAPI + SSE + a 30-line HTML page that proves the loop
  tkapp.py         STUB Tkinter classic-look shell (thread→queue→.after pump)
  pipeline/        vendored scripts the engine shells out to (clip_features.py,
                   track_analyze.py, sync_clips.py, clip_order.py, clip_gallery.py)
  tests/           pytest per stage (test_engine.py + media fixtures)  ✅ 19 pass
  _smoke/          committed synthetic fixtures (no real media)
  requirements.txt deps for both tiers
  README.md        setup / run / test
  HANDOFF.md       this file
```

Verified working: the whole chain (`ingest`→`detect`→`catalog`→`analyze`→
`arrange`→`render`) runs through the core against tiny generated fixtures, and
`analyze`/`arrange` against the real `02 Erased`. Run the suite with
`python3 -m pytest tests` (≈4 s; integration tests self-skip if a tool is
missing).

Step 1 (engine hardening) is **done** — see the checklist below.

## Build order (recommended)

1. **Engine first, harden it.** ✅ DONE:
   - `ingest()` added (libx264/crf18, optional fps/scale normalize, idempotent).
   - Every stage now **fails loud**: it verifies its declared output files
     exist before reporting `done`, raising `StageError` with the captured
     output tail. This caught a real bug — `track_analyze.py` swallows a
     per-track exception and still exits 0, so the old engine reported a result
     dict pointing at a JSON it never wrote. (Regression-guarded by a test.)
   - `render` has real fractional progress: `arrange` writes `[seg/total]`
     markers into the generated `render-*.sh` and `render()` parses them.
   - `analyze()` owns the `tracks_summary.csv` merge (snapshot → run → re-merge
     in track order), so re-analyzing one track no longer drops the others.
   - `arrange` outputs are tag-suffixed (default: the grid name) via a new
     `sync_clips.py --tag`, so comparing grids no longer clobbers sidecars.
   - `pytest` per stage in `tests/` (pure-logic always; integration
     self-skips when ffmpeg/scenedetect/librosa/opencv are absent).
2. **Web tier (do this first — fastest to something playable).** Job registry +
   ids, parameter forms for the grid/reuse knobs, reuse `clip_gallery.py`'s HTML
   for the catalog view, and the `<video>` preview (already stubbed). This is the
   demo that sells the project.
3. **Tk tier (offline).** Same engine, the thread/queue pattern is stubbed. Add
   file pickers, a thumbnail `Canvas` gallery, parameter widgets, cancellation.
   Keep the **classic** widgets — do not switch to `ttk` (that's the modern look
   we're deliberately not using).
4. **Package.** Web tier ships as-is (run from a venv) or behind a Tauri/Electron
   shell. The Tk tier is the one to bundle as a true double-click app, and the
   pain there is the deps, not your code — see below.

## Gotchas worth knowing up front

- **Preview parity is intentional, not a bug.** Web gets inline `<video>`; Tk has
  no embedded video widget, so it shells out to the OS player on render-complete.
  That's the documented trade between the two tiers.
- **Concurrency differs per tier, engine doesn't.** Web runs stages in a FastAPI
  background task and streams SSE; Tk runs them on a worker thread and pumps a
  `queue.Queue` via `.after(100, ...)`. Never touch Tk widgets from the worker.
- **`track_analyze` rewrites `tracks_summary.csv`** for whatever set it's given.
  ✅ Handled: `analyze()` snapshots the existing summary, runs the single track,
  and re-merges the row in track order, so the combined summary survives.
- **`arrange` output names are keyed on track only**, no grid suffix. ✅ Handled:
  `sync_clips.py` now takes `--tag` (sanitized, mirrors `clip_order.py`) and
  `arrange()` defaults the tag to the grid name, so `sections` and `downbeats`
  cuts keep separate `order-sync-<track>-<grid>.csv` / `.labels.txt` /
  `.markers.csv` / `render-*.sh` / `cut-*.mp4`. Pass `tag=""` for the old names.
- **`coverage`/`numba` import trap.** `numba` (0.65.x here) imports
  `coverage.types.Tracer` at load; `coverage` <7.14 exposed it as `TTracer`, so
  an old `coverage` makes `import librosa` blow up — and `track_analyze.py`
  silently swallowed it, which is how the engine looked like it "ran" while
  writing nothing. Fixed by pinning `coverage>=7.14` (see `requirements.txt`).
- **`-c copy` concat needs identical codec/res/params** across clips. True if all
  clips came from one scenedetect split of one encode; the ingest step exists to
  guarantee it when sources are mixed.

## Packaging notes (the real cost of a "double-click" app)

The Tk tier's hard part is bundling `librosa` (→ numba, scipy), `opencv-python`,
and `scikit-learn` with PyInstaller/py2app — these are notoriously fiddly,
hidden-import-heavy, and large. Plus a bundled `ffmpeg`/`scenedetect` binary and,
on macOS, codesigning + notarization. Budget real time here. The web tier sidesteps
all of it by running from a venv, which is another reason to lead with it.

## Dependencies

See `requirements.txt`. System: `ffmpeg`, `ffprobe`, and `scenedetect` on PATH.
Tkinter ships with CPython. The web tier adds `fastapi` + `uvicorn`.

## Quick start (from the project root)

```bash
export DV2MV_MEDIA=/Volumes/Footage/musicvideo            # where the media lives
python3 engine.py "album-audio/02 Erased.mp3" /tmp/out    # smoke-test the core
uvicorn webapp:app --reload                                # web tier  → localhost:8000
python3 tkapp.py                                           # offline tier
python3 -m pytest tests                                    # the suite
```

Paths passed to the engine resolve against `DV2MV_MEDIA` (the scripts run with
that as their working directory).
