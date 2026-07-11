#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
song_split.py — chop a live recording into songs at sustained quiet gaps.

A pre-processing tool, not a pipeline stage. Point it at a continuous live
recording (audio or video — anything ffmpeg can decode) and it finds the
between-song gaps: stretches where the level stays a set depth below the
take's overall music level for long enough to be a band stop rather than a
quiet passage. Each cut lands on the quietest instant of its gap, and
segments too short to be songs are merged into a neighbour.

By default it only prints the segment table so you can sanity-check the
boundaries; --cut writes one file per segment via ffmpeg stream copy
(lossless, no re-encode), named "<take> - NN (start-end).<ext>".

Usage:
  python3 tools/song_split.py "/path/set.m4a"                # table only
  python3 tools/song_split.py "/path/set.m4a" --cut          # write segments
  python3 tools/song_split.py *.m4a --cut --out songs/
  python3 tools/song_split.py set.m4a --gap-db 15 --min-gap 3   # stricter gaps

Requires: ffmpeg/ffprobe on PATH, numpy.
"""
import argparse
import os
import subprocess
import sys

import numpy as np

SR = 8000          # analysis rate; envelope detail, not audio quality
HOP = SR // 4      # 0.25 s envelope resolution


def decode_mono(path):
    """Decode any av file to a float array at SR via an ffmpeg s16le pipe."""
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True)
    if p.returncode != 0 or not p.stdout:
        sys.exit(f"ffmpeg could not decode {path}: {p.stderr.decode().strip()}")
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float64)


def envelope_db(x, smooth_s):
    """Smoothed frame-RMS loudness curve, one point per HOP."""
    n = len(x) // HOP
    frames = x[: n * HOP].reshape(n, HOP)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    k = max(1, int(smooth_s / (HOP / SR)))
    return np.convolve(db, np.ones(k) / k, mode="same")


def find_cuts(db, gap_db, min_gap_s):
    """Cut times: the quietest instant of each sustained sub-threshold run."""
    thr = np.median(db) - gap_db
    step = HOP / SR
    cuts = []
    i = 0
    while i < len(db):
        if db[i] < thr:
            j = i
            while j < len(db) and db[j] < thr:
                j += 1
            if (j - i) * step >= min_gap_s:
                cuts.append((i + int(np.argmin(db[i:j]))) * step)
            i = j
        else:
            i += 1
    return cuts, float(np.median(db)), float(thr)


def merge_short(cuts, total, min_song_s):
    """Segment endpoints with any too-short segment folded into a neighbour."""
    pts = [0.0] + cuts + [total]
    while len(pts) > 2:
        segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        short = [i for i, (a, b) in enumerate(segs) if b - a < min_song_s]
        if not short:
            break
        i = short[0]
        if i == 0:
            pts.pop(1)
        elif i == len(segs) - 1:
            pts.pop(-2)
        else:  # fold into the shorter neighbour
            left = segs[i - 1][1] - segs[i - 1][0]
            right = segs[i + 1][1] - segs[i + 1][0]
            pts.pop(i if left < right else i + 1)
    return pts


def mmss(t):
    return f"{int(t) // 60}m{int(t) % 60:02d}"


def cut_segments(path, pts, out_dir):
    """Stream-copy each [a,b) of the source into out_dir; returns file list."""
    os.makedirs(out_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(path))
    written = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        out = os.path.join(out_dir, f"{base} - {i + 1:02d} ({mmss(a)}-{mmss(b)}){ext}")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{a:.2f}", "-to", f"{b:.2f}",
             "-i", path, "-c", "copy", "-y", out], check=True)
        written.append(out)
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("audio", nargs="+", help="live recording(s) to split")
    ap.add_argument("--gap-db", type=float, default=12.0,
                    help="gap = this many dB below the take's median level (12)")
    ap.add_argument("--min-gap", type=float, default=2.0,
                    help="gap must stay quiet at least this many seconds (2)")
    ap.add_argument("--min-song", type=float, default=45.0,
                    help="segments shorter than this merge into a neighbour (45)")
    ap.add_argument("--smooth", type=float, default=3.0,
                    help="envelope smoothing window in seconds (3)")
    ap.add_argument("--cut", action="store_true",
                    help="write the segments (default: print the table only)")
    ap.add_argument("--out", default=None,
                    help="output dir for --cut (default: songs/ beside the input)")
    args = ap.parse_args()

    for path in args.audio:
        x = decode_mono(path)
        total = len(x) / SR
        db = envelope_db(x, args.smooth)
        cuts, lvl, thr = find_cuts(db, args.gap_db, args.min_gap)
        pts = merge_short(cuts, total, args.min_song)
        n = len(pts) - 1
        print(f"\n=== {os.path.basename(path)}  ({mmss(total)}, "
              f"music {lvl:.0f} dB, gap thr {thr:.0f} dB) → {n} segment(s)")
        for i in range(n):
            a, b = pts[i], pts[i + 1]
            print(f"  {i + 1:02d}  {mmss(a):>7} – {mmss(b):>7}  ({b - a:6.0f}s)")
        if args.cut and n > 1:
            out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(path)), "songs")
            for f in cut_segments(path, pts, out_dir):
                print(f"  wrote {f}")
        elif args.cut:
            print("  single segment — nothing to cut")


if __name__ == "__main__":
    main()
