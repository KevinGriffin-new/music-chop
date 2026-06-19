# dv2mv

Turn a pile of DV footage into a music video synced to a track. One headless
core (`engine.py`) wraps a set of proven pipeline scripts; two thin front ends
(a FastAPI web UI and a classic-look Tkinter app) sit on top of it.

```
  web (FastAPI + <video>, SSE)  ─┐
                                 ├─▶  engine.py  ─▶  pipeline/ scripts + ffmpeg
  Tkinter (classic, threads)   ─┘
```

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

It defaults to the current working directory if unset.

## The six stages

| stage   | engine fn   | wraps                  | output |
|---------|-------------|------------------------|--------|
| ingest  | `ingest()`  | ffmpeg transcode       | normalized source mp4s |
| detect  | `detect()`  | scenedetect            | `clips/*-Scene-*.mp4` |
| catalog | `catalog()` | `clip_features.py`     | `manifest.csv`, `histograms.npz`, `thumbs/` |
| analyze | `analyze()` | `track_analyze.py`     | `<track>.analysis.json` (+ `.png`) |
| arrange | `arrange()` | `sync_clips.py`        | `order-sync-*.csv`, labels, markers, `render-*.sh` |
| render  | `render()`  | the generated `render-*.sh` | `cut-<track>.mp4` |

Each stage is a generator yielding `ProgressEvent(stage, message, frac, done,
result)` and raises `StageError` on failure — no printing, no globals. Stages
verify their declared outputs exist before reporting `done`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# system binaries on PATH: ffmpeg, ffprobe, scenedetect
```

## Run

```bash
export DV2MV_MEDIA=/path/to/your/media
python3 engine.py "album-audio/02 Erased.mp3" "$DV2MV_MEDIA/catalog_audio"  # smoke-test the core
uvicorn webapp:app --reload    # web tier  → localhost:8000
python3 tkapp.py               # offline tier
```

## Test

```bash
python3 -m pytest tests        # ≈4 s; integration tests self-skip if a tool is missing
```
