# Roadmap

Planned features and enhancements (things not built yet). This is distinct from
the **bug tracker** (https://todo.sr.ht/~kevin_griffin/music-chop-dv2mv), which
is for things that are broken. See [REPORTING.md](REPORTING.md) for filing bugs.

## Session handoff (2026-06-21)
For the next context — where things stand:

- **CI/CD is live.** GitHub `KevinGriffin-new/music-chop` is a downstream mirror
  of sr.ht: `origin` has two push URLs, so one `git push` updates both. GitHub
  Actions (`.github/workflows/ci.yml`) runs the test suite on macOS + Linux every
  push — currently green. Push to sr.ht only with
  `git push git@git.sr.ht:~kevin_griffin/music-chop main`.
- **Key constraint:** the render stage runs a generated **bash** script, so
  native Windows isn't supported (CI is macOS+Linux; a Windows installer is out
  until the render is driven from Python — see "Render de-bash"). Packaging
  targets are therefore macOS `.app` + Linux `.deb`/`.rpm`.
- **Reviewer install today:** `environment.yml` (conda, one command, bundles
  ffmpeg+rubberband) and a web-tier `Dockerfile` (built + smoke-tested green).
- **Suggested next:** (1) packaging scaffolds — macOS `.app` (py2app/PyInstaller)
  + Linux `.deb`/`.rpm` Actions workflows (iterate on real runners:
  codesign/notarize, bundling ffmpeg/Tk); (2) the dv2mv MCP server (below);
  (3) webapp UI for the match presets + compare metrics; (4) optional render
  de-bash to unlock Windows.

## Next
- **Web E2E tests** with Playwright (the web tier is a real web app; server-side
  is already covered by FastAPI TestClient).
- **Chorus-aware arrange.** Consume the new `segments`/`chorus` analysis fields
  in `sync_clips`: tier 1, a cost term that favors high-motion clips inside
  `is_chorus` slots; tier 2, visual-motif reuse — map each section label to a
  clip family (hue/luma cluster or source tape) so the same visual family
  returns when the chorus does. Surface the chorus call in both UIs so a wrong
  call is visible before arranging.
- **Help follow-ups.** (Core shipped — see session log.) Instructive empty
  states (no footage / no analyzed track panes explain the next step), Tk
  tooltips on the ArrangeOptions dialog, web-only click-through tour later.

