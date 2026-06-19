#!/usr/bin/env python3
"""
track_analyze.py — musical analysis of a folder of audio tracks.

For each track it estimates:
  - tempo (BPM) and the beat grid
  - approximate downbeats (bar starts; assumes 4/4)
  - overall key (Krumhansl-Schmuckler)
  - section boundaries (verse/chorus-type structural shifts)
  - harmonic-change points (where the harmony shifts — chord-change proxy,
    via a Harmonic Change Detection Function over chroma)
  - a normalized energy (RMS) envelope over time

Outputs (into --out, default ./catalog_audio):
  <track>.analysis.json   full analysis, consumed by sync_clips.py
  tracks_summary.csv      one row per track (tempo, key, counts)
  <track>.png             optional plot (--plot), needs matplotlib

Usage:
  python3 track_analyze.py "/Volumes/Foortage/musicvideo/album-audio"
  python3 track_analyze.py ./album-audio -o ./catalog_audio --plot

Requires: librosa, numpy, scipy  (pip install librosa)
          ffmpeg on PATH helps librosa decode mp3/m4a.
"""
import argparse
import json
import os
import sys
from glob import glob

import numpy as np

AUDIO_EXTS = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".wma")

# Krumhansl-Schmuckler key profiles
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def estimate_key(chroma):
    """Return a key string like 'G major' from a mean chroma vector."""
    v = chroma.mean(axis=1)
    v = v / (v.sum() + 1e-9)
    best = (-1e9, "C", "major")
    for i in range(12):
        maj = np.corrcoef(np.roll(KS_MAJOR, i), v)[0, 1]
        minr = np.corrcoef(np.roll(KS_MINOR, i), v)[0, 1]
        if maj > best[0]:
            best = (maj, PITCHES[i], "major")
        if minr > best[0]:
            best = (minr, PITCHES[i], "minor")
    return f"{best[1]} {best[2]}"


def find_downbeats(beat_times, beat_strength, meter=4):
    """Pick the bar-start phase (0..meter-1) that maximizes onset strength."""
    if len(beat_times) < meter:
        return beat_times[:1].tolist()
    best_phase, best_score = 0, -1e9
    for p in range(meter):
        s = beat_strength[p::meter].sum()
        if s > best_score:
            best_score, best_phase = s, p
    return beat_times[best_phase::meter].tolist()


def harmonic_changes(y, sr, hop):
    """Harmonic Change Detection Function -> list of change times (seconds)."""
    import librosa
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    # smooth across time, then measure frame-to-frame tonal distance
    chroma = np.apply_along_axis(
        lambda m: np.convolve(m, np.ones(9) / 9, mode="same"), axis=1, arr=chroma)
    diff = np.sqrt(((chroma[:, 1:] - chroma[:, :-1]) ** 2).sum(axis=0))
    if diff.max() > 0:
        diff = diff / diff.max()
    peaks = librosa.util.peak_pick(
        diff, pre_max=8, post_max=8, pre_avg=16, post_avg=16, delta=0.12, wait=16)
    return librosa.frames_to_time(peaks, sr=sr, hop_length=hop).tolist()


def sections(y, sr, hop, k):
    """Structural boundaries (seconds) via agglomerative clustering of features."""
    import librosa
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
    feat = np.vstack([librosa.util.normalize(chroma, axis=0),
                      librosa.util.normalize(mfcc, axis=0)])
    k = max(2, min(k, feat.shape[1] - 1))
    bounds = librosa.segment.agglomerative(feat, k)
    return sorted(set(librosa.frames_to_time(bounds, sr=sr, hop_length=hop).tolist()))


def downsample_env(rms, times, hz):
    """Resample an RMS curve to a fixed rate, normalized 0..1."""
    if len(times) < 2:
        return [], []
    dur = times[-1]
    n = max(2, int(dur * hz))
    grid = np.linspace(0, dur, n)
    vals = np.interp(grid, times, rms)
    if vals.max() > vals.min():
        vals = (vals - vals.min()) / (vals.max() - vals.min())
    return [round(t, 3) for t in grid], [round(float(v), 4) for v in vals]


