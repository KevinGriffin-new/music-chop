#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
track_analyze.py — musical analysis of a folder of audio tracks.

For each track it estimates:
  - tempo (BPM) and the beat grid
  - approximate downbeats (bar starts; assumes 4/4)
  - overall key (Krumhansl-Schmuckler)
  - section boundaries (verse/chorus-type structural shifts)
  - section labels: segments clustered by similarity (A/B/A/B...) so repeated
    material shares a letter, plus a chorus call — the recurring label with
    the highest energy ("" when nothing recurs; a fake chorus is worse than
    no chorus). If a <track>.lyrics.json sidecar exists in the output dir
    (from lyrics_analyze.py), the labels fuse lyric repetition with the
    acoustics — on band recordings acoustics alone often can't tell sections
    apart — and the chorus call is restricted to sung segments
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
import re
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


def harmonic_changes(chroma, sr, hop):
    """Harmonic Change Detection Function -> list of change times (seconds)."""
    import librosa
    # smooth across time, then measure frame-to-frame tonal distance
    chroma = np.apply_along_axis(
        lambda m: np.convolve(m, np.ones(9) / 9, mode="same"), axis=1, arr=chroma)
    diff = np.sqrt(((chroma[:, 1:] - chroma[:, :-1]) ** 2).sum(axis=0))
    if diff.max() > 0:
        diff = diff / diff.max()
    peaks = librosa.util.peak_pick(
        diff, pre_max=8, post_max=8, pre_avg=16, post_avg=16, delta=0.12, wait=16)
    return librosa.frames_to_time(peaks, sr=sr, hop_length=hop).tolist()


def sections(chroma, mfcc, sr, hop, k):
    """Structural boundaries (seconds) via agglomerative clustering of features."""
    import librosa
    feat = np.vstack([librosa.util.normalize(chroma, axis=0),
                      librosa.util.normalize(mfcc, axis=0)])
    k = max(2, min(k, feat.shape[1] - 1))
    bounds = librosa.segment.agglomerative(feat, k)
    return sorted(set(librosa.frames_to_time(bounds, sr=sr, hop_length=hop).tolist()))


def _norm_cols(M):
    """Min-max each column of an (n_seg, d) matrix onto 0..1 (flat cols -> 0)."""
    M = np.asarray(M, dtype=float)
    lo, hi = M.min(axis=0), M.max(axis=0)
    span = np.where(hi > lo, hi - lo, 1.0)
    out = (M - lo) / span
    out[:, hi <= lo] = 0.0
    return out


def segment_features(bounds, sr, hop, chroma, mfcc, rms):
    """Per-segment summary for labelling.

    bounds is the boundary list in seconds ([0, ..., duration]). Returns an
    (n_seg, d) feature matrix — mean chroma + mean MFCC per segment, every
    dimension min-maxed across segments so both blocks weigh comparably — and
    a 0..1 mean-RMS energy per segment.
    """
    def fr(t):  # seconds -> feature-frame index
        return int(round(t * sr / hop))

    n = chroma.shape[1]
    rows, energy = [], []
    for a, b in zip(bounds[:-1], bounds[1:]):
        i = min(fr(a), n - 1)
        j = max(i + 1, min(fr(b), n))
        rows.append(np.concatenate([chroma[:, i:j].mean(axis=1),
                                    mfcc[:, i:j].mean(axis=1)]))
        k0 = min(i, len(rms) - 1)
        k1 = max(k0 + 1, min(j, len(rms)))
        energy.append(float(np.mean(rms[k0:k1])))
    e = np.asarray(energy)
    if e.max() > e.min():
        e = (e - e.min()) / (e.max() - e.min())
    else:
        e = np.zeros_like(e)
    return _norm_cols(np.array(rows)), e


def label_segments(X):
    """Cluster segment feature rows so repeated material shares a letter.

    Hierarchical (average-linkage) clustering, cut at a fraction of the tallest
    merge: material as different as the most-different pair splits apart,
    repeats of the same material stay together. Letters by first appearance,
    so the labels read A/B/A/B down the track.
    """
    n = len(X)
    if n <= 1:
        return ["A"] * n
    from scipy.cluster.hierarchy import fcluster, linkage
    Z = linkage(X, method="average")
    top = Z[:, 2].max()
    if top <= 1e-9:
        return ["A"] * n              # every segment (near-)identical
    cl = fcluster(Z, t=0.6 * top, criterion="distance")
    letter, out = {}, []
    for c in cl:
        if c not in letter:
            letter[c] = chr(ord("A") + len(letter) % 26)
        out.append(letter[c])
    return out


