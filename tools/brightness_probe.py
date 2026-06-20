#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
brightness_probe.py — measure how a reference video's brightness tracks the beat.

A calibration tool, not a pipeline stage. Point it at a music video (e.g.
Prodigy's "Firestarter", which cuts bright flashes hard on the rhythm) and it
answers, with numbers: do the bright frames land on musical accents, and how
tightly? The output sets the flash-on-accent weights for the brightness-aware
matcher (sync_clips) instead of guessing them.

What it does:
  1. luma(t)   — mean frame brightness over time, pulled at the video's own fps
                 via ffmpeg (downscaled to a tiny gray frame, so 4K AV1 is cheap).
  2. accents   — beats / downbeats / onset strength from the *audio*, using the
                 exact librosa calls track_analyze uses (sr=22050, hop=512), so
                 the result is in the same frame of reference as a real arrange.
  3. alignment — cross-correlation of luma vs onset strength (best lag + r), plus
                 a flash↔accent timing histogram: of the detected brightness
                 flashes, what fraction land within ±k frames of a beat/downbeat,
                 and what fraction of downbeats get a flash.

Usage:
  python3 tools/brightness_probe.py "/path/Firestarter.mp4"
  python3 tools/brightness_probe.py VIDEO --audio AUDIO.m4a --tol-frames 2 --out probe.png

Requires: ffmpeg/ffprobe on PATH, librosa, numpy, scipy, matplotlib.
"""
import argparse
import os
import subprocess
import sys

import numpy as np

# reuse the pipeline's downbeat picker so accents match a real arrangement
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.track_analyze import find_downbeats  # noqa: E402

SR = 22050
HOP = 512


def video_fps(path):
    """Real frame rate from ffprobe's r_frame_rate ('25/1' -> 25.0)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.strip()
    if "/" in out:
        n, d = out.split("/")
        return float(n) / float(d) if float(d) else 25.0
    return float(out) if out else 25.0


def luma_curve(path, fps, w=64, h=36):
    """mean brightness per frame (0..1) + frame times, via an ffmpeg gray pipe.

    ffmpeg decodes at full res then scales to w*h gray, so cost is the decode;
    the tiny output keeps memory/parse trivial even for a 4-minute 4K clip."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
           "-vf", f"fps={fps},scale={w}:{h},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    frame_bytes = w * h
    n = len(raw) // frame_bytes
    if n == 0:
        sys.exit("brightness_probe: ffmpeg returned no frames (decode failed?).")
    arr = np.frombuffer(raw[:n * frame_bytes], dtype=np.uint8).reshape(n, h * w).astype(np.float32)
    luma = arr.mean(axis=1) / 255.0
    times = np.arange(n) / fps
    return times, luma, arr


def audio_accents(path, bpm=None):
    """beats, downbeats, and the onset-strength envelope (times, values, 0..1).
    Pass bpm to pin the tempo (librosa often locks onto half/double time)."""
    import librosa
    y, sr = librosa.load(path, sr=SR, mono=True)
    kw = {"bpm": float(bpm)} if bpm else {}
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP, **kw)
    tempo = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    bf = np.clip(beat_frames, 0, len(onset_env) - 1)
    beat_strength = onset_env[bf] if len(bf) else np.array([])
    downbeats = np.asarray(find_downbeats(beat_times, beat_strength, meter=4), dtype=float)
    env_t = librosa.frames_to_time(np.arange(len(onset_env)), sr=sr, hop_length=HOP)
    env = onset_env.astype(float)
    env = (env - env.min()) / (env.max() - env.min()) if env.max() > env.min() else env
    return tempo, np.asarray(beat_times, dtype=float), downbeats, env_t, env


def _peaks(z, height, distance):
    """Local maxima of a z-scored signal (scipy if present, else a simple scan)."""
    try:
        from scipy.signal import find_peaks
        idx, _ = find_peaks(z, height=height, distance=max(1, distance))
        return idx
    except Exception:
        return np.where((z[1:-1] > height) & (z[1:-1] >= z[:-2]) & (z[1:-1] > z[2:]))[0] + 1


def detect_flashes(times, luma, fps):
    """Brightness flashes: prominent local maxima of luma (>=0.75 sigma)."""
    z = (luma - luma.mean()) / (luma.std() or 1.0)
    return times[_peaks(z, 0.75, int(fps / 8))]


def content_change(frames):
    """Frame-to-frame content change (mean abs pixel diff, 0..1) — spikes at cuts
    (and hard strobes), even between shots of equal brightness."""
    if len(frames) < 2:
        return np.zeros(len(frames))
    d = np.abs(np.diff(frames, axis=0)).mean(axis=1) / 255.0
    return np.r_[0.0, d]


def detect_cuts(times, change, fps):
    """Shot cuts: prominent spikes in the content-change signal (>=1.5 sigma,
    at least ~1/6 s apart so a single cut isn't double-counted)."""
    z = (change - change.mean()) / (change.std() or 1.0)
    return times[_peaks(z, 1.5, int(fps / 6))]


def chance_pct(n_refs, duration, tol):
    """Fraction of the timeline within +/-tol of one of n_refs uniform events —
    the null hypothesis an alignment % must beat to mean anything."""
    return min(1.0, n_refs * 2 * tol / duration) * 100 if duration else float("nan")


def nearest_delta(events, refs):
    """For each event time, signed seconds to the nearest ref time (event - ref)."""
    if len(events) == 0 or len(refs) == 0:
        return np.array([])
    refs = np.sort(refs)
    pos = np.searchsorted(refs, events)
    deltas = []
    for e, p in zip(events, pos):
        cands = []
        if p < len(refs):
            cands.append(e - refs[p])
        if p > 0:
            cands.append(e - refs[p - 1])
        deltas.append(min(cands, key=abs))
    return np.asarray(deltas)


def xcorr_lag(luma_t, luma, env_t, env, max_lag_s=0.5):
    """Best lag (s) and Pearson r between luma and onset env on a common grid.
    Positive lag = luma follows the onset (brightness comes after the hit)."""
    dur = min(luma_t[-1], env_t[-1])
    grid = np.arange(0, dur, 1 / 50.0)            # 50 Hz common grid
    L = np.interp(grid, luma_t, luma)
    E = np.interp(grid, env_t, env)
    L = (L - L.mean()) / (L.std() or 1.0)
    E = (E - E.mean()) / (E.std() or 1.0)
    max_lag = int(max_lag_s * 50)
    best = (0.0, -2.0)
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            a, b = L[-lag:], E[:lag] if lag else E
        elif lag > 0:
            a, b = L[:-lag], E[lag:]
        else:
            a, b = L, E
        m = min(len(a), len(b))
        if m < 10:
            continue
        r = float(np.corrcoef(a[:m], b[:m])[0, 1])
        if r > best[1]:
            best = (lag / 50.0, r)
    return best


def main():
    ap = argparse.ArgumentParser(description="Measure brightness-vs-beat alignment in a reference video.")
    ap.add_argument("video", help="reference music video (any ffmpeg-readable file)")
    ap.add_argument("--audio", default="", help="separate audio file (default: the video's own track)")
    ap.add_argument("--tol-frames", type=int, default=2, help="flash/accent match tolerance, in video frames")
    ap.add_argument("--bpm", type=float, default=None, help="pin tempo (librosa often locks half/double time)")
    ap.add_argument("--out", default="", help="plot path (default: <video>.brightness.png)")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        sys.exit(f"not found: {args.video}")
    audio = args.audio or args.video
    fps = video_fps(args.video)

    print(f"[1/3] luma + content-change @ {fps:g} fps …", flush=True)
    lt, luma, frames = luma_curve(args.video, fps)
    change = content_change(frames)
    print(f"[2/3] audio accents (beats/downbeats/onset) …", flush=True)
    tempo, beats, downbeats, env_t, env = audio_accents(audio, bpm=args.bpm)
    print(f"[3/3] alignment …", flush=True)

    flashes = detect_flashes(lt, luma, fps)
    cuts = detect_cuts(lt, change, fps)
    tol = args.tol_frames / fps
    dur = float(lt[-1])
    lag, r = xcorr_lag(lt, luma, env_t, env)

    def pct(d):
        return 100.0 * np.mean(np.abs(d) <= tol) if len(d) else float("nan")

    def line(label, events, refs, n_refs):
        d = nearest_delta(events, refs)
        if not len(d):
            return f"  {label:22}: n/a"
        ch = chance_pct(n_refs, dur, tol)
        med = np.median(np.abs(d)) * 1000
        lift = pct(d) / ch if ch else float("nan")
        return (f"  {label:22}: {pct(d):2.0f}%  (chance {ch:2.0f}%, {lift:.1f}× | "
                f"median |Δ| {med:3.0f} ms)")

    print("\n──────── brightness/cuts ↔ beat alignment ────────")
    print(f"  video                 : {os.path.basename(args.video)}")
    print(f"  duration / fps        : {dur:.1f}s @ {fps:g}fps  ({len(luma)} frames)")
    print(f"  tempo                 : {tempo:.1f} BPM   beats={len(beats)} downbeats={len(downbeats)}")
    print(f"  match tolerance       : ±{args.tol_frames} frames (±{tol*1000:.0f} ms)")
    print(f"  mean/peak luma        : {luma.mean():.3f} / {luma.max():.3f}")
    print(f"  -- brightness flashes ({len(flashes)}) --")
    print(line("flashes on a beat", flashes, beats, len(beats)))
    print(line("flashes on a downbeat", flashes, downbeats, len(downbeats)))
    print(f"  -- shot cuts ({len(cuts)}) --")
    print(line("cuts on a beat", cuts, beats, len(beats)))
    print(line("cuts on a downbeat", cuts, downbeats, len(downbeats)))
    print(f"  luma×onset xcorr      : r={r:.2f} at lag {lag*1000:+.0f} ms")
    print("──────────────────────────────────────────────────")
    print("  (lift = observed / chance; ~1.0× means no real lock, >1.5× is a real tendency)")

    # plot
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        out = args.out or (os.path.splitext(args.video)[0] + ".brightness.png")
        fig, (a1, a3, a2) = plt.subplots(3, 1, figsize=(16, 8), sharex=True)
        for ax in (a1, a3):
            for t in beats:
                ax.axvline(t, color="#ddd", lw=0.3, zorder=0)
            for t in downbeats:
                ax.axvline(t, color="#5b9dff", lw=0.8, zorder=0)
        a1.plot(lt, luma, color="#222", lw=0.7, label="luma")
        a1.plot(flashes, np.interp(flashes, lt, luma), "r.", ms=5, label="flash")
        a1.set_ylabel("brightness"); a1.legend(loc="upper right")
        a1.set_title(f"{os.path.basename(args.video)} — luma & cuts vs beats (blue=downbeat), onset env")
        a3.plot(lt, change, color="#444", lw=0.6, label="frame Δ")
        a3.plot(cuts, np.interp(cuts, lt, change), "g.", ms=5, label="cut")
        a3.set_ylabel("content Δ"); a3.legend(loc="upper right")
        a2.plot(env_t, env, color="#e07b39", lw=0.7); a2.set_ylabel("onset"); a2.set_xlabel("seconds")
        a2.set_xlim(0, dur)
        fig.tight_layout(); fig.savefig(out, dpi=110)
        print(f"  plot → {out}")
    except Exception as exc:
        print(f"  (plot skipped: {exc})")


if __name__ == "__main__":
    main()
