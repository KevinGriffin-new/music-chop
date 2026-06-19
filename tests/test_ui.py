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
    # outputs landed in the project's own folder
    assert os.path.exists(os.path.join(media, "projects", "P", "render-Song.sh"))


def test_web_index_has_project_controls(client):
    html = client.get("/").text
    assert "id=project" in html and "id=psources" in html and "createProject" in html


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
                            on_apply=lambda c: applied.update(clips=c))
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
        g._apply()                          # on_apply gets the sorted selection (destroys g)
        assert applied["clips"] == sorted(picks)
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