def segment_tokens(bounds, words):
    """Lyric tokens per segment: each timed word (from lyrics_analyze) goes to
    the segment containing its midpoint. bounds = [t0, ..., duration]."""
    toks = [[] for _ in range(len(bounds) - 1)]
    edges = np.asarray(bounds, dtype=float)
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        i = int(np.searchsorted(edges, mid, side="right")) - 1
        if 0 <= i < len(toks):
            toks[i] += re.findall(r"[a-z']+", w["w"].lower())
    return toks


def _containment(a, b):
    """Token-set containment 0..1 — how much of the smaller set the two share.
    Robust to unequal segment lengths where cosine similarity dilutes."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _cut_labels(D):
    """label_segments' clustering recipe on a precomputed distance matrix."""
    m = len(D)
    if m <= 1:
        return [1] * m
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform
    Z = linkage(squareform(D, checks=False), method="average")
    top = Z[:, 2].max()
    if top <= 1e-9:
        return [1] * m
    return list(fcluster(Z, t=0.6 * top, criterion="distance"))


def label_segments_fused(X, toks, w_text=0.5, min_tokens=4):
    """Labels like label_segments, plus lyric evidence.

    Segments with >= min_tokens lyric tokens ("vocal") cluster on a blend of
    token-containment distance (uni+bigram sets — repeated lyrics identify
    repeated sections, and mis-hearings repeat consistently so accuracy
    barely matters) and acoustic distance. The rest ("instrumental") cluster
    on acoustics alone, so silent segments don't pollute the text space.

    Returns (labels, vocal_label_set) — the caller restricts the chorus call
    to vocal labels: a chorus is sung.
    """
    n = len(X)
    vocal = [i for i in range(n) if len(toks[i]) >= min_tokens]
    if len(vocal) < 2:
        return label_segments(X), set()
    from scipy.spatial.distance import pdist, squareform
    instr = [i for i in range(n) if i not in vocal]
    D_a = squareform(pdist(np.asarray(X, dtype=float)))
    if D_a.max() > 0:
        D_a = D_a / D_a.max()

    sets = [set(t) | {" ".join(p) for p in zip(t, t[1:])} for t in toks]
    D_v = np.zeros((len(vocal), len(vocal)))
    for a, i in enumerate(vocal):
        for b, j in enumerate(vocal):
            if a < b:
                d = (w_text * (1.0 - _containment(sets[i], sets[j]))
                     + (1.0 - w_text) * D_a[i, j])
                D_v[a, b] = D_v[b, a] = d
    cl_v = _cut_labels(D_v)
    cl_i = _cut_labels(D_a[np.ix_(instr, instr)]) if instr else []

    key = {}
    for pos, i in enumerate(vocal):
        key[i] = ("v", cl_v[pos])
    for pos, i in enumerate(instr):
        key[i] = ("i", cl_i[pos])
    letter, labels = {}, []
    for i in range(n):                      # letters by first appearance
        if key[i] not in letter:
            letter[key[i]] = chr(ord("A") + len(letter) % 26)
        labels.append(letter[key[i]])
    return labels, {labels[i] for i in vocal}


def pick_chorus(labels, energy, durations):
    """The chorus call: among labels that RECUR (>=2 segments), the one with
    the highest duration-weighted mean energy. Returns "" when nothing recurs
    or the track is all one label — through-composed or mislabelled material
    gets no chorus rather than a fake one."""
    if len(set(labels)) < 2:
        return ""
    best, best_score = "", -1.0
    for lab in sorted(set(labels)):
        idx = [i for i, l in enumerate(labels) if l == lab]
        if len(idx) < 2:
            continue
        d = float(sum(durations[i] for i in idx)) or 1.0
        score = sum(energy[i] * durations[i] for i in idx) / d
        if score > best_score:
            best, best_score = lab, score
    return best


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


def _step(i, n, msg):
    # progress marker parsed by engine.analyze() (regex: "PROG i/n msg").
    # librosa stages are long and silent, so without these the UI looks frozen.
    print(f"PROG {i}/{n} {msg}", flush=True)


