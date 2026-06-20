#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
export_timeline.py — emit an editable timeline from a dv2mv arrangement.

dv2mv decides the *cut*; a finishing app (DaVinci Resolve) *finishes* it (color,
audio, transitions, delivery). Instead of — or alongside — the ffmpeg render,
this writes an interchange timeline that Resolve imports:

  * <track>[-tag].otio     OpenTimelineIO (portable interchange)
  * <track>[-tag].fcpxml   FCP X XML (the most reliable import into Resolve)

Both are verified to import into DaVinci Resolve (Studio 21). Two things matter
for a clean import, both handled here:
  * Source timecode. These clips are PySceneDetect splits that carry the
    capture's tape timecode, so each clip's first frame sits at a non-zero TC.
    Resolve indexes an in-point in the source's *timecode* space, so every
    in-point is offset by the clip's embedded TC (read via ffprobe) — a 0-based
    in-point on such a clip lands before the media and Resolve refuses the whole
    timeline. ffmpeg/ffprobe on PATH is needed for this; without it clips fall
    back to a 0 TC (fine only for clips whose TC is already 00:00:00:00).
  * Frame rate. The FCPXML declares its own <format>, so it always builds a
    timeline at the arrangement's fps regardless of project settings. The OTIO
    has no equivalent — Resolve builds it at the *project's current* timeline
    frame rate and conforms — so set the project to the arrangement's fps
    (arrange.json timeline.fps) before importing the .otio, or use the .fcpxml.

The timeline is two tracks:
  V1  one clip per cut slot, trimmed to the slot's source in-point + duration
      (read straight from order-sync-<track>.csv — no recompute, so the trims
      match what the render would have produced frame-for-frame)
  A1  one clip: the music, full length

It reads the self-describing <track>.arrange.json (which points at its order CSV
and carries the music path + timeline fps/size), so a single --arrange argument
is enough.

Usage:
  python3 export_timeline.py --arrange catalog_audio/song-sections.arrange.json
  python3 export_timeline.py --arrange ... --out /some/dir --formats otio,fcpxml

Requires: opentimelineio (for the .otio output). The .fcpxml is hand-emitted —
no FCP X adapter needed — so it lands in exactly the structure Resolve imports.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange

# extension -> the formats we know how to write (drives --formats validation)
EXT = {"otio": "otio", "fcpxml": "fcpxml"}


def _url(path):
    """Absolute filesystem path -> a file:// URL OTIO can store as a media ref
    (handles spaces / non-ASCII via proper percent-encoding)."""
    return Path(os.path.abspath(path)).as_uri()


def _tc_to_frames(tc, nominal_fps):
    """SMPTE timecode 'HH:MM:SS:FF' -> integer frame count at nominal_fps (the
    media's own rate base — e.g. 30 for 29.97, 60 for 59.94). A ';' or '.' before
    the frame field flags drop-frame, which we compensate. 0 if unparseable."""
    parts = re.split(r"[:;.]", tc.strip())
    if len(parts) != 4:
        return 0
    try:
        hh, mm, ss, ff = (int(p) for p in parts)
    except ValueError:
        return 0
    frames = ((hh * 60 + mm) * 60 + ss) * round(nominal_fps) + ff
    if ";" in tc or "." in tc:                 # drop-frame: 2 frames dropped per
        total_min = hh * 60 + mm               # minute, except every 10th minute
        frames -= 2 * (total_min - total_min // 10)
    return frames


_TC_CACHE = {}


def _source_tc_string(path):
    """Embedded start timecode string of a source clip ('' if none).

    PySceneDetect splits inherit the capture's tape timecode, so each clip's
    first frame sits at a non-zero TC. Both exporters offset their in-points by
    it (Resolve indexes the source in timecode space; a 0-based in-point on a
    non-zero-TC clip lands before the media exists and the import is rejected).
    Cached per path; missing ffprobe or no timecode degrades to ''."""
    key = os.path.abspath(path)
    if key in _TC_CACHE:
        return _TC_CACHE[key]
    tc = ""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format_tags=timecode:stream_tags=timecode",
             "-of", "default=nw=1:nk=1", key],
            capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            if line.strip():
                tc = line.strip()
                break
    except (OSError, subprocess.SubprocessError):
        tc = ""
    _TC_CACHE[key] = tc
    return tc


