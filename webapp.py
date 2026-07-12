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

This is a skeleton: enough to upload media, run analyze→arrange→render (or
→export an editable Resolve timeline), browse the catalog gallery, and preview
the result. Uploading a track saves it to
album-audio/; uploading footage saves to sources/ then scene-splits + catalogs
it; /api/gallery reuses clip_gallery.py's HTML. Each running stage gets a job id
+ cancel token (the Cancel button POSTs /api/cancel?job=…), so a long
render/analyze can be stopped. Still to flesh out: parameter forms for the
grid/reuse knobs and richer error surfacing.

Run:  uvicorn webapp:app --reload      (pip install fastapi uvicorn python-multipart)
"""
from __future__ import annotations

import html
import json
import os
import shutil
import sys
import threading
import uuid
from typing import List
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import (HTMLResponse, StreamingResponse, FileResponse,
                               PlainTextResponse)

import engine

# make the vendored pipeline importable regardless of the launch cwd
if engine.HERE not in sys.path:
    sys.path.insert(0, engine.HERE)
from pipeline import clip_gallery   # noqa: E402  (reuse its HTML, don't fork it)

app = FastAPI(title="dv2mv (web)")

# Media-derived paths. Kept as module globals so the endpoints (and tests that
# monkeypatch them) stay simple; _apply_media() recomputes them when the library
# changes at runtime via the in-app picker. Initial values come from engine.MEDIA
# (which already applied env > saved-config > cwd).
MEDIA = CATALOG_AUDIO = CATALOG = MANIFEST = ALBUM_AUDIO = SOURCES = CLIPS = ""


def _apply_media(media: str) -> None:
    """(Re)derive the media-rooted path globals from `media`."""
    global MEDIA, CATALOG_AUDIO, CATALOG, MANIFEST, ALBUM_AUDIO, SOURCES, CLIPS
    MEDIA = media
    CATALOG_AUDIO = os.path.join(MEDIA, "catalog_audio")
    CATALOG = os.path.join(MEDIA, "catalog")
    MANIFEST = os.path.join(CATALOG, "manifest.csv")
    ALBUM_AUDIO = os.path.join(MEDIA, "album-audio")
    SOURCES = os.path.join(MEDIA, "sources")        # uploaded footage lands here
    CLIPS = os.path.join(MEDIA, "clips")


_apply_media(engine.MEDIA)


def set_media(path: str) -> str:
    """Switch the web tier to a new media library (validated + persisted),
    recomputing the path globals. Raises StageError on a bad folder."""
    p = engine.set_media(path)        # validates, persists, updates engine.MEDIA
    _apply_media(p)
    return p


FAVICON = os.path.join(engine.HERE, "assets", "icons", "favicon.ico")
HERO = os.path.join(engine.HERE, "assets", "img", "cameraman.jpg")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(FAVICON, media_type="image/x-icon")


@app.get("/hero.jpg")
def hero():
    return FileResponse(HERO, media_type="image/jpeg")


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


# ── job registry (so a running stage can be cancelled) ──────────────────────
# Each streaming stage mints a job id + a cancel Event; the browser learns the
# id from the SSE events and POSTs it to /api/cancel to stop the work. The
# engine watches the Event and terminates the underlying subprocess (and, for
# render, its ffmpeg child group). Kept in-process — fine for the single-user
# local tool this is.
JOBS: dict = {}


def _new_job():
    """Register a new cancellable job; return (job_id, cancel_event)."""
    job_id = uuid.uuid4().hex[:12]
    cancel = threading.Event()
    JOBS[job_id] = cancel
    return job_id, cancel


def _sse(stage_gen, cancel=None, job_id=None) -> StreamingResponse:
    """Adapter: turn a stage generator into an SSE stream of JSON events.

    A StageError (e.g. a missing prerequisite) is turned into a final `error`
    event so the browser can show an actionable message instead of the stream
    just dying. A Cancelled (the user pressed Cancel) becomes a `cancelled`
    event — a clean stop, not an error. Any other exception is reported as an
    error. Every event carries the `job` id so the browser knows what to cancel;
    the job is unregistered when the stream ends.
    """
    def event_source():
        try:
            engine.check_media_root()      # fail loud if MEDIA is the code checkout
            for ev in stage_gen:
                payload = {"stage": ev.stage, "message": ev.message,
                           "frac": ev.frac, "done": ev.done, "result": ev.result,
                           "job": job_id}
                yield f"data: {json.dumps(payload)}\n\n"
        except engine.Cancelled:
            yield ("data: " + json.dumps(
                {"stage": "cancelled", "message": "cancelled", "frac": None,
                 "done": True, "cancelled": True, "job": job_id}) + "\n\n")
        except engine.StageError as exc:
            yield ("data: " + json.dumps(
                {"stage": "error", "message": str(exc), "frac": None,
                 "done": True, "error": True, "job": job_id}) + "\n\n")
        except Exception as exc:  # last-resort: never leave the UI hanging
            yield ("data: " + json.dumps(
                {"stage": "error", "message": f"unexpected: {exc}", "frac": None,
                 "done": True, "error": True, "job": job_id}) + "\n\n")
        finally:
            if job_id is not None:
                JOBS.pop(job_id, None)
    return StreamingResponse(event_source(), media_type="text/event-stream")


@app.get("/api/cancel")
def api_cancel(job: str):
    """Signal a running job to stop. Returns whether the job was still live."""
    cancel = JOBS.get(job)
    if cancel is not None:
        cancel.set()
        return {"cancelled": True}
    return {"cancelled": False}      # already finished / unknown id — no-op


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
def api_footage(mode: str = "encode"):
    """Scene-split everything in sources/, then (re)build the clip catalog.

    mode=copy stream-copies scene clips (lossless + fast, keyframe-snapped
    cuts) instead of the default frame-exact re-encode.
    """
    job_id, cancel = _new_job()
    mode_ = mode if mode in ("encode", "copy") else "encode"
    def chain():
        yield from engine.detect(SOURCES, CLIPS, mode=mode_, cancel=cancel)
        # incremental: only catalog the newly-split clips, append to the manifest
        yield from engine.catalog(CLIPS, CATALOG, append=True, cancel=cancel)
    return _sse(chain(), cancel=cancel, job_id=job_id)


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
  <span style="opacity:.5">|</span>
  edit:
  <select id=peditsel><option value="">— pick a project —</option></select>
  <button id=padd>Add →</button>
  <button id=premove>Remove ←</button>
  <button id=preplace>Replace</button>
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

// populate the edit-project dropdown
(async () => {
  const sel = document.getElementById('peditsel');
  const j = await (await fetch('/api/projects')).json();
  for (const p of j.projects){
    const o = document.createElement('option');
    o.value = p.name; o.textContent = `${p.name} (${p.clips} clips)`;
    sel.appendChild(o);
  }
})();

async function _editProject(op){
  const msg = document.getElementById('selmsg');
  const name = document.getElementById('peditsel').value;
  if (!name){ msg.textContent = 'pick a project to edit'; return; }
  if (!_sel.size){ msg.textContent = 'select some clips first'; return; }
  const fd = new FormData();
  fd.append('op', op);
  _sel.forEach(c => fd.append('clips', c));
  msg.textContent = op + 'ing…';
  const r = await fetch('/api/projects/' + encodeURIComponent(name) + '/clips',
                        {method:'POST', body:fd});
  if (!r.ok){ msg.textContent = 'failed: ' + ((await r.json()).detail || ''); return; }
  const j = await r.json();
  msg.textContent = `${name}: ${op} → ${j.clips} clips`;
}
document.getElementById('padd').addEventListener('click', () => _editProject('add'));
document.getElementById('premove').addEventListener('click', () => _editProject('remove'));
document.getElementById('preplace').addEventListener('click', () => _editProject('replace'));
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


@app.get("/catalog-files/{relpath:path}")
def catalog_files(relpath: str):
    """Serve a file from the current catalog dir (the gallery's thumbs/ load via
    this). A route, not a static mount, so the catalog can change at runtime when
    the media library is switched. Guards against path traversal."""
    full = os.path.realpath(os.path.join(CATALOG, relpath))
    root = os.path.realpath(CATALOG)
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(403, "outside catalog")
    if not os.path.isfile(full):
        raise HTTPException(404, "not found")
    return FileResponse(full)


# ── media library (switch the media root at runtime; remembered in config) ───
@app.get("/api/thumbnails")
def api_thumbnails(per_group: int = 8, exclude: str = None):
    """Scout thumbnail frames from the catalog (SSE, like the other stages).

    `exclude` omitted -> the saved filter applies; passing it (even empty)
    updates the saved filter, so a private tape stays excluded next time.
    """
    if exclude is None:
        exclude = engine.load_config().get("thumbs_exclude", "")
    else:
        cfg = engine.load_config()
        if cfg.get("thumbs_exclude", "") != exclude:
            cfg["thumbs_exclude"] = exclude
            engine.save_config(cfg)
    out = os.path.join(MEDIA, "thumbnails")
    job_id, cancel = _new_job()
    return _sse(engine.thumbnails(MANIFEST, out, per_group=per_group,
                                  exclude_re=exclude, cancel=cancel),
                cancel=cancel, job_id=job_id)


@app.get("/api/help", response_class=PlainTextResponse)
def api_help():
    """HELP.md, shared verbatim with the Tk app (one source of truth)."""
    path = os.path.join(engine.HERE, "HELP.md")
    if not os.path.exists(path):
        raise HTTPException(404, "HELP.md missing from the installation")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@app.get("/api/preflight")
def api_preflight():
    """Required + recommended system tooling (ffmpeg/ffprobe/rubberband). Both
    UIs surface the same check so first-run guidance matches."""
    return engine.preflight()


@app.get("/api/tour")
def api_tour():
    """The shared tour steps (one source of truth — same data the Tk app uses).
    Each step has a `target` selector the page knows how to highlight."""
    return {"steps": list(engine.TOUR_STEPS)}


@app.get("/api/media")
def api_media():
    """Current media library + whether it's actually set (vs. the checkout)."""
    return {"media": MEDIA, "source": engine.MEDIA_SOURCE,
            "ok": not engine.looks_like_code_checkout(MEDIA)}