def analyze(path, sr_target, want_plot, out_dir):
    import librosa
    N = 8
    _step(1, N, "loading audio")
    y, sr = librosa.load(path, sr=sr_target, mono=True)
    hop = 512
    duration = float(librosa.get_duration(y=y, sr=sr))

    _step(2, N, "tempo & beat tracking")
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop)
    tempo = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)

    _step(3, N, "downbeats")
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    bf = np.clip(beat_frames, 0, len(onset_env) - 1)
    beat_strength = onset_env[bf] if len(bf) else np.array([])
    downbeats = find_downbeats(beat_times, beat_strength, meter=4)

    _step(4, N, "key estimation")
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    key = estimate_key(chroma)

    _step(5, N, "section boundaries")
    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
    k = int(round(duration / 18)) + 1  # ~ a section every 18s
    secs = sections(chroma, mfcc, sr, hop, k)
    _step(6, N, "harmonic changes")
    hchanges = harmonic_changes(chroma, sr, hop)

    _step(7, N, "energy envelope")
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    env_t, env_v = downsample_env(rms, rms_times, hz=4)

    name = os.path.splitext(os.path.basename(path))[0]

    _step(8, N, "labelling segments")
    bounds = sorted(set([0.0] + [t for t in secs if 0 < t < duration] + [duration]))
    X, seg_energy = segment_features(bounds, sr, hop, chroma, mfcc, rms)
    # fuse timed lyric words (lyrics_analyze.py sidecar) into the labels when
    # they exist — on real band recordings acoustics alone often can't tell
    # the sections apart, but repeated lyrics can
    lyrics_path = os.path.join(out_dir, f"{name}.lyrics.json")
    lyr_words = []
    if os.path.exists(lyrics_path):
        with open(lyrics_path) as fh:
            lyr_words = json.load(fh).get("words", [])
    toks = segment_tokens(bounds, lyr_words) if lyr_words else None
    if toks is not None:
        labels, vocal_labels = label_segments_fused(X, toks)
    else:
        labels, vocal_labels = label_segments(X), set()
    if vocal_labels:
        # a chorus is sung: instrumental labels can't win the chorus call
        masked = [l if l in vocal_labels else f"_i{i}"
                  for i, l in enumerate(labels)]
        chorus = pick_chorus(masked, seg_energy, np.diff(bounds))
    else:
        chorus = pick_chorus(labels, seg_energy, np.diff(bounds))
    segments = []
    for i in range(len(labels)):
        seg = {"start": round(bounds[i], 3), "end": round(bounds[i + 1], 3),
               "label": labels[i], "is_chorus": bool(chorus) and labels[i] == chorus,
               "energy": round(float(seg_energy[i]), 3)}
        if toks is not None:
            seg["text"] = " ".join(toks[i])
        segments.append(seg)
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
        "segments": segments,
        "chorus": chorus or None,
        "lyrics": ({"path": os.path.abspath(lyrics_path),
                    "n_words": len(lyr_words)} if lyr_words else None),
        "harmonic_changes": [round(t, 3) for t in hchanges],
        "energy_envelope": {"hz": 4, "times": env_t, "rms": env_v},
    }

    if want_plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(14, 3))
            # segments shaded by label (same letter = same colour); the chorus
            # call gets a stronger tint — this PNG is the eyeball QA for it
            palette = ["#ff8b5b", "#5bd6a0", "#c39bff", "#ffd75b",
                       "#5bc8ff", "#ff9bd2"]
            seg_color = {}
            for s in segments:
                c = seg_color.setdefault(
                    s["label"], palette[len(seg_color) % len(palette)])
                ax.axvspan(s["start"], s["end"], color=c,
                           alpha=0.45 if s["is_chorus"] else 0.15, lw=0)
                ax.text((s["start"] + s["end"]) / 2, 1.02, s["label"],
                        ha="center", va="bottom", fontsize=8, color=c)
            ax.plot(env_t, env_v, color="#5b9dff", lw=1, label="energy")
            for t in secs:
                ax.axvline(t, color="#ff8b5b", lw=1.2)
            for t in downbeats:
                ax.axvline(t, color="#5bd6a0", lw=0.4, alpha=0.5)
            ch = f", chorus {chorus}" if chorus else ""
            ax.set_title(f"{name} — {tempo:.0f} BPM, {key}{ch}")
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
        via = " (lyrics-fused)" if res.get("lyrics") else ""
        spans = [s for s in res["segments"] if s["is_chorus"]]
        if spans:
            where = ", ".join(f"{s['start']:.0f}–{s['end']:.0f}s" for s in spans)
            print(f"   chorus: {res['chorus']} ×{len(spans)}  ({where}){via}")
        else:
            print(f"   chorus: none called (no recurring section){via}")
        summary.append(res)

    import csv
    with open(os.path.join(args.out, "tracks_summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["track", "duration_s", "tempo_bpm", "key", "n_beats",
                    "n_downbeats", "n_sections", "chorus", "n_harmonic_changes"])
        for r in summary:
            nch = sum(1 for s in r["segments"] if s["is_chorus"])
            w.writerow([r["track"], r["duration_s"], r["tempo_bpm"], r["key"],
                        len(r["beats"]), len(r["downbeats"]),
                        len(r["sections"]),
                        f"{r['chorus']}×{nch}" if r["chorus"] else "",
                        len(r["harmonic_changes"])])

    print(f"\nDone. {len(summary)} tracks → {args.out}")
    print(f"  summary: {os.path.join(args.out, 'tracks_summary.csv')}")
    print("Next: pick a track and sync clips to it, e.g.")
    if summary:
        ex = summary[0]["track"]
        print(f"  python3 sync_clips.py --analysis {args.out}/{ex}.analysis.json "
              f"--manifest ./catalog/manifest.csv --grid sections")


if __name__ == "__main__":
    main()
