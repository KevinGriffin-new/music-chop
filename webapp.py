#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
webapp.py — STUB: the fast, browser-based front end (web tier).

Thin client over engine.py. Demonstrates the one pattern the whole design hinges
on for the web side: run a stage in a background task and stream its
ProgressEvents to the browser over Server-Sent Events (SSE). The browser shows a
progress bar from `frac`, then drops the finished cut into a <video> element —
the inline-preview win that makes the web tier worth building first.

This is a skeleton: enough to upload media, run analyze→arrange→render, browse
the catalog gallery, and preview the result. Uploading a track saves it to
album-audio/; uploading footage saves to sources/ then scene-splits + catalogs
it; /api/gallery reuses clip_gallery.py's HTML. Still to flesh out: job ids / a
job registry, parameter forms for the grid/reuse knobs, and richer error
surfacing.

Run:  uvicorn webapp:app --reload      (pip install fastapi uvicorn python-multipart)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from typing import List
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import engine

# make the vendored pipeline importable regardless of the launch cwd
if engine.HERE not in sys.path:
    sys.path.insert(0, engine.HERE)
from pipeline import clip_gallery   # noqa: E402  (reuse its HTML, don't fork it)

app = FastAPI(title="dv2mv (web)")

MEDIA = engine.MEDIA          # media root (set DV2MV_MEDIA); not the code repo
CATALOG_AUDIO = os.path.join(MEDIA, "catalog_audio")
CATALOG = os.path.join(MEDIA, "catalog")
MANIFEST = os.path.join(CATALOG, "manifest.csv")
ALBUM_AUDIO = os.path.join(MEDIA, "album-audio")
SOURCES = os.path.join(MEDIA, "sources")        # uploaded footage lands here
CLIPS = os.path.join(MEDIA, "clips")

# serve the catalog dir so the gallery's thumbs/ load over http
os.makedirs(CATALOG, exist_ok=True)
app.mount("/catalog-files", StaticFiles(directory=CATALOG), name="catalog")

FAVICON = os.path.join(engine.HERE, "assets", "icons", "favicon.ico")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(FAVICON, media_type="image/x-icon")


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
    """Adapter: turn a stage generator into an SSE stream of JSON events.

    A StageError (e.g. a missing prerequisite) is turned into a final `error`
    event so the browser can show an actionable message instead of the stream
    just dying. Any other exception is reported the same way.
    """
    def event_source():
        try:
            for ev in stage_gen:
                payload = {"stage": ev.stage, "message": ev.message,
                           "frac": ev.frac, "done": ev.done, "result": ev.result}
                yield f"data: {json.dumps(payload)}\n\n"
        except engine.StageError as exc:
            yield ("data: " + json.dumps(
                {"stage": "error", "message": str(exc), "frac": None,
                 "done": True, "error": True}) + "\n\n")
        except Exception as exc:  # last-resort: never leave the UI hanging
            yield ("data: " + json.dumps(
                {"stage": "error", "message": f"unexpected: {exc}", "frac": None,
                 "done": True, "error": True}) + "\n\n")
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


# ── catalog gallery (reuses clip_gallery.py's HTML) ─────────────────────────
@app.get("/api/gallery", response_class=HTMLResponse)
def api_gallery():
    """The clip contact sheet for the current catalog, served over http.

    Reuses clip_gallery's data builder + template, then rewrites the local
    file:// thumb/clip references to served URLs so they work in a browser.
    """
    if not os.path.exists(MANIFEST):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<body style='font:14px system-ui;margin:2rem'>"
            "<p>No catalog yet — upload footage first, then reload.</p>")
    data = clip_gallery.build_gallery_data(MANIFEST)
    for d in data:
        if d.get("thumb"):
            d["thumb"] = "/catalog-files/" + d["thumb"].lstrip("/")
        # file:// can't be opened from an http page; route through /api/clip
        if d.get("clip", "").startswith("file://"):
            d["clip"] = "/api/clip?path=" + quote(d["clip"][len("file://"):])
    html = clip_gallery.render_from_data(data)
    # append a selection layer: click cards to select, then make a project from
    # them (kept here so the shared clip_gallery template stays browse-only)
    return HTMLResponse(html.replace("</body>", _GALLERY_SELECT_LAYER + "</body>"))


