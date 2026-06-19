#!/usr/bin/env python3
"""
sync_clips.py — match clips to a track's musical structure.

Takes a track analysis (from track_analyze.py) and the clip manifest (from
clip_features.py), carves the song into cut slots at musical boundaries, and
assigns each slot a clip whose motion energy matches the song's energy there.
So action clips land on loud passages, calm clips on quiet ones, and cuts fall
on the beat / section change.

Grids (--grid):
  sections   one cut per structural section          (default, calmest)
  downbeats  one cut per bar                          (driving, on-beat)
  beats      one cut every --beats-per-cut beats      (fast, montage)
  harmonic   one cut per harmonic-change point        (cut on chord changes)

Outputs (into the analysis dir):
  order-sync-<track>.csv   slot table: time, duration, target vs clip energy
  <track>.labels.txt       Audacity label track (import to see cuts on the wave)
  <track>.markers.csv      generic time,label markers (Reaper/DAW import)
  render-<track>.sh        ffmpeg script: cut clips to grid + lay music on top

Usage:
  python3 sync_clips.py --analysis catalog_audio/song.analysis.json \
                        --manifest catalog/manifest.csv --grid downbeats
  python3 sync_clips.py --analysis ... --manifest ... --grid beats --beats-per-cut 2

Requires: numpy  (scikit-learn optional, only for similarity tiebreak)
"""
import argparse
import csv
import json
import os
import re
import sys

import numpy as np


def load_manifest(path):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            r["motion_energy"] = float(r.get("motion_energy") or 0)
            r["duration_s"] = float(r.get("duration_s") or 0)
            r["sharpness"] = float(r.get("sharpness") or 0)
            rows.append(r)
    return rows


def build_grid(an, grid, beats_per_cut):
    dur = an["duration_s"]
    if grid == "sections":
        pts = an["sections"]
    elif grid == "downbeats":
        pts = an["downbeats"]
    elif grid == "harmonic":
        pts = an["harmonic_changes"]
    elif grid == "beats":
        pts = an["beats"][::max(1, beats_per_cut)]
    else:
        pts = an["sections"]
    pts = sorted(set([0.0] + [p for p in pts if 0 < p < dur] + [dur]))
    # slots = consecutive pairs
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def energy_at(an, t0, t1):
    env = an["energy_envelope"]
    ts, vs = np.array(env["times"]), np.array(env["rms"])
    if len(ts) == 0:
        return 0.5
    mask = (ts >= t0) & (ts < t1)
    return float(vs[mask].mean()) if mask.any() else float(np.interp((t0 + t1) / 2, ts, vs))


