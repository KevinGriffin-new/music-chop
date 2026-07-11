#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
scene_split.py — scene-detect one video and split it into clips, with progress.

Replaces the opaque PySceneDetect CLI call (`scenedetect ... split-video`)
that engine.detect() used to make. That call was silent for the whole run —
on a long source the UI sat on one message for half an hour with no way to
tell "working" from "stuck". This script does the same job in two visible
phases, each streaming progress the engine can render:

  1. scan   — ContentDetector over the video (the PySceneDetect Python API),
              progress polled from the decoder about once a second.
  2. split  — one ffmpeg per scene. Default re-encodes (frame-exact cuts,
              same libx264 settings the CLI used); --mode copy stream-copies
              instead: lossless and ~disk-speed, with cuts snapped to the
              nearest keyframe (fine when scenes are minutes, e.g. live sets).

Progress protocol (parsed by engine.detect, same as track_analyze):
  PROG i/1000 <message>     e.g. "PROG 412/1000 re-encoding scene 3/17"
Lines starting with "WARN " are surfaced to the UI as-is.

Output naming matches the old CLI (<base>-Scene-NNN.mp4) so idempotence
globs and the catalog keep working unchanged.

Usage:
  python3 scene_split.py -i VIDEO -o CLIPS_DIR
  python3 scene_split.py -i VIDEO -o CLIPS_DIR --mode copy
  python3 scene_split.py -i VIDEO -o CLIPS_DIR --threshold 27 \\
          --min-scene-len 0.6s --rate-factor 18 --preset slow

Requires: scenedetect (PySceneDetect), ffmpeg/ffprobe on PATH.
"""
import argparse
import os
import subprocess
import sys
import threading

SCALE = 1000                     # PROG denominator (fixed so the bar only advances)
# codecs that survive a stream copy into an .mp4 container
MP4_COPY_SAFE = {"h264", "hevc", "mpeg4", "av1"}


def prog(i, msg):
    print(f"PROG {min(int(i), SCALE)}/{SCALE} {msg}", flush=True)


def ffprobe_field(path, entries, select=None):
    cmd = ["ffprobe", "-v", "error", "-show_entries", entries,
           "-of", "default=nw=1:nk=1", path]
    if select:
        cmd[3:3] = ["-select_streams", select]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def keyframe_times(path):
    """pts of every video keyframe, via a packet scan (no decode)."""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True)
    return sorted(float(line.split(",")[0]) for line in p.stdout.splitlines()
                  if ",K" in line and line.split(",")[0])


def snap_to_keyframes(bounds, keyframes, min_len_s=0.25):
    """Snap interior scene bounds to the nearest keyframe.

    `bounds` is [0, cut1, .., cutK, duration]; first/last stay put. Snapped
    cuts that collapse a segment below min_len_s are dropped (the two scenes
    merge). Pure function — unit-tested without video files.
    """
    if not keyframes or len(bounds) < 3:
        return list(bounds)
    snapped = [bounds[0]]
    for b in bounds[1:-1]:
        k = min(keyframes, key=lambda t: abs(t - b))
        if k - snapped[-1] >= min_len_s:
            snapped.append(k)
    if bounds[-1] - snapped[-1] < min_len_s and len(snapped) > 1:
        snapped.pop()
    snapped.append(bounds[-1])
    return snapped


def scan_scenes(path, threshold, min_len_s, on_progress):
    """Detect scene bounds → [0, cut.., duration] (seconds). Progress is the
    decoder's frame position, polled from a side thread ~1/s."""
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(path)
    fps = video.frame_rate or 25.0
    total = max(1, video.duration.get_frames())
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=threshold,
                                    min_scene_len=max(1, round(min_len_s * fps))))
    stop = threading.Event()

    def poll():
        while not stop.wait(1.0):
            on_progress(video.frame_number / total)

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    try:
        sm.detect_scenes(video, show_progress=False)
    finally:
        stop.set()
        t.join()

    duration = video.duration.get_seconds()
    scenes = sm.get_scene_list()
    cuts = [s.get_seconds() for s, _ in scenes[1:]]  # interior boundaries only
    return [0.0] + cuts + [duration]


