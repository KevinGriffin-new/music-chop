# dv2mv Help

dv2mv turns a pile of video footage into a music video synced to a track.
You point it at a **media library** folder, feed it footage and music, and it
cuts clips to the music's structure. The cut can be rendered straight to an
mp4, or exported as an editable timeline for DaVinci Resolve.

## What does this software do? (tour)

Both UIs have a **Tour** button — a short interactive walkthrough that
highlights each control in turn and points you at free, public-domain footage
and music so you can run the whole loop end-to-end without any of your own
media. The walk matches the steps below (Quick start).

## Preflight — required vs recommended tools

Both UIs show a **Preflight** check on launch (a status badge in the web tier,
a Preflight… dialog in the Tk app). It reports the system tooling dv2mv shells
out to:

- **ffmpeg / ffprobe** — *required*. Decode/transcode footage, render the cut,
  read clip metadata. Without them dv2mv cannot run; install via
  `conda install ffmpeg` (macOS: `brew install ffmpeg`).
- **rubberband** — *recommended* (only for Retempo). Best pitch-preserved
  time-stretch; Retempo falls back to ffmpeg's `atempo` without it.

The conda `environment.yml` in the repo bundles ffmpeg + rubberband in one
command; the web `Dockerfile` zero-host-setup image already has them.

## Quick start

- **Media library…** — pick the folder your media lives in (never the dv2mv
  code folder). Everything dv2mv makes lands inside it.
- **Add: Video footage…** — pick source videos. dv2mv splits them into
  scene clips and catalogs each clip's look (motion, brightness, color).
- **Add: Music track…** — pick a song. It is analyzed in place: tempo, key,
  beats, sections, and (0.2.x) section labels with a chorus call.
- **Arrange** — match clips to the song on a cut grid. Then **Render to MP4**
  for a finished file, or **Export to editor** for Resolve.

## The media library

One folder holds everything: `album-audio/` (your tracks), `clips/` (scene
clips), `catalog/` (the clip manifest + thumbnails), `catalog_audio/`
(track analyses), `projects/`, and `cuts/`. The choice is remembered between
launches. dv2mv refuses to treat its own code checkout as the library —
pick a real media folder instead.

## Analyze

Reads the track and writes an analysis: tempo (BPM), key, the beat grid,
downbeats (bar starts), section boundaries, harmonic-change points, and an
energy envelope. Since 0.2.0 it also labels sections by similarity —
repeated material shares a letter (A/B/A/B…) — and calls the **chorus**:
the recurring section with the highest energy. If no section recurs, no
chorus is called; that is deliberate honesty, not a failure.

Advanced: a lyric transcript sidecar (`<track>.lyrics.json`, made by
`pipeline/lyrics_analyze.py` from a checkout — it needs its own Python
environment with demucs + mlx-whisper) sharpens the labels considerably on
band recordings, and restricts the chorus call to sung sections.

## Arrange

Carves the song into slots on a **grid** and fills each slot with the clip
whose look best fits the music there. Action lands on loud passages, calm
clips on quiet ones, and every cut falls on the beat.

- **Grid** — where cuts happen. `sections`: one cut per structural section
  (calmest). `downbeats`: one cut per bar (driving). `beats`: every N beats
  (fast montage; set **Beats/cut**). `harmonic`: cut on chord changes.
- **Match** — how clips are chosen. `energy`: motion tracks the song's
  loudness (the classic behavior). `contrast`: also alternate bright/dark
  between adjacent cuts. `variety`: alternate color too. On black-and-white
  footage use `contrast` — color terms have nothing to work with.
- **Allow reuse** — let clips repeat when there are more slots than clips.
- **Drop blurry** — ignore clips below a sharpness threshold.
- **Clip piece from** — take the slot-length piece from the clip's `middle`
  (default) or its `start`.

## Compare grids

Arranges the track on every grid × match combination and ranks them by
energy match, brightness contrast, and color variety. Every variant's files
stay on disk, so the winner (preselected) is ready to Render or Export —
and so is any runner-up you like better.

## Render vs Export

Both work from the same arrangement. **Render to MP4** bakes a finished
video with the music muxed in. **Export to editor** writes an OpenTimelineIO
`.otio` and an FCP X `.fcpxml` timeline of the same cut — import into
DaVinci Resolve to grade and finish. dv2mv decides the cut; Resolve
finishes it.

## Tempo… (retempo)

Time-stretches a track to a target BPM without chipmunking the vocals
(Rubber Band when installed, ffmpeg otherwise), writing a new
`<track>-<bpm>bpm.wav` you then Analyze and Arrange like any track. A faster
grid cuts faster — handy for matching a reference video's drive.

## Projects

A project pins a track + a footage selection + arrange options under a name,
so different videos don't trample each other. Its cuts and timelines collect
in the project's own folder. Use the **Gallery** to scope footage: select
clips, then add/remove/replace the project's selection. Double-click any
gallery thumbnail to play the clip.

## Live shoots from OBS (Latham)

Named for the OBS operator whose multicam shoot provoked the feature.

OBS names each recording with its start time (`2025-09-26 18-26-38.mov`),
and when several machines record the same show those names are enough to
pair every camera with its take — no timecode needed. Only one machine
needs real audio (the one wired to the board); the other cameras' silent
tracks don't matter, because renders are always scored by the project's
track. The workflow:

- Copy every machine's recordings into the media library folder, then
  **Add: Video footage…** with **fast split** checked — a locked-off live
  camera has almost no scene changes, so a lossless stream copy beats
  re-encoding by hours.
- Put each take's board audio in `album-audio/`, named exactly like its
  recording: `ffmpeg -i "… 18-26-38.mov" -vn -c:a copy "… 18-26-38.m4a"`.
  To also make one track per *song*, chop a set with `tools/song_split.py`
  — it cuts at the quiet gaps between songs.
- Press **From takes** (web: **Projects from takes**). One project per take
  appears, named like `9-26-18-26`, its footage scoped to every camera of
  that take. Cameras that started seconds late — or minutes into the set —
  land in the right take by capture time.
- Open a project: **Analyze**, then **Arrange** or **Compare**, then
  **Render** / **Export**.

## Thumbnail suggestions

Scores every cataloged clip for cover-frame promise (sharp, well exposed,
colorful), frame-scans the best of each source tape at full resolution —
favoring sharp frames with faces — and writes 1280x720 JPEG candidates plus
a `_contact.jpg` overview into `thumbnails/` in the library. Use them as
YouTube/cover thumbnail starting points. The **skip** pattern excludes whole
tapes from the sheet and is remembered between runs.

## Troubleshooting

- **"refusing to use the code checkout as the media root"** — pick your
  actual media folder with Media library…
- **A track added from outside the library analyzed once but won't
  re-analyze** — keep tracks in `album-audio/` inside the library for now;
  re-analyze by name looks there.
- A running stage can always be stopped with **Cancel**.
- Bugs: https://todo.sr.ht/~kevin_griffin/music-chop-dv2mv — for desktop-app
  crashes the launch terminal's traceback is usually the whole answer (see
  REPORTING.md in the repo).