def main():
    ap = argparse.ArgumentParser(description="Sync clips to a track.")
    ap.add_argument("--analysis", required=True, help="track .analysis.json")
    ap.add_argument("--manifest", required=True, help="catalog/manifest.csv")
    ap.add_argument("--grid", default="sections",
                    choices=["sections", "downbeats", "beats", "harmonic"])
    ap.add_argument("--beats-per-cut", type=int, default=4)
    ap.add_argument("--allow-reuse", action="store_true",
                    help="let clips repeat if there are more slots than clips")
    ap.add_argument("--drop-blurry", type=float, default=0.0,
                    help="ignore clips below this sharpness")
    ap.add_argument("--clip-from", default="middle", choices=["middle", "start"],
                    help="where in a long clip to take the slot-length piece")
    ap.add_argument("--tag", default="",
                    help="suffix for output names so different grids don't "
                         "overwrite each other (e.g. order-sync-<track>-<tag>.csv)")
    args = ap.parse_args()

    with open(args.analysis) as fh:
        an = json.load(fh)
    clips = [c for c in load_manifest(args.manifest) if c["sharpness"] >= args.drop_blurry]
    if not clips:
        sys.exit("No clips after culling.")

    slots = build_grid(an, args.grid, args.beats_per_cut)
    if not slots:
        sys.exit("No slots — does the analysis have the chosen grid points?")

    # normalize clip motion and slot energy onto 0..1 so they're comparable
    cm = np.array([c["motion_energy"] for c in clips])
    cm_n = (cm - cm.min()) / (cm.max() - cm.min()) if cm.max() > cm.min() else np.zeros_like(cm)
    targets = np.array([energy_at(an, a, b) for a, b in slots])

    # greedy assignment: each slot takes the available clip closest in energy
    available = set(range(len(clips)))
    assignments = []
    for si, (a, b) in enumerate(slots):
        slot_dur = b - a
        tgt = targets[si]
        pool = available if (available and not args.allow_reuse) else set(range(len(clips)))
        # prefer clips at least as long as the slot so we can trim cleanly
        long_enough = [i for i in pool if clips[i]["duration_s"] >= slot_dur]
        cand = long_enough or list(pool)
        best = min(cand, key=lambda i: abs(cm_n[i] - tgt))
        assignments.append((si, a, b, slot_dur, tgt, best))
        if not args.allow_reuse:
            available.discard(best)
            if not available:
                available = set(range(len(clips)))  # wrap if we run out

    out_dir = os.path.dirname(os.path.abspath(args.analysis))
    track = an["track"]
    # optional tag suffix so different grids don't clobber each other's sidecars
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", args.tag).strip("-")
    sfx = f"-{tag}" if tag else ""

    # 1) order csv
    order_csv = os.path.join(out_dir, f"order-sync-{track}{sfx}.csv")
    with open(order_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["slot", "song_start_s", "song_end_s", "slot_dur_s",
                    "target_energy", "clip_motion_norm", "clip"])
        for si, a, b, d, tgt, i in assignments:
            w.writerow([si, round(a, 3), round(b, 3), round(d, 3),
                        round(tgt, 3), round(float(cm_n[i]), 3), clips[i]["clip"]])

    # 2) Audacity label track  (start \t end \t label)
    labels = os.path.join(out_dir, f"{track}{sfx}.labels.txt")
    with open(labels, "w") as fh:
        for si, a, b, d, tgt, i in assignments:
            fh.write(f"{a:.3f}\t{b:.3f}\tcut{si} ({os.path.basename(clips[i]['clip'])})\n")

    # 3) generic markers (time,label) for DAW import
    markers = os.path.join(out_dir, f"{track}{sfx}.markers.csv")
    with open(markers, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_s", "label"])
        for si, a, b, d, tgt, i in assignments:
            w.writerow([round(a, 3), f"cut{si}"])

    # 4) ffmpeg render script: trim each clip to its slot, concat, add music
    render = os.path.join(out_dir, f"render-{track}{sfx}.sh")
    nseg = len(assignments)
    # one segment + concat + mux -> total progress steps the engine can parse
    ntotal = nseg + 2
    with open(render, "w") as fh:
        fh.write("#!/usr/bin/env bash\nset -euo pipefail\n")
        fh.write(f'# Renders a cut of {track} synced to the music ({args.grid} grid)\n')
        fh.write('TMP="$(mktemp -d)"\n')
        fh.write(f'MUSIC="{an["path"]}"\n')
        fh.write('echo "rendering segments..."\n')
        for seg, (si, a, b, d, tgt, i) in enumerate(assignments):
            clip = clips[i]["clip"].replace('"', '\\"')
            cdur = clips[i]["duration_s"]
            ss = max(0.0, (cdur - d) / 2) if args.clip_from == "middle" else 0.0
            # progress marker the engine parses as [done/total] for a real bar
            fh.write(f'echo "[{seg + 1}/{ntotal}] segment {seg + 1}/{nseg}"\n')
            fh.write(
                f'ffmpeg -nostdin -loglevel error -ss {ss:.3f} -i "{clip}" -t {d:.3f} '
                f'-vf "scale=720:480:force_original_aspect_ratio=decrease,'
                f'pad=720:480:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30" '
                f'-an -c:v libx264 -crf 18 -preset fast "$TMP/{seg:04d}.mp4"\n')
        fh.write(f'echo "[{nseg + 1}/{ntotal}] concatenating segments"\n')
        fh.write('for f in "$TMP"/*.mp4; do echo "file \'$f\'"; done > "$TMP/list.txt"\n')
        fh.write('ffmpeg -nostdin -loglevel error -f concat -safe 0 -i "$TMP/list.txt" '
                 '-c copy "$TMP/video.mp4"\n')
        fh.write(f'echo "[{ntotal}/{ntotal}] muxing audio"\n')
        fh.write(f'ffmpeg -nostdin -loglevel error -i "$TMP/video.mp4" -i "$MUSIC" '
                 f'-map 0:v -map 1:a -c:v copy -c:a aac -shortest "./cut-{track}{sfx}.mp4"\n')
        fh.write(f'echo "wrote ./cut-{track}{sfx}.mp4"\nrm -rf "$TMP"\n')
    os.chmod(render, 0o755)

    # console summary
    matchpct = 100 * (1 - np.mean([abs(cm_n[i] - tgt) for _, _, _, _, tgt, i in assignments]))
    print(f"Synced {len(assignments)} cuts to '{track}' "
          f"({an['tempo_bpm']:.0f} BPM, {an['key']}) on the {args.grid} grid")
    print(f"  energy match: ~{matchpct:.0f}%   slots: {len(slots)}   clips: {len(clips)}")
    print(f"  order:   {order_csv}")
    print(f"  labels:  {labels}   (Audacity: File > Import > Labels)")
    print(f"  markers: {markers}")
    print(f"  render:  bash {render}   ->  cut-{track}{sfx}.mp4")


if __name__ == "__main__":
    main()
