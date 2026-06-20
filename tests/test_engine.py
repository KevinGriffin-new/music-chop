# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""Per-stage tests for the dv2mv engine.

Two tiers:
  * pure-logic tests (always run): the progress/error contract, _stream, the
    fail-loud verification, and the summary merge.
  * integration tests (skip if a tool is missing): each stage end-to-end on
    tiny generated media.
"""
import csv
import os
import stat
import sys
import threading
import time

import pytest

from conftest import (engine, REPO, write_analysis, requires_ffmpeg,
                      requires_scenedetect, requires_librosa, requires_cv2,
                      requires_otio, requires_fcpx)


def drain(gen):
    """Collect all ProgressEvents from a stage generator into a list."""
    return list(gen)


# ── pure: progress / error contract ─────────────────────────────────────────
def test_progress_event_defaults():
    ev = engine.ProgressEvent("detect")
    assert ev.stage == "detect" and ev.frac is None and ev.done is False
    assert ev.result == {} and ev.message == ""


def test_run_stage_returns_final_result():
    def fake():
        yield engine.ProgressEvent("x", "step", 0.5)
        yield engine.ProgressEvent("x", "done", 1.0, True, {"out": "/tmp/z"})
    seen = []
    result = engine.run_stage(fake(), seen.append)
    assert result == {"out": "/tmp/z"}
    assert len(seen) == 2 and seen[-1].done


def test_stream_yields_lines():
    lines = list(engine._stream(["bash", "-c", "printf 'a\\nb\\nc\\n'"], cwd=REPO))
    assert lines == ["a", "b", "c"]


def test_stream_raises_with_tail_on_nonzero():
    with pytest.raises(engine.StageError) as ei:
        list(engine._stream(["bash", "-c", "echo boom; exit 3"], cwd=REPO))
    msg = str(ei.value)
    assert "exited 3" in msg and "boom" in msg


# ── pure: cancellation ───────────────────────────────────────────────────────
def test_cancelled_is_distinct_from_stage_error():
    # the UIs branch on this: a cancel is a clean stop, not a failure dialog
    assert not issubclass(engine.Cancelled, engine.StageError)
    assert issubclass(engine.Cancelled, RuntimeError)


def test_check_cancel_raises_only_when_set():
    ev = threading.Event()
    engine._check_cancel(ev, "x")        # not set → no-op
    engine._check_cancel(None, "x")      # no token → no-op
    ev.set()
    with pytest.raises(engine.Cancelled):
        engine._check_cancel(ev, "detect")


def test_stream_cancel_stops_early_and_raises_cancelled():
    """Setting the token mid-stream terminates the subprocess and raises
    Cancelled (NOT StageError, even though the killed process exits nonzero)."""
    cancel = threading.Event()
    gen = engine._stream(
        ["bash", "-c", "for i in $(seq 1 200); do echo $i; sleep 0.05; done"],
        cwd=REPO, cancel=cancel)
    seen = []
    with pytest.raises(engine.Cancelled):
        for line in gen:
            seen.append(line)
            if len(seen) == 3:
                cancel.set()
    assert 0 < len(seen) < 200           # stopped well short of the full run


@pytest.mark.skipif(os.name != "posix", reason="process-group kill is POSIX-only")
def test_stream_cancel_kills_child_process_group(tmp_path):
    """render runs `bash script` which spawns ffmpeg children; cancelling must
    kill the whole tree, not just bash. Simulate with a backgrounded child that
    keeps touching a marker file — after cancel it must stop being updated."""
    marker = tmp_path / "alive"
    script = tmp_path / "tree.sh"
    script.write_text(
        f"( while true; do touch '{marker}'; sleep 0.05; done ) &\n"
        "echo started\n"
        "wait\n")
    cancel = threading.Event()
    gen = engine._stream(["bash", str(script)], cwd=str(tmp_path), cancel=cancel)
    with pytest.raises(engine.Cancelled):
        for line in gen:
            if line == "started":
                time.sleep(0.2)          # let the child touch the marker a few times
                cancel.set()             # then cancel — kills bash AND the child
    # _stream only returns after the group is terminated, so the child is gone:
    # the marker exists (it ran) but its mtime must stop advancing.
    assert marker.exists()
    m1 = marker.stat().st_mtime
    time.sleep(0.4)
    assert marker.stat().st_mtime == m1, "child survived cancel — group not killed"


def test_stream_abandoned_generator_reaps_subprocess(tmp_path):
    """If the consumer stops iterating (e.g. an SSE client disconnects), the
    finally-clause must terminate the subprocess rather than leak it."""
    marker = tmp_path / "alive"
    script = tmp_path / "leak.sh"
    # echo once so the first next() returns; then loop silently touching marker
    script.write_text(
        "echo go\n"
        f"while true; do touch '{marker}'; sleep 0.05; done\n")
    gen = engine._stream(["bash", str(script)], cwd=str(tmp_path))
    assert next(gen) == "go"             # started; child is now touching marker
    time.sleep(0.15)                     # let it touch the marker at least once
    gen.close()                          # GeneratorExit → finally should reap it
    assert marker.exists()
    m1 = marker.stat().st_mtime
    time.sleep(0.4)
    assert marker.stat().st_mtime == m1, "subprocess leaked after generator close"


def test_catalog_propagates_cancel(tmp_path, monkeypatch):
    """A real stage forwards the token: catalog stops with Cancelled (no done
    event) when the token fires mid-run."""
    stub = tmp_path / "slow_features.py"
    stub.write_text(
        "import sys, time\n"
        "for i in range(1, 200):\n"
        "    print(f'[{i}/199] clip'); sys.stdout.flush(); time.sleep(0.05)\n")
    monkeypatch.setitem(engine.SCRIPT, "features", str(stub))
    cancel = threading.Event()
    gen = engine.catalog(str(tmp_path), str(tmp_path / "out"), cancel=cancel)
    seen = []
    with pytest.raises(engine.Cancelled):
        for ev in gen:
            seen.append(ev)
            if len(seen) >= 2:
                cancel.set()
    assert seen and all(not e.done for e in seen)   # never reached completion


# ── pure: media-root guard (the DV2MV_MEDIA-unset footgun) ───────────────────
def test_looks_like_code_checkout_detects_repo(tmp_path):
    assert engine.looks_like_code_checkout(engine.HERE)        # the repo itself
    assert not engine.looks_like_code_checkout(str(tmp_path))  # an empty dir


def test_check_media_root_raises_when_media_is_checkout(monkeypatch):
    """Unset DV2MV_MEDIA → media defaults to the repo → actionable error naming
    the real cause (not a downstream 'No such file')."""
    monkeypatch.setattr(engine, "MEDIA", engine.HERE)
    monkeypatch.setattr(engine, "MEDIA_FROM_ENV", False)
    with pytest.raises(engine.StageError) as ei:
        engine.check_media_root()
    msg = str(ei.value)
    assert "DV2MV_MEDIA" in msg and "unset" in msg


def test_check_media_root_message_differs_when_explicitly_set(monkeypatch):
    monkeypatch.setattr(engine, "MEDIA", engine.HERE)
    monkeypatch.setattr(engine, "MEDIA_FROM_ENV", True)
    with pytest.raises(engine.StageError) as ei:
        engine.check_media_root()
    assert "points at the code checkout" in str(ei.value)


def test_check_media_root_ok_for_real_media(tmp_path):
    engine.check_media_root(str(tmp_path))   # not a checkout → no raise


# ── pure: config + runtime media switch (the in-app library picker) ──────────
def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert engine.config_path().startswith(str(tmp_path))
    engine.save_config({"media": "/x/y"})
    assert engine.load_config() == {"media": "/x/y"}


def test_set_media_validates_persists_and_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(engine, "MEDIA", "/old")          # pinned → restored at teardown
    monkeypatch.setattr(engine, "MEDIA_SOURCE", "env")
    lib = tmp_path / "lib"
    lib.mkdir()
    out = engine.set_media(str(lib))
    assert out == str(lib) and engine.MEDIA == str(lib)
    assert engine.load_config()["media"] == str(lib)      # remembered
    # persist=False updates MEDIA but leaves the saved choice alone
    other = tmp_path / "lib2"
    other.mkdir()
    engine.set_media(str(other), persist=False)
    assert engine.MEDIA == str(other)
    assert engine.load_config()["media"] == str(lib)      # unchanged


def test_set_media_rejects_nonexistent_and_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(engine, "MEDIA", engine.MEDIA)    # pin for restore
    with pytest.raises(engine.StageError):
        engine.set_media(str(tmp_path / "nope"))          # not a folder
    with pytest.raises(engine.StageError):
        engine.set_media(engine.HERE)                     # the code checkout


def test_initial_media_precedence(tmp_path, monkeypatch):
    """env > saved config > cwd."""
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    saved = tmp_path / "saved"
    saved.mkdir()
    engine.save_config({"media": str(saved)})
    # env wins
    monkeypatch.setenv("DV2MV_MEDIA", str(tmp_path / "fromenv"))
    assert engine._initial_media() == (str(tmp_path / "fromenv"), "env")
    # no env → saved config
    monkeypatch.delenv("DV2MV_MEDIA", raising=False)
    assert engine._initial_media() == (str(saved), "config")


# ── pure: fail-loud verification ─────────────────────────────────────────────
def test_require_passes_when_present(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("ok")
    assert engine._require("s", {"f": str(p)})["f"] == str(p)


def test_require_raises_when_missing(tmp_path):
    missing = str(tmp_path / "nope.json")
    with pytest.raises(engine.StageError) as ei:
        engine._require("analyze", {"analysis": missing}, tail=["line1", "! failed: boom"])
    msg = str(ei.value)
    assert "missing" in msg and "nope.json" in msg and "! failed: boom" in msg


def test_require_ignores_non_path_values(tmp_path):
    # ints / None / non-output strings shouldn't be treated as required files
    engine._require("arrange", {"energy_match": 99, "note": None, "n": "sections"})


# ── pure: tracks_summary merge ───────────────────────────────────────────────
def _write_summary(path, rows):
    fields = ["track", "duration_s", "tempo_bpm", "key", "n_beats",
              "n_downbeats", "n_sections", "n_harmonic_changes"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({**{k: 0 for k in fields}, **r})


def test_merge_summary_inserts_and_replaces(tmp_path):
    path = str(tmp_path / "tracks_summary.csv")
    # combined summary already has two tracks
    _write_summary(path, [{"track": "01 A", "tempo_bpm": 100},
                          {"track": "02 B", "tempo_bpm": 110}])
    before = engine._read_summary(path)
    # track_analyze re-runs ONE track and clobbers the file with just that row,
    # but with a new value (simulating a re-analysis of 02 B)
    _write_summary(path, [{"track": "02 B", "tempo_bpm": 999}])
    engine._merge_summary(path, before)
    _, rows = engine._read_summary(path)
    assert set(rows) == {"01 A", "02 B"}            # 01 A survived the clobber
    assert rows["02 B"]["tempo_bpm"] == "999"       # 02 B updated, not duplicated
    # written in track order
    assert list(rows) == ["01 A", "02 B"]


def test_merge_summary_handles_no_prior_file(tmp_path):
    path = str(tmp_path / "tracks_summary.csv")
    before = engine._read_summary(path)               # absent -> (None, {})
    _write_summary(path, [{"track": "01 A", "tempo_bpm": 100}])
    engine._merge_summary(path, before)
    _, rows = engine._read_summary(path)
    assert list(rows) == ["01 A"]


def test_tag_suffix_sanitizes():
    assert engine._tag_suffix("sections") == "-sections"
    assert engine._tag_suffix("") == ""
    assert engine._tag_suffix("fast montage!") == "-fast-montage"


# ── fail-loud: a script that exits 0 but writes nothing must raise ───────────
def test_analyze_fails_loud_on_silent_script(tmp_path, monkeypatch):
    """Guards the real bug we found: track_analyze swallows a per-track error
    and still exits 0, so the engine must NOT report a bogus result dict."""
    stub = tmp_path / "fake_analyze.py"
    stub.write_text(
        "import sys\n"
        "print('[1/1] thing'); print('   ! failed: kaboom'); sys.exit(0)\n")
    monkeypatch.setitem(engine.SCRIPT, "analyze", str(stub))
    with pytest.raises(engine.StageError) as ei:
        drain(engine.analyze(str(tmp_path / "whatever.mp3"), str(tmp_path), plot=False))
    assert "missing" in str(ei.value) and "kaboom" in str(ei.value)


def test_ingest_rejects_same_dir(tmp_path):
    with pytest.raises(engine.StageError):
        drain(engine.ingest(str(tmp_path), str(tmp_path)))


# ── pure: source resolution for file pickers / uploads ───────────────────────
def test_list_sources_dir_file_and_list(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mov").write_bytes(b"x")
    (tmp_path / "note.txt").write_bytes(b"x")          # not a video
    a, b = str(tmp_path / "a.mp4"), str(tmp_path / "b.mov")
    assert engine._list_sources(str(tmp_path)) == [a, b]      # dir, sorted, video-only
    assert engine._list_sources(a) == [a]                      # single file
    assert engine._list_sources([b, a]) == [a, b]              # unsorted list -> sorted
    assert engine._list_sources([str(tmp_path / "missing.mp4")]) == []  # nonexistent filtered
    assert engine._list_sources([str(tmp_path / "note.txt")]) == []     # non-video filtered
    assert engine._list_sources("/no/such/dir") == []


def test_detect_empty_source_raises(tmp_path):
    with pytest.raises(engine.StageError):
        drain(engine.detect(str(tmp_path), str(tmp_path / "out")))   # empty dir


# ── integration: ingest ──────────────────────────────────────────────────────
@requires_ffmpeg
def test_ingest_normalizes_and_is_idempotent(clips_dir, tmp_path):
    out = str(tmp_path / "ingested")
    evs = drain(engine.ingest(clips_dir, out, preset="ultrafast", fps=None, scale=None))
    final = evs[-1]
    assert final.done and final.frac == 1.0
    assert final.result["normalized"] == 2
    assert all(os.path.exists(p) for p in final.result["outputs"])
    # second run skips everything
    evs2 = drain(engine.ingest(clips_dir, out, preset="ultrafast"))
    assert evs2[-1].result["normalized"] == 0


# ── integration: detect ──────────────────────────────────────────────────────
@requires_scenedetect
def test_detect_splits_into_clips(clips_dir, tmp_path):
    out = str(tmp_path / "scenes")
    # detect runs per-source; one tiny source yields at least one scene clip
    import shutil as _sh
    src = str(tmp_path / "one_src")
    os.makedirs(src, exist_ok=True)
    first = sorted(f for f in os.listdir(clips_dir) if f.endswith(".mp4"))[0]
    _sh.copy(os.path.join(clips_dir, first),
             os.path.join(src, "src2004.05.07_20-00-09.mp4"))
    evs = drain(engine.detect(src, out))
    final = evs[-1]
    assert final.done and final.result["clips"] >= 1
    assert os.path.isdir(final.result["clips_dir"])


@requires_scenedetect
def test_detect_accepts_picked_file_list(clips_dir, tmp_path):
    """The file-picker / upload path: detect() fed explicit files, not a dir."""
    out = str(tmp_path / "scenes")
    first = sorted(os.path.join(clips_dir, f) for f in os.listdir(clips_dir)
                   if f.endswith(".mp4"))[0]
    evs = drain(engine.detect([first], out))
    assert evs[-1].done and evs[-1].result["clips"] >= 1


# ── integration: catalog ─────────────────────────────────────────────────────
@requires_cv2
@requires_ffmpeg
def test_catalog_writes_manifest(clips_dir, tmp_path):
    out = str(tmp_path / "catalog")
    evs = drain(engine.catalog(clips_dir, out, frames=4, width=120))
    final = evs[-1]
    assert final.done and os.path.exists(final.result["manifest"])
    # fractional progress actually advanced
    fracs = [e.frac for e in evs if e.frac is not None]
    assert fracs and max(fracs) <= 1.0
    with open(final.result["manifest"]) as fh:
        assert len(list(csv.DictReader(fh))) == 2


def test_catalog_append_passes_flag(tmp_path, monkeypatch):
    """Pure: append=True adds --append to the wrapped command; default doesn't."""
    stub = tmp_path / "echo_argv.py"
    stub.write_text(
        "import sys, os\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "os.makedirs(out, exist_ok=True)\n"
        "open(os.path.join(out, 'argv.txt'), 'w').write(' '.join(sys.argv))\n"
        "open(os.path.join(out, 'manifest.csv'), 'w').write('clip\\n')\n")
    monkeypatch.setitem(engine.SCRIPT, "features", str(stub))
    out = str(tmp_path / "cat")
    list(engine.catalog(str(tmp_path), out, append=True))
    assert "--append" in open(os.path.join(out, "argv.txt")).read()
    list(engine.catalog(str(tmp_path), out, append=False))
    assert "--append" not in open(os.path.join(out, "argv.txt")).read()