@app.post("/api/media")
def api_set_media(path: str = Form(...)):
    """Point the app at a new media library (validated + remembered)."""
    try:
        return {"media": set_media(path)}
    except engine.StageError as exc:
        raise HTTPException(400, str(exc))


# ── stage endpoints (each streams progress) ────────────────────────────────
@app.get("/api/analyze")
def api_analyze(track: str):
    audio = os.path.join(MEDIA, "album-audio", track)
    job_id, cancel = _new_job()
    return _sse(engine.analyze(audio, CATALOG_AUDIO, plot=True, cancel=cancel),
                cancel=cancel, job_id=job_id)


@app.get("/api/arrange")
def api_arrange(track: str = "", grid: str = "sections", beats_per_cut: int = 4,
                allow_reuse: bool = False, drop_blurry: float = 0.0,
                clip_from: str = "middle", match: str = "energy", project: str = ""):
    job_id, cancel = _new_job()
    if project:
        # arrange within the project: (re)point it at the requested track (the
        # box is the source of truth), save the chosen options, scope to clips
        p = engine.load_project(MEDIA, project)
        if track:
            p.track = track
        (p.grid, p.beats_per_cut, p.allow_reuse, p.drop_blurry, p.clip_from,
         p.match) = (grid, beats_per_cut, allow_reuse, drop_blurry, clip_from, match)
        p.save()
        return _sse(engine.arrange_project(p, MEDIA, cancel=cancel),
                    cancel=cancel, job_id=job_id)
    analysis = os.path.join(CATALOG_AUDIO,
                            f"{os.path.splitext(track)[0]}.analysis.json")
    return _sse(engine.arrange(analysis, MANIFEST, grid=grid,
                               beats_per_cut=beats_per_cut, allow_reuse=allow_reuse,
                               drop_blurry=drop_blurry, clip_from=clip_from, match=match,
                               cut_dir=os.path.join(MEDIA, "cuts"), cancel=cancel),
                cancel=cancel, job_id=job_id)


