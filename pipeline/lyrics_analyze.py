#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
lyrics_analyze.py — transcribe a track's vocals into time-stamped lyric words.

For each track: separate the vocal stem (demucs), find the sung regions from
the stem's energy, transcribe each region independently (mlx-whisper, word
timestamps), and keep only words backed by real vocal energy. The result feeds
track_analyze's segment labelling: repeated lyrics identify repeated sections
(verse/chorus) far more reliably than acoustics alone on band recordings.

Two design points learned the hard way (probed on real material):
  * Hallucinations are filtered by VOCAL-STEM ENERGY, never by text
    repetition — a real chorus vamp ("I'm going to run" x30) is statistically
    indistinguishable from a hallucination loop, but it has the highest vocal
    energy in the song while hallucinations sing over silence.
  * Each sung region is transcribed independently (VAD-style chunking,
    condition_on_previous_text=False) because whisper's word timestamps
    degenerate over long repeated vamps and its context-carry loops.

Transcription accuracy barely matters: repeated sections mis-hear the same
way, and the downstream matching needs consistency, not correct words.

Outputs (into --out, default ./catalog_audio, next to the analyses):
  <track>.lyrics.json      time-stamped gated words; track_analyze
                           auto-discovers this by name and fuses it
  stems/<track>.vocals16k.wav   the 16 kHz mono vocal stem (kept so a
                           re-transcribe doesn't pay for separation again)

Usage:
  .venv-lyrics/bin/python pipeline/lyrics_analyze.py ./album-audio -o ./catalog_audio

Requires (NOT part of dv2mv's core deps — keep them in a separate venv):
  pip install demucs mlx-whisper soundfile     (+ ffmpeg on PATH)
Note: demucs 4.0.1 with torchaudio>=2.9 can't save audio (torchcodec); this
script saves stems itself via soundfile, so that combination still works.

PyTorch (demucs) and MLX (whisper) must NOT share a process — their bundled
OpenMP/Metal runtimes clash and segfault with no traceback. Separation
therefore runs in a child process (`--_separate`, self-invocation); the parent
only ever imports mlx. Don't "simplify" this back into one process.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from glob import glob

import numpy as np

AUDIO_EXTS = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".wma")
SR = 16000                 # whisper's native rate; also the gate's analysis rate
HOP = 512                  # gate frame = hop (non-overlapping), ~32 ms


def _need(module, hint):
    try:
        return __import__(module)
    except ImportError:
        sys.exit(f"{module} is not importable — run this under the lyrics venv "
                 f"({hint}), not dv2mv's core environment.")


def _step(i, n, msg):
    # progress marker parsed by the engine (same protocol as track_analyze)
    print(f"PROG {i}/{n} {msg}", flush=True)


def separate_vocals(path, out16k):
    """demucs vocal stem -> 16 kHz mono wav at out16k (saved via soundfile)."""
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    model = get_model("htdemucs")
    model.eval()
    with tempfile.TemporaryDirectory() as td:
        dec = os.path.join(td, "in.wav")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", path,
             "-ar", str(model.samplerate), "-ac", str(model.audio_channels), dec],
            check=True)
        wav, _sr = sf.read(dec, dtype="float32", always_2d=True)

        x = torch.from_numpy(wav.T)
        ref = x.mean(0)                       # demucs.separate's normalization
        mean, std = ref.mean(), ref.std() + 1e-8
        with torch.no_grad():
            sources = apply_model(model, ((x - mean) / std)[None], device="cpu",
                                  shifts=1, split=True, overlap=0.25)[0]
        vocals = (sources[model.sources.index("vocals")] * std + mean)

        full = os.path.join(td, "vocals.wav")
        sf.write(full, vocals.T.numpy(), model.samplerate)
        os.makedirs(os.path.dirname(out16k), exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", full,
             "-ar", str(SR), "-ac", "1", out16k],
            check=True)


def frame_rms(y):
    """Non-overlapping HOP-sized RMS frames of a mono signal."""
    n = len(y) // HOP * HOP
    if n == 0:
        return np.zeros(1)
    f = y[:n].reshape(-1, HOP)
    return np.sqrt((f.astype("float64") ** 2).mean(axis=1))


def sung_regions(vrms, thr, max_len_s=26.0, close_gap_s=2.0, min_len_s=0.4,
                 pad_s=0.4):
    """Active [start,end) second-spans from the stem's RMS frames.

    Gaps shorter than close_gap_s merge, islands shorter than min_len_s drop,
    pad_s is added each side, and anything longer than max_len_s splits at its
    quietest interior frame — long vamps must be transcribed in pieces or
    whisper's word timestamps bunch up.
    """
    fps = SR / HOP
    mask = vrms > thr
    spans, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            spans.append([start, i]); start = None
    if start is not None:
        spans.append([start, len(mask)])
    merged = []
    for s in spans:
        if merged and s[0] - merged[-1][1] < close_gap_s * fps:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    merged = [s for s in merged if s[1] - s[0] >= min_len_s * fps]

    def split(a, b):
        if (b - a) <= max_len_s * fps:
            return [[a, b]]
        lo, hi = a + int((b - a) * 0.25), a + int((b - a) * 0.75)
        cut = lo + int(np.argmin(vrms[lo:hi]))
        return split(a, cut) + split(cut, b)

    out = []
    for a, b in merged:
        out += split(a, b)
    pad = pad_s * fps
    return [(max(0.0, (a - pad) / fps), (b + pad) / fps) for a, b in out]