@requires_cv2
@requires_ffmpeg
def test_catalog_append_only_adds_new_clips(tmp_path):
    """Incremental: append catalogs only clips not already in the manifest,
    carries the existing rows + histograms forward, and is a no-op when nothing
    is new."""
    from conftest import _make_clip
    clips = tmp_path / "clips"
    clips.mkdir()
    _make_clip(str(clips / "a.mp4"))
    out = str(tmp_path / "cat")
    manifest = os.path.join(out, "manifest.csv")
    engine.run_stage(engine.catalog(str(clips), out, frames=4, width=120),
                     lambda e: None)
    rows1 = list(csv.DictReader(open(manifest)))
    assert {os.path.basename(r["clip"]) for r in rows1} == {"a.mp4"}
    a_motion = rows1[0]["motion_energy"]

    # add a second clip, append-catalog: only the new clip is processed
    _make_clip(str(clips / "b.mp4"))
    engine.run_stage(engine.catalog(str(clips), out, frames=4, width=120, append=True),
                     lambda e: None)
    rows2 = list(csv.DictReader(open(manifest)))
    assert {os.path.basename(r["clip"]) for r in rows2} == {"a.mp4", "b.mp4"}
    # the pre-existing row is carried forward unchanged (not recomputed)
    a_row = next(r for r in rows2 if os.path.basename(r["clip"]) == "a.mp4")
    assert a_row["motion_energy"] == a_motion
    # histograms grew to match (one vector per clip)
    import numpy as np
    hz = np.load(os.path.join(out, "histograms.npz"), allow_pickle=True)
    assert len(hz["vecs"]) == 2

    # re-running append with nothing new leaves the manifest unchanged
    engine.run_stage(engine.catalog(str(clips), out, frames=4, width=120, append=True),
                     lambda e: None)
    assert len(list(csv.DictReader(open(manifest)))) == 2


