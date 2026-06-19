#!/usr/bin/env python3
"""
webapp.py — STUB: the fast, browser-based front end (web tier).

Thin client over engine.py. Demonstrates the one pattern the whole design hinges
on for the web side: run a stage in a background task and stream its
ProgressEvents to the browser over Server-Sent Events (SSE). The browser shows a
progress bar from `frac`, then drops the finished cut into a <video> element —
the inline-preview win that makes the web tier worth building first.

This is a skeleton: enough to upload media, run analyze→arrange→render, and
preview the result. Uploading a track saves it to album-audio/; uploading
footage saves to sources/ then scene-splits + catalogs it. Still to flesh out:
job ids / a job registry, the gallery view (reuse clip_gallery.html), parameter
forms for the grid/reuse knobs, and richer error surfacing.

Run:  uvicorn webapp:app --reload      (pip install fastapi uvicorn python-multipart)
"""
from __future__ import annotations

import json
import os
import shutil
from typing import List

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse

import engine

app = FastAPI(title="dv2mv (web)")

MEDIA = engine.MEDIA          # media root (set DV2MV_MEDIA); not the code repo
CATALOG_AUDIO = os.path.join(MEDIA, "catalog_audio")
MANIFEST = os.path.join(MEDIA, "catalog", "manifest.csv")
ALBUM_AUDIO = os.path.join(MEDIA, "album-audio")
SOURCES = os.path.join(MEDIA, "sources")        # uploaded footage lands here
CLIPS = os.path.join(MEDIA, "clips")
CATALOG = os.path.join(MEDIA, "catalog")


def _save_upload(upload: UploadFile, dest_dir: str, allowed: tuple) -> str:
    """Save an uploaded file into dest_dir under a sanitized basename."""
    name = os.path.basename(upload.filename or "").strip()
    if not name:
        raise HTTPException(400, "missing filename")
    if not name.lower().endswith(allowed):
        raise HTTPException(400, f"unsupported type: {name}")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    with open(dest, "wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return name


def _sse(stage_gen) -> StreamingResponse:
    """Adapter: turn a stage generator into an SSE stream of JSON events."""
    def event_source():
        for ev in stage_gen:
            payload = {"stage": ev.stage, "message": ev.message,
                       "frac": ev.frac, "done": ev.done, "result": ev.result}
            yield f"data: {json.dumps(payload)}\n\n"
    return StreamingResponse(event_source(), media_type="text/event-stream")


# ── upload endpoints (bring new media into the tree) ────────────────────────
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".aif", ".aiff")


@app.post("/api/upload/track")
def upload_track(file: UploadFile = File(...)):
    """Save an uploaded audio file to album-audio/; returns its track name."""
    name = _save_upload(file, ALBUM_AUDIO, AUDIO_EXTS)
    return {"track": name}


@app.post("/api/upload/footage")
def upload_footage(files: List[UploadFile] = File(...)):
    """Save uploaded video files to sources/; returns the saved names."""
    saved = [_save_upload(f, SOURCES, engine.VIDEO_EXTS) for f in files]
    return {"files": saved}


@app.get("/api/footage")
def api_footage():
    """Scene-split everything in sources/, then (re)build the clip catalog."""
    def chain():
        yield from engine.detect(SOURCES, CLIPS)
        yield from engine.catalog(CLIPS, CATALOG)
    return _sse(chain())


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

<fieldset style="margin-bottom:1rem">
<legend>Add media</legend>
<div style="margin:.3rem 0">
  <input type=file id=trackfile accept="audio/*">
  <button onclick="uploadTrack()">Upload music track</button>
</div>
<div style="margin:.3rem 0">
  <input type=file id=footagefiles accept="video/*" multiple>
  <button onclick="uploadFootage()">Upload + analyze footage</button>
</div>
</fieldset>

<input id=track value="02 Erased.mp3" style="width:60%">
<button onclick="go('analyze')">Analyze</button>
<button onclick="go('arrange')">Arrange</button>
<button onclick="go('render')">Render</button>
<progress id=bar value=0 max=1 style="width:100%;display:block;margin:1rem 0"></progress>
<pre id=log style="background:#eee;padding:1rem;height:160px;overflow:auto"></pre>
<video id=vid controls style="width:100%;display:none"></video>
<script>
const log = m => document.getElementById('log').textContent += m + "\\n";

function stream(url, stage, track){
  const es = new EventSource(url);
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

function go(stage){
  const track = encodeURIComponent(document.getElementById('track').value);
  stream(`/api/${stage}?track=${track}`, stage, track);
}

async function uploadTrack(){
  const f = document.getElementById('trackfile').files[0];
  if (!f){ log('pick an audio file first'); return; }
  const fd = new FormData(); fd.append('file', f);
  log('uploading ' + f.name + ' …');
  const r = await fetch('/api/upload/track', {method:'POST', body:fd});
  if (!r.ok){ log('upload failed: ' + (await r.text())); return; }
  const j = await r.json();
  document.getElementById('track').value = j.track;
  log('added track: ' + j.track + ' — click Analyze');
}

async function uploadFootage(){
  const files = document.getElementById('footagefiles').files;
  if (!files.length){ log('pick one or more video files first'); return; }
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  log('uploading ' + files.length + ' clip(s) …');
  const r = await fetch('/api/upload/footage', {method:'POST', body:fd});
  if (!r.ok){ log('upload failed: ' + (await r.text())); return; }
  const j = await r.json();
  log('uploaded: ' + j.files.join(', ') + ' — detecting + cataloging …');
  stream('/api/footage', 'footage', null);
}
</script>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX
