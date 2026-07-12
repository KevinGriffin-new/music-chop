# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""Front-end tests: web upload endpoints + Tk picker wiring.

The web tests use FastAPI's TestClient (no server, no network) and monkeypatch
the media-dir constants to a tmp dir so nothing touches the real tree. They skip
cleanly if the web deps aren't installed.
"""
import os

import pytest

# Web tier deps — skip the whole module if any are missing.
pytest.importorskip("fastapi")
pytest.importorskip("multipart")          # python-multipart (form parsing)
pytest.importorskip("httpx")              # TestClient transport

from fastapi.testclient import TestClient  # noqa: E402

import webapp  # noqa: E402  (conftest puts the project root on sys.path)
from conftest import REPO, write_analysis  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient with the media dirs redirected into tmp_path."""
    monkeypatch.setattr(webapp, "ALBUM_AUDIO", str(tmp_path / "album-audio"))
    monkeypatch.setattr(webapp, "SOURCES", str(tmp_path / "sources"))
    return TestClient(webapp.app)


# Every audio extension track_analyze.py accepts must survive upload. This is a
# regression guard: if someone trims an ext list, a format silently stops
# importing. (.m4a was the one we explicitly checked decodes via ffmpeg.)
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".aif", ".aiff")


@pytest.mark.parametrize("ext", AUDIO_EXTS)
def test_upload_track_accepts_audio_format(client, tmp_path, ext):
    name = f"New Song{ext}"
    r = client.post("/api/upload/track",
                    files={"file": (name, b"\x00fake-audio", "application/octet-stream")})
    assert r.status_code == 200, f"{ext} rejected"
    assert r.json()["track"] == name
    assert os.path.exists(tmp_path / "album-audio" / name)


def test_web_audio_exts_match_analyzer():
    """The web allow-list must stay a subset of what the analyzer can read,
    so we never accept an upload the analyze stage will then choke on."""
    import importlib
    analyzer = importlib.import_module("pipeline.track_analyze")
    assert set(webapp.AUDIO_EXTS).issubset(set(analyzer.AUDIO_EXTS))