# ── integration: analyze (real librosa) ──────────────────────────────────────
@requires_librosa
@requires_ffmpeg
def test_analyze_writes_json_and_merges(tiny_wav, tmp_path):
    out = str(tmp_path / "audio_out")
    evs = drain(engine.analyze(tiny_wav, out, plot=False))
    final = evs[-1].result
    assert os.path.exists(final["analysis"])
    import json
    an = json.load(open(final["analysis"]))
    assert an["track"] == "synthsong" and an["duration_s"] > 0
    assert os.path.exists(os.path.join(out, "tracks_summary.csv"))
    # real step progress (not just start + done) so the UI never looks frozen
    mids = [e.frac for e in evs if e.frac is not None and not e.done]
    assert any(0 < f < 1 for f in mids), f"no intermediate progress: {mids}"
    # clean completion line, not track_analyze's trailing "Next: …" chatter
    assert "Analyzed synthsong" in evs[-1].message and "Next:" not in evs[-1].message


# ── integration: arrange (sync_clips, numpy only) ────────────────────────────
def test_arrange_builds_sidecars_with_tag(synth_analysis, smoke_manifest):
    final = engine.run_stage(
        engine.arrange(synth_analysis, smoke_manifest, grid="sections",
                       beats_per_cut=3, allow_reuse=True, clip_from="start"),
        lambda e: None)
    for key in ("order", "labels", "markers", "render_sh", "options"):
        assert os.path.exists(final[key]), f"{key} not written"
    # tag defaults to the grid, so the suffix is in the names
    assert "-sections" in os.path.basename(final["render_sh"])
    assert isinstance(final["energy_match"], int)

    # the arrange.json records exactly what options produced this cut
    import json
    meta = json.load(open(final["options"]))
    assert meta["grid"] == "sections" and meta["beats_per_cut"] == 3
    assert meta["allow_reuse"] is True and meta["clip_from"] == "start"
    assert meta["tag"] == "sections" and "energy_match_pct" in meta
    assert meta["outputs"]["cut"].startswith("cut-") and meta["outputs"]["cut"].endswith(".mp4")

    # the same summary rides along in the event so the UIs can show it
    assert final["summary"]["grid"] == "sections"
    assert final["summary"]["energy_match_pct"] == meta["energy_match_pct"]

    # and the render script header is stamped with the same options
    sh = open(final["render_sh"]).read()
    assert "grid=sections" in sh and "clip_from=start" in sh
    # render script is executable
    assert os.stat(final["render_sh"]).st_mode & stat.S_IXUSR


# ── pure: timeline-export prerequisites (source in-point recorded) ────────────
def test_clip_in_point_middle_and_start():
    from pipeline.sync_clips import clip_in_point
    assert clip_in_point(10.0, 4.0, "middle") == 3.0   # centered: (10-4)/2
    assert clip_in_point(4.0, 4.0, "middle") == 0.0    # exact fit
    assert clip_in_point(3.0, 4.0, "middle") == 0.0    # never negative (clip < slot)
    assert clip_in_point(10.0, 4.0, "start") == 0.0    # start always takes from 0


def test_arrange_records_source_in_point_for_export(synth_analysis, smoke_manifest):
    """The order CSV must carry each slot's source in-point + clip duration, and
    arrange.json the timeline geometry + music path — everything a timeline
    export needs to trim exactly, with no recompute."""
    from pipeline.sync_clips import clip_in_point
    final = engine.run_stage(
        engine.arrange(synth_analysis, smoke_manifest, grid="sections",
                       allow_reuse=True, clip_from="middle"),
        lambda e: None)
    with open(final["order"]) as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "no slots"
    for r in rows:
        ss, cdur, d = (float(r["clip_in_s"]), float(r["clip_src_dur_s"]),
                       float(r["slot_dur_s"]))
        assert ss >= 0.0
        # the recorded in-point matches the helper the render uses, exactly
        assert abs(ss - clip_in_point(cdur, d, "middle")) < 1e-6

    import json
    meta = json.load(open(final["options"]))
    assert meta["timeline"] == {"fps": 30, "width": 720, "height": 480}
    assert meta["music"] and meta["duration_s"]      # self-sufficient for export


