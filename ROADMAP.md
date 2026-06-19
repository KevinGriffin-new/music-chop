# Roadmap

Planned features and enhancements (things not built yet). This is distinct from
the **bug tracker** (https://todo.sr.ht/~kevin_griffin/music-chop-dv2mv), which
is for things that are broken. See [REPORTING.md](REPORTING.md) for filing bugs.

## Next
- **Cancellation** of a running stage (both tiers) — stop a long render/analyze.

## Later
- **Incremental catalog** — "Add footage" should feature-extract only the *new*
  clips and append to the manifest, instead of re-cataloging the whole library.
- **Gallery "Use selection" semantics** — currently replaces a project's clips
  wholesale; consider an add/remove mode.
- **Web E2E tests** with Playwright (the web tier is a real web app; server-side
  is already covered by FastAPI TestClient).
- **Packaging** — bundle the Tk tier as a double-click app (PyInstaller/py2app;
  the pain is librosa/numba/opencv/scenedetect + a bundled ffmpeg, and macOS
  codesigning/notarization). The web tier ships from a venv.

## Done (for context)
Hardened engine (6 stages, fail-loud, real progress, provenance); both UIs with
uploads/pickers; catalog gallery + gallery-based clip selection (Tk + web);
library cuts collected in a cuts/ folder;
arrange options + energy-match summary; IRIX/4Dwm Tk theme + SGI font; projects
(track + scoped clips + options) in the engine, Tk, and the web tier; MPL-2.0;
on sr.ht.
