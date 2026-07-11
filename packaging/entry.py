#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
entry.py — single entry point for the packaged macOS .app (PyInstaller).

A frozen single-file app has ONE executable, but dv2mv's engine drives several
distinct programs as subprocesses (the GUI and the pipeline scripts — incl.
scene_split.py, which wraps PySceneDetect's Python API). In source mode each is
`python <thing>`; in a bundle there is no separate `python`, so `sys.executable`
IS this app. This module makes the app re-entrant: the engine spawns the app
again with a `--run-*` flag, and the dispatch below runs the requested program
instead of the GUI.

  (no flag)                → the Tkinter desktop GUI            (tkapp.main)
  --run-pipeline <name> …  → pipeline/<name>.py as __main__     (engine.SCRIPT)
  --preflight-smoke        → print preflight JSON, exit 1 if a required tool
                             is missing. Used by the release CI smoke after
                             the .app is built, so bundle-precedence is verified
                             on the actual runner output.

It also prepends the bundle's `bin/` (vendored ffmpeg/ffprobe/rubberband) to
PATH so every bare-name binary call in the engine — and the generated
`render-*.sh` bash script — resolves to the bundled binaries with no Homebrew.

In source mode (`python packaging/entry.py`) everything still works: the PATH
prepend is a no-op (no bundled bin/) and dispatch runs the same code paths.
"""
from __future__ import annotations

import multiprocessing
import os
import runpy
import sys


def _bundle_base() -> str:
    """Root of bundled resources: PyInstaller's _MEIPASS when frozen, else the
    repo root (this file's parent's parent)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return base
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _prepare_environment() -> None:
    """Make bundled binaries findable and keep numba's JIT cache writable."""
    base = _bundle_base()
    bin_dir = os.path.join(base, "bin")
    if os.path.isdir(bin_dir):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    # Source mode: this file lives in packaging/, so the repo root (engine.py,
    # tkapp.py, pipeline/) isn't on sys.path. Add it so `python packaging/entry.py`
    # is testable. Frozen mode imports these as bundled modules, so skip.
    if not getattr(sys, "frozen", False) and base not in sys.path:
        sys.path.insert(0, base)

    # numba (pulled in by librosa during Analyze) may try to write a JIT cache;
    # inside a read-only .app that fails. Point it at a writable user cache.
    if getattr(sys, "frozen", False):
        cache = os.path.join(
            os.path.expanduser("~"), "Library", "Caches", "dv2mv", "numba")
        os.makedirs(cache, exist_ok=True)
        os.environ.setdefault("NUMBA_CACHE_DIR", cache)


def _run_pipeline(name: str, argv_rest: list) -> None:
    """Run pipeline script `name` as if invoked `python <script> <argv_rest>`."""
    import engine  # frozen module; engine.SCRIPT resolves to the bundled .py
    script = engine.SCRIPT[name]
    sys.argv = [script, *argv_rest]
    runpy.run_path(script, run_name="__main__")


def _preflight_smoke() -> None:
    """Concrete fold of preflight() for the release smoke: prints preflight JSON,
    exits 0 iff every *required* tool was found (recommended missing is OK — the
    bundled app vendors rubberband when available, but the spec treats it as
    optional, so a no-rubberband build is still release-grade). The bundled-bin
    precedence is exercised end-to-end because this runs AS the frozen app, so
    _prepare_environment() already prepended <bundle>/bin to PATH and
    engine._bundle_bin() returns that path.
    """
    import json
    import engine
    p = engine.preflight()
    print(json.dumps(p, indent=2))
    sys.exit(0 if p["ok"] else 1)


def main() -> None:
    multiprocessing.freeze_support()   # required for frozen apps that fork
    _prepare_environment()

    argv = sys.argv[1:]
    if argv and argv[0] == "--run-pipeline":
        _run_pipeline(argv[1], argv[2:])
    elif argv and argv[0] == "--preflight-smoke":
        _preflight_smoke()
    else:
        import tkapp
        tkapp.main()


if __name__ == "__main__":
    main()