def test_arrange_missing_analysis_prompts_analyze(tmp_path, smoke_manifest):
    with pytest.raises(engine.StageError) as ei:
        drain(engine.arrange(str(tmp_path / "Ghost.analysis.json"), smoke_manifest))
    assert "Analyze" in str(ei.value)


def test_arrange_missing_manifest_prompts_footage(synth_analysis, tmp_path):
    with pytest.raises(engine.StageError) as ei:
        drain(engine.arrange(synth_analysis, str(tmp_path / "no-manifest.csv")))
    assert "footage" in str(ei.value)


def test_render_missing_script_prompts_arrange(tmp_path):
    with pytest.raises(engine.StageError) as ei:
        drain(engine.render(str(tmp_path / "render-Ghost.sh")))
    assert "Arrange" in str(ei.value)


def test_find_render_script_resolves_suffixed_newest(tmp_path):
    import time
    d = str(tmp_path)
    assert engine.find_render_script(d, "02 Erased.mp3") is None       # none yet
    older = tmp_path / "render-02 Erased-sections.sh"
    older.write_text("#!/bin/bash\n")
    time.sleep(0.02)
    newer = tmp_path / "render-02 Erased-beats.sh"
    newer.write_text("#!/bin/bash\n")
    # track given with OR without extension resolves to the newest match
    assert engine.find_render_script(d, "02 Erased.mp3") == str(newer)
    assert engine.find_render_script(d, "02 Erased") == str(newer)
    # a different track doesn't match
    assert engine.find_render_script(d, "Other.mp3") is None


