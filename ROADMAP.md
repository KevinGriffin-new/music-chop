# Roadmap

Planned features and enhancements (things not built yet). This is distinct from
the **bug tracker** (https://todo.sr.ht/~kevin_griffin/music-chop-dv2mv), which
is for things that are broken. See [REPORTING.md](REPORTING.md) for filing bugs.

## Next
- **Gallery "Use selection" semantics** — currently replaces a project's clips
  wholesale; consider an add/remove mode.

## Bigger features
- **Export polish.** The export stage ships (OTIO + FCPXML, below). Follow-ups
  if Resolve import needs them: spatial/scale conform per clip, EDL as a third
  format (OTIO's cmx3600 adapter), and a UI toggle for which formats to write.

## Later
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
(track + scoped clips + options) in the engine, Tk, and the web tier;
**cancellation** of a running stage in both tiers (a `threading.Event` token
threaded through every stage; the engine terminates the subprocess process
group so a render's `ffmpeg` child dies too — web has a job registry +
`/api/cancel`, Tk has a Cancel button);
**editable-timeline export** to DaVinci Resolve (new `export` stage →
OpenTimelineIO `.otio` + FCP X `.fcpxml`, one video track of the cut clips
trimmed at the recorded source in-points + one audio track of the music; both
UIs have an Export button);
**compare timing schemes by energy match** (`compare()` arranges every grid and
ranks them by match%/cuts/clips, leaving each variant ready to render/export;
both UIs have a "Compare grids" button that preselects the winner);
**incremental "Add footage"** (`catalog(append=True)` feature-extracts only the
clips not already in the manifest and appends them — existing rows + histograms
carried forward — so adding a tape doesn't re-process the whole library);
**media-root guard + in-app library picker** (refuses to treat the code checkout
as the media root with an actionable message; both UIs have a Media library
control to choose the folder at runtime, remembered in `~/.config/dv2mv`;
precedence DV2MV_MEDIA env > saved choice > cwd);
MPL-2.0; on sr.ht.