def _source_tc_seconds(path, fps):
    """Start timecode in seconds at the timeline fps (for the FCPXML path)."""
    return _tc_to_frames(_source_tc_string(path), fps) / fps


_FPS_CACHE = {}


def _source_media_fps(path):
    """True frame rate of a source clip (e.g. 29.97 for NTSC), or None if it has
    no video stream / ffprobe is unavailable. Cached per path."""
    key = os.path.abspath(path)
    if key in _FPS_CACHE:
        return _FPS_CACHE[key]
    rate = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "default=nw=1:nk=1", key],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if "/" in out:
            n, d = out.split("/")
            rate = float(n) / float(d) if float(d) else None
        elif out:
            rate = float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        rate = None
    _FPS_CACHE[key] = rate
    return rate


def _clip(name, src_path, in_s, dur_s, avail_s, fps):
    """One OTIO clip: an external media ref with the clip's full available range,
    source-trimmed to [in_s, in_s+dur_s).

    Media-side times (the available range and the source in-point) are expressed
    at the clip's TRUE frame rate (mfps), anchored at its start timecode (tc) —
    Resolve reads the real media's rate + TC when conforming OTIO, so declaring
    them at the timeline fps instead drifts the in-point (it can even go negative,
    trimming the head). The in-point also rides on tc because Resolve indexes the
    source in timecode space. The media reference is named (the file's basename)
    so the imported media pool shows real clip names rather than blanks."""
    mfps = _source_media_fps(src_path) or fps
    # The available range uses the media's true rate, but only when it shares the
    # timeline's nominal base (e.g. 29.97 under a 30 timeline) — otherwise the TC
    # frame count and the timeline-rate source range disagree and the clip lands
    # out of its own available range, which aborts the import. Odd-rate clips
    # (e.g. 59.94) fall back to the timeline rate so their ranges stay consistent.
    if round(mfps) != round(fps):
        mfps = float(fps)
    tcf = _tc_to_frames(_source_tc_string(src_path), round(mfps))  # TC frames at media rate
    mr = otio.schema.ExternalReference(
        # Resolve's OTIO importer resolves media by plain filesystem path; a
        # file:// URL (what FCPXML wants) makes it drop every clip and fail the
        # whole import. So OTIO gets the absolute path, not _url().
        target_url=os.path.abspath(src_path),
        available_range=TimeRange(RationalTime(tcf, mfps),
                                  RationalTime(round(max(avail_s, dur_s) * mfps), mfps)))
    mr.name = os.path.basename(src_path)
    # source_range stays at the timeline fps (Resolve's own OTIO export does the
    # same: available_range carries the media rate, source_range the timeline
    # rate). start = TC frame count + the in-point; duration = the slot.
    return otio.schema.Clip(
        name=name, media_reference=mr,
        source_range=TimeRange(RationalTime(tcf + round(in_s * fps), fps),
                               RationalTime(round(dur_s * fps), fps)))


def build_timeline(meta, rows):
    """Pure: an arrange.json `meta` dict + order-CSV `rows` -> an OTIO Timeline
    (V1 = the cut clips, A1 = the music). No file I/O, so it's unit-testable."""
    tl_meta = meta.get("timeline") or {}
    fps = float(tl_meta.get("fps") or 30)
    tag = meta.get("tag") or ""
    name = f"{meta['track']}-{tag}" if tag else meta["track"]
    # Resolve's OTIO importer wants a global_start_time at the timeline fps;
    # without it the timeline is built at the project's current rate.
    tl = otio.schema.Timeline(name=name,
                              global_start_time=RationalTime(0, fps))
    video = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    audio = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
    tl.tracks.append(video)
    tl.tracks.append(audio)

    for r in rows:
        in_s = float(r.get("clip_in_s") or 0)
        dur_s = float(r["slot_dur_s"])
        avail_s = float(r.get("clip_src_dur_s") or dur_s)
        clip_name = os.path.splitext(os.path.basename(r["clip"]))[0]
        video.append(_clip(clip_name, r["clip"], in_s, dur_s, avail_s, fps))

    music = meta.get("music")
    song = float(meta.get("duration_s") or 0)
    if music and song > 0:
        audio.append(_clip(os.path.splitext(os.path.basename(music))[0],
                           music, 0.0, song, song, fps))
    return tl