def test_arrange_different_grids_do_not_clobber(synth_analysis, smoke_manifest):
    r1 = engine.run_stage(engine.arrange(synth_analysis, smoke_manifest, grid="sections"),
                          lambda e: None)
    r2 = engine.run_stage(engine.arrange(synth_analysis, smoke_manifest, grid="downbeats"),
                          lambda e: None)
    assert r1["render_sh"] != r2["render_sh"]
    assert os.path.exists(r1["render_sh"]) and os.path.exists(r2["render_sh"])


# ── integration: render (full chain, exercises per-segment progress) ─────────
@requires_cv2
@requires_ffmpeg
def test_render_produces_video_with_real_progress(clips_dir, tiny_wav, tmp_path):
    # catalog the real generated clips so the manifest points at files that exist
    cat = str(tmp_path / "catalog")
    engine.run_stage(engine.catalog(clips_dir, cat, frames=4, width=120), lambda e: None)
    manifest = os.path.join(cat, "manifest.csv")
    analysis = write_analysis(str(tmp_path), tiny_wav, track="synthsong", duration=4.0)
    cuts = str(tmp_path / "cuts")
    arr = engine.run_stage(
        engine.arrange(analysis, manifest, grid="sections", allow_reuse=True, cut_dir=cuts),
        lambda e: None)
    evs = drain(engine.render(arr["render_sh"]))
    final = evs[-1]
    assert final.done and os.path.exists(final.result["video"])
    # cut_dir routed the mp4 to the cuts folder (not the sidecar dir)
    assert os.path.dirname(final.result["video"]) == cuts
    # the completion event prints the absolute output path (so the UIs surface it)
    assert final.result["video"] in final.message and "Render complete" in final.message
    # the per-segment echoes give a real (non-None) advancing fraction
    fracs = [e.frac for e in evs if e.frac is not None]
    assert fracs and max(fracs) == pytest.approx(1.0)


# ── projects ─────────────────────────────────────────────────────────────────
def _smoke_clip_paths(manifest, n):
    import csv as _csv
    with open(manifest, newline="") as fh:
        return [r["clip"] for r in list(_csv.DictReader(fh))[:n]]


def test_project_save_load_roundtrip(tmp_path):
    p = engine.Project(name="My Vid", track="02 Erased.mp3", clips=["/a.mp4"],
                       grid="beats", beats_per_cut=2, allow_reuse=True,
                       dir=str(tmp_path / "proj"))
    path = p.save()
    q = engine.Project.load(path)
    assert q.name == "My Vid" and q.track == "02 Erased.mp3"
    assert q.clips == ["/a.mp4"] and q.grid == "beats" and q.allow_reuse is True
    assert q.arrange_opts()["beats_per_cut"] == 2


def test_new_list_load_project(tmp_path):
    media = str(tmp_path)
    engine.new_project(media, "Summer Reel", "05 Of Ash.mp3", clips="all", grid="downbeats")
    assert engine.list_projects(media) == ["Summer Reel"]
    p = engine.load_project(media, "Summer Reel")
    assert p.track == "05 Of Ash.mp3" and p.grid == "downbeats" and p.clips == "all"


def test_write_scoped_manifest(tmp_path, smoke_manifest):
    # "all" -> use the library manifest untouched
    assert engine.write_scoped_manifest(smoke_manifest, "all", "x") == smoke_manifest
    # a subset -> a filtered manifest with only those rows
    picks = _smoke_clip_paths(smoke_manifest, 2)
    out = str(tmp_path / "scope.csv")
    engine.write_scoped_manifest(smoke_manifest, picks, out)
    import csv as _csv
    rows = list(_csv.DictReader(open(out)))
    assert len(rows) == 2 and {r["clip"] for r in rows} == set(picks)
    # a selection matching nothing fails loud
    with pytest.raises(engine.StageError):
        engine.write_scoped_manifest(smoke_manifest, ["/nope.mp4"], str(tmp_path / "y.csv"))


def test_manifest_sources(smoke_manifest):
    src = engine.manifest_sources(smoke_manifest)
    assert src and all(isinstance(v, list) and v for v in src.values())


