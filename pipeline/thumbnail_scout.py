#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
thumbnail_scout.py — suggest YouTube-thumbnail frames from the clip catalog.

Uses the features the catalog already computed to pre-rank clips (sharp,
well-exposed, colorful), then samples the finalists at full resolution and
scores individual frames: Laplacian sharpness, a brightness sweet-spot, and
face presence (Haar cascade — a face sells a music-video thumbnail). Writes
the winners as 1280x720 center-cropped JPEGs plus a contact sheet.

Grouped by source so one prolific tape can't sweep the board: by default the
top N overall and the top N per source group are all represented.

Usage:
  python3 tools/thumbnail_scout.py /path/to/catalog/manifest.csv -o ./thumbs-yt
  python3 tools/thumbnail_scout.py manifest.csv -o out --per-group 8 --group-re '^Chelovek'

Requires: opencv-python, numpy (same as clip_features).
"""
import argparse
import csv
import os
import re
import sys

import cv2
import numpy as np

YT_W, YT_H = 1280, 720


def luma_sweet(l, center=0.45, width=0.28):
    """1.0 at a pleasant exposure, falling off toward crushed/blown."""
    return float(np.exp(-(((l - center) / width) ** 2)))


def prescore(rows):
    """Rank manifest rows by thumbnail promise using catalog features."""
    sharp = np.array([float(r["sharpness"]) for r in rows])
    s_n = sharp / (sharp.max() + 1e-9)
    color = np.array([float(r["colorfulness"]) for r in rows])
    c_n = color / (color.max() + 1e-9)
    out = []
    for i, r in enumerate(rows):
        if float(r["duration_s"]) < 1.0:      # too short to hold a stable frame
            continue
        s = s_n[i] * luma_sweet(float(r["mean_luma"])) * (1 + 0.5 * c_n[i])
        out.append((s, r))
    out.sort(key=lambda t: -t[0])
    return out


def face_counter():
    """count(gray) -> n_faces using the best detector this cv2 has, or None.

    OpenCV 5 removed the legacy Haar CascadeClassifier and the bundled
    cascade XMLs (its FaceDetectorYN/YuNet replacement needs a model file we
    don't vendor yet — see ROADMAP). Faces are a bonus scoring term, never a
    requirement, so a build without a detector degrades instead of crashing.
    """
    cls = getattr(cv2, "CascadeClassifier", None)
    data = getattr(cv2, "data", None)
    if cls is None or data is None:
        return None
    xml = os.path.join(data.haarcascades, "haarcascade_frontalface_default.xml")
    if not os.path.exists(xml):
        return None
    cascade = cls(xml)
    if cascade.empty():
        return None
    return lambda gray: len(cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)))


def best_frame(path, count_faces, n_samples=7):
    """(score, frame, t_seconds, n_faces) of the best frame in the clip's
    middle half. count_faces may be None (no face detector available)."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    if total < 2:
        cap.release()
        return None
    best = None
    for k in range(n_samples):
        idx = int(total * (0.25 + 0.5 * k / max(1, n_samples - 1)))
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(idx, total - 1))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean = gray.mean() / 255.0
        if mean < 0.12:                        # unusably dark
            continue
        lap = cv2.Laplacian(gray, cv2.CV_64F).var()
        n_faces = count_faces(gray) if count_faces else 0
        score = lap * luma_sweet(mean) * (1 + 0.8 * min(n_faces, 3))
        if best is None or score > best[0]:
            best = (score, frame, idx / fps, n_faces)
    cap.release()
    return best


def to_yt(frame):
    """Center-crop to 16:9 and scale to 1280x720."""
    h, w = frame.shape[:2]
    target = w / (16 / 9)
    if h > target:                             # too tall (4:3): crop rows
        top = int((h - target) / 2)
        frame = frame[top:top + int(target)]
    else:                                      # too wide: crop cols
        target_w = int(h * 16 / 9)
        left = (w - target_w) // 2
        frame = frame[:, left:left + target_w]
    return cv2.resize(frame, (YT_W, YT_H), interpolation=cv2.INTER_LANCZOS4)


def main():
    ap = argparse.ArgumentParser(description="Scout YouTube thumbnail frames.")
    ap.add_argument("manifest", help="catalog/manifest.csv")
    ap.add_argument("-o", "--out", default="./thumbs-youtube", help="output dir")
    ap.add_argument("--per-group", type=int, default=8,
                    help="winners per source group (default 8)")
    ap.add_argument("--candidates", type=int, default=60,
                    help="pre-ranked clips to frame-scan per group (default 60)")
    ap.add_argument("--group-re", default="",
                    help="regex splitting sources into its-own-group vs rest "
                         "(e.g. '^Chelovek'); default: group by source prefix "
                         "before digits")
    ap.add_argument("--exclude-re", default="",
                    help="regex of sources to leave out entirely (private or "
                         "otherwise off-limits tapes)")
    args = ap.parse_args()

    with open(args.manifest, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if args.exclude_re:
        rows = [r for r in rows if not re.search(args.exclude_re, r["source"])]
    if not rows:
        sys.exit("Empty manifest.")

    def group_of(r):
        if args.group_re:
            return "match" if re.search(args.group_re, r["source"]) else "rest"
        return re.split(r"\d", r["source"], 1)[0] or "unknown"

    groups = {}
    for r in rows:
        groups.setdefault(group_of(r), []).append(r)

    count_faces = face_counter()
    if count_faces is None:
        print("note: no face detector in this OpenCV build — ranking by "
              "sharpness/exposure only", flush=True)
    os.makedirs(args.out, exist_ok=True)

    winners = []
    for gi, (gname, grows) in enumerate(sorted(groups.items()), 1):
        ranked = prescore(grows)[:args.candidates]
        # progress marker parsed by engine.thumbnails() ("PROG i/n msg")
        print(f"PROG {gi}/{len(groups)} scanning {gname} "
              f"({len(ranked)} candidates)", flush=True)
        scored = []
        for pre, r in ranked:
            b = best_frame(r["clip"], count_faces)
            if b:
                scored.append((b[0] * (0.5 + 0.5 * pre), b, r))
        scored.sort(key=lambda t: -t[0])
        for rank, (s, (fs, frame, t, nf), r) in enumerate(scored[:args.per_group], 1):
            stem = os.path.splitext(os.path.basename(r["clip"]))[0]
            name = f"{gname}-{rank:02d}-{stem}-t{t:.1f}s.jpg"
            cv2.imwrite(os.path.join(args.out, name), to_yt(frame),
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            winners.append((gname, rank, name, nf))
            print(f"  #{rank}  {stem}  @{t:.1f}s"
                  + (f"  ({nf} face{'s' if nf > 1 else ''})" if nf else ""))

    # contact sheet: rows = groups
    per = args.per_group
    tiles = {}
    for g, rank, name, _ in winners:
        img = cv2.imread(os.path.join(args.out, name))
        tiles.setdefault(g, []).append(cv2.resize(img, (320, 180)))
    if tiles:
        rows_img = []
        width = per * 320
        for g in sorted(tiles):
            row = np.hstack(tiles[g])
            if row.shape[1] < width:
                row = np.hstack([row, np.zeros((180, width - row.shape[1], 3),
                                               dtype=row.dtype)])
            rows_img.append(row)
        cv2.imwrite(os.path.join(args.out, "_contact.jpg"),
                    np.vstack(rows_img), [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"\n{len(winners)} thumbnails -> {args.out} (see _contact.jpg)")


if __name__ == "__main__":
    main()