## Bigger features
- **Export polish.** The export stage ships (OTIO + FCPXML, below). Follow-ups
  if Resolve import needs them: spatial/scale conform per clip, EDL as a third
  format (OTIO's cmx3600 adapter), and a UI toggle for which formats to write.
- **dv2mv MCP server.** Expose the engine stages (analyze / retempo / arrange /
  compare / render / export + projects) as MCP tools — a thin third front-end
  over `engine.py` — so an agent can drive the pipeline conversationally and
  compose it with the DaVinci Resolve MCP (export → import → grade). Tool-surface
  spec sketched; ~half a day for a stdio server (`_run` drains each stage
  generator into one tool result).
- **Render de-bash.** Drive the render from Python (subprocess ffmpeg per
  segment + concat + mux) instead of emitting a bash `render-*.sh`. Removes the
  POSIX-shell dependency (unlocks native Windows), and lets render report
  progress/cancel directly rather than parsing script echoes.

## Later
- **Native installers.** **macOS `.app`/`.dmg` now builds end-to-end locally**
  (`packaging/` — PyInstaller spec + `build_macos.sh`; a re-entrant `entry.py`
  dispatcher lets the frozen app shell out to its own pipeline stages, and
  ffmpeg/ffprobe/rubberband are bundled under the app's `bin/`). Verified in a
  clean env: scenedetect/cv2, bundled ffmpeg, and the librosa→numba→llvmlite
  Analyze path all run; see README *Install (macOS app)*. **Remaining:**
  (a) **Developer ID signing + notarization** — scaffolded and gated on
  `DEVELOPER_ID`/`NOTARY_PROFILE`, just needs the Apple Developer Program
  membership (current builds are ad-hoc-signed → Gatekeeper-blocked on download);
  (b) run the build on **GitHub Actions** so the `.dmg` is a downloadable
  Release artifact (needs signing secrets); (c) **Linux** `.deb`/`.rpm`
  (Windows needs render de-bash first). conda `environment.yml` + the web
  `Dockerfile` cover reviewers in the meantime.

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
**gallery add/remove/replace** (editing a project's footage in the gallery is
now an explicit union/difference/replace — `revise_clip_selection()` — not just
a wholesale replace; both tiers);
MPL-2.0; on sr.ht.

_Session 2026-07-06 (thumbnails button):_
**Thumbnail suggestions in both tiers** — thumbnail_scout promoted from
tools/ to pipeline/ (so the frozen app ships it; PROG markers added), wrapped
as `engine.thumbnails()` (fail-loud on a missing catalog, real progress per
source group). Tk: a Thumbnails… button + options dialog (winners per group,
skip-sources regex) that opens the contact sheet in the viewer when done.
Web: a Thumbnail suggestions button + skip field, SSE progress, contact sheet
shown inline. The skip regex persists in the config (`thumbs_exclude`) and
prefills both UIs — an excluded private tape STAYS excluded. Verified live
end-to-end in the web tier against the real library.

_Session 2026-07-06 (help):_
**Help in both tiers** — one `HELP.md` at the repo root is the single source
of truth (quick start, media-library rules, every stage + arrange knob,
chorus/lyrics notes, troubleshooting). Tk: a Help button (singleton window,
markdown-lite rendered into a themed `tk.Text` via the pure `parse_help` —
headings/bullets/**bold**/`code`, hard-wrapped lines reflowed by `_unwrap`).
Web: `?` button on the hero → `/api/help` + a ~30-line client renderer of the
same subset; tooltips on the arrange controls. Bundled into the .app via the
spec datas. Tests: endpoint, parser/unwrap, HelpWindow widget, and a
HELP.md-mentions-every-stage guard.

_Session 2026-07-06 (later):_
**lyric-fused labelling** — acoustic-only labels degenerate on real band
recordings (on "05 Of Ash" one label ate 10/15 segments), so there's now an
optional lyrics layer. `pipeline/lyrics_analyze.py` (runs under a separate
`.venv-lyrics` — demucs + mlx-whisper + soundfile are NOT core deps): demucs
vocal stem → sung regions from stem RMS → each region transcribed
independently by mlx-whisper (word timestamps, no context-carry — whisper's
timestamps degenerate over long vamps) → words gated by VOCAL-STEM ENERGY,
never text stats (a real chorus vamp is textually indistinguishable from a
hallucination loop; probed on real material — the "I'm going to run" vamp is
the loudest singing in the song) → `<track>.lyrics.json` + cached
`stems/<track>.vocals16k.wav`. `track_analyze` auto-discovers the sidecar:
sung segments cluster on token-containment + acoustics fused 50/50,
instrumental segments on acoustics alone, chorus call restricted to sung
labels; segments carry per-segment `text`. Two hard-won constraints live in
the lyrics_analyze docstring: torch and mlx segfault if they share a process
(separation runs in a `--_separate` child), and the torchaudio>=2.9 save
break. Tested: fused-labelling units + a lyrics-sidecar integration test.

_Session 2026-07-06:_
**section labels + chorus call** — `track_analyze` now clusters the segments its
existing boundaries carve (mean chroma+MFCC per segment, scipy average-linkage
cut at 0.6× the tallest merge) into repeated-material labels A/B/A/B…, and calls
the chorus: the recurring label with the highest duration-weighted energy, or
no call at all when nothing recurs (a fake chorus is worse than none). Analysis
JSON gains `segments` `[{start,end,label,is_chorus,energy}]` + `chorus`
(`sections` unchanged — nothing downstream breaks); summary CSV gains a
`chorus` column ("B×2"); `--plot` shades spans by label with the chorus tinted
stronger (the eyeball QA). Chroma/MFCC now computed once and shared by key /
sections / HCDF (was 3× chroma_cqt). Tests: pure-logic labelling/chorus tests
+ an integration test on a synthesized quiet-verse/loud-chorus A/B/A/B track
(`tests/test_track_analyze.py`).

_Session 2026-06-21:_
**brightness-aware matching** — `sync_clips` weighted clip↔slot cost: motion↔song
energy (as before) plus luma-contrast and hue-variety terms that alternate
brightness/colour between adjacent cuts (`--match energy|contrast|variety`, tk
Match radio). Derived from measuring that frame brightness has ~zero correlation
with the beat on reference videos (`tools/brightness_probe.py`) — its value is
the alternation, not beat-timing.
**compare sweeps grid × match** with a trade-off table (motion-only energy
yardstick + luma_contrast/hue_variety per cell; best = winning grid-match tag).
**Retempo** — pitch-preserved BPM slider (Rubber Band R3 when installed → ffmpeg
`atempo` fallback); `engine.retempo` + a tk "Tempo…" dialog.
**timecode-correct export** — FCPXML/OTIO now bake each clip's embedded source
timecode (PySceneDetect splits carry tape TC), so Resolve imports them instead
of producing offline/empty timelines; verified by importing into Resolve.
**per-match output names** — arrangements tagged `<grid>-<match>` everywhere
(`engine.arrange_tag`) so a different match doesn't overwrite a cut;
**commit/date stamp in the Tk offline banner** (so testers know the build).
Fixes: render no longer yields a videoless cut (coalesce sub-frame grid slots +
`ffmpeg -y` so re-renders overwrite); gallery reflects an "all" clip scope on
reopen; Tempo refuses without a real analyzed track; snappier Tk buttons (idle
progress-bar animation stopped); dashed output filenames.
**GitHub mirror + macOS/Linux CI** (downstream of sr.ht; `.github/workflows/ci.yml`).
