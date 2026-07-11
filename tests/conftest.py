# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""Shared fixtures for the dv2mv engine tests.

The heavy stages (ingest/detect/catalog/analyze/render) shell out to real
ffmpeg / scenedetect / librosa / opencv. Each integration test is guarded by a
skip marker so the suite still runs (and the pure-logic tests still pass) on a
machine that's missing one of those. Fixtures generate tiny throwaway media so
nothing depends on the user's actual footage.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

# Make `import engine` work from dv2mv/tests/ without installing anything.
DV2MV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DV2MV_DIR not in sys.path:
    sys.path.insert(0, DV2MV_DIR)

# Point the media root at a throwaway dir BEFORE importing engine (which reads
# DV2MV_MEDIA at import). This keeps the suite hermetic — no test writes into the
# checkout — and means engine.MEDIA never resolves to the repo, so the media-root
# guard doesn't fire mid-test. (Honors an already-set DV2MV_MEDIA.)
os.environ.setdefault("DV2MV_MEDIA", tempfile.mkdtemp(prefix="dv2mv-test-media-"))
# Force the config dir to a throwaway too, so set_media()/save_config() during
# tests never touch the user's real ~/.config/dv2mv.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="dv2mv-test-config-")

import engine  # noqa: E402

# Project root (engine.py + pipeline/ + _smoke/ live here). Used as the _stream
# cwd in the pure tests and to locate the committed _smoke fixture.
REPO = DV2MV_DIR

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _have_lib(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


# scene_split.py uses the PySceneDetect Python API (no CLI on PATH needed)
HAVE_SCENEDETECT = _have_lib("scenedetect")
HAVE_LIBROSA = _have_lib("librosa")
HAVE_CV2 = _have_lib("cv2")
HAVE_OTIO = _have_lib("opentimelineio")

requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")
requires_scenedetect = pytest.mark.skipif(
    not (HAVE_SCENEDETECT and HAVE_FFMPEG),
    reason="scenedetect not importable / ffmpeg not on PATH")
requires_librosa = pytest.mark.skipif(not HAVE_LIBROSA, reason="librosa not importable")
requires_cv2 = pytest.mark.skipif(not HAVE_CV2, reason="opencv not importable")
requires_otio = pytest.mark.skipif(not HAVE_OTIO, reason="opentimelineio not importable")


def _make_clip(path, duration=2, size="320x240", rate=30):
    """Render a tiny, readable test video at `path`."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "30", path],
        check=True)
    return path


@pytest.fixture(scope="session")
def clips_dir(tmp_path_factory):
    """A folder with two tiny clips named like the real DV footage."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    d = tmp_path_factory.mktemp("clips")
    _make_clip(str(d / "testcam2004.05.07_20-00-01.mp4"))
    _make_clip(str(d / "testcam2004.05.07_20-00-02.mp4"))
    return str(d)


@pytest.fixture(scope="session")
def tiny_wav(tmp_path_factory):
    """A short sine tone for the analyze stage."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    p = tmp_path_factory.mktemp("audio") / "synthsong.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=5",
         "-c:a", "pcm_s16le", str(p)],
        check=True)
    return str(p)


def write_analysis(out_dir, audio_path, track="synthsong", duration=4.0):
    """Write a minimal but complete analysis.json that sync_clips can consume."""
    mid = round(duration / 2, 3)
    times = [round(i * 0.25, 3) for i in range(int(duration * 4) + 1)]
    rms = [round(0.2 + 0.6 * (t / duration), 4) for t in times]
    an = {
        "track": track,
        "path": os.path.abspath(audio_path),
        "duration_s": duration,
        "sr": 22050,
        "tempo_bpm": 120.0,
        "key": "C major",
        "beats": [round(i * 0.5, 3) for i in range(int(duration / 0.5))],
        "downbeats": [0.0, mid],
        "sections": [mid],
        "harmonic_changes": [round(mid / 2, 3), mid],
        "energy_envelope": {"hz": 4, "times": times, "rms": rms},
    }
    path = os.path.join(out_dir, f"{track}.analysis.json")
    with open(path, "w") as fh:
        json.dump(an, fh)
    return path


@pytest.fixture
def synth_analysis(tmp_path, tiny_wav):
    """A work dir containing a synthetic analysis.json (track='synthsong')."""
    return write_analysis(str(tmp_path), tiny_wav)


@pytest.fixture
def smoke_manifest():
    """The committed synthetic manifest under _smoke/ (no real clips needed)."""
    p = os.path.join(REPO, "_smoke", "catalog", "manifest.csv")
    if not os.path.exists(p):
        pytest.skip("_smoke/catalog/manifest.csv missing")
    return p


@pytest.fixture(scope="session")
def app():
    """One shared dv2mv Tk root for the whole session.

    Creating/destroying multiple tk.Tk() roots in a single process aborts the
    interpreter on macOS, so every Tk test reuses this one App (the main window,
    which is itself the root) and only makes/destroys Toplevels off it.
    """
    pytest.importorskip("tkinter")
    import tkinter as tk
    import tkapp
    try:
        a = tkapp.App()
        a.withdraw()
    except tk.TclError:
        pytest.skip("no display")
    yield a
    try:
        a.destroy()
    except Exception:
        pass