# Injected only into the web gallery (not the standalone CLI gallery). Clicking a
# card toggles selection; "Create project" POSTs the selected clip paths.
_GALLERY_SELECT_LAYER = r"""
<style>.card.sel{outline:3px solid #5b9dff;outline-offset:-3px}
#seltoolbar{position:sticky;bottom:0;background:#1c1f24;color:#e8e8e8;
  padding:8px 12px;border-top:1px solid #2c2f36;display:flex;gap:8px;
  align-items:center;flex-wrap:wrap;font:13px system-ui}
#seltoolbar input{background:#0f1113;color:#e8e8e8;border:1px solid #333;
  border-radius:6px;padding:5px 8px}</style>
<div id=seltoolbar>
  <b id=selcount>0 selected</b>
  <input id=pname placeholder="project name">
  <input id=ptrack placeholder="track e.g. 02 Erased.mp3">
  <button id=pcreate>Create project from selection</button>
  <span id=selmsg style="color:#8a93a0"></span>
</div>
<script>
const _sel = new Set();
function _refresh(){ document.getElementById('selcount').textContent = _sel.size + ' selected'; }
document.querySelectorAll('.card').forEach(card => {
  const a = card.querySelector('a'); if (!a) return;
  const clip = new URL(a.href, location.href).searchParams.get('path');
  if (!clip) return;                       // synthetic/unsupported clip
  a.addEventListener('click', e => {
    e.preventDefault();                    // click selects instead of navigating
    if (_sel.has(clip)){ _sel.delete(clip); card.classList.remove('sel'); }
    else { _sel.add(clip); card.classList.add('sel'); }
    _refresh();
  });
});
document.getElementById('pcreate').addEventListener('click', async () => {
  const msg = document.getElementById('selmsg');
  if (!_sel.size){ msg.textContent = 'select some clips first'; return; }
  const name = document.getElementById('pname').value.trim();
  if (!name){ msg.textContent = 'enter a project name'; return; }
  const fd = new FormData();
  fd.append('name', name);
  fd.append('track', document.getElementById('ptrack').value.trim());
  _sel.forEach(c => fd.append('clips', c));
  msg.textContent = 'creating…';
  const r = await fetch('/api/projects', {method:'POST', body:fd});
  msg.textContent = r.ok ? `created '${(await r.json()).name}' (${_sel.size} clips) — switch to the main tab`
                         : 'failed: ' + await r.text();
});
</script>
"""


@app.get("/api/clip")
def api_clip(path: str):
    """Serve a clip by absolute path, but only if it's inside the media root."""
    real = os.path.realpath(path)
    root = os.path.realpath(MEDIA)
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(403, "outside media root")
    if not os.path.isfile(real):
        raise HTTPException(404, "not found")
    return FileResponse(real)


# ── stage endpoints (each streams progress) ────────────────────────────────
@app.get("/api/analyze")
def api_analyze(track: str):
    audio = os.path.join(MEDIA, "album-audio", track)
    return _sse(engine.analyze(audio, CATALOG_AUDIO, plot=True))


@app.get("/api/arrange")
def api_arrange(track: str = "", grid: str = "sections", beats_per_cut: int = 4,
                allow_reuse: bool = False, drop_blurry: float = 0.0,
                clip_from: str = "middle", project: str = ""):
    if project:
        # arrange within the project: save the chosen options, scope to its clips
        p = engine.load_project(MEDIA, project)
        (p.grid, p.beats_per_cut, p.allow_reuse, p.drop_blurry, p.clip_from) = (
            grid, beats_per_cut, allow_reuse, drop_blurry, clip_from)
        p.save()
        return _sse(engine.arrange_project(p, MEDIA))
    analysis = os.path.join(CATALOG_AUDIO,
                            f"{os.path.splitext(track)[0]}.analysis.json")
    return _sse(engine.arrange(analysis, MANIFEST, grid=grid,
                               beats_per_cut=beats_per_cut, allow_reuse=allow_reuse,
                               drop_blurry=drop_blurry, clip_from=clip_from,
                               cut_dir=os.path.join(MEDIA, "cuts")))


@app.get("/api/render")
def api_render(track: str = "", grid: str = "", project: str = ""):
    if project:
        return _sse(engine.render_project(engine.load_project(MEDIA, project)))
    # library mode: prefer the script for the chosen grid (render that specific
    # arrangement); else fall back to the newest render-<track>-*.sh.
    sh = None
    if grid:
        stem = os.path.splitext(os.path.basename(track))[0]
        cand = os.path.join(CATALOG_AUDIO, f"render-{stem}{engine._tag_suffix(grid)}.sh")
        sh = cand if os.path.exists(cand) else None
    sh = sh or engine.find_render_script(CATALOG_AUDIO, track)
    if not sh:
        stem = os.path.splitext(os.path.basename(track))[0]
        def need_arrange():
            raise engine.StageError(
                f"No render script for '{stem}' yet — run Arrange first.")
            yield  # noqa: unreachable — makes this a generator for _sse
        return _sse(need_arrange())
    return _sse(engine.render(sh))