@app.get("/api/compare")
def api_compare(track: str = "", beats_per_cut: int = 4, allow_reuse: bool = False,
                drop_blurry: float = 0.0, clip_from: str = "middle",
                match: str = "energy", project: str = "", grid: str = ""):
    """Arrange the track across all grids and stream a ranked comparison. `grid`
    is accepted but ignored (compare sweeps every grid)."""
    job_id, cancel = _new_job()
    if project:
        p = engine.load_project(MEDIA, project)
        if track:
            p.track = track
        (p.beats_per_cut, p.allow_reuse, p.drop_blurry, p.clip_from, p.match) = (
            beats_per_cut, allow_reuse, drop_blurry, clip_from, match)
        p.save()
        return _sse(engine.compare_project(p, MEDIA, cancel=cancel),
                    cancel=cancel, job_id=job_id)
    analysis = os.path.join(CATALOG_AUDIO,
                            f"{os.path.splitext(track)[0]}.analysis.json")
    # compare sweeps every match strategy (energy/contrast/variety), so the
    # single `match` query param isn't used here — like `grid`, it's ignored.
    return _sse(engine.compare(analysis, MANIFEST, beats_per_cut=beats_per_cut,
                               allow_reuse=allow_reuse, drop_blurry=drop_blurry,
                               clip_from=clip_from,
                               cut_dir=os.path.join(MEDIA, "cuts"), cancel=cancel),
                cancel=cancel, job_id=job_id)


@app.get("/api/render")
def api_render(track: str = "", grid: str = "", project: str = ""):
    job_id, cancel = _new_job()
    if project:
        return _sse(engine.render_project(engine.load_project(MEDIA, project),
                                          cancel=cancel),
                    cancel=cancel, job_id=job_id)
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
        return _sse(need_arrange(), cancel=cancel, job_id=job_id)
    return _sse(engine.render(sh, cancel=cancel), cancel=cancel, job_id=job_id)


@app.get("/api/export")
def api_export(track: str = "", grid: str = "", project: str = ""):
    """Export the arrangement to an editable timeline (OTIO + FCPXML) for Resolve."""
    job_id, cancel = _new_job()
    if project:
        return _sse(engine.export_project(engine.load_project(MEDIA, project), MEDIA,
                                          cancel=cancel),
                    cancel=cancel, job_id=job_id)
    arr = engine.find_arrange_json(CATALOG_AUDIO, track, grid or None)
    if not arr:
        stem = os.path.splitext(os.path.basename(track))[0]
        def need_arrange():
            raise engine.StageError(
                f"No arrangement for '{stem}' yet — run Arrange first.")
            yield  # noqa: unreachable — makes this a generator for _sse
        return _sse(need_arrange(), cancel=cancel, job_id=job_id)
    return _sse(engine.export(arr, cancel=cancel), cancel=cancel, job_id=job_id)


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


@app.get("/api/tracks")
def api_tracks():
    """Audio tracks present in album-audio/, for the track dropdown."""
    return {"tracks": engine.list_audio_tracks(MEDIA)}


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


