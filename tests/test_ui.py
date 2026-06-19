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
