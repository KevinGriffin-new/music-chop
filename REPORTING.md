# Reporting bugs

Issue tracker: **https://todo.sr.ht/~kevin_griffin/music-chop-dv2mv**
(SourceHut lets you file/comment by email too:
`~kevin_griffin/music-chop-dv2mv@todo.sr.ht`.)

UI apps have a lot of surface area, so a little structure makes bugs much faster
to fix. **One bug per ticket**, smallest steps that trigger it, and paste the
*exact* error text rather than paraphrasing.

## The single most useful thing

**Tk:** exceptions print to the **terminal you launched from**; the window just
looks dead ("the button does nothing"). That traceback is usually the whole
answer — paste it.

**Web:** check the **uvicorn console** and the **browser devtools console**
(Cmd-Opt-I → Console). The page also logs each stage line and an SSE `⚠` line on
errors.

## Where each stage writes (helps locate "missing output" bugs)

| stage   | writes |
|---------|--------|
| analyze | `catalog_audio/<track>.analysis.json` (+ merges `tracks_summary.csv`) |
| catalog | `catalog/manifest.csv`, `catalog/histograms.npz`, `catalog/thumbs/` |
| arrange | `order-sync-…csv`, `…labels.txt`, `…markers.csv`, `render-…sh`, `<track>…arrange.json` |
| render  | `cut-<track>….mp4` (printed on completion: "Render complete → …") |
| project | everything above for that project under `projects/<name>/` |

Media lives under `DV2MV_MEDIA`; in **library mode** arrange/render outputs land
in `catalog_audio/`, in **project mode** under `projects/<name>/`.

## Ticket template

```markdown
**Tier:** Tk | Web | Engine/CLI
**Stage:** ingest | detect | catalog | analyze | arrange | render | project | gallery
**Launched with:** e.g. `DV2MV_MEDIA=/Volumes/Footage/musicvideo python3 tkapp.py`
**Project active?** none (library mode) | <project name>

**Steps to reproduce:**
1.
2.

**Expected:**
**Actual:**

**Terminal / console output:** (Tk: launch terminal. Web: uvicorn + devtools.
Paste verbatim.)

**Inputs:** track filename; for arrange, the grid + options — or attach the
`<track>.arrange.json`, which records all of them.

**Environment:** macOS version, `python3 --version`, `ffmpeg -version | head -1`,
`scenedetect version`. Note whether DV2MV_MEDIA was set.

**Screenshot:** (for any layout/visual bug)
```

## Handy attachments

- `<track>.arrange.json` — the exact grid/reuse/blur/clip-from + result stats
  that produced a cut (for "wrong arrangement" reports).
- `project.json` — a project's track + clip selection + options.
- The render-complete path line — confirms where the mp4 went.
