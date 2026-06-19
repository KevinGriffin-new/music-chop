# Roadmap

Planned features and enhancements (things not built yet). This is distinct from
the **bug tracker** (https://todo.sr.ht/~kevin_griffin/music-chop-dv2mv), which
is for things that are broken. See [REPORTING.md](REPORTING.md) for filing bugs.

## Next
- **Projects in the web tier.** New/Open project + scoped arrange in `webapp.py`,
  mirroring the Tk flow. (The engine project model is already shared.)
- **Library-mode cuts → a dedicated `cuts/` folder** instead of `catalog_audio/`
  (output discoverability; the other half of the render-path work). Projects
  already isolate their cut under `projects/<name>/`.

## Later
- **Cancellation** of a running stage (both tiers) — stop a long render/analyze.
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
uploads/pickers; catalog gallery (web + Tk, with gallery-based clip selection);
arrange options + energy-match summary; IRIX/4Dwm Tk theme + SGI font; projects
(track + scoped clips + options) in the engine and Tk; MPL-2.0; on sr.ht.