# ── FCPXML (hand-emitted) ────────────────────────────────────────────────────
# We don't use OTIO's fcpx adapter: it emits a <project> with no <library>/<event>
# wrapper (Resolve fails with 'Unable to find inherited value for key "library"')
# plus stray top-level <asset-clip>s. Emitting the document ourselves guarantees
# the structure Resolve imports, and keeps the dep surface to OTIO core only.
def _attr(s) -> str:
    """Escape a value for an XML double-quoted attribute."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_fcpxml(meta, rows) -> str:
    """Pure: arrange `meta` + order `rows` -> an FCPXML 1.9 document string.

    One <asset> per unique source clip (deduped), the cut on the primary spine
    (each asset-clip trimmed at its source in-point), and the music as a
    connected clip (lane -1) on the first cut clip. Times are exact frame
    rationals (<frames>/<fps>s) at the timeline's fps.
    """
    tl = meta.get("timeline") or {}
    fps = int(tl.get("fps") or 30)
    w, h = int(tl.get("width") or 1920), int(tl.get("height") or 1080)

    def t(seconds) -> str:
        return f"{round(float(seconds) * fps)}/{fps}s"

    # dedupe sources → asset ids (r1 is reserved for the format)
    asset_id, resources = {}, []
    for r in rows:
        p = os.path.abspath(r["clip"])
        if p in asset_id:
            continue
        aid = f"r{len(asset_id) + 2}"
        asset_id[p] = aid
        # Available media spans [tc, tc+duration]; declare a duration that covers
        # the source-trimmed segment so the in-point never falls past the end.
        src_dur = float(r.get("clip_src_dur_s") or r["slot_dur_s"])
        used_out = float(r.get("clip_in_s") or 0) + float(r["slot_dur_s"])
        dur = t(max(src_dur, used_out))
        start = t(_source_tc_seconds(p, fps))
        resources.append(
            f'    <asset id="{aid}" name="{_attr(os.path.splitext(os.path.basename(p))[0])}" '
            f'src="{_attr(_url(p))}" start="{start}" duration="{dur}" '
            f'hasVideo="1" hasAudio="0" format="r1"/>')

    music = meta.get("music")
    song = float(meta.get("duration_s") or 0)
    audio_id = None
    if music and song > 0:
        audio_id = f"r{len(asset_id) + 2}"
        resources.append(
            f'    <asset id="{audio_id}" name="{_attr(os.path.splitext(os.path.basename(music))[0])}" '
            f'src="{_attr(_url(music))}" start="0s" duration="{t(song)}" '
            f'hasVideo="0" hasAudio="1"/>')

    # spine: video cuts in order; the music connected (lane -1) to the first cut
    spine, off = [], 0.0
    for i, r in enumerate(rows):
        p = os.path.abspath(r["clip"])
        name = _attr(os.path.splitext(os.path.basename(p))[0])
        dur, instart = float(r["slot_dur_s"]), float(r.get("clip_in_s") or 0)
        # in-point lives in the source's timecode space (start = tc + in-point)
        instart += _source_tc_seconds(p, fps)
        open_tag = (f'<asset-clip ref="{asset_id[p]}" name="{name}" '
                    f'offset="{t(off)}" start="{t(instart)}" duration="{t(dur)}" format="r1"')
        if i == 0 and audio_id:
            mname = _attr(os.path.splitext(os.path.basename(music))[0])
            # A connected clip's offset is in the PARENT clip's source-time frame,
            # so to sit at the parent's timeline position its offset must equal the
            # parent's source in-point (t(instart)), not 0 — otherwise the music
            # lands at (parent_pos - parent_start), i.e. far into negative time.
            spine.append(
                f'            {open_tag}>\n'
                f'              <asset-clip ref="{audio_id}" name="{mname}" lane="-1" '
                f'offset="{t(instart)}" start="0s" duration="{t(song)}" audioRole="music"/>\n'
                f'            </asset-clip>')
        else:
            spine.append(f'            {open_tag}/>')
        off += dur

    tag = meta.get("tag") or ""
    proj = _attr(f"{meta['track']}-{tag}" if tag else meta["track"])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.9">\n'
        '  <resources>\n'
        f'    <format id="r1" name="FFVideoFormat{w}x{h}p{fps}" '
        f'frameDuration="1/{fps}s" width="{w}" height="{h}"/>\n'
        + "\n".join(resources) + "\n"
        '  </resources>\n'
        '  <library>\n'
        '    <event name="dv2mv">\n'
        f'      <project name="{proj}">\n'
        f'        <sequence format="r1" duration="{t(off)}" tcStart="0s" '
        'tcFormat="NDF" audioLayout="stereo" audioRate="48k">\n'
        '          <spine>\n'
        + "\n".join(spine) + "\n"
        '          </spine>\n'
        '        </sequence>\n'
        '      </project>\n'
        '    </event>\n'
        '  </library>\n'
        '</fcpxml>\n')


def load_arrange(arrange_json):
    """Read an arrange.json and its order CSV (resolved relative to that file's
    dir). Returns (meta, rows)."""
    with open(arrange_json) as fh:
        meta = json.load(fh)
    base = os.path.dirname(os.path.abspath(arrange_json))
    order_name = (meta.get("outputs") or {}).get("order")
    if not order_name:
        sys.exit("arrange.json has no outputs.order — re-run Arrange.")
    order_csv = os.path.join(base, order_name)
    if not os.path.exists(order_csv):
        sys.exit(f"order CSV not found next to arrange.json: {order_csv}")
    with open(order_csv, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit("order CSV has no slots.")
    return meta, rows


def main():
    ap = argparse.ArgumentParser(description="Export a dv2mv arrangement to an "
                                             "editable timeline (OTIO / FCPXML).")
    ap.add_argument("--arrange", required=True, help="<track>.arrange.json")
    ap.add_argument("--out", default="",
                    help="output directory (default: the arrange.json's dir)")
    ap.add_argument("--formats", default="otio,fcpxml",
                    help="comma list of: otio, fcpxml (default: both)")
    args = ap.parse_args()

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in EXT]
    if bad:
        sys.exit(f"unknown format(s): {', '.join(bad)} (known: {', '.join(EXT)})")

    meta, rows = load_arrange(args.arrange)

    out_dir = os.path.abspath(args.out) if args.out else \
        os.path.dirname(os.path.abspath(args.arrange))
    os.makedirs(out_dir, exist_ok=True)
    tag = meta.get("tag") or ""
    sfx = f"-{tag}" if tag else ""
    stem = f"{meta['track']}{sfx}"

    wrote = []
    for f in formats:
        path = os.path.join(out_dir, f"{stem}.{EXT[f]}")
        if f == "fcpxml":
            with open(path, "w") as fh:           # hand-emitted (Resolve-importable)
                fh.write(build_fcpxml(meta, rows))
        else:
            otio.adapters.write_to_file(build_timeline(meta, rows), path)
        wrote.append(path)
        # the engine parses these "wrote <path>" lines into the result
        print(f"wrote {path}")

    n = len(rows)
    print(f"Exported {n} cuts → {', '.join(os.path.basename(w) for w in wrote)}")


if __name__ == "__main__":
    main()