# ── projects (mirror the Tk project flow on the shared engine model) ─────────
@app.get("/api/projects")
def api_projects():
    """List projects with their track + clip-scope, for the picker."""
    out = []
    for name in engine.list_projects(MEDIA):
        p = engine.load_project(MEDIA, name)
        out.append({"name": p.name, "track": p.track,
                    "clips": (len(p.clips) if isinstance(p.clips, list) else "all")})
    return {"projects": out}


@app.get("/api/sources")
def api_sources():
    """source/tape -> clip count, for the New Project footage picker."""
    return {"sources": {k: len(v) for k, v in engine.manifest_sources(MANIFEST).items()}}


@app.post("/api/projects")
def api_create_project(name: str = Form(...), track: str = Form(...),
                       sources: List[str] = Form(default=[]),
                       clips: List[str] = Form(default=[])):
    """Create a project. Explicit `clips` (from the gallery) win; else `sources`
    (tapes); else the whole library."""
    if not name.strip():
        raise HTTPException(400, "project name required")
    if clips:
        picked = clips
    elif sources:
        srcmap = engine.manifest_sources(MANIFEST)
        picked = [c for s in sources for c in srcmap.get(s, [])]
    else:
        picked = "all"
    if isinstance(picked, list) and not picked:
        picked = "all"
    p = engine.new_project(MEDIA, name.strip(), track.strip(), clips=picked)
    return {"name": p.name, "track": p.track,
            "clips": (len(p.clips) if isinstance(p.clips, list) else "all")}

# The finished cut lives under CATALOG_AUDIO (inside MEDIA); the page plays it
# via /api/clip using the absolute path from render's result, so there's no
# separate video endpoint to keep in sync with the grid suffix.


