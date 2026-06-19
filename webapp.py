#!/usr/bin/env python3
"""
webapp.py — STUB: the fast, browser-based front end (web tier).

Thin client over engine.py. Demonstrates the one pattern the whole design hinges
on for the web side: run a stage in a background task and stream its
ProgressEvents to the browser over Server-Sent Events (SSE). The browser shows a
progress bar from `frac`, then drops the finished cut into a <video> element —
the inline-preview win that makes the web tier worth building first.

This is a skeleton: enough to run analyze→arrange→render and preview the result.
Claude Code should flesh out: job ids / a job registry, the ingest + detect +
catalog stages, the gallery view (reuse clip_gallery.html), parameter forms for
the grid/reuse knobs, and error surfacing.

Run:  uvicorn webapp:app --reload      (pip install fastapi uvicorn)
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse

import engine

app = FastAPI(title="dv2mv (web)")

MEDIA = engine.MEDIA          # media root (set DV2MV_MEDIA); not the code repo
CATALOG_AUDIO = os.path.join(MEDIA, "catalog_audio")
MANIFEST = os.path.join(MEDIA, "catalog", "manifest.csv")


def _sse(stage_gen) -> StreamingResponse:
    """Adapter: turn a stage generator into an SSE stream of JSON events."""
    def event_source():
        for ev in stage_gen:
            payload = {"stage": ev.stage, "message": ev.message,
                       "frac": ev.frac, "done": ev.done, "result": ev.result}
            yield f"data: {json.dumps(payload)}\n\n"
    return StreamingResponse(event_source(), media_type="text/event-stream")


# ── stage endpoints (each streams progress) ────────────────────────────────
@app.get("/api/analyze")
def api_analyze(track: str):
    audio = os.path.join(MEDIA, "album-audio", track)
    return _sse(engine.analyze(audio, CATALOG_AUDIO, plot=True))


@app.get("/api/arrange")
def api_arrange(track: str, grid: str = "beats", beats_per_cut: int = 2,
                allow_reuse: bool = True):
    analysis = os.path.join(CATALOG_AUDIO,
                            f"{os.path.splitext(track)[0]}.analysis.json")
    return _sse(engine.arrange(analysis, MANIFEST, grid=grid,
                               beats_per_cut=beats_per_cut, allow_reuse=allow_reuse))


@app.get("/api/render")
def api_render(track: str):
    sh = os.path.join(CATALOG_AUDIO, f"render-{os.path.splitext(track)[0]}.sh")
    return _sse(engine.render(sh))


@app.get("/api/video")
def api_video(track: str):
    mp4 = os.path.join(MEDIA, f"cut-{os.path.splitext(track)[0]}.mp4")
    return FileResponse(mp4, media_type="video/mp4")


# ── the world's smallest front end, to prove the loop ──────────────────────
INDEX = """<!doctype html><meta charset=utf-8><title>dv2mv</title>
<body style="font:14px system-ui;max-width:680px;margin:2rem auto">
<h1>dv2mv — web</h1>
<input id=track value="02 Erased.mp3" style="width:60%">
<button onclick="go('analyze')">Analyze</button>
<button onclick="go('arrange')">Arrange</button>
<button onclick="go('render')">Render</button>
<progress id=bar value=0 max=1 style="width:100%;display:block;margin:1rem 0"></progress>
<pre id=log style="background:#eee;padding:1rem;height:160px;overflow:auto"></pre>
<video id=vid controls style="width:100%;display:none"></video>
<script>
const log = m => document.getElementById('log').textContent += m + "\\n";
function go(stage){
  const track = encodeURIComponent(document.getElementById('track').value);
  const es = new EventSource(`/api/${stage}?track=${track}`);
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.frac != null) document.getElementById('bar').value = ev.frac;
    log(`[${ev.stage}] ${ev.message}`);
    if (ev.done){
      es.close();
      if (stage === 'render'){
        const v = document.getElementById('vid');
        v.src = `/api/video?track=${track}`; v.style.display = 'block';
      }
    }
  };
  es.onerror = () => { log('-- stream error --'); es.close(); };
}
</script>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX
