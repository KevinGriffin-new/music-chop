# dv2mv

Turn a pile of DV footage into a music video synced to a track. One headless
core (`engine.py`) wraps a set of proven pipeline scripts; two thin front ends
(a FastAPI web UI and a classic-look Tkinter app) sit on top of it.

```
  web (FastAPI + <video>, SSE)  ─┐
                                 ├─▶  engine.py  ─▶  pipeline/ scripts + ffmpeg
  Tkinter (classic, threads)   ─┘
```

**Bugs / issues:** https://todo.sr.ht/~kevin_griffin/music-chop-dv2mv — see
[REPORTING.md](REPORTING.md) for what to include (for Tk bugs, the launch
terminal's traceback is usually the whole answer). Planned features live in
[ROADMAP.md](ROADMAP.md) (kept separate from the bug tracker).

## Layout

```
dv2mv/
  engine.py        headless core — six stages, ProgressEvent, StageError
  webapp.py        FastAPI + SSE front end (stub, being fleshed out)
  tkapp.py         Tkinter classic-look front end (stub)
  pipeline/        vendored pipeline scripts the engine shells out to
    clip_features.py  track_analyze.py  sync_clips.py  clip_order.py  clip_gallery.py
  tests/           pytest per stage (+ media fixtures)
  _smoke/          committed synthetic fixtures (no real media)
  requirements.txt
  HANDOFF.md       design notes / build order
```

**Code lives here; media lives elsewhere.** Footage, audio, and render outputs
are *not* in this repo. Point the engine at them with `DV2MV_MEDIA`:

```bash
export DV2MV_MEDIA=/Volumes/Footage/musicvideo
```

You don't have to use the env var: both front ends have a **Media library**
control (Tk: a folder picker; web: a path field) to choose the folder at
runtime, and the choice is **remembered** across launches in
`~/.config/dv2mv/config.json`. Resolution precedence is **`DV2MV_MEDIA` env >
saved choice > current directory**.

If none of those is set and the directory resolves to the code checkout itself,
the app **won't silently proceed** — the Tk app prompts for a library, the web
tier returns an actionable error, and the CLI exits — since treating the repo as
the media root writes outputs into the source tree and makes `analyze` fail with
a confusing "No such file".

## The stages

| stage   | engine fn   | wraps                  | output |
|---------|-------------|------------------------|--------|
| ingest  | `ingest()`  | ffmpeg transcode       | normalized source mp4s |
| detect  | `detect()`  | scenedetect            | `clips/*-Scene-*.mp4` |
| catalog | `catalog()` | `clip_features.py`     | `manifest.csv`, `histograms.npz`, `thumbs/` |
| analyze | `analyze()` | `track_analyze.py`     | `<track>.analysis.json` (+ `.png`) |
| retempo | `retempo()` | Rubber Band R3 / ffmpeg `atempo` | `<track>-<bpm>bpm.wav` (pitch-preserved) |
| arrange | `arrange()` | `sync_clips.py`        | `order-sync-*.csv`, labels, markers, `render-*.sh` |
| render  | `render()`  | the generated `render-*.sh` | `cut-<track>.mp4` |
| export  | `export()`  | `export_timeline.py`   | `<track>.otio`, `<track>.fcpxml` |

`render` and `export` are alternatives on the same arrangement: `render` bakes a
finished mp4; `export` emits an editable timeline (OpenTimelineIO + FCP X XML)
to hand the cut to DaVinci Resolve for finishing (color, audio, transitions,
delivery). dv2mv decides the *cut*; Resolve *finishes* it.

**Compare grids** (`compare()`) arranges the track on every grid and ranks them
by energy match, so you can pick the scheme that best fits — each candidate's
sidecars are left on disk, so the winner is ready to render or export. Both UIs
have a "Compare grids" button; the winning grid is preselected afterward.

**Retempo** (`retempo()`) time-stretches a track to a target BPM while
preserving pitch, writing `<track>-<bpm>bpm.wav` as a new track you then Analyze
+ Arrange — handy for matching a reference's drive (a downbeats cut at a higher
BPM cuts faster) without the chipmunk vocals of a plain speed-up. It uses Rubber
Band's R3 engine when the `rubberband` CLI is installed (best on vocals) and
falls back to ffmpeg `atempo` otherwise. The Tk app exposes it as a **Tempo…**
slider, centered on the track's detected tempo (needs Analyze first).

**Add footage is incremental** — `detect` skips sources already split, and
`catalog(append=True)` feature-extracts only the clips not already in the
manifest and appends them (carrying the existing rows + histograms forward), so
adding a tape doesn't re-process the whole library. A full rebuild is
`catalog()` with `append=False`.

Each stage is a generator yielding `ProgressEvent(stage, message, frac, done,
result)` and raises `StageError` on failure — no printing, no globals. Stages
verify their declared outputs exist before reporting `done`.

**Cancelling a stage.** Every stage accepts an optional `cancel` token (a
`threading.Event`); set it from another thread to stop a long render/analyze.
The engine terminates the running subprocess *and its process group*, so a
render's `ffmpeg` child is killed too, then raises `Cancelled` (distinct from
`StageError` — a clean stop, not a failure). The web tier exposes this as a job
registry + `GET /api/cancel?job=<id>` behind the **Cancel** button; the Tk tier
has a **Cancel** button that sets the worker's token.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# system binaries on PATH: ffmpeg, ffprobe, scenedetect
# optional: rubberband  (R3 time-stretch for Retempo; falls back to ffmpeg atempo)
#   macOS: brew install rubberband
```

## Run

```bash
export DV2MV_MEDIA=/path/to/your/media
python3 engine.py "album-audio/02 Erased.mp3" "$DV2MV_MEDIA/catalog_audio"  # smoke-test the core
uvicorn webapp:app --reload    # web tier  → localhost:8000
python3 tkapp.py               # offline tier (IRIX/4Dwm look)
```

The Tk app's options dialog uses the SGI screen font for its IRIX look. It's
vendored (CC0) in `assets/fonts/`; install it for the real look, else it falls
back to a mono family:

```bash
cp assets/fonts/IrixScreenMono15*.ttf ~/Library/Fonts/    # macOS
```

With a project open, the gallery edits its footage by **add / remove / replace**
(the project's current clips come up preselected): the checked thumbnails are
added to, removed from, or replace the project's selection — rather than only
wholesale-replacing it. The web gallery offers the same via a project dropdown.

The Tk gallery opens a clicked clip in the OS default app. If that's not a
player (e.g. a tag editor owns `.mp4`), force one with `DV2MV_PLAYER`:

```bash
DV2MV_MEDIA=/path/to/media DV2MV_PLAYER=VLC python3 tkapp.py   # macOS: open -a VLC
```

## Test

```bash
python3 -m pytest tests        # ≈9 s; integration tests self-skip if a tool is missing
```

There's also an **opt-in** real-window GUI smoke for the Tk app (driven by
pyautogui — it moves the mouse and needs macOS Accessibility permission for your
terminal):

```bash
pip install pyautogui
DV2MV_MEDIA=/path/to/media python3 tests/gui_smoke.py   # opens the app, clicks through New project
```

## License

[Mozilla Public License 2.0](LICENSE) (file-level copyleft — you can embed the
engine in larger works, but changes to MPL-covered files stay open). Source
files carry an `SPDX-License-Identifier: MPL-2.0` header.

The vendored SGI screen font in `assets/fonts/` is a separate work under
**CC0-1.0** (public domain) — see `assets/fonts/license.txt`. System tools it
drives (ffmpeg, scenedetect) keep their own licenses; they're invoked as
separate processes, not linked.