def analyze(path, sr_target, want_plot, out_dir):
    import librosa
    y, sr = librosa.load(path, sr=sr_target, mono=True)
    hop = 512
    duration = float(librosa.get_duration(y=y, sr=sr))

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop)
    tempo = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    bf = np.clip(beat_frames, 0, len(onset_env) - 1)
    beat_strength = onset_env[bf] if len(bf) else np.array([])
    downbeats = find_downbeats(beat_times, beat_strength, meter=4)

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    key = estimate_key(chroma)

    k = int(round(duration / 18)) + 1  # ~ a section every 18s
    secs = sections(y, sr, hop, k)
    hchanges = harmonic_changes(y, sr, hop)

    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    env_t, env_v = downsample_env(rms, rms_times, hz=4)

    name = os.path.splitext(os.path.basename(path))[0]
    result = {
        "track": name,
        "path": os.path.abspath(path),
        "duration_s": round(duration, 3),
        "sr": sr,
        "tempo_bpm": round(tempo, 2),
        "key": key,
        "beats": [round(t, 3) for t in beat_times.tolist()],
        "downbeats": [round(t, 3) for t in downbeats],
        "sections": [round(t, 3) for t in secs],
        "harmonic_changes": [round(t, 3) for t in hchanges],
        "energy_envelope": {"hz": 4, "times": env_t, "rms": env_v},
    }

    if want_plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(14, 3))
            ax.plot(env_t, env_v, color="#5b9dff", lw=1, label="energy")
            for t in secs:
                ax.axvline(t, color="#ff8b5b", lw=1.2)
            for t in downbeats:
                ax.axvline(t, color="#5bd6a0", lw=0.4, alpha=0.5)
            ax.set_title(f"{name} — {tempo:.0f} BPM, {key}")
            ax.set_xlabel("seconds"); ax.set_ylabel("energy"); ax.set_xlim(0, duration)
            fig.tight_layout()
            fig.savefig(os.path.join(out_dir, name + ".png"), dpi=110)
            plt.close(fig)
        except Exception as e:
            print(f"   (plot skipped: {e})")

    return result


def main():
    ap = argparse.ArgumentParser(description="Analyze audio tracks.")
    ap.add_argument("audio", help="audio file or folder of tracks")
    ap.add_argument("-o", "--out", default="./catalog_audio", help="output dir")
    ap.add_argument("--sr", type=int, default=22050, help="analysis sample rate")
    ap.add_argument("--plot", action="store_true", help="also write a PNG per track")
    args = ap.parse_args()

    if os.path.isdir(args.audio):
        paths = sorted(p for p in glob(os.path.join(args.audio, "*"))
                       if p.lower().endswith(AUDIO_EXTS))
    else:
        paths = [args.audio]
    if not paths:
        sys.exit(f"No audio files found at {args.audio}")

    os.makedirs(args.out, exist_ok=True)
    summary = []
    for i, p in enumerate(paths, 1):
        print(f"[{i}/{len(paths)}] {os.path.basename(p)}", flush=True)
        try:
            res = analyze(p, args.sr, args.plot, args.out)
        except Exception as e:
            print(f"   ! failed: {e}")
            continue
        with open(os.path.join(args.out, res["track"] + ".analysis.json"), "w") as fh:
            json.dump(res, fh, indent=1)
        print(f"   {res['tempo_bpm']:.0f} BPM · {res['key']} · "
              f"{len(res['sections'])} sections · "
              f"{len(res['harmonic_changes'])} harmonic changes")
        summary.append(res)

    import csv
    with open(os.path.join(args.out, "tracks_summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["track", "duration_s", "tempo_bpm", "key", "n_beats",
                    "n_downbeats", "n_sections", "n_harmonic_changes"])
        for r in summary:
            w.writerow([r["track"], r["duration_s"], r["tempo_bpm"], r["key"],
                        len(r["beats"]), len(r["downbeats"]),
                        len(r["sections"]), len(r["harmonic_changes"])])

    print(f"\nDone. {len(summary)} tracks → {args.out}")
    print(f"  summary: {os.path.join(args.out, 'tracks_summary.csv')}")
    print("Next: pick a track and sync clips to it, e.g.")
    if summary:
        ex = summary[0]["track"]
        print(f"  python3 sync_clips.py --analysis {args.out}/{ex}.analysis.json "
              f"--manifest ./catalog/manifest.csv --grid sections")


if __name__ == "__main__":
    main()