# ── the world's smallest front end, to prove the loop ──────────────────────
INDEX = """<!doctype html><meta charset=utf-8><title>dv2mv</title>
<link rel="icon" href="/favicon.ico">
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
<div style="margin:.3rem 0">
  <a href="/api/gallery" target="_blank"><button type=button>View catalog gallery ↗</button></a>
</div>
</fieldset>

<fieldset style="margin-bottom:1rem">
<legend>Project</legend>
<div style="margin:.3rem 0">
  Active: <select id=project onchange="selectProject()">
    <option value="">— library mode —</option></select>
</div>
<div style="margin:.3rem 0">
  <input id=pname placeholder="new project name" size=20>
  footage: <select id=psources multiple size=4 style="vertical-align:top;min-width:13rem"></select>
  <button type=button onclick="createProject()">Create</button>
  <div style="font-size:11px;color:#666">pick tapes to scope the project, or select none for the whole library</div>
</div>
</fieldset>

<input id=track value="02 Erased.mp3" style="width:60%">
<button onclick="go('analyze')">Analyze</button>
<button onclick="go('arrange')">Arrange</button>
<button onclick="go('render')">Render</button>

<fieldset style="margin:1rem 0">
<legend>Arrange options</legend>
<label>Grid
  <select id=grid onchange="syncGrid()">
    <option value=sections>sections</option>
    <option value=downbeats>downbeats</option>
    <option value=beats>beats</option>
    <option value=harmonic>harmonic</option>
  </select></label>
<label>Beats/cut <input id=bpc type=number value=4 min=1 max=32 style="width:4rem"></label>
<label><input id=reuse type=checkbox> allow reuse</label>
<label>Drop blurry &lt; <input id=blur type=number value=0 min=0 step=1 style="width:5rem"></label>
<label>Clip from
  <select id=clipfrom><option value=middle>middle</option><option value=start>start</option></select></label>
</fieldset>

<progress id=bar value=0 max=1 style="width:100%;display:block;margin:1rem 0"></progress>
<div id=summary style="display:none;margin:.5rem 0;padding:.5rem .7rem;
  background:#eef3ff;border:1px solid #cdd9f0;border-radius:6px;font-size:13px"></div>
<pre id=log style="background:#eee;padding:1rem;height:160px;overflow:auto"></pre>
<video id=vid controls style="width:100%;display:none"></video>
<div id=vidpath style="display:none;margin:.4rem 0;font:12px ui-monospace,monospace;
  color:#444;word-break:break-all"></div>
<script>
const log = m => document.getElementById('log').textContent += m + "\\n";
const bar = () => document.getElementById('bar');
const busy = () => bar().removeAttribute('value');   // <progress> animates when value-less
const determinate = f => bar().value = f;
const idle = () => bar().value = 0;

function stream(url, stage, track){
  busy();                       // show motion immediately so it never looks frozen
  const es = new EventSource(url);
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.error){
      log('⚠ ' + ev.message);
      if (/run Analyze/i.test(ev.message)) log('   → click "Analyze" first.');
      else if (/add footage/i.test(ev.message)) log('   → upload footage first.');
      else if (/run Arrange/i.test(ev.message)) log('   → click "Arrange" first.');
      es.close(); idle(); return;
    }
    if (ev.frac != null) determinate(ev.frac); else busy();
    log(`[${ev.stage}] ${ev.message}`);
    if (ev.done && ev.result && ev.result.summary){
      const s = ev.result.summary, d = document.getElementById('summary');
      d.style.display = 'block';
      d.innerHTML = `<b>${s.track}</b> — ${s.grid} grid · ${s.cuts} cuts · `
        + `<b>${s.energy_match_pct}% energy match</b> · ${s.clips} clips`
        + (s.allow_reuse ? ' · reuse' : '')
        + (s.grid === 'beats' ? ` · ${s.beats_per_cut} beats/cut` : '')
        + (s.drop_blurry ? ` · drop&lt;${s.drop_blurry}` : '')
        + ` · clip-from ${s.clip_from}`;
    }
    if (ev.done){
      es.close(); idle();
      if (stage === 'render' && ev.result && ev.result.video){
        const v = document.getElementById('vid');
        v.src = '/api/clip?path=' + encodeURIComponent(ev.result.video);
        v.style.display = 'block';
        const vp = document.getElementById('vidpath');
        vp.textContent = '✓ wrote ' + ev.result.video;
        vp.style.display = 'block';
      }
    }
  };
  es.onerror = () => { log('-- stream error --'); es.close(); idle(); };
}

const $ = id => document.getElementById(id);

function syncGrid(){           // beats/cut only matters on the beats grid
  $('bpc').disabled = $('grid').value !== 'beats';
}

function arrangeQuery(){
  return `&grid=${$('grid').value}`
       + `&beats_per_cut=${$('bpc').value || 4}`
       + `&allow_reuse=${$('reuse').checked}`
       + `&drop_blurry=${$('blur').value || 0}`
       + `&clip_from=${$('clipfrom').value}`;
}

let activeProject = "";
let projectTracks = {};

async function loadProjects(){
  const sel = $('project'), cur = sel.value;
  const j = await (await fetch('/api/projects')).json();
  sel.innerHTML = '<option value="">— library mode —</option>';
  projectTracks = {};
  for (const p of j.projects){
    projectTracks[p.name] = p.track;
    const o = document.createElement('option');
    o.value = p.name; o.textContent = `${p.name} (${p.clips} clips · ${p.track})`;
    sel.appendChild(o);
  }
  sel.value = cur;
}

async function loadSources(){
  const j = await (await fetch('/api/sources')).json();
  const sel = $('psources'); sel.innerHTML = '';
  for (const [s, n] of Object.entries(j.sources)){
    const o = document.createElement('option');
    o.value = s; o.textContent = `${s} (${n})`;
    sel.appendChild(o);
  }
}

function selectProject(){
  activeProject = $('project').value;
  if (activeProject){
    $('track').value = projectTracks[activeProject] || $('track').value;
    log('● project: ' + activeProject);
  } else { log('● library mode'); }
}

async function createProject(){
  const name = $('pname').value.trim();
  if (!name){ log('enter a project name first'); return; }
  const fd = new FormData();
  fd.append('name', name);
  fd.append('track', $('track').value);
  for (const o of $('psources').selectedOptions) fd.append('sources', o.value);
  const r = await fetch('/api/projects', {method:'POST', body:fd});
  if (!r.ok){ log('create failed: ' + (await r.text())); return; }
  const p = await r.json();
  log(`created project '${p.name}' (${p.clips} clips)`);
  await loadProjects();
  $('project').value = p.name; selectProject();
  $('pname').value = '';
}

function go(stage){
  const track = encodeURIComponent($('track').value);
  let url = `/api/${stage}?track=${track}`;
  if (stage === 'arrange') url += arrangeQuery();
  if (stage === 'render')  url += `&grid=${$('grid').value}`;  // render the chosen grid
  if (activeProject && (stage === 'arrange' || stage === 'render'))
    url += `&project=${encodeURIComponent(activeProject)}`;   // scope to the project
  stream(url, stage, track);
}
syncGrid();
loadProjects();
loadSources();

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