def test_list_audio_tracks(tmp_path):
    aa = tmp_path / "album-audio"
    aa.mkdir()
    for name in ("02 Erased.mp3", "05 Of Ash.m4a", "cover.jpg", "notes.txt"):
        (aa / name).write_bytes(b"")
    assert engine.list_audio_tracks(str(tmp_path)) == ["02 Erased.mp3", "05 Of Ash.m4a"]
    assert engine.list_audio_tracks(str(tmp_path / "nope")) == []   # no album-audio


def test_arrange_project_outputs_into_project_dir(tmp_path, smoke_manifest):
    import shutil
    media = tmp_path / "media"
    (media / "catalog_audio").mkdir(parents=True)
    (media / "catalog").mkdir()
    write_analysis(str(media / "catalog_audio"), "/tmp/Song.mp3", track="Song")
    shutil.copy(smoke_manifest, media / "catalog" / "manifest.csv")
    p = engine.new_project(str(media), "Proj1", "Song.mp3", clips="all", grid="sections")
    final = engine.run_stage(engine.arrange_project(p, str(media)), lambda e: None)
    # outputs live in the project folder, not the shared catalog_audio
    assert os.path.dirname(final["render_sh"]) == p.dir
    assert os.path.exists(final["render_sh"]) and os.path.exists(final["options"])
    assert final["summary"]["grid"] == "sections"


def test_arrange_project_keeps_grid_variants(tmp_path, smoke_manifest):
    """Trying a different sync scheme in a project accumulates side-by-side
    variants (so you can compare cuts), instead of overwriting one."""
    import shutil
    media = tmp_path / "media"
    (media / "catalog_audio").mkdir(parents=True)
    (media / "catalog").mkdir()
    write_analysis(str(media / "catalog_audio"), "/tmp/Song.mp3", track="Song")
    shutil.copy(smoke_manifest, media / "catalog" / "manifest.csv")
    p = engine.new_project(str(media), "Proj", "Song.mp3", clips="all", grid="sections")
    engine.run_stage(engine.arrange_project(p, str(media)), lambda e: None)
    p.grid = "downbeats"
    engine.run_stage(engine.arrange_project(p, str(media)), lambda e: None)
    have = sorted(f for f in os.listdir(p.dir) if f.startswith("render-"))
    assert have == ["render-Song-downbeats.sh", "render-Song-sections.sh"]   # both kept


# ── export: editable timeline (OTIO / FCPXML) ────────────────────────────────
@requires_otio
def test_build_timeline_structure():
    """Pure: meta + order rows -> an OTIO timeline (V1 cut clips, A1 music) with
    frame-accurate source trims, no file I/O."""
    import opentimelineio as otio
    from pipeline.export_timeline import build_timeline
    meta = {"track": "Song", "tag": "sections", "music": "/tmp/song.mp3",
            "duration_s": 4.0, "timeline": {"fps": 30, "width": 720, "height": 480}}
    rows = [{"clip": "/v/a.mp4", "slot_dur_s": "2.0", "clip_in_s": "3.0",
             "clip_src_dur_s": "6.83"},
            {"clip": "/v/b.mp4", "slot_dur_s": "2.0", "clip_in_s": "0.0",
             "clip_src_dur_s": "2.0"}]
    tl = build_timeline(meta, rows)
    video, audio = tl.tracks[0], tl.tracks[1]
    assert video.kind == otio.schema.TrackKind.Video
    assert audio.kind == otio.schema.TrackKind.Audio
    clips = lambda trk: [c for c in trk if isinstance(c, otio.schema.Clip)]
    vclips = clips(video)
    assert [c.name for c in vclips] == ["a", "b"]
    # first slot: middle in-point 3.0s @30fps = frame 90, 2.0s = 60 frames
    assert vclips[0].source_range.start_time.value == 90
    assert vclips[0].source_range.duration.value == 60
    aclips = clips(audio)
    assert len(aclips) == 1                       # the music, full song
    assert aclips[0].source_range.duration.value == round(4.0 * 30)


@requires_otio
def test_export_writes_otio_and_reads_back(synth_analysis, smoke_manifest, tmp_path):
    """End-to-end: arrange -> export. The .otio round-trips to the same clip
    count, and the timeline name carries the tag."""
    import opentimelineio as otio
    arr = engine.run_stage(
        engine.arrange(synth_analysis, smoke_manifest, grid="sections",
                       allow_reuse=True),
        lambda e: None)
    out = str(tmp_path / "export")
    final = engine.run_stage(engine.export(arr["options"], out_dir=out,
                                           formats=("otio",)), lambda e: None)
    assert "otio" in final and os.path.exists(final["otio"])
    tl = otio.adapters.read_from_file(final["otio"])
    # one clip per arranged slot on V1, plus the music on A1
    import csv as _csv
    n_slots = len(list(_csv.DictReader(open(arr["order"]))))
    clips = lambda trk: [c for c in trk if isinstance(c, otio.schema.Clip)]
    assert len(clips(tl.tracks[0])) == n_slots
    assert len(clips(tl.tracks[1])) == 1
    assert tl.name.endswith("-sections")