@app.post("/api/projects/from-takes")
def api_projects_from_takes():
    """One project per OBS take (multicam live shoots): take audio in
    album-audio pairs with same-named footage across all cameras."""
    return {"results": engine.projects_from_takes(MEDIA)}


@app.post("/api/projects/{name}/clips")
def api_edit_project_clips(name: str, op: str = Form("replace"),
                           clips: List[str] = Form(default=[])):
    """Revise an existing project's clip selection from the gallery: op is
    add (∪), remove (−), or replace."""
    try:
        p = engine.load_project(MEDIA, name)
    except (OSError, ValueError):
        raise HTTPException(404, f"no such project: {name}")
    try:
        p.clips = engine.revise_clip_selection(
            p.clips, clips, op, engine.all_catalog_clips(MANIFEST))
    except engine.StageError as exc:
        raise HTTPException(400, str(exc))
    p.save()
    return {"name": p.name, "op": op,
            "clips": (len(p.clips) if isinstance(p.clips, list) else "all")}

# The finished cut lives under CATALOG_AUDIO (inside MEDIA); the page plays it
# via /api/clip using the absolute path from render's result, so there's no
# separate video endpoint to keep in sync with the grid suffix.


# ── the world's smallest front end, to prove the loop ──────────────────────
INDEX = """<!doctype html><meta charset=utf-8><title>dv2mv</title>
<link rel="icon" href="/favicon.ico">
<body style="font:14px system-ui;max-width:680px;margin:2rem auto">
<div style="position:relative;margin-bottom:1rem">
  <img src="/hero.jpg" alt="Man with a Movie Camera (Vertov, 1929)"
    style="width:100%;max-height:200px;object-fit:cover;object-position:center 32%;
    border-radius:8px;display:block">
  <h1 style="position:absolute;left:14px;bottom:6px;margin:0;color:#fff;
    font:600 24px system-ui;text-shadow:0 1px 5px #000">dv2mv</h1>
  <div style="position:absolute;right:10px;top:8px;display:flex;gap:6px">
    <button onclick="startTour()" title="interactive walkthrough"
      style="width:28px;height:28px;border-radius:50%;border:1px solid #fff8;
      background:#0006;color:#fff;font:600 13px system-ui;cursor:pointer">▶</button>
    <button onclick="helpSection('Latham')" title="live shoots from OBS"
      style="height:28px;border-radius:14px;border:1px solid #fff8;background:#0006;
      color:#fff;font:600 12px system-ui;cursor:pointer;padding:0 8px">Latham</button>
    <button onclick="togglePreflight()" title="check required + recommended tools"
      style="height:28px;border-radius:14px;border:1px solid #fff8;background:#0006;
      color:#fff;font:600 12px system-ui;cursor:pointer;padding:0 8px">
      <span id=preflightbadge style="border-radius:9px;padding:1px 6px;background:#888;min-width:46px;display:inline-block;text-align:center">…</span>
      <span style="margin-left:4px">tools</span></button>
    <button onclick="toggleHelp()" title="help" style="width:28px;height:28px;
      border-radius:50%;border:1px solid #fff8;background:#0006;color:#fff;
      font:600 15px system-ui;cursor:pointer">?</button>
  </div>
</div>

<div id=preflight style="display:none;margin:.4rem 0;padding:.5rem .7rem;
  background:#f6f6f0;border:1px solid #d4d4cc;border-radius:6px;font-size:13px"></div>

<div id=helppanel style="display:none;position:fixed;inset:5% 10%;background:#fff;
  border:1px solid #999;border-radius:8px;box-shadow:0 8px 40px #0006;z-index:10;
  padding:1rem 1.6rem;overflow:auto;font-size:14px;line-height:1.45"></div>

<fieldset style="margin-bottom:1rem" data-tour=media-library>
<legend>Media library</legend>
<div style="margin:.3rem 0;font-size:13px">
  current: <code id=medianow style="font-size:12px">…</code>
</div>
<div style="margin:.3rem 0">
  <input id=mediapath placeholder="/Volumes/Footage/musicvideo" style="width:60%">
  <button type=button onclick="setMedia()">Use this folder</button>
  <div id=mediamsg style="font-size:11px;color:#a33;white-space:pre-line"></div>
</div>
</fieldset>

<fieldset style="margin-bottom:1rem" data-tour=add-media>
<legend>Add media</legend>
<div style="margin:.3rem 0" data-tour=add-track>
  <input type=file id=trackfile accept="audio/*">
  <button onclick="uploadTrack()">Upload music track</button>
</div>
<div style="margin:.3rem 0" data-tour=add-footage>
  <input type=file id=footagefiles accept="video/*" multiple>
  <button onclick="uploadFootage()">Upload + analyze footage</button>
  <label title="split scenes by stream copy: lossless and much faster, but cuts land on keyframes (a few seconds of slack) — good for long-scene material like live sets or screen recordings">
    <input type=checkbox id=fastsplit> fast split (no re-encode)</label>
</div>
<div style="margin:.3rem 0">
  <a href="/api/gallery" target="_blank" data-tour=gallery><button type=button>View catalog gallery ↗</button></a>
  <button type=button onclick="goThumbs()"
    title="pick sharp, well-lit, face-bearing frames from the catalog as cover/YouTube thumbnail candidates">Thumbnail suggestions</button>
  skip: <input id=thumbexcl value="__THUMBEXCL__" placeholder="tape regex"
    title="sources matching this regex stay out of the sheet (remembered)" style="width:8rem">
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
  <button type=button onclick="projectsFromTakes()"
    title="multicam live shoots: one project per OBS take — take audio in album-audio pairs with same-named footage across all cameras">Projects from takes</button>
  <div style="font-size:11px;color:#666">pick tapes to scope the project, or select none for the whole library</div>
</div>
</fieldset>

<input id=track value="02 Erased.mp3" list=tracklist style="width:60%"
  placeholder="track filename (pick or type)">
<datalist id=tracklist></datalist>
<button onclick="go('analyze')">Analyze</button>
<button onclick="go('arrange')" data-tour=arrange>Arrange</button>
<button onclick="go('compare')" title="arrange every grid and rank them by energy match">Compare grids</button>
<button onclick="go('render')" data-tour=render>Render</button>
<button onclick="go('export')" data-tour=export title="emit an editable timeline (OTIO + FCPXML) for DaVinci Resolve">Export ⤓</button>
<button id=cancelbtn onclick="cancelJob()" disabled>Cancel</button>

<fieldset style="margin:1rem 0">
<legend>Arrange options</legend>
<label title="where cuts happen: sections = one per structural section (calmest), downbeats = one per bar (driving), beats = every N beats (fast montage), harmonic = on chord changes">Grid
  <select id=grid onchange="syncGrid()">
    <option value=sections>sections</option>
    <option value=downbeats>downbeats</option>
    <option value=beats>beats</option>
    <option value=harmonic>harmonic</option>
  </select></label>
<label title="on the beats grid, cut every N beats (4 = once per bar at 4/4)">Beats/cut <input id=bpc type=number value=4 min=1 max=32 style="width:4rem"></label>
<label title="let clips repeat when there are more slots than clips"><input id=reuse type=checkbox> allow reuse</label>
<label title="ignore clips whose sharpness is below this (0 = keep everything)">Drop blurry &lt; <input id=blur type=number value=0 min=0 step=1 style="width:5rem"></label>
<label title="take the slot-length piece from the clip's middle (default) or its start">Clip from
  <select id=clipfrom><option value=middle>middle</option><option value=start>start</option></select></label>
<div id=gridhelp style="margin-top:.4rem;font-size:12px;color:#555"></div>
</fieldset>

<progress id=bar value=0 max=1 style="width:100%;display:block;margin:1rem 0"></progress>
<div id=summary style="display:none;margin:.5rem 0;padding:.5rem .7rem;
  background:#eef3ff;border:1px solid #cdd9f0;border-radius:6px;font-size:13px"></div>
<div id=compare style="display:none;margin:.5rem 0;padding:.5rem .7rem;
  background:#f3f7ee;border:1px solid #d6e3c5;border-radius:6px;font-size:13px"></div>
<pre id=log style="background:#eee;padding:1rem;height:160px;overflow:auto"></pre>
<video id=vid controls style="width:100%;display:none"></video>
<div id=vidpath style="display:none;margin:.4rem 0;font:12px ui-monospace,monospace;
  color:#444;word-break:break-all"></div>
<div id=exports style="display:none;margin:.4rem 0;font:12px ui-monospace,monospace;
  color:#444;word-break:break-all"></div>
<img id=contactimg style="width:100%;display:none;border:1px solid #ccc;border-radius:6px">

<script>
const log = m => document.getElementById('log').textContent += m + "\\n";

// help: /api/help serves HELP.md (shared with the Tk app); render a tiny
// markdown subset — headings, bullets, **bold**, `code` — nothing more.
async function toggleHelp(){
  const p = document.getElementById('helppanel');
  if (p.style.display === 'block'){ p.style.display = 'none'; return; }
  if (!p.dataset.loaded){
    const md = await (await fetch('/api/help')).text();
    p.innerHTML = '<button style="float:right" onclick="toggleHelp()">close ✕</button>'
      + mdToHtml(md);
    p.dataset.loaded = '1';
  }
  p.style.display = 'block';
}
async function helpSection(match){    // open help scrolled to a ## section
  const p = document.getElementById('helppanel');
  if (p.style.display !== 'block') await toggleHelp();
  for (const h of p.querySelectorAll('h3'))
    if (h.textContent.toLowerCase().includes(match.toLowerCase())){
      // the panel is its own fixed scroll container — scroll it directly
      p.scrollTop += h.getBoundingClientRect().top
                     - p.getBoundingClientRect().top - 8;
      return;
    }
}
function mdToHtml(md){
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const inline = s => esc(s).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>')
                            .replace(/`([^`]+)`/g,'<code>$1</code>');
  // unwrap hard-wrapped continuation lines into their paragraph/bullet
  const lines = [];
  for (const raw of md.split('\\n')){
    const prev = lines[lines.length - 1];
    if (raw && !raw.startsWith('#') && !raw.startsWith('- ')
        && prev && !prev.startsWith('#'))
      lines[lines.length - 1] = prev + ' ' + raw.trim();
    else lines.push(raw);
  }
  let html = '', list = false;
  for (const line of lines){
    if (line.startsWith('- ')){
      if (!list){ html += '<ul>'; list = true; }
      html += '<li>' + inline(line.slice(2)) + '</li>'; continue;
    }
    if (list){ html += '</ul>'; list = false; }
    if (line.startsWith('## ')) html += '<h3>' + inline(line.slice(3)) + '</h3>';
    else if (line.startsWith('# ')) html += '<h2>' + inline(line.slice(2)) + '</h2>';
    else if (line.trim()) html += '<p>' + inline(line) + '</p>';
  }
  if (list) html += '</ul>';
  return html;
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape'){
    document.getElementById('helppanel').style.display = 'none';
    if (document.getElementById('tourcard')) endTour();
  }
});

// ── preflight: required + recommended tooling ─────────────────────────────
// Fetched once at load; Toggle re-renders. The PREFLIGHT_BADGE in the header is
// green when ok, red when a required tool is missing.
let _preflightCache = null;
async function loadPreflight(){
  if (_preflightCache) return _preflightCache;
  try { _preflightCache = await (await fetch('/api/preflight')).json(); }
  catch (e) { _preflightCache = {ok: false, tools: [], summary: 'preflight fetch failed'}; }
  const badge = document.querySelector('#preflightbadge');
  if (badge){
    badge.textContent = _preflightCache.ok ? '✓ tools' : '⚠ tools';
    badge.style.background = _preflightCache.ok ? '#3a7' : '#c44';
  }
  return _preflightCache;
}
async function togglePreflight(){
  const box = document.getElementById('preflight');
  if (box.style.display === 'block'){ box.style.display = 'none'; return; }
  const p = await loadPreflight();
  const row = t => `<tr><td>${t.found ? '✓' : '✗'}</td>
    <td><b>${t.name}</b> <span style="color:#888">(${t.kind})</span>${t.bundled ? ' <span style="font-size:10px;color:#888">(bundled)</span>' : ''}</td>
    <td style="color:#666">${t.why}</td>
    <td style="font-size:11px;color:#888">${t.found ? 'found' : (t.install || 'see install docs')}</td></tr>`;
  // clipboard button only when a tool is missing AND we have a canned command
  const missing = p.tools.filter(t => !t.found && t.install);
  const clipBtn = missing.length ?
    `<button onclick="copyInstall()" title="copy the install command(s) for the missing tool(s)"
       style="margin-left:.6rem;padding:2px 8px;font-size:12px;cursor:pointer">Copy install command</button>` : '';
  box.innerHTML = `<div style="display:flex;align-items:baseline">${'System tooling'} — ${p.summary}${clipBtn}</div>
    <table style="border-collapse:collapse;margin-top:.3rem;width:100% font-size:12px">
    ${p.tools.map(row).join('')}</table>`;
  box.style.display = 'block';
}
function copyInstall(){
  const p = _preflightCache; if (!p) return;
  const cmds = [...new Set(p.tools.filter(t => !t.found && t.install).map(t => t.install))];
  if (!cmds.length) return;
  const text = cmds.join('\\n');
  navigator.clipboard.writeText(text).then(
    () => alert('Copied:\\n' + text),
    () => prompt('Copy this install command:', text));
}

// ── interactive tour ("What does this software do?") ───────────────────────
// Steps come from /api/tour (shared with the Tk app). Each step targets a
// `[data-tour=<name>]` element by `target`; a few targets are composite
// ("render-export"), resolved here. The card highlights the element with an
// outline, scrolls it into view, and shows title/body/cue/demo on a floating
// card. Pure DOM — no library.
let _tourSteps = null, _tourIdx = 0;
function _tourTarget(sel){
  if (sel === 'root') return document.body;
  if (sel === 'render-export')
    return document.querySelector('[data-tour=render]');
  return document.querySelector(`[data-tour="${sel}"]`);
}
async function startTour(){
  if (_tourSteps === null){
    try { _tourSteps = (await (await fetch('/api/tour')).json()).steps; }
    catch (e) { _tourSteps = []; }
  }
  if (!_tourSteps.length) return;
  _tourIdx = 0;
  _renderTour();
}
function _renderTour(){
  // strip + re-add so a re-rendered element clears the highlight
  document.querySelectorAll('.tour-hl').forEach(e => e.classList.remove('tour-hl'));
  const step = _tourSteps[_tourIdx];
  const el = _tourTarget(step.target);
  if (el && el !== document.body){
    el.classList.add('tour-hl');
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
  }
  const card = document.getElementById('tourcard') || (function(){
    const c = document.createElement('div');
    c.id = 'tourcard';
    c.style.cssText = 'position:fixed;right:18px;bottom:18px;max-width:380px;'
      + 'background:#fff;border:1px solid #999;border-radius:8px;'
      + 'box-shadow:0 8px 40px #0005;padding:1rem 1.2rem;'
      + 'font:14px system-ui;z-index:11';
    document.body.appendChild(c);
    return c;
  })();
  const last = _tourIdx === _tourSteps.length - 1;
  card.innerHTML = `<div style="display:flex;justify-content:space-between;
    align-items:baseline;margin-bottom:.4rem">
      <b style="font-size:15px">${step.title}</b>
      <span style="color:#888;font-size:12px">${_tourIdx + 1}/${_tourSteps.length}</span>
    </div>
    <div style="line-height:1.45">${step.body}</div>
    ${step.demo ? `<div style="margin-top:.5rem;padding:.4rem .6rem;
      background:#f6f6f0;border:1px solid #e0e0d8;border-radius:5px;
      font-size:12px"><b>Try:</b> ${step.demo}${
        step.demo_url ? ` · <a href="${step.demo_url}" target=_blank>link ↗</a>` : ''
      }</div>` : ''}
    <div style="margin-top:.6rem;color:#3a6;font-size:12px">${step.cue}</div>
    <div style="margin-top:.6rem;display:flex;gap:6px;justify-content:flex-end">
      <button onclick="endTour()" style="padding:4px 10px">Skip</button>
      ${_tourIdx > 0 ? '<button onclick="_tourBack()" style="padding:4px 10px">Prev</button>' : ''}
      <button onclick="_tourNext()" style="padding:4px 10px;font-weight:600">
        ${last ? 'Done' : 'Next'}</button>
    </div>`;
}
function _tourNext(){
  if (_tourIdx + 1 >= _tourSteps.length){ endTour(); return; }
  _tourIdx++; _renderTour();
}
function _tourBack(){ if (_tourIdx > 0){ _tourIdx--; _renderTour(); } }
function endTour(){
  document.querySelectorAll('.tour-hl').forEach(e => e.classList.remove('tour-hl'));
  const c = document.getElementById('tourcard');
  if (c) c.remove();
  _tourIdx = 0;
}
const _TOUR_CSS = document.createElement('style');
_TOUR_CSS.textContent = `.tour-hl{outline:3px solid #5b9dff !important;
  outline-offset:3px;border-radius:6px;transition:outline .12s}`;
document.head.appendChild(_TOUR_CSS);
const bar = () => document.getElementById('bar');
const busy = () => bar().removeAttribute('value');   // <progress> animates when value-less
const determinate = f => bar().value = f;
const idle = () => bar().value = 0;

let currentJob = null;
const cancelBtn = () => document.getElementById('cancelbtn');
function endStream(es){ es.close(); idle(); currentJob = null; cancelBtn().disabled = true; }

async function cancelJob(){
  if (!currentJob){ return; }
  cancelBtn().disabled = true;          // one click; the stream confirms the stop
  log('■ cancelling …');
  try { await fetch('/api/cancel?job=' + encodeURIComponent(currentJob)); }
  catch (e) { log('cancel request failed: ' + e); }
}

function stream(url, stage, track){
  busy();                       // show motion immediately so it never looks frozen
  currentJob = null; cancelBtn().disabled = false;   // armed for the run
  const es = new EventSource(url);
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.job) currentJob = ev.job;    // learn the id so Cancel can target it
    if (ev.cancelled){ log('■ cancelled'); endStream(es); return; }
    if (ev.error){
      log('⚠ ' + ev.message);
      if (/run Analyze/i.test(ev.message)) log('   → click "Analyze" first.');
      else if (/add footage/i.test(ev.message)) log('   → upload footage first.');
      else if (/run Arrange/i.test(ev.message)) log('   → click "Arrange" first.');
      endStream(es); return;
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
      endStream(es);
      if (stage === 'render' && ev.result && ev.result.video){
        const v = document.getElementById('vid');
        v.src = '/api/clip?path=' + encodeURIComponent(ev.result.video);
        v.style.display = 'block';
        const vp = document.getElementById('vidpath');
        vp.textContent = '✓ wrote ' + ev.result.video;
        vp.style.display = 'block';
      }
      if (stage === 'export' && ev.result && ev.result.outputs){
        const e = document.getElementById('exports');
        e.innerHTML = '✓ timeline: ' + ev.result.outputs.map(p =>
          `<a href="/api/clip?path=${encodeURIComponent(p)}" download>`
          + p.split('/').pop() + '</a>').join(' · ')
          + ' — import into DaVinci Resolve';
        e.style.display = 'block';
      }
      if (stage === 'compare' && ev.result && ev.result.comparison){
        showComparison(ev.result);
      }
      if (stage === 'thumbnails' && ev.result && ev.result.contact){
        const img = document.getElementById('contactimg');
        img.src = '/api/clip?path=' + encodeURIComponent(ev.result.contact)
                + '&t=' + Date.now();          // bust the cache on re-runs
        img.style.display = 'block';
        log('✓ thumbnails in ' + ev.result.out_dir);
      }
    }
  };
  es.onerror = () => { log('-- stream error --'); endStream(es); };
}

const $ = id => document.getElementById(id);
const GRID_HELP = /*GRIDHELP*/;

function syncGrid(){           // beats/cut only matters on the beats grid
  const g = $('grid').value;
  $('bpc').disabled = g !== 'beats';
  $('gridhelp').textContent = GRID_HELP[g] || '';
}

function goThumbs(){
  const excl = $('thumbexcl').value.trim();
  stream('/api/thumbnails?per_group=8&exclude=' + encodeURIComponent(excl),
         'thumbnails', '');
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

async function projectsFromTakes(){
  const j = await (await fetch('/api/projects/from-takes', {method:'POST'})).json();
  if (!j.results.length){ log('no take tracks in album-audio (OBS-named whole-take audio)'); return; }
  for (const r of j.results)
    log(`take ${r.name} (${r.track}): ${r.status}` +
        (r.status === 'created' ? ` — ${r.clips} clips` : ''));
  await loadProjects();
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

async function loadTracks(){
  const j = await (await fetch('/api/tracks')).json();
  const dl = $('tracklist'); dl.innerHTML = '';
  for (const t of j.tracks){
    const o = document.createElement('option'); o.value = t; dl.appendChild(o);
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

function showComparison(res){
  const ranked = res.ranked || res.comparison, best = res.best;
  const cell = (v, r) => `<td style="padding:2px 10px;text-align:right">${v == null ? '—' : v}${r||''}</td>`;
  let html = '<b>Grid comparison</b> — ranked by energy match '
    + '<span style="color:#6a7">(click a row to use that grid)</span>'
    + '<table style="border-collapse:collapse;margin-top:.3rem">'
    + '<tr><th style="text-align:left;padding:2px 10px">grid</th>'
    + '<th style="padding:2px 10px">match</th><th style="padding:2px 10px">cuts</th>'
    + '<th style="padding:2px 10px">clips</th></tr>';
  for (const r of ranked){
    const isBest = r.grid === best;
    html += `<tr data-grid="${r.grid}" style="cursor:pointer;`
      + (isBest ? 'background:#dff0d8;font-weight:600' : '') + '">'
      + `<td style="padding:2px 10px">${r.grid}${isBest ? ' ★' : ''}</td>`
      + cell(r.energy_match_pct == null ? null : r.energy_match_pct + '%')
      + cell(r.cuts) + cell(r.clips) + '</tr>';
  }
  html += '</table>';
  const c = document.getElementById('compare');
  c.innerHTML = html; c.style.display = 'block';
  c.querySelectorAll('tr[data-grid]').forEach(tr => tr.addEventListener('click', () => {
    $('grid').value = tr.dataset.grid; syncGrid();
    log('● grid set to ' + tr.dataset.grid + ' — now Render or Export');
  }));
  if (best){ $('grid').value = best; syncGrid(); }   // preselect the winner
}

function go(stage){
  const track = encodeURIComponent($('track').value);
  let url = `/api/${stage}?track=${track}`;
  // arrange + compare carry the arrange knobs (compare sweeps grids itself)
  if (stage === 'arrange' || stage === 'compare') url += arrangeQuery();
  // render + export both target a specific grid's arrangement
  if (stage === 'render' || stage === 'export') url += `&grid=${$('grid').value}`;
  if (activeProject && ['arrange', 'compare', 'render', 'export'].includes(stage))
    url += `&project=${encodeURIComponent(activeProject)}`;   // scope to the project
  stream(url, stage, track);
}
async function loadMedia(){
  const j = await (await fetch('/api/media')).json();
  $('medianow').textContent = j.media + (j.ok ? '' : '  ⚠ not set — pick your library below');
}

async function setMedia(){
  const path = $('mediapath').value.trim();
  if (!path){ $('mediamsg').textContent = 'enter a folder path'; return; }
  const fd = new FormData(); fd.append('path', path);
  const r = await fetch('/api/media', {method:'POST', body:fd});
  if (!r.ok){ $('mediamsg').textContent = '✗ ' + ((await r.json()).detail || 'failed'); return; }
  $('mediamsg').textContent = ''; $('mediapath').value = '';
  await loadMedia();
  loadProjects(); loadSources(); loadTracks();   // re-read the new library
  log('● media library set to ' + (await (await fetch('/api/media')).json()).media);
}

syncGrid();
loadMedia();
loadProjects();
loadSources();
loadTracks();
loadPreflight();

async function uploadTrack(){
  const f = document.getElementById('trackfile').files[0];
  if (!f){ log('pick an audio file first'); return; }
  const fd = new FormData(); fd.append('file', f);
  log('uploading ' + f.name + ' …');
  const r = await fetch('/api/upload/track', {method:'POST', body:fd});
  if (!r.ok){ log('upload failed: ' + (await r.text())); return; }
  const j = await r.json();
  document.getElementById('track').value = j.track;
  loadTracks();                          // surface the new track in the dropdown
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
  const mode = document.getElementById('fastsplit').checked ? 'copy' : 'encode';
  stream('/api/footage?mode=' + mode, 'footage', null);
}
</script>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    saved = html.escape(engine.load_config().get("thumbs_exclude", ""))
    return (INDEX.replace("/*GRIDHELP*/", json.dumps(engine.GRID_HELP))
                 .replace("__THUMBEXCL__", saved))
