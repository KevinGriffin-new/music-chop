#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
clip_order.py — turn the feature manifest into an ordered playlist.

Reads catalog/manifest.csv (+ histograms.npz) and arranges the clips using one
of several strategies, then writes out something you can render or import:

  modes
  -----
  combined    (default) cluster the pile, arrange clusters into an energy arc,
              then within each cluster chain clips by visual similarity.
  cluster     group by k-means only (ordered by cluster, then motion).
  energy      ignore clusters; order everything by an energy curve.
  similarity  greedy nearest-neighbor chain through color space (smooth cuts).
  chrono      sort by capture_time (then filename).

  energy curve shapes (--curve): arch (default), rise, fall, valley

Outputs (into the catalog dir):
  order.csv            the chosen order, with all features + cluster id
  playlist.txt         ffmpeg concat list  -> render with the printed command
  playlist.m3u         simple playlist you can drop into VLC to audition

Usage:
  python3 clip_order.py catalog/manifest.csv
  python3 clip_order.py catalog/manifest.csv --mode energy --curve rise
  python3 clip_order.py catalog/manifest.csv --clusters 6 --drop-blurry 40

Requires: numpy, scikit-learn  (pip install numpy scikit-learn)
"""
import argparse
import csv
import os
import re
import sys

import numpy as np


def load(manifest):
    rows = []
    with open(manifest, newline="") as fh:
        for r in csv.DictReader(fh):
            for k in ("duration_s", "motion_energy", "mean_luma", "hue_deg",
                      "colorfulness", "sharpness"):
                r[k] = float(r[k]) if r.get(k) not in (None, "") else 0.0
            rows.append(r)
    cat = os.path.dirname(os.path.abspath(manifest))
    hpath = os.path.join(cat, "histograms.npz")
    hist = {}
    if os.path.exists(hpath):
        z = np.load(hpath, allow_pickle=True)
        for k, v in zip(z["keys"], z["vecs"]):
            hist[os.path.abspath(str(k))] = v.astype("float32")
    return rows, hist


def norm(vals):
    a = np.asarray(vals, dtype="float")
    lo, hi = a.min(), a.max()
    return (a - lo) / (hi - lo) if hi > lo else np.zeros_like(a)


def energy_order(idxs, motion, curve):
    """Return idxs reordered so motion follows the requested curve shape."""
    asc = sorted(idxs, key=lambda i: motion[i])
    if curve == "rise":
        return asc
    if curve == "fall":
        return asc[::-1]
    if curve == "arch":  # low -> high -> low
        out = asc[::2] + asc[1::2][::-1]
        return out
    if curve == "valley":  # high -> low -> high
        out = asc[::2] + asc[1::2][::-1]
        return out[::-1]
    return asc


def similarity_chain(idxs, hist, rows, seed=None):
    """Greedy nearest-neighbor path through color-histogram space."""
    if not hist:
        return idxs  # no histograms available; leave as-is
    vecs = {i: hist.get(os.path.abspath(rows[i]["clip"])) for i in idxs}
    pool = [i for i in idxs if vecs[i] is not None]
    missing = [i for i in idxs if vecs[i] is None]
    if len(pool) < 2:
        return idxs
    cur = seed if seed in pool else pool[0]
    order, remaining = [cur], set(pool) - {cur}
    while remaining:
        cv = vecs[cur]
        nxt = min(remaining, key=lambda j: float(np.linalg.norm(cv - vecs[j])))
        order.append(nxt)
        remaining.discard(nxt)
        cur = nxt
    return order + missing


def main():
    ap = argparse.ArgumentParser(description="Order a clip pile.")
    ap.add_argument("manifest", help="path to catalog/manifest.csv")
    ap.add_argument("--mode", default="combined",
                    choices=["combined", "cluster", "energy", "similarity", "chrono"])
    ap.add_argument("--curve", default="arch",
                    choices=["arch", "rise", "fall", "valley"])
    ap.add_argument("--clusters", type=int, default=0,
                    help="k for k-means (0 = auto: ~sqrt(n/2))")
    ap.add_argument("--drop-blurry", type=float, default=0.0,
                    help="cull clips with sharpness below this value")
    ap.add_argument("--min-dur", type=float, default=0.0,
                    help="cull clips shorter than this many seconds")
    ap.add_argument("--tag", default="",
                    help="label for output filenames (default: auto from mode/curve)")
    args = ap.parse_args()

    rows, hist = load(args.manifest)
    if not rows:
        sys.exit("Empty manifest.")

    # culling
    kept = [r for r in rows
            if r["sharpness"] >= args.drop_blurry and r["duration_s"] >= args.min_dur]
    dropped = len(rows) - len(kept)
    rows = kept
    n = len(rows)
    if n == 0:
        sys.exit("Everything got culled — loosen --drop-blurry / --min-dur.")

    motion = [r["motion_energy"] for r in rows]

    # feature matrix for clustering: luma, motion, colorfulness + hue on a circle
    hue_rad = np.deg2rad([r["hue_deg"] for r in rows])
    feat = np.column_stack([
        norm([r["mean_luma"] for r in rows]),
        norm(motion),
        norm([r["colorfulness"] for r in rows]),
        np.cos(hue_rad), np.sin(hue_rad),
    ])

    labels = np.zeros(n, dtype=int)
    if args.mode in ("combined", "cluster"):
        from sklearn.cluster import KMeans
        k = args.clusters or max(2, int(round((n / 2) ** 0.5)))
        k = min(k, n)
        labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(feat)

    # build the order
    if args.mode == "chrono":
        order = sorted(range(n), key=lambda i: (rows[i]["capture_time"], rows[i]["clip"]))
    elif args.mode == "energy":
        order = energy_order(list(range(n)), motion, args.curve)
    elif args.mode == "similarity":
        order = similarity_chain(list(range(n)), hist, rows)
    elif args.mode == "cluster":
        order = sorted(range(n), key=lambda i: (labels[i], -motion[i]))
    else:  # combined
        cl_ids = sorted(set(labels), key=lambda c: np.mean([motion[i] for i in range(n) if labels[i] == c]))
        cl_seq = energy_order(cl_ids, {c: np.mean([motion[i] for i in range(n) if labels[i] == c]) for c in cl_ids}, args.curve)
        order = []
        for c in cl_seq:
            members = [i for i in range(n) if labels[i] == c]
            seed = min(members, key=lambda i: motion[i])
            order += similarity_chain(members, hist, rows, seed=seed)

    # ---- pick an output tag ------------------------------------------------
    if args.tag:
        tag = re.sub(r"[^A-Za-z0-9._-]+", "-", args.tag).strip("-") or "custom"
    elif args.mode in ("energy", "combined"):
        tag = f"{args.mode}-{args.curve}"
    else:
        tag = args.mode

    cat = os.path.dirname(os.path.abspath(args.manifest))

    def write_order_csv(path):
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["position", "cluster", "clip", "motion_energy", "mean_luma",
                        "hue_deg", "duration_s", "capture_time"])
            for pos, i in enumerate(order):
                w.writerow([pos, int(labels[i]), rows[i]["clip"], rows[i]["motion_energy"],
                            rows[i]["mean_luma"], rows[i]["hue_deg"], rows[i]["duration_s"],
                            rows[i]["capture_time"]])

    def write_concat(path):
        with open(path, "w") as fh:
            for i in order:
                p = rows[i]["clip"].replace("'", "'\\''")
                fh.write(f"file '{p}'\n")

    def write_m3u(path):
        with open(path, "w") as fh:
            fh.write("#EXTM3U\n")
            for i in order:
                fh.write(rows[i]["clip"] + "\n")

    # tagged copies — these accumulate so you can compare arrangements
    order_csv = os.path.join(cat, f"order-{tag}.csv")
    concat = os.path.join(cat, f"playlist-{tag}.txt")
    m3u = os.path.join(cat, f"playlist-{tag}.m3u")
    write_order_csv(order_csv)
    write_concat(concat)
    write_m3u(m3u)

    # canonical "latest" copies so clip_gallery.py + muscle-memory commands
    # always reflect the most recent run
    write_order_csv(os.path.join(cat, "order.csv"))
    write_concat(os.path.join(cat, "playlist.txt"))
    write_m3u(os.path.join(cat, "playlist.m3u"))

    out_mp4 = os.path.join(cat, f"sequence-{tag}.mp4")
    print(f"Ordered {n} clips ({dropped} culled) — mode={args.mode}, curve={args.curve}, tag={tag}")
    print(f"  order:    {order_csv}")
    print(f"  concat:   {concat}")
    print(f"  playlist: {m3u}  (open in VLC to audition)")
    print("  (also refreshed order.csv / playlist.txt as the 'latest' for the gallery)")
    print("\nRender this arrangement with:")
    print(f"  ffmpeg -f concat -safe 0 -i '{concat}' -c copy '{out_mp4}'")
    print("  # if clips have mismatched params, re-encode instead of -c copy:")
    print(f"  ffmpeg -f concat -safe 0 -i '{concat}' -c:v libx264 -crf 18 -c:a aac '{out_mp4}'")


if __name__ == "__main__":
    main()
