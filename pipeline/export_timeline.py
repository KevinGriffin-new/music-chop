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

Requires: opentimelineio, and the FCP X adapter (otio-fcpx-xml-adapter) for the
.fcpxml output. Both are pip-installable; see requirements.txt.
"""
import argparse
import csv
import json
import os
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


def _clip(name, src_path, in_s, dur_s, avail_s, fps):
    """One OTIO clip: an external media ref with the clip's full available range,
    source-trimmed to [in_s, in_s+dur_s). Times are snapped to whole frames at
    fps (sub-frame drift across clips is editorial slack, conformed on import)."""
    mr = otio.schema.ExternalReference(
        target_url=_url(src_path),
        available_range=TimeRange(RationalTime(0, fps),
                                  RationalTime(round(max(avail_s, dur_s) * fps), fps)))
    return otio.schema.Clip(
        name=name, media_reference=mr,
        source_range=TimeRange(RationalTime(round(in_s * fps), fps),
                               RationalTime(round(dur_s * fps), fps)))


def build_timeline(meta, rows):
    """Pure: an arrange.json `meta` dict + order-CSV `rows` -> an OTIO Timeline
    (V1 = the cut clips, A1 = the music). No file I/O, so it's unit-testable."""
    tl_meta = meta.get("timeline") or {}
    fps = float(tl_meta.get("fps") or 30)
    tag = meta.get("tag") or ""
    name = f"{meta['track']}-{tag}" if tag else meta["track"]
    tl = otio.schema.Timeline(name=name)
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
    tl = build_timeline(meta, rows)

    out_dir = os.path.abspath(args.out) if args.out else \
        os.path.dirname(os.path.abspath(args.arrange))
    os.makedirs(out_dir, exist_ok=True)
    tag = meta.get("tag") or ""
    sfx = f"-{tag}" if tag else ""
    stem = f"{meta['track']}{sfx}"

    wrote = []
    for f in formats:
        path = os.path.join(out_dir, f"{stem}.{EXT[f]}")
        otio.adapters.write_to_file(tl, path)
        wrote.append(path)
        # the engine parses these "wrote <path>" lines into the result
        print(f"wrote {path}")

    n = len(rows)
    print(f"Exported {n} cuts → {', '.join(os.path.basename(w) for w in wrote)}")


if __name__ == "__main__":
    main()