def test_upload_track_rejects_non_audio(client):
    r = client.post("/api/upload/track",
                    files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_upload_track_rejects_path_traversal(client, tmp_path):
    # a sneaky filename must be reduced to a bare basename under the media dir
    r = client.post("/api/upload/track",
                    files={"file": ("../../etc/pwned.mp3", b"x", "audio/mpeg")})
    assert r.status_code == 200
    assert os.path.exists(tmp_path / "album-audio" / "pwned.mp3")
    assert not os.path.exists(tmp_path / "etc" / "pwned.mp3")


def test_upload_footage_saves_all_files(client, tmp_path):
    files = [("files", ("a.mp4", b"x", "video/mp4")),
             ("files", ("b.mov", b"y", "video/quicktime"))]
    r = client.post("/api/upload/footage", files=files)
    assert r.status_code == 200
    assert set(r.json()["files"]) == {"a.mp4", "b.mov"}
    assert os.path.exists(tmp_path / "sources" / "a.mp4")
    assert os.path.exists(tmp_path / "sources" / "b.mov")


def test_upload_footage_rejects_non_video(client):
    r = client.post("/api/upload/footage",
                    files=[("files", ("song.mp3", b"x", "audio/mpeg"))])
    assert r.status_code == 400


def test_index_exposes_upload_controls(client):
    html = client.get("/").text
    assert "Upload music track" in html
    assert "uploadFootage" in html and "/api/upload/footage" in html


def test_tkapp_wires_pickers():
    pytest.importorskip("tkinter")
    import tkapp           # import only — don't construct a window (needs a display)
    assert hasattr(tkapp.App, "add_track")
    assert hasattr(tkapp.App, "add_footage")


# ── catalog gallery ──────────────────────────────────────────────────────────
def test_gallery_data_build_merges_order(smoke_manifest):
    from pipeline import clip_gallery
    data = clip_gallery.build_gallery_data(smoke_manifest)
    assert data and {"name", "thumb", "clip", "motion", "pos"} <= set(data[0])
    # _smoke ships an order.csv, so the arranged position should be merged in
    assert any(d["pos"] != "" for d in data)
    html = clip_gallery.render_from_data(data)
    assert "Clip gallery" in html and "DATA =" in html


def test_gallery_empty_when_no_catalog(client, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "MANIFEST", str(tmp_path / "missing.csv"))
    r = client.get("/api/gallery")
    assert r.status_code == 200 and "No catalog yet" in r.text


def test_gallery_serves_html_with_rewritten_urls(client, smoke_manifest, monkeypatch):
    monkeypatch.setattr(webapp, "MANIFEST", smoke_manifest)
    r = client.get("/api/gallery")
    assert r.status_code == 200
    body = r.text
    assert "shot000.mp4" in body                  # a real clip name from _smoke
    assert "/catalog-files/" in body              # thumb rewritten to served URL
    assert "/api/clip?path=" in body              # clip link routed through the API


def test_clip_endpoint_blocks_paths_outside_media(client):
    r = client.get("/api/clip", params={"path": "/etc/passwd"})
    assert r.status_code == 403


def test_clip_endpoint_404_for_missing_inside_media(client):
    r = client.get("/api/clip", params={"path": os.path.join(webapp.MEDIA, "nope.mp4")})
    assert r.status_code == 404


# ── progress liveness + actionable prompts ───────────────────────────────────
def test_arrange_missing_prereq_streams_error_event(client, tmp_path, monkeypatch):
    """A missing analysis must arrive as a clean SSE error event (not a dead
    stream), carrying the 'run Analyze' prompt for the UI to surface."""
    monkeypatch.setattr(webapp, "CATALOG_AUDIO", str(tmp_path / "catalog_audio"))
    monkeypatch.setattr(webapp, "MANIFEST", str(tmp_path / "manifest.csv"))
    body = client.get("/api/arrange", params={"track": "Ghostsong.mp3"}).text
    assert '"error": true' in body or '"error":true' in body
    assert "Analyze" in body


def test_index_has_indeterminate_and_error_handling(client):
    html = client.get("/").text
    assert "removeAttribute('value')" in html      # indeterminate <progress>
    assert "ev.error" in html                        # error events are handled


def test_tkapp_has_progress_and_prompt_machinery():
    pytest.importorskip("tkinter")
    import tkapp
    for attr in ("_pb_tick", "_begin", "_end"):
        assert hasattr(tkapp.App, attr), attr


# ── cancellation: web job registry + endpoint ────────────────────────────────
def test_cancel_unknown_job_is_noop(client):
    """Cancelling a finished/unknown id is a graceful no-op, not an error."""
    r = client.get("/api/cancel", params={"job": "does-not-exist"})
    assert r.status_code == 200 and r.json()["cancelled"] is False


def test_cancel_sets_registered_job_event(client):
    """/api/cancel sets the cancel Event the engine watches for the given job."""
    import threading
    ev = threading.Event()
    webapp.JOBS["testjob"] = ev
    try:
        r = client.get("/api/cancel", params={"job": "testjob"})
        assert r.json()["cancelled"] is True
        assert ev.is_set()
    finally:
        webapp.JOBS.pop("testjob", None)


def test_index_has_cancel_control(client):
    html = client.get("/").text
    assert "cancelbtn" in html and "cancelJob" in html   # button + handler
    assert "/api/cancel" in html                          # wired to the endpoint
    assert "ev.cancelled" in html                         # clean-stop event handled


def test_tkapp_has_cancel_machinery():
    pytest.importorskip("tkinter")
    import tkapp
    assert hasattr(tkapp.App, "cancel_stage")


# ── timeline export (OTIO/FCPXML) wiring ─────────────────────────────────────
def test_export_missing_arrange_streams_error_event(client, tmp_path, monkeypatch):
    """Exporting with no arrangement yields a clean SSE error event prompting
    Arrange (not a dead stream) — and doesn't need OTIO to reach that path."""
    monkeypatch.setattr(webapp, "CATALOG_AUDIO", str(tmp_path / "catalog_audio"))
    body = client.get("/api/export", params={"track": "Ghostsong.mp3"}).text
    assert ('"error": true' in body or '"error":true' in body)
    assert "Arrange" in body


def test_index_has_export_control(client):
    html = client.get("/").text
    assert "go('export')" in html          # Export button wired
    assert "id=exports" in html            # output-links area present


def test_tkapp_has_export_flow():
    pytest.importorskip("tkinter")
    import tkapp
    assert hasattr(tkapp.App, "_export_flow")


# ── grid comparison (rank schemes by energy match) ───────────────────────────
def test_compare_missing_analysis_streams_error(client, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "CATALOG_AUDIO", str(tmp_path / "catalog_audio"))
    monkeypatch.setattr(webapp, "MANIFEST", str(tmp_path / "manifest.csv"))
    body = client.get("/api/compare", params={"track": "Ghostsong.mp3"}).text
    assert ('"error": true' in body or '"error":true' in body) and "Analyze" in body


def test_compare_endpoint_streams_ranked_table(client, tmp_path, monkeypatch,
                                               smoke_manifest):
    """End-to-end through the web tier: every grid arranges and a ranked
    comparison (with a best) comes back over SSE."""
    cat = tmp_path / "catalog_audio"
    cat.mkdir()
    write_analysis(str(cat), "/tmp/Song.mp3", track="Song")
    monkeypatch.setattr(webapp, "CATALOG_AUDIO", str(cat))
    monkeypatch.setattr(webapp, "MANIFEST", smoke_manifest)
    monkeypatch.setattr(webapp, "MEDIA", str(tmp_path))     # cut_dir lands under MEDIA
    body = client.get("/api/compare",
                      params={"track": "Song.mp3", "allow_reuse": "true"}).text
    assert '"comparison"' in body and '"best"' in body
    assert '"stage": "compare"' in body or '"stage":"compare"' in body


def test_index_has_compare_control(client):
    html = client.get("/").text
    assert "go('compare')" in html and "showComparison" in html
    assert "id=compare" in html


def test_tkapp_has_compare_flow():
    pytest.importorskip("tkinter")
    import tkapp
    assert hasattr(tkapp.App, "_compare_flow") and hasattr(tkapp.App, "_show_comparison")


# ── media library picker (switch the media root at runtime) ──────────────────
@pytest.fixture
def restore_media():
    """Snapshot + restore the media-derived globals so a set_media() test can't
    leak its tmp paths into other tests."""
    keys = ("MEDIA", "CATALOG_AUDIO", "CATALOG", "MANIFEST", "ALBUM_AUDIO",
            "SOURCES", "CLIPS")
    saved = {k: getattr(webapp, k) for k in keys}
    saved_engine = (webapp.engine.MEDIA, webapp.engine.MEDIA_SOURCE)
    yield
    for k, v in saved.items():
        setattr(webapp, k, v)
    webapp.engine.MEDIA, webapp.engine.MEDIA_SOURCE = saved_engine


def test_api_media_get_reports_current(client):
    j = client.get("/api/media").json()
    assert "media" in j and "ok" in j and "source" in j


def test_api_media_set_switches_and_recomputes_paths(client, tmp_path, monkeypatch,
                                                     restore_media):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    lib = tmp_path / "lib"
    lib.mkdir()
    r = client.post("/api/media", data={"path": str(lib)})
    assert r.status_code == 200 and r.json()["media"] == str(lib)
    # the derived globals followed the switch
    assert webapp.MEDIA == str(lib)
    assert webapp.CATALOG == os.path.join(str(lib), "catalog")
    assert client.get("/api/media").json()["media"] == str(lib)


def test_api_media_rejects_checkout(client, restore_media):
    r = client.post("/api/media", data={"path": webapp.engine.HERE})
    assert r.status_code == 400 and "DV2MV_MEDIA" in r.json()["detail"]


def test_index_has_media_library_control(client):
    html = client.get("/").text
    assert "setMedia()" in html and "id=medianow" in html and "/api/media" in html


def test_catalog_files_route_serves_thumb(client, tmp_path, monkeypatch):
    """The gallery's thumbs/ load via this route (replaced the static mount, so
    the catalog can change at runtime)."""
    cat = tmp_path / "catalog"
    (cat / "thumbs").mkdir(parents=True)
    (cat / "thumbs" / "a.jpg").write_bytes(b"JPEGDATA")
    monkeypatch.setattr(webapp, "CATALOG", str(cat))
    r = client.get("/catalog-files/thumbs/a.jpg")
    assert r.status_code == 200 and r.content == b"JPEGDATA"
    assert client.get("/catalog-files/thumbs/missing.jpg").status_code == 404


def test_tkapp_has_media_library_controls():
    pytest.importorskip("tkinter")
    import tkapp
    for attr in ("choose_media", "_ensure_media", "_refresh_lib_label"):
        assert hasattr(tkapp.App, attr), attr


def test_tk_lib_label_shows_current_media(app):
    assert "Library:" in app.lib_label.cget("text")


# ── gallery "use selection" add/remove/replace ───────────────────────────────
def test_edit_project_clips_add_remove_replace(client, tmp_path, monkeypatch):
    import engine
    monkeypatch.setattr(webapp, "MEDIA", str(tmp_path))
    engine.new_project(str(tmp_path), "P", "t.mp3", clips=["/a.mp4", "/b.mp4"])
    r = client.post("/api/projects/P/clips", data={"op": "add", "clips": ["/c.mp4"]})
    assert r.status_code == 200 and r.json()["clips"] == 3
    r = client.post("/api/projects/P/clips", data={"op": "remove", "clips": ["/a.mp4"]})
    assert r.json()["clips"] == 2
    r = client.post("/api/projects/P/clips", data={"op": "replace", "clips": ["/z.mp4"]})
    assert r.json()["clips"] == 1
    # the change persisted to the project on disk
    assert engine.load_project(str(tmp_path), "P").clips == ["/z.mp4"]


def test_edit_project_clips_unknown_project_404(client, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "MEDIA", str(tmp_path))
    r = client.post("/api/projects/nope/clips", data={"op": "add", "clips": ["/a.mp4"]})
    assert r.status_code == 404


def test_gallery_layer_has_edit_controls(client, smoke_manifest, monkeypatch):
    monkeypatch.setattr(webapp, "MANIFEST", smoke_manifest)
    html = client.get("/api/gallery").text
    assert "peditsel" in html and "_editProject" in html
    assert "/clips" in html                       # posts to the edit endpoint


def test_tk_set_project_clips_add_remove_replace(app, tmp_path):
    import engine
    p = engine.Project(name="P", track="t.mp3", clips=["/a.mp4", "/b.mp4"],
                       dir=str(tmp_path / "p"))
    app._set_project(p)
    try:
        app._set_project_clips(["/c.mp4"], op="add")
        assert set(app.project.clips) == {"/a.mp4", "/b.mp4", "/c.mp4"}
        app._set_project_clips(["/a.mp4"], op="remove")
        assert set(app.project.clips) == {"/b.mp4", "/c.mp4"}
        app._set_project_clips(["/z.mp4"], op="replace")
        assert app.project.clips == ["/z.mp4"]
    finally:
        app._set_project(None)                    # leave the shared app clean


# ── media-root guard surfaces over the web tier ──────────────────────────────
def test_media_guard_streams_actionable_error(client, monkeypatch):
    """If the media root is the code checkout (DV2MV_MEDIA unset), a stage stream
    surfaces an actionable error naming DV2MV_MEDIA — not a downstream symptom."""
    monkeypatch.setattr(webapp.engine, "MEDIA", webapp.engine.HERE)
    monkeypatch.setattr(webapp.engine, "MEDIA_FROM_ENV", False)
    body = client.get("/api/analyze", params={"track": "x.mp3"}).text
    assert ('"error": true' in body or '"error":true' in body)
    assert "DV2MV_MEDIA" in body


def test_tk_cancel_button_present_and_idle_safe(app):
    """Cancel is disabled while idle, and pressing it with nothing running is a
    safe no-op (must not raise or arm a phantom stop)."""
    assert hasattr(app, "cancel_btn")
    assert str(app.cancel_btn.cget("state")) == "disabled"
    app.cancel_stage()                                    # no active stage
    assert str(app.cancel_btn.cget("state")) == "disabled"
    assert app._cancel is None


# ── Tk clip gallery ──────────────────────────────────────────────────────────
def test_tk_gallery_path_helpers():
    pytest.importorskip("tkinter")
    import tkapp
    mp = os.path.join("x", "catalog", "manifest.csv")
    # thumb path resolves relative to the manifest's own directory
    assert tkapp.gallery_thumb_path(mp, "thumbs/a.jpg") == \
        os.path.abspath(os.path.join("x", "catalog", "thumbs", "a.jpg"))
    assert tkapp.gallery_thumb_path(mp, "") == ""
    # clip path strips the file:// the gallery data builder adds
    assert tkapp.gallery_clip_path({"clip": "file:///v/clips/a.mp4"}) == "/v/clips/a.mp4"
    assert tkapp.gallery_clip_path({"clip": "/v/clips/b.mp4"}) == "/v/clips/b.mp4"
    assert tkapp.gallery_clip_path({}) == ""


def test_tk_gallery_window_and_button_present():
    pytest.importorskip("tkinter")
    import tkapp
    assert hasattr(tkapp, "GalleryWindow")
    for attr in ("_load_chunk", "_add_card", "_wheel", "_bind_wheel"):
        assert hasattr(tkapp.GalleryWindow, attr), attr
    assert hasattr(tkapp.App, "open_gallery")


# ── Tk arrange options dialog (IRIX-themed) ──────────────────────────────────
def test_tk_project_wiring_present():
    pytest.importorskip("tkinter")
    import tkapp
    assert hasattr(tkapp, "NewProjectDialog")
    for attr in ("new_project_dialog", "open_project_dialog", "_set_project",
                 "_arrange_project_flow"):
        assert hasattr(tkapp.App, attr), attr


def test_tk_set_project_updates_label(app, tmp_path):
    import engine
    try:
        p = engine.Project(name="Demo", track="X.mp3", clips="all", dir=str(tmp_path))
        app._set_project(p)
        assert "Demo" in app.proj_label.cget("text")
        assert app.track.get() == "X.mp3"
        app._set_project(None)
        assert "library mode" in app.proj_label.cget("text")
    finally:
        app._set_project(None)                # leave shared app in a clean state


def test_tk_new_project_dialog_builds(app, tmp_path):
    import tkapp
    captured = {}
    dlg = tkapp.NewProjectDialog(
        app, str(tmp_path), "02 Erased.mp3",
        on_ok=lambda n, t, c: captured.update(name=n, track=t, clips=c))
    try:
        app.update_idletasks()
        assert dlg.clips() == "all"           # default scope = whole library
        dlg.v_name.set("My Project")
        dlg._ok()
        assert captured == {"name": "My Project", "track": "02 Erased.mp3", "clips": "all"}
    finally:
        if dlg.winfo_exists():
            dlg.destroy()


# ── web projects ─────────────────────────────────────────────────────────────
@pytest.fixture
def proj_client(tmp_path, monkeypatch, smoke_manifest):
    """A TestClient over a tmp media tree with an analysis + library manifest."""
    import shutil
    media = tmp_path / "media"
    (media / "catalog_audio").mkdir(parents=True)
    (media / "catalog").mkdir()
    write_analysis(str(media / "catalog_audio"), "/tmp/Song.mp3", track="Song")
    shutil.copy(smoke_manifest, media / "catalog" / "manifest.csv")
    monkeypatch.setattr(webapp, "MEDIA", str(media))
    monkeypatch.setattr(webapp, "MANIFEST", str(media / "catalog" / "manifest.csv"))
    monkeypatch.setattr(webapp, "CATALOG_AUDIO", str(media / "catalog_audio"))
    return TestClient(webapp.app), str(media)


def test_web_sources_lists_tapes(proj_client):
    client, _ = proj_client
    src = client.get("/api/sources").json()["sources"]
    assert src and all(isinstance(v, int) for v in src.values())


def test_web_create_and_list_projects(proj_client):
    client, _ = proj_client
    r = client.post("/api/projects", data={"name": "Web Cut", "track": "Song.mp3"})
    assert r.status_code == 200 and r.json()["clips"] == "all"
    names = [p["name"] for p in client.get("/api/projects").json()["projects"]]
    assert "Web Cut" in names


def test_web_create_project_scoped_by_source(proj_client):
    client, _ = proj_client
    one = next(iter(client.get("/api/sources").json()["sources"]))
    r = client.post("/api/projects",
                    data={"name": "Scoped", "track": "Song.mp3", "sources": [one]})
    assert r.status_code == 200 and isinstance(r.json()["clips"], int) and r.json()["clips"] > 0


def test_web_arrange_within_project(proj_client):
    client, media = proj_client
    client.post("/api/projects", data={"name": "P", "track": "Song.mp3"})
    body = client.get("/api/arrange", params={"project": "P", "grid": "sections"}).text
    done = [ln for ln in body.splitlines() if '"done": true' in ln][-1]
    import json
    ev = json.loads(done.split("data: ", 1)[1])
    assert ev["result"]["summary"]["grid"] == "sections"
    # outputs landed in the project's own folder, tagged grid-match
    assert os.path.exists(os.path.join(media, "projects", "P", "render-Song-sections-energy.sh"))


def test_web_index_has_project_controls(client):
    html = client.get("/").text
    assert "id=project" in html and "id=psources" in html and "createProject" in html


def test_web_tracks_endpoint_and_datalist(proj_client, tmp_path):
    client, media = proj_client
    os.makedirs(os.path.join(media, "album-audio"), exist_ok=True)
    for name in ("02 Erased.mp3", "05 Of Ash.m4a", "notes.txt"):
        open(os.path.join(media, "album-audio", name), "w").close()
    tracks = client.get("/api/tracks").json()["tracks"]
    assert tracks == ["02 Erased.mp3", "05 Of Ash.m4a"]      # audio only, sorted
    html = client.get("/").text
    assert "list=tracklist" in html and 'id=tracklist' in html and "loadTracks" in html


def test_web_arrange_repoints_project_track(proj_client):
    """A project bound to the wrong track arranges fine when the request carries
    the right one — the track box wins and the project is re-pointed + saved."""
    import json
    import engine
    client, media = proj_client
    client.post("/api/projects", data={"name": "RP", "track": "Wrong.mp3"})
    body = client.get("/api/arrange",
                      params={"project": "RP", "track": "Song.mp3", "grid": "sections"}).text
    done = [ln for ln in body.splitlines() if '"done": true' in ln][-1]
    ev = json.loads(done.split("data: ", 1)[1])
    assert ev.get("result", {}).get("summary", {}).get("track") == "Song"   # used Song
    assert engine.load_project(media, "RP").track == "Song.mp3"             # re-pointed


def test_web_create_project_from_explicit_clips(proj_client):
    client, _ = proj_client
    clips = ["/v/clips/a.mp4", "/v/clips/b.mp4", "/v/clips/c.mp4"]
    r = client.post("/api/projects",
                    data={"name": "From Gallery", "track": "Song.mp3", "clips": clips})
    assert r.status_code == 200 and r.json()["clips"] == 3


def test_web_favicon_served_and_linked(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.content[:4] in (b"\x00\x00\x01\x00", b"\x89PNG")
    assert 'rel="icon"' in client.get("/").text


def test_icons_vendored():
    icons = os.path.join(REPO, "assets", "icons")
    assert os.path.exists(os.path.join(icons, "favicon.ico"))
    assert os.path.exists(os.path.join(icons, "dv2mv-256.png"))


def test_grid_help_covers_all_grids():
    pytest.importorskip("tkinter")
    import engine
    import tkapp
    assert set(tkapp.GRIDS) == set(engine.GRID_HELP)     # every grid has a note
    assert all(engine.GRID_HELP.values())


def test_web_index_injects_grid_help(client):
    html = client.get("/").text
    assert "id=gridhelp" in html and "one cut per song section" in html
    assert "/*GRIDHELP*/" not in html and '"downbeats"' in html   # placeholder filled


def test_web_hero_served_and_shown(client):
    r = client.get("/hero.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert '/hero.jpg' in client.get("/").text
    assert os.path.exists(os.path.join(REPO, "assets", "img", "cameraman.jpg"))


def test_web_gallery_has_selection_layer(proj_client):
    client, _ = proj_client
    html = client.get("/api/gallery").text
    assert "id=seltoolbar" in html and "Create project from selection" in html
    assert "/api/projects" in html        # the create POST target
    assert "card.classList" in html       # click-to-select wiring


def test_tk_gallery_selection_and_apply(app, smoke_manifest):
    pytest.importorskip("PIL")
    import tkapp
    applied = {}
    g = tkapp.GalleryWindow(app, smoke_manifest,
                            on_apply=lambda c, op: applied.update(clips=c, op=op))
    try:
        for _ in range(80):                 # pump the chunked loader to completion
            app.update()
            if not g._queue:
                break
        assert g._cells, "no thumbnails loaded"
        picks = sorted(g._cells)[:2]
        for c in picks:
            g.toggle(c)
        assert g.selected == set(picks)
        g.toggle(picks[0])                  # toggling again deselects
        assert g.selected == {picks[1]}
        g.select_all()
        assert g.selected == set(g._cells)
        g.clear_selection()
        assert g.selected == set()
        for c in picks:
            g.toggle(c)
        g._apply("add")                     # on_apply gets (sorted selection, op); destroys g
        assert applied["clips"] == sorted(picks) and applied["op"] == "add"
    finally:
        if g.winfo_exists():
            g.destroy()


def test_tk_new_project_dialog_preset_clips(app, tmp_path):
    import tkapp
    captured = {}
    dlg = tkapp.NewProjectDialog(
        app, str(tmp_path), "Song.mp3",
        on_ok=lambda n, t, c: captured.update(name=n, track=t, clips=c),
        clips=["/a.mp4", "/b.mp4"])
    try:
        app.update_idletasks()
        # a preset selection defaults to the 'selected' scope and returns as-is
        assert dlg.clips() == ["/a.mp4", "/b.mp4"]
        dlg.v_name.set("Gallery Cut")
        dlg._ok()
        assert captured == {"name": "Gallery Cut", "track": "Song.mp3",
                            "clips": ["/a.mp4", "/b.mp4"]}
    finally:
        if dlg.winfo_exists():
            dlg.destroy()


def test_tk_drain_collects_gc_on_main_thread(app, monkeypatch):
    """With cyclic GC disabled (so it can't fire on a worker thread and abort
    via Tcl_AsyncDelete), the UI pump must reclaim cycles on the main thread."""
    import gc as _gc
    calls = []
    monkeypatch.setattr(_gc, "collect", lambda *a: calls.append(1))
    was = _gc.isenabled()
    _gc.disable()
    try:
        app._gc_tick = 0
        for _ in range(50):
            app._drain()
        assert calls, "pump did not gc.collect() on the main thread"
    finally:
        if was:
            _gc.enable()


def test_tk_arrange_options_present():
    pytest.importorskip("tkinter")
    import tkapp
    # all four grids are offered (the thing the user called out)
    assert tkapp.GRIDS == ["sections", "downbeats", "beats", "harmonic"]
    assert hasattr(tkapp, "ArrangeOptions")
    for attr in ("params", "_ok", "_sync"):
        assert hasattr(tkapp.ArrangeOptions, attr), attr
    assert hasattr(tkapp.App, "_open_arrange_options")


def test_tk_irix_theme_helper():
    pytest.importorskip("tkinter")
    import tkapp
    assert callable(tkapp.apply_irix_theme)
    assert {"bg", "light", "dark", "select", "fg", "field"} <= set(tkapp.IRIX)
    # IRIX menus/labels are slanted (oblique); the shell font stays fixed-width
    assert "italic" in tkapp.IRIX_MENU_FONT


def test_pick_irix_font_uses_sgi_font_else_falls_back():
    pytest.importorskip("tkinter")
    import tkapp
    # SGI font present -> use it
    got = tkapp.pick_irix_font({"Irix Screen Mono 15", "Helvetica"})
    assert got == ("Irix Screen Mono 15", tkapp.IRIX_FONT_SIZE)
    # absent -> mono fallback, never crashes
    fb = tkapp.pick_irix_font({"Helvetica"})
    assert fb == (tkapp.IRIX_FONT_FALLBACK, tkapp.IRIX_FONT_SIZE)


def test_format_arrange_summary():
    pytest.importorskip("tkinter")
    import tkapp
    meta = {"track": "Song", "grid": "beats", "cuts": 50, "clips": 300,
            "energy_match_pct": 88.0, "allow_reuse": True, "beats_per_cut": 2,
            "drop_blurry": 40.0, "clip_from": "start"}
    s = tkapp.format_arrange_summary(meta)
    assert "beats grid" in s and "50 cuts" in s and "88.0% energy match" in s
    assert "reuse" in s and "2 beats/cut" in s and "drop<40.0" in s and "clip-from start" in s
    # non-beats grid omits beats/cut; no-reuse omits reuse
    s2 = tkapp.format_arrange_summary(
        {"grid": "sections", "cuts": 12, "clips": 9, "energy_match_pct": 91.0,
         "allow_reuse": False, "clip_from": "middle"})
    assert "beats/cut" not in s2 and "reuse" not in s2


def test_index_shows_arrange_summary(client):
    html = client.get("/").text
    assert "id=summary" in html and "ev.result.summary" in html


def test_index_has_arrange_form(client):
    html = client.get("/").text
    for ctrl in ("id=grid", "id=bpc", "id=reuse", "id=blur", "id=clipfrom"):
        assert ctrl in html, ctrl
    assert "arrangeQuery" in html and "syncGrid" in html
    # all four grids are offered
    for g in ("sections", "downbeats", "beats", "harmonic"):
        assert f"value={g}" in html


def test_arrange_form_params_flow_through(client, tmp_path, monkeypatch, smoke_manifest):
    """The form's params reach engine.arrange and come back in the summary."""
    cat = str(tmp_path / "catalog_audio")
    os.makedirs(cat)
    monkeypatch.setattr(webapp, "CATALOG_AUDIO", cat)
    monkeypatch.setattr(webapp, "MANIFEST", smoke_manifest)
    write_analysis(cat, "/tmp/Foo.mp3", track="Foo")        # Foo.analysis.json
    r = client.get("/api/arrange", params={
        "track": "Foo.mp3", "grid": "beats", "beats_per_cut": 3,
        "allow_reuse": "true", "drop_blurry": 0, "clip_from": "start"})
    done = [ln for ln in r.text.splitlines() if '"done": true' in ln][-1]
    import json
    ev = json.loads(done.split("data: ", 1)[1])
    s = ev["result"]["summary"]
    assert s["grid"] == "beats" and s["beats_per_cut"] == 3
    assert s["allow_reuse"] is True and s["clip_from"] == "start"


def test_player_command_honors_preferred_player():
    pytest.importorskip("tkinter")
    import tkapp
    p = "/v/clips/a.mp4"
    # macOS: default vs forced app
    assert tkapp.player_command(p, "", "Darwin") == ["open", p]
    assert tkapp.player_command(p, "VLC", "Darwin") == ["open", "-a", "VLC", p]
    # linux: xdg-open vs explicit command
    assert tkapp.player_command(p, "", "Linux") == ["xdg-open", p]
    assert tkapp.player_command(p, "mpv", "Linux") == ["mpv", p]
    # windows: None means caller uses os.startfile; override runs the app
    assert tkapp.player_command(p, "", "Windows") is None
    assert tkapp.player_command(p, "vlc.exe", "Windows") == ["vlc.exe", p]


def test_irix_font_vendored_in_repo():
    # the CC0 font ships with the project so the look is reproducible
    fonts = os.path.join(REPO, "assets", "fonts")
    assert os.path.exists(os.path.join(fonts, "IrixScreenMono15.ttf"))
    assert os.path.exists(os.path.join(fonts, "license.txt"))


# ── render resolves the suffixed arrange output (the uploaded-track chain) ────
@pytest.fixture
def render_client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "CATALOG_AUDIO", str(tmp_path / "catalog_audio"))
    os.makedirs(webapp.CATALOG_AUDIO, exist_ok=True)
    return TestClient(webapp.app)


def test_render_without_arrange_streams_prompt(render_client):
    body = render_client.get("/api/render", params={"track": "Ghost.mp3"}).text
    assert ("error" in body) and "Arrange" in body


def test_render_finds_suffixed_script_and_completes(render_client):
    # a fake render-<track>-beats.sh (no ffmpeg): echo a progress marker and
    # create the cut the engine verifies. Exercises the suffix resolution.
    cat = webapp.CATALOG_AUDIO
    script = os.path.join(cat, "render-My Song-beats.sh")
    with open(script, "w") as fh:
        fh.write('#!/usr/bin/env bash\nset -e\n'
                 'echo "[1/1] fake segment"\n'
                 ': > "cut-My Song-beats.mp4"\n')
    os.chmod(script, 0o755)
    body = render_client.get("/api/render", params={"track": "My Song.mp3"}).text
    assert '"done": true' in body
    assert "error" not in body
    assert os.path.exists(os.path.join(cat, "cut-My Song-beats.mp4"))
    # the done event hands back the cut path for the <video> to play via /api/clip
    assert "cut-My Song-beats.mp4" in body


# ── help ─────────────────────────────────────────────────────────────────────

def test_help_endpoint_serves_help_md(client):
    r = client.get("/api/help")
    assert r.status_code == 200
    assert "Arrange" in r.text and r.text.startswith("# dv2mv Help")
    # the index page has the button + panel that render it
    page = client.get("/").text
    assert "toggleHelp" in page and "helppanel" in page


def test_help_parser_spans():
    tkinter = pytest.importorskip("tkinter")  # noqa: F841  (import side only)
    from tkapp import parse_help
    spans = parse_help("# Title\n## Sect\n- a **b** c\n\nplain `x`")
    assert spans[0] == ("h1", "Title\n")
    assert spans[1] == ("h2", "Sect\n")
    assert ("bold", "b") in spans and ("code", "x") in spans
    # the bullet keeps its marker and the line ends with a newline span
    assert ("text", "  • ") in spans and spans[-1] == ("text", "\n")
    # hard-wrapped continuation lines reflow into their paragraph/bullet
    from tkapp import _unwrap
    assert _unwrap("one\ntwo\n\n- b\n  cont\n# H") == \
        ["one two", "", "- b cont", "# H"]


def test_tk_help_window_renders_and_is_singleton(app):
    app.open_help()
    w = app._help_win
    try:
        assert w.winfo_exists()
        body = w.text.get("1.0", "end")
        assert "Quick start" in body and "Arrange" in body
        assert w.text.cget("state") == "disabled"     # read-only
        app.open_help()                               # raises, doesn't stack
        assert app._help_win is w
        # build id lives in the help footer now (banner stays clean)
        assert w.info_label.cget("text").startswith("dv2mv ")
    finally:
        if w.winfo_exists():
            w.destroy()


def test_tk_help_window_jumps_to_section(app):
    """The Latham… button deep-links the OBS live-shoot section."""
    app.open_help(jump="Latham")
    w = app._help_win
    try:
        w.update_idletasks()
        assert any("Latham" in h for h in w.anchors)
        assert "OBS" in w.text.get("1.0", "end")
        assert w.text.yview()[0] > 0            # actually scrolled off the top
    finally:
        if w.winfo_exists():
            w.destroy()


def test_release_info_in_checkout():
    """In a git checkout the help footer identifies the build by commit."""
    from tkapp import _release_info
    info = _release_info()
    assert info and "·" in info                  # "<sha> · <date>"


def test_help_md_covers_every_stage_button():
    with open(os.path.join(REPO, "HELP.md"), encoding="utf-8") as fh:
        md = fh.read()
    for topic in ("Analyze", "Arrange", "Compare", "Render", "Export",
                  "Gallery", "Tempo", "Media library", "Thumbnail"):
        assert topic in md, f"HELP.md says nothing about {topic}"


# ── thumbnails ───────────────────────────────────────────────────────────────

def test_thumbnails_endpoint_streams_and_persists_exclude(client, monkeypatch):
    def fake(manifest, out, per_group=8, exclude_re="", cancel=None):
        yield webapp.engine.ProgressEvent(
            "thumbnails", "ok", 1.0, True,
            {"contact": "/x/_contact.jpg", "out_dir": "/x"})
    monkeypatch.setattr(webapp.engine, "thumbnails", fake)
    body = client.get("/api/thumbnails", params={"exclude": "^private"}).text
    assert '"done": true' in body and "_contact.jpg" in body
    assert webapp.engine.load_config().get("thumbs_exclude") == "^private"

    # param omitted -> the saved filter flows through to the stage
    seen = {}
    def spy(manifest, out, per_group=8, exclude_re="", cancel=None):
        seen["exclude"] = exclude_re
        yield webapp.engine.ProgressEvent("thumbnails", "ok", 1.0, True, {})
    monkeypatch.setattr(webapp.engine, "thumbnails", spy)
    client.get("/api/thumbnails")
    assert seen["exclude"] == "^private"


def test_index_has_thumbnail_button(client):
    page = client.get("/").text
    assert "goThumbs" in page and "id=thumbexcl" in page and "contactimg" in page
    assert "__THUMBEXCL__" not in page            # placeholder filled


def test_tk_thumbnail_dialog_returns_options(app):
    import tkapp
    got = {}
    dlg = tkapp.ThumbnailDialog(app, "^white",
                                on_ok=lambda p, e: got.update(per=p, excl=e))
    try:
        app.update_idletasks()
        assert dlg.v_excl.get() == "^white"       # saved filter prefilled
        dlg.v_per.set(5)
        dlg._ok()
        assert got == {"per": 5, "excl": "^white"}
    finally:
        if dlg.winfo_exists():
            dlg.destroy()
    assert hasattr(tkapp.App, "open_thumbnails")  # the button's target


# ── preflight + interactive tour (required vs recommended tooling) ──────────
def test_api_preflight_returns_tool_list(client):
    r = client.get("/api/preflight")
    assert r.status_code == 200
    j = r.json()
    assert {"ok", "tools", "summary", "clip_install"} <= set(j.keys())
    names = {t["name"] for t in j["tools"]}
    assert {"ffmpeg", "ffprobe", "rubberband"} <= names
    assert all("install" in t for t in j["tools"])


def test_api_tour_returns_steps_with_targets(client):
    r = client.get("/api/tour")
    assert r.status_code == 200
    steps = r.json()["steps"]
    assert len(steps) >= 7
    for s in steps:
        assert {"title", "target", "body", "cue"} <= set(s.keys())


def test_index_has_tour_and_preflight_controls(client):
    html = client.get("/").text
    assert "startTour()" in html and "/api/tour" in html
    assert "togglePreflight()" in html and "/api/preflight" in html
    assert "data-tour=media-library" in html
    assert "data-tour=add-track" in html and "data-tour=add-footage" in html
    assert "data-tour=arrange" in html and "data-tour=render" in html
    assert "data-tour=export" in html and "data-tour=gallery" in html
    # "Copy install command" only renders when something is missing; its JS is
    # always in the page so the missing-tool path can fire.
    assert "Copy install command" in html and "copyInstall" in html


def test_tkapp_has_preflight_and_tour_methods():
    pytest.importorskip("tkinter")
    import tkapp
    for attr in ("open_preflight", "open_tour"):
        assert hasattr(tkapp.App, attr), attr
    for cls in ("PreflightDialog", "TourDialog"):
        assert hasattr(tkapp, cls), cls


def test_tk_preflight_dialog_lists_tools_and_has_copy_button(app):
    import tkinter as tk, engine, tkapp
    dlg = tkapp.PreflightDialog(app)
    try:
        app.update_idletasks()
        assert dlg.summary.cget("text")                          # non-empty summary
        # walk every Label in the grid; one per tool must carry its name
        names = ""
        for child in dlg.grid.winfo_children():
            for lab in child.winfo_children():
                try:
                    names += " " + str(lab.cget("text"))
                except tk.TclError:
                    pass
        assert "ffmpeg" in names and "ffprobe" in names and "rubberband" in names
        # copy button exists; its enabled/disabled state mirrors the engine result
        assert dlg.copy_btn is not None
        p = engine.preflight()
        missing = any(not t["found"] and t["install"] for t in p["tools"])
        assert (dlg.copy_btn.cget("state") == "normal") == missing
    finally:
        dlg.destroy()


def test_tk_tour_dialog_steps_through_engine_tour(app):
    import engine, tkapp
    dlg = tkapp.TourDialog(app)
    try:
        app.update_idletasks()
        assert dlg._i == 0
        assert dlg._steps is engine.TOUR_STEPS
        # step 0 ("What dv2mv does") targets "root" -> no highlight rectangle
        assert dlg._steps[0]["target"] == "root"
        dlg.next()
        assert dlg._i == 1
        assert dlg._steps[1]["target"] == "media-library"
        # all later targets must resolve to a registered widget (no step orphaned)
        for step in dlg._steps:
            assert step["target"] == "root" or \
                   step["target"] in app._tour_targets, \
                   f"tour target '{step['target']}' has no registered widget"
        # back goes back, closing tears down the highlight canvas
        dlg.prev(); assert dlg._i == 0
        dlg.close()
    finally:
        if dlg.winfo_exists():
            dlg.close()