def run_ffmpeg(args, on_out_time=None):
    """Run ffmpeg with -progress on stdout; call on_out_time(seconds) as the
    encode advances. Raises on non-zero exit with stderr context."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error",
           "-progress", "pipe:1", "-nostats", *args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1)
    for line in p.stdout:
        if on_out_time and line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                on_out_time(int(line.split("=", 1)[1]) / 1e6)
            except ValueError:
                pass
    p.wait()
    if p.returncode != 0:
        tail = (p.stderr.read() or "").strip()[-2000:]
        sys.exit(f"ffmpeg exited {p.returncode} splitting {args[-1]}\n{tail}")


def split(src, bounds, out_dir, mode, rate_factor, preset, scan_end):
    """One ffmpeg per scene; PROG advances from scan_end to SCALE weighted by
    scene duration (re-encode cost tracks length, and copy is near-instant)."""
    base = os.path.splitext(os.path.basename(src))[0]
    total_dur = max(bounds[-1] - bounds[0], 1e-6)
    span = SCALE - scan_end
    n = len(bounds) - 1
    done = 0.0
    for j in range(n):
        a, b = bounds[j], bounds[j + 1]
        out = os.path.join(out_dir, f"{base}-Scene-{j + 1:03d}.mp4")
        label = ("copying" if mode == "copy" else "re-encoding") + f" scene {j + 1}/{n}"
        prog(scan_end + span * done / total_dur, label)

        def tick(t, _done=done):
            prog(scan_end + span * (_done + min(t, b - a)) / total_dur, label)

        if mode == "copy":
            run_ffmpeg(["-y", "-ss", f"{a + 0.001:.3f}", "-i", src,
                        "-t", f"{b - a:.3f}", "-map", "0:v:0", "-map", "0:a?",
                        "-sn", "-c", "copy", "-avoid_negative_ts", "make_zero",
                        out], on_out_time=tick)
        else:
            run_ffmpeg(["-y", "-ss", f"{a:.3f}", "-i", src,
                        "-t", f"{b - a:.3f}", "-map", "0:v:0", "-map", "0:a?",
                        "-sn", "-c:v", "libx264", "-preset", preset,
                        "-crf", str(rate_factor), "-c:a", "aac",
                        out], on_out_time=tick)
        done += b - a
    prog(SCALE, f"split into {n} clip(s)")


def parse_seconds(v):
    """'0.6s' or '0.6' → 0.6 (mirrors the scenedetect CLI's suffix style)."""
    return float(v[:-1] if v.rstrip().endswith("s") else v)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("-i", "--input", required=True, help="source video")
    ap.add_argument("-o", "--output", required=True, help="clips dir")
    ap.add_argument("--threshold", type=float, default=27.0)
    ap.add_argument("--min-scene-len", default="0.6s")
    ap.add_argument("--rate-factor", type=int, default=18)
    ap.add_argument("--preset", default="slow")
    ap.add_argument("--mode", choices=("encode", "copy"), default="encode",
                    help="encode = frame-exact re-encode (default); copy = "
                         "lossless stream copy, cuts snap to keyframes")
    args = ap.parse_args()

    src, mode = args.input, args.mode
    os.makedirs(args.output, exist_ok=True)
    min_len_s = parse_seconds(args.min_scene_len)

    if mode == "copy":
        codec = ffprobe_field(src, "stream=codec_name", select="v:0")
        if codec not in MP4_COPY_SAFE:
            print(f"WARN {codec or 'unknown'} video can't stream-copy into "
                  f".mp4 — re-encoding instead", flush=True)
            mode = "encode"

    # copy is ~free, so the scan dominates its bar; encode dominates the other
    scan_end = 900 if mode == "copy" else 300
    prog(0, "scanning for scene changes")
    bounds = scan_scenes(src, args.threshold, min_len_s,
                         lambda f: prog(scan_end * f, "scanning for scene changes"
                                        f" — {f:.0%}"))
    prog(scan_end, f"{len(bounds) - 1} scene(s) found")

    if mode == "copy":
        bounds = snap_to_keyframes(bounds, keyframe_times(src))
    split(src, bounds, args.output, mode, args.rate_factor, args.preset, scan_end)


if __name__ == "__main__":
    main()
