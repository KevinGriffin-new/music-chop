#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
clip_features.py — extract per-clip features from a pile of video clips.

Walks a folder of clips (e.g. the output of `scenedetect ... split-video`),
samples a handful of frames from each, and computes cheap visual features that
drive downstream sorting/arrangement:

  - duration_s     : clip length in seconds
  - capture_time   : parsed from filename if it looks like 2004.07.18_05-29-17
  - source         : filename prefix before the timestamp (the "tape")
  - motion_energy  : mean abs frame-to-frame difference (action vs. static)
  - mean_luma      : average brightness (day/night, indoor/outdoor)
  - hue_deg        : dominant hue, circular mean (0-360, warm/cool)
  - colorfulness   : Hasler-Susstrunk colorfulness metric
  - sharpness      : variance of Laplacian (blur detection / culling)

Outputs:
  manifest.csv      one row per clip with the scalar features above
  histograms.npz    HS color histogram per clip (for similarity ordering)
  thumbs/<name>.jpg one thumbnail per clip (middle sampled frame)

Usage:
  python3 clip_features.py /path/to/clips
  python3 clip_features.py /path/to/clips -o ./catalog --frames 16 --width 160

Requires: opencv-python, numpy  (pip install opencv-python numpy)
"""
import argparse
import csv
import os
import re
import sys
from glob import glob

import cv2
import numpy as np

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".avi", ".dv")

# matches 2004.07.18_05-29-17 anywhere in the name
TS_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})-(\d{2})-(\d{2})")


def parse_name(path):
    """Return (source_prefix, capture_time_iso) parsed from the filename."""
    base = os.path.basename(path)
    m = TS_RE.search(base)
    if not m:
        return os.path.splitext(base)[0], ""
    y, mo, d, h, mi, s = m.groups()
    iso = f"{y}-{mo}-{d}T{h}:{mi}:{s}"
    prefix = base[: m.start()].rstrip("-_ .") or "unknown"
    return prefix, iso


def sample_indices(total, n):
    """Evenly spaced frame indices, avoiding the very first/last frame."""
    if total <= 0:
        return []
    n = min(n, total)
    if n == 1:
        return [total // 2]
    return [int(round(i * (total - 1) / (n - 1))) for i in range(n)]


def colorfulness(bgr):
    """Hasler & Susstrunk (2003) colorfulness metric."""
    b, g, r = cv2.split(bgr.astype("float"))
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    std = np.sqrt(rg.std() ** 2 + yb.std() ** 2)
    mean = np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    return float(std + 0.3 * mean)


def analyze(path, n_frames, width):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if fps else 0.0

    idxs = sample_indices(total, n_frames)
    if not idxs:
        cap.release()
        return None

    frames, grays = [], []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, int(h * width / w)))
        frames.append(frame)
        grays.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    cap.release()
    if not frames:
        return None

    # motion: mean abs diff between consecutive sampled frames, normalized 0-1
    if len(grays) > 1:
        diffs = [np.mean(np.abs(grays[i].astype("int16") - grays[i - 1].astype("int16")))
                 for i in range(1, len(grays))]
        motion = float(np.mean(diffs)) / 255.0
    else:
        motion = 0.0

    mean_luma = float(np.mean([g.mean() for g in grays])) / 255.0

    # accumulate an 8x8 Hue-Saturation histogram across sampled frames
    hist = np.zeros((8, 8), dtype="float32")
    hue_x = hue_y = 0.0  # for circular mean of hue
    for f in frames:
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        h = cv2.calcHist([hsv], [0, 1], None, [8, 8], [0, 180, 0, 256])
        hist += h
        hch = hsv[:, :, 0].astype("float") * 2.0  # 0-360
        rad = np.deg2rad(hch)
        hue_x += np.cos(rad).mean()
        hue_y += np.sin(rad).mean()
    hist = hist.flatten()
    hist /= (hist.sum() + 1e-9)
    hue_deg = float(np.rad2deg(np.arctan2(hue_y, hue_x)) % 360)

    colorful = float(np.mean([colorfulness(f) for f in frames]))
    mid = frames[len(frames) // 2]
    sharp = float(cv2.Laplacian(cv2.cvtColor(mid, cv2.COLOR_BGR2GRAY),
                                cv2.CV_64F).var())

    return {
        "duration_s": round(duration, 3),
        "motion_energy": round(motion, 5),
        "mean_luma": round(mean_luma, 4),
        "hue_deg": round(hue_deg, 1),
        "colorfulness": round(colorful, 2),
        "sharpness": round(sharp, 1),
        "_hist": hist,
        "_thumb": mid,
    }


def main():
    ap = argparse.ArgumentParser(description="Extract per-clip features.")
    ap.add_argument("clips_dir", help="folder containing the clip pile")
    ap.add_argument("-o", "--out", default="./catalog", help="output dir")
    ap.add_argument("--frames", type=int, default=12,
                    help="frames sampled per clip (default 12)")
    ap.add_argument("--width", type=int, default=160,
                    help="downscale width for analysis (default 160)")
    ap.add_argument("--recursive", action="store_true",
                    help="search clips_dir recursively")
    ap.add_argument("--append", action="store_true",
                    help="incremental: feature-extract only clips not already in "
                         "the manifest and append them (instead of re-cataloging "
                         "the whole pile and overwriting)")
    args = ap.parse_args()

    pattern = "**/*" if args.recursive else "*"
    paths = sorted(
        p for p in glob(os.path.join(args.clips_dir, pattern), recursive=args.recursive)
        if p.lower().endswith(VIDEO_EXTS)
    )
    if not paths:
        sys.exit(f"No video files found in {args.clips_dir}")

    os.makedirs(args.out, exist_ok=True)
    thumbs_dir = os.path.join(args.out, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)

    fields = ["clip", "source", "capture_time", "duration_s", "motion_energy",
              "mean_luma", "hue_deg", "colorfulness", "sharpness", "thumb"]
    manifest = os.path.join(args.out, "manifest.csv")
    hist_path = os.path.join(args.out, "histograms.npz")

    # incremental: carry the existing manifest + histograms forward and only
    # process clips we haven't seen, so "Add footage" doesn't re-extract the
    # whole library every time.
    existing_rows, existing_keys, existing_vecs = [], [], []
    if args.append and os.path.exists(manifest):
        with open(manifest, newline="") as fh:
            existing_rows = list(csv.DictReader(fh))
        have = {os.path.abspath(r["clip"]) for r in existing_rows if r.get("clip")}
        if os.path.exists(hist_path):
            d = np.load(hist_path, allow_pickle=True)
            existing_keys = list(d["keys"])
            existing_vecs = list(d["vecs"])
        paths = [p for p in paths if os.path.abspath(p) not in have]
        if not paths:
            print(f"Nothing new to catalog — {len(existing_rows)} clips already "
                  f"in the manifest.")
            return

    rows, hist_keys, hist_vecs = [], [], []

    for i, path in enumerate(paths, 1):
        print(f"[{i}/{len(paths)}] {os.path.basename(path)}", flush=True)
        feats = analyze(path, args.frames, args.width)
        if feats is None:
            print(f"   ! skipped (unreadable)")
            continue
        source, ctime = parse_name(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        thumb_rel = os.path.join("thumbs", stem + ".jpg")
        cv2.imwrite(os.path.join(args.out, thumb_rel), feats.pop("_thumb"),
                    [cv2.IMWRITE_JPEG_QUALITY, 80])
        hist_keys.append(path)
        hist_vecs.append(feats.pop("_hist"))
        rows.append({
            "clip": os.path.abspath(path),
            "source": source,
            "capture_time": ctime,
            "thumb": thumb_rel,
            **feats,
        })

    all_rows = existing_rows + rows                 # existing first (append mode)
    with open(manifest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)

    np.savez(hist_path,
             keys=np.array(existing_keys + hist_keys),
             vecs=np.array(existing_vecs + hist_vecs))

    if existing_rows:
        print(f"\nDone. {len(rows)} new clip(s) cataloged ({len(all_rows)} total).")
    else:
        print(f"\nDone. {len(all_rows)} clips cataloged.")
    print(f"  manifest:   {manifest}")
    print(f"  thumbnails: {thumbs_dir}")
    print(f"  histograms: {hist_path}")


if __name__ == "__main__":
    main()
