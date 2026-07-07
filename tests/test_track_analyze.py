# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""Tests for track_analyze's segment labelling + chorus call.

Same two tiers as test_engine: pure-logic tests on the labelling functions
(numpy/scipy only), and one guarded integration test that synthesizes an
A/B/A/B track — quiet sine verses, loud triad-plus-noise choruses — and
asserts the analysis labels it that way.
"""
import numpy as np
import pytest

from conftest import requires_librosa  # noqa: F401  (also wires sys.path)
from pipeline.track_analyze import (label_segments, label_segments_fused,
                                    pick_chorus, segment_features,
                                    segment_tokens)


# ── pure: labelling ──────────────────────────────────────────────────────────

def test_label_segments_abab():
    a = [1.0, 0.0, 0.0, 0.0, 5.0, 5.0]
    b = [0.0, 1.0, 1.0, 1.0, 0.0, 0.0]
    assert label_segments(np.array([a, b, a, b])) == ["A", "B", "A", "B"]


def test_label_segments_letters_by_first_appearance():
    a, b, c = [0.0, 0.0], [1.0, 0.0], [0.0, 1.0]
    assert label_segments(np.array([b, a, b, c])) == ["A", "B", "A", "C"]


def test_label_segments_degenerate():
    assert label_segments(np.zeros((0, 4))) == []
    assert label_segments(np.zeros((1, 4))) == ["A"]
    # all-identical segments: one label, not n clusters
    assert label_segments(np.zeros((3, 4))) == ["A", "A", "A"]


# ── pure: chorus call ────────────────────────────────────────────────────────

def test_pick_chorus_recurring_loudest():
    assert pick_chorus(["A", "B", "A", "B"],
                       [0.2, 0.9, 0.3, 0.8], [10, 10, 10, 10]) == "B"


def test_pick_chorus_requires_recurrence():
    # the loudest label appears once -> the recurring one wins
    assert pick_chorus(["A", "B", "A"], [0.2, 1.0, 0.3], [10, 10, 10]) == "A"
    # nothing recurs -> no chorus
    assert pick_chorus(["A", "B", "C"], [0.2, 1.0, 0.3], [10, 10, 10]) == ""
    # everything is one label -> no chorus (it would just be "the song")
    assert pick_chorus(["A", "A"], [0.5, 0.6], [10, 10]) == ""


def test_pick_chorus_duration_weighted():
    # B is louder on paper but only for a sliver; the long loud A wins
    assert pick_chorus(["A", "B", "A", "B"],
                       [0.8, 1.0, 0.8, 0.0], [20, 1, 20, 30]) == "A"


# ── pure: per-segment features ───────────────────────────────────────────────

def test_segment_features_means_and_energy():
    sr, hop = 100, 10                      # 10 feature frames per second
    n = 40                                 # 4 s -> two 2 s segments
    chroma = np.zeros((12, n))
    chroma[0, :20] = 1.0                   # segment 1 material
    chroma[5, 20:] = 1.0                   # segment 2 material
    mfcc = np.zeros((13, n))
    rms = np.concatenate([np.full(20, 0.1), np.full(20, 0.9)])

    X, e = segment_features([0.0, 2.0, 4.0], sr, hop, chroma, mfcc, rms)
    assert X.shape == (2, 25)
    # energy min-maxed across segments; the loud half is the loud half
    assert e[0] == 0.0 and e[1] == 1.0
    # the distinguishing chroma dims survive the per-column min-max
    assert X[0][0] == 1.0 and X[1][0] == 0.0
    assert X[0][5] == 0.0 and X[1][5] == 1.0


def test_segment_features_tiny_tail_segment():
    # a boundary landing at the last frame must not produce an empty slice
    sr, hop, n = 100, 10, 40
    chroma, mfcc, rms = np.ones((12, n)), np.ones((13, n)), np.ones(n)
    X, e = segment_features([0.0, 3.99, 4.0], sr, hop, chroma, mfcc, rms)
    assert X.shape[0] == 2 and not np.isnan(X).any() and not np.isnan(e).any()


# ── pure: lyric fusion ───────────────────────────────────────────────────────

def _words(text, t0, t1):
    """Fake lyrics_analyze words: `text` spread evenly over [t0, t1)."""
    toks = text.split()
    step = (t1 - t0) / len(toks)
    return [{"w": w, "start": round(t0 + i * step, 3),
             "end": round(t0 + (i + 1) * step, 3), "p": 0.9}
            for i, w in enumerate(toks)]


def test_segment_tokens_assigns_by_midpoint():
    words = _words("one two", 0.0, 2.0) + _words("three four five six", 5.0, 6.0)
    toks = segment_tokens([0.0, 4.0, 8.0], words)
    assert toks == [["one", "two"], ["three", "four", "five", "six"]]
    # out-of-range words are dropped, not crashed on
    assert segment_tokens([0.0, 1.0], [{"w": "late", "start": 5, "end": 6}]) == [[]]


def test_fused_labels_repeat_lyrics_despite_flat_acoustics():
    # four segments acoustically identical — acoustics alone sees one label,
    # but the lyrics repeat verse/chorus/verse/chorus
    X = np.zeros((4, 25))
    verse = "so many times we find ourselves lost".split()
    chorus = "i'm not afraid to feel the rage".split()
    labels, vocal = label_segments_fused(X, [verse, chorus, verse, chorus])
    assert labels == ["A", "B", "A", "B"]
    assert vocal == {"A", "B"}
    assert label_segments(X) == ["A", "A", "A", "A"]  # what acoustics said


def test_fused_instrumentals_cluster_separately():
    # segments 0/2 sing the same words; 1/3 are instrumental and acoustically
    # alike each other but unlike everything else
    X = np.array([[0.0] * 4, [1.0] * 4, [0.0] * 4, [1.0] * 4])
    sung = "we built a fire from the ashes".split()
    labels, vocal = label_segments_fused(X, [sung, [], sung, []])
    assert labels[0] == labels[2] and labels[1] == labels[3]
    assert labels[0] != labels[1]
    assert vocal == {labels[0]}


def test_fused_falls_back_when_too_few_vocal_segments():
    X = np.array([[0.0] * 4, [1.0] * 4, [0.0] * 4])
    labels, vocal = label_segments_fused(X, [["hi"], [], []])  # < min_tokens
    assert labels == label_segments(X)
    assert vocal == set()


# ── integration: chorus call on a structured synthetic track ────────────────

SR_SYNTH = 11025
SEC = 12      # 4 x 12 s -> the ~18 s section heuristic yields k=4 boundaries


def _abab_wav(tmp_path, sf, name="abab.wav"):
    """Quiet sine verses alternating with loud triad-plus-noise choruses."""
    def tone(freqs, amp):
        t = np.arange(SEC * SR_SYNTH) / SR_SYNTH
        y = sum(np.sin(2 * np.pi * f * t) for f in freqs)
        return (amp * y / len(freqs)).astype(np.float32)

    rng = np.random.default_rng(0)
    verse = tone([220.0], 0.15)
    chorus = (tone([262.0, 330.0, 392.0], 0.6)
              + (0.05 * rng.standard_normal(SEC * SR_SYNTH)).astype(np.float32))
    wav = str(tmp_path / name)
    sf.write(wav, np.concatenate([verse, chorus, verse, chorus]), SR_SYNTH)
    return wav


@requires_librosa
def test_analyze_labels_chorus_on_structured_track(tmp_path):
    sf = pytest.importorskip("soundfile")
    from pipeline import track_analyze

    sec = SEC
    wav = _abab_wav(tmp_path, sf)
    res = track_analyze.analyze(wav, SR_SYNTH, False, str(tmp_path))

    segs = res["segments"]
    # segments tile the whole track contiguously
    assert segs[0]["start"] == 0.0
    assert segs[-1]["end"] == pytest.approx(res["duration_s"], abs=0.01)
    for s0, s1 in zip(segs, segs[1:]):
        assert s0["end"] == s1["start"]

    # the verse/chorus contrast is detected and the chorus is called
    assert len({s["label"] for s in segs}) >= 2
    assert res["chorus"]
    chorus_segs = [s for s in segs if s["is_chorus"]]
    assert chorus_segs and all(s["label"] == res["chorus"] for s in chorus_segs)

    # every chorus-labelled segment sits (by midpoint) in a loud region
    loud = [(sec, 2 * sec), (3 * sec, 4 * sec)]
    for s in chorus_segs:
        mid = (s["start"] + s["end"]) / 2
        assert any(a - 2 <= mid <= b + 2 for a, b in loud), s
    # no lyrics sidecar -> acoustic-only provenance
    assert res["lyrics"] is None
    assert "text" not in segs[0]


@requires_librosa
def test_analyze_fuses_lyrics_sidecar(tmp_path):
    import json

    sf = pytest.importorskip("soundfile")
    from pipeline import track_analyze

    wav = _abab_wav(tmp_path, sf)
    # a lyrics sidecar naming the loud sections with the same sung words
    verse1 = _words("so many times we find ourselves lost", 2, 10)
    chor1 = _words("i'm not afraid to feel the rage", SEC + 2, SEC + 10)
    verse2 = _words("so many times we find ourselves lost", 2 * SEC + 2, 2 * SEC + 10)
    chor2 = _words("i'm not afraid to feel the rage", 3 * SEC + 2, 3 * SEC + 10)
    with open(tmp_path / "abab.lyrics.json", "w") as fh:
        json.dump({"track": "abab", "words": verse1 + chor1 + verse2 + chor2},
                  fh)

    res = track_analyze.analyze(wav, SR_SYNTH, False, str(tmp_path))
    segs = res["segments"]
    assert res["lyrics"]["n_words"] == 28
    # per-segment transcript text is carried into the analysis
    sung = [s for s in segs if s.get("text")]
    assert len(sung) == 4
    assert sung[0]["text"] == sung[2]["text"] and sung[1]["text"] == sung[3]["text"]
    assert sung[0]["label"] == sung[2]["label"] and sung[1]["label"] == sung[3]["label"]
    # the chorus is one of the sung labels, and it's the loud one
    assert res["chorus"] == sung[1]["label"]