@requires_fcpx
def test_export_writes_fcpxml(synth_analysis, smoke_manifest, tmp_path):
    arr = engine.run_stage(
        engine.arrange(synth_analysis, smoke_manifest, grid="sections",
                       allow_reuse=True),
        lambda e: None)
    out = str(tmp_path / "export")
    final = engine.run_stage(engine.export(arr["options"], out_dir=out,
                                           formats=("fcpxml",)), lambda e: None)
    assert "fcpxml" in final and os.path.exists(final["fcpxml"])
    head = open(final["fcpxml"]).read(200)
    assert "<fcpxml" in head                      # a real FCPXML document


def test_export_missing_arrange_prompts_arrange(tmp_path):
    """No arrangement yet → an actionable StageError, not a cryptic file error."""
    with pytest.raises(engine.StageError) as ei:
        list(engine.export(str(tmp_path / "nope.arrange.json")))
    assert "Arrange" in str(ei.value)


@requires_otio
def test_export_project_outputs_into_project_dir(tmp_path, smoke_manifest):
    import shutil
    media = tmp_path / "media"
    (media / "catalog_audio").mkdir(parents=True)
    (media / "catalog").mkdir()
    write_analysis(str(media / "catalog_audio"), "/tmp/Song.mp3", track="Song")
    shutil.copy(smoke_manifest, media / "catalog" / "manifest.csv")
    p = engine.new_project(str(media), "Proj", "Song.mp3", clips="all", grid="sections")
    engine.run_stage(engine.arrange_project(p, str(media)), lambda e: None)
    final = engine.run_stage(engine.export_project(p, str(media), formats=("otio",)),
                             lambda e: None)
    assert os.path.dirname(final["otio"]) == p.dir
    assert os.path.basename(final["otio"]) == "Song-sections.otio"


# ── compare: arrange across grids, rank by energy match ──────────────────────
def test_compare_ranks_grids_and_writes_variants(synth_analysis, smoke_manifest, tmp_path):
    """compare() arranges every grid (leaving each variant on disk) and returns
    a table ranked best-match-first."""
    cuts = str(tmp_path / "cuts")
    grids = ("sections", "downbeats", "beats")
    final = engine.run_stage(
        engine.compare(synth_analysis, smoke_manifest, grids=grids,
                       allow_reuse=True, cut_dir=cuts),
        lambda e: None)
    rows = final["comparison"]
    assert [r["grid"] for r in rows] == list(grids)        # one row per grid, in order
    assert all(isinstance(r["energy_match_pct"], (int, float)) for r in rows)
    # ranked is sorted best-match-first; best matches the top of ranked
    ranked = final["ranked"]
    pcts = [r["energy_match_pct"] for r in ranked]
    assert pcts == sorted(pcts, reverse=True)
    assert final["best"] == ranked[0]["grid"]
    # every grid's sidecars were actually written (variants ready to render/export)
    for r in rows:
        assert os.path.exists(r["render_sh"]) and os.path.exists(r["options"])


def test_compare_one_bad_grid_becomes_error_row(synth_analysis, smoke_manifest):
    """A grid that can't be arranged is recorded as an error row, not an abort."""
    final = engine.run_stage(
        engine.compare(synth_analysis, smoke_manifest,
                       grids=("sections", "bogus"), allow_reuse=True),
        lambda e: None)
    by_grid = {r["grid"]: r for r in final["comparison"]}
    assert by_grid["sections"]["energy_match_pct"] is not None
    assert by_grid["bogus"]["energy_match_pct"] is None and "error" in by_grid["bogus"]
    assert final["best"] == "sections"                     # the good one still wins


def test_compare_missing_analysis_prompts_analyze(tmp_path, smoke_manifest):
    with pytest.raises(engine.StageError) as ei:
        list(engine.compare(str(tmp_path / "nope.analysis.json"), smoke_manifest))
    assert "Analyze" in str(ei.value)


def test_compare_project_sweeps_into_project_dir(tmp_path, smoke_manifest):
    import shutil
    media = tmp_path / "media"
    (media / "catalog_audio").mkdir(parents=True)
    (media / "catalog").mkdir()
    write_analysis(str(media / "catalog_audio"), "/tmp/Song.mp3", track="Song")
    shutil.copy(smoke_manifest, media / "catalog" / "manifest.csv")
    p = engine.new_project(str(media), "Proj", "Song.mp3", clips="all", allow_reuse=True)
    final = engine.run_stage(
        engine.compare_project(p, str(media), grids=("sections", "downbeats")),
        lambda e: None)
    assert {r["grid"] for r in final["comparison"]} == {"sections", "downbeats"}
    have = sorted(f for f in os.listdir(p.dir) if f.startswith("render-"))
    assert have == ["render-Song-downbeats.sh", "render-Song-sections.sh"]
