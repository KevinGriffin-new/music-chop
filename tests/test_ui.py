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


def test_upload_track_lands_in_album_audio(client, tmp_path):
    r = client.post("/api/upload/track",
                    files={"file": ("New Song.mp3", b"ID3\x00fake", "audio/mpeg")})
    assert r.status_code == 200
    assert r.json()["track"] == "New Song.mp3"
    assert os.path.exists(tmp_path / "album-audio" / "New Song.mp3")


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