def transcribe(y16, regions, vrms, thr, model, language):
    """Whisper each region independently; keep words backed by vocal energy."""
    import mlx_whisper
    words_all, n_raw = [], 0
    for ri, (a, b) in enumerate(regions, 1):
        _step(2 + ri, 2 + len(regions) + 1, f"transcribing {a:.0f}-{b:.0f}s")
        seg = y16[int(a * SR):int(b * SR)].astype(np.float32)
        res = mlx_whisper.transcribe(
            seg, path_or_hf_repo=model, language=language,
            word_timestamps=True, condition_on_previous_text=False,
            verbose=None)
        for ws in res["segments"]:
            for w in ws.get("words", []):
                n_raw += 1
                t0, t1 = a + w["start"], a + w["end"]
                i = int(t0 * SR / HOP)
                j = max(i + 1, int(t1 * SR / HOP))
                if np.mean(vrms[min(i, len(vrms) - 1):min(j, len(vrms))]) > thr:
                    words_all.append({"w": w["word"].strip(),
                                      "start": round(t0, 3), "end": round(t1, 3),
                                      "p": round(float(w["probability"]), 3)})
    words_all.sort(key=lambda w: w["start"])
    return words_all, n_raw


def analyze(path, out_dir, model, language, force):
    import soundfile as sf
    name = os.path.splitext(os.path.basename(path))[0]
    lyr_path = os.path.join(out_dir, f"{name}.lyrics.json")
    if os.path.exists(lyr_path) and not force:
        print(f"   already transcribed (use --force to redo): {lyr_path}")
        return None
    stem = os.path.join(out_dir, "stems", f"{name}.vocals16k.wav")

    if os.path.exists(stem) and not force:
        _step(1, 3, "vocal stem cached")
    else:
        _step(1, 3, "separating vocals (demucs)")
        # child process: torch (demucs) and mlx (whisper) segfault when they
        # share a process — see module docstring
        subprocess.run([sys.executable, os.path.abspath(__file__),
                        "--_separate", path, stem], check=True)

    _step(2, 3, "finding sung regions")
    y16, _ = sf.read(stem, dtype="float32", always_2d=False)
    vrms = frame_rms(y16)
    thr = 0.10 * np.percentile(vrms, 95)
    duration = len(y16) / SR
    regions = sung_regions(vrms, thr)
    if not regions:
        words, n_raw = [], 0
    else:
        words, n_raw = transcribe(y16, regions, vrms, thr, model, language)

    result = {
        "track": name,
        "path": os.path.abspath(path),
        "duration_s": round(duration, 3),
        "model": model,
        "stem": os.path.relpath(stem, out_dir),
        "sung_regions": [[round(a, 2), round(b, 2)] for a, b in regions],
        "n_raw_words": n_raw,
        "words": words,
        "text": " ".join(w["w"] for w in words),
    }
    with open(lyr_path, "w") as fh:
        json.dump(result, fh, indent=1)
    print(f"   {len(words)} words kept ({n_raw - len(words)} gated out) · "
          f"{len(regions)} sung regions")
    print(f"   lyrics: {lyr_path}")
    return result


def main():
    # child-process entry: separation only (torch stays out of the parent)
    if len(sys.argv) > 1 and sys.argv[1] == "--_separate":
        _need("torch", "pip install demucs")
        _need("demucs", "pip install demucs")
        _need("soundfile", "pip install soundfile")
        separate_vocals(sys.argv[2], sys.argv[3])
        return

    ap = argparse.ArgumentParser(description="Transcribe vocals to timed words.")
    ap.add_argument("audio", help="audio file or folder of tracks")
    ap.add_argument("-o", "--out", default="./catalog_audio",
                    help="output dir (put it where the analyses live so "
                         "track_analyze auto-discovers the lyrics)")
    ap.add_argument("--model", default="mlx-community/whisper-large-v3-turbo",
                    help="mlx-whisper model repo")
    ap.add_argument("--language", default="en", help="lyrics language")
    ap.add_argument("--force", action="store_true",
                    help="re-separate and re-transcribe even if outputs exist")
    args = ap.parse_args()

    _need("mlx_whisper", "pip install mlx-whisper")
    _need("soundfile", "pip install soundfile")

    if os.path.isdir(args.audio):
        paths = sorted(p for p in glob(os.path.join(args.audio, "*"))
                       if p.lower().endswith(AUDIO_EXTS))
    else:
        paths = [args.audio]
    if not paths:
        sys.exit(f"No audio files found at {args.audio}")
    os.makedirs(args.out, exist_ok=True)

    done = 0
    for i, p in enumerate(paths, 1):
        print(f"[{i}/{len(paths)}] {os.path.basename(p)}", flush=True)
        try:
            if analyze(p, args.out, args.model, args.language, args.force):
                done += 1
        except Exception as e:
            print(f"   ! failed: {e}")
    print(f"\nDone. {done} track(s) transcribed → {args.out}")
    print("Next: (re)Analyze the tracks — track_analyze picks up "
          "<track>.lyrics.json automatically and fuses it into the labels.")


if __name__ == "__main__":
    main()
