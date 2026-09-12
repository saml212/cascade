"""Regression tests for transcript-grounded delivery audio continuity."""

from unittest.mock import patch

import numpy as np

from agents.qa import (
    _output_continuity_targets,
    clip_review_revision,
    quality_revision,
)
from lib.audio_qa import (
    OutputContinuityConfig,
    WindowStats,
    analyze_output_continuity,
    analyze_output_windows,
)
from lib.timeline import Timeline

FRAME_SECONDS = 0.1
SETTINGS = OutputContinuityConfig(
    frame_seconds=FRAME_SECONDS,
    min_issue_seconds=0.3,
    bridge_seconds=0,
)


def _stats(duration=10.0, level=-20.0):
    frames = round(duration / FRAME_SECONDS)
    rms = np.full((frames, 2), level, dtype=float)
    peak = np.full((frames, 2), 10 ** (level / 20), dtype=float)
    zero = np.zeros((frames, 2), dtype=float)
    return WindowStats(FRAME_SECONDS, rms, peak, zero)


def _set_silence(stats, start, end, *, level=-240.0, exact=True):
    first = round(start / FRAME_SECONDS)
    last = round(end / FRAME_SECONDS)
    stats.rms_dbfs[first:last, :] = level
    stats.peak[first:last, :] = 0 if exact else 10 ** (level / 20)
    stats.zero_fraction[first:last, :] = 1 if exact else 0


def _word(start, end, text="speech"):
    return {"start": start, "end": end, "speaker": "host", "word": text}


def _scan_identity(path):
    stat = path.stat()
    return {
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def test_speech_dropout_is_blocking_but_natural_pause_is_not():
    timeline = Timeline.from_edits(10, [])
    stats = _stats()
    _set_silence(stats, 4.0, 4.5)

    dropout = analyze_output_windows(
        stats,
        words=[_word(4.05, 4.45)],
        timeline=timeline,
        episode_timeline=timeline,
        artifact_clock="source",
        role="selected_audio_master",
        config=SETTINGS,
    )
    pause = analyze_output_windows(
        stats,
        words=[_word(1.0, 2.0)],
        timeline=timeline,
        episode_timeline=timeline,
        artifact_clock="source",
        role="selected_audio_master",
        config=SETTINGS,
    )

    assert len(dropout) == 1
    assert dropout[0]["kind"] == "digital_zero"
    assert dropout[0]["evidence"]["speech_overlap_seconds"] == 0.4
    assert pause == []


def test_selected_master_ignores_silence_and_speech_removed_by_preroll_trim():
    timeline = Timeline.from_edits(10, [{"type": "trim_start", "seconds": 2.0}])
    stats = _stats()
    _set_silence(stats, 0.2, 1.0)

    findings = analyze_output_windows(
        stats,
        words=[_word(0.3, 0.9)],
        timeline=timeline,
        episode_timeline=timeline,
        artifact_clock="source",
        role="selected_audio_master",
        config=SETTINGS,
    )

    assert findings == []


def test_near_silence_on_both_channels_is_detected():
    timeline = Timeline.from_edits(10, [])
    stats = _stats()
    _set_silence(stats, 4.0, 4.5, level=-65.0, exact=False)

    findings = analyze_output_windows(
        stats,
        words=[_word(4.05, 4.45)],
        timeline=timeline,
        episode_timeline=timeline,
        artifact_clock="output",
        role="podcast_audio",
        config=SETTINGS,
    )

    assert [finding["kind"] for finding in findings] == ["near_zero"]


def test_edited_output_maps_silence_back_through_an_interior_cut():
    timeline = Timeline.from_edits(
        12, [{"type": "cut", "start_seconds": 4.0, "end_seconds": 6.0}]
    )
    stats = _stats(duration=timeline.duration)
    _set_silence(stats, 4.0, 4.5)

    findings = analyze_output_windows(
        stats,
        words=[_word(6.05, 6.45)],
        timeline=timeline,
        episode_timeline=timeline,
        artifact_clock="output",
        role="upload_video",
        config=SETTINGS,
    )

    assert len(findings) == 1
    assert findings[0]["artifact_time"]["start_seconds"] == 4.0
    assert findings[0]["source_ranges"] == [{"start_seconds": 6.0, "end_seconds": 6.5}]
    assert findings[0]["episode_output_ranges"] == [
        {"start_seconds": 4.0, "end_seconds": 4.5}
    ]


def test_short_output_uses_clip_local_clock_and_global_episode_mapping():
    episode_timeline = Timeline.from_edits(
        12, [{"type": "cut", "start_seconds": 4.0, "end_seconds": 6.0}]
    )
    short_timeline = episode_timeline.slice(7.0, 9.0)
    stats = _stats(duration=2.0)
    _set_silence(stats, 0.0, 0.5)

    findings = analyze_output_windows(
        stats,
        words=[_word(7.05, 7.45)],
        timeline=short_timeline,
        episode_timeline=episode_timeline,
        artifact_clock="output",
        role="short",
        config=SETTINGS,
    )

    assert findings[0]["artifact_time"]["start_seconds"] == 0.0
    assert findings[0]["source_ranges"] == [{"start_seconds": 7.0, "end_seconds": 7.5}]
    assert findings[0]["episode_output_ranges"] == [
        {"start_seconds": 5.0, "end_seconds": 5.5}
    ]


def test_stale_artifact_is_blocking_and_is_never_decoded(tmp_path):
    timeline = Timeline.from_edits(10, [])
    target = {
        "role": "upload_video",
        "path": str(tmp_path / "upload_video.mp4"),
        "clock": "output",
        "required": True,
        "revision": "sha256:old",
        "status": "stale",
        "detail": "Render inputs changed.",
        "timeline": timeline,
    }

    with patch("lib.audio_qa.decode_audio_windows") as decode:
        report = analyze_output_continuity(
            [target],
            {"utterances": [{"speaker": "host", "words": [_word(1, 2)]}]},
            episode_timeline=timeline,
            transcript_fingerprint="sha256:transcript",
            config=SETTINGS,
        )

    assert report["status"] == "stale"
    assert report["safe"] is False
    assert report["artifacts"][0]["currentness"] == "stale"
    decode.assert_not_called()


def test_truncated_current_artifact_cannot_pass(tmp_path):
    path = tmp_path / "upload_video.mp4"
    path.write_bytes(b"current")
    timeline = Timeline.from_edits(10, [])
    target = {
        "role": "upload_video",
        "path": str(path),
        "clock": "output",
        "required": True,
        "revision": "sha256:current",
        "status": "current",
        "detail": "current",
        "timeline": timeline,
        "scan_identity": _scan_identity(path),
    }

    with patch("lib.audio_qa.decode_audio_windows", return_value=_stats(duration=8)):
        report = analyze_output_continuity(
            [target],
            {"utterances": [{"speaker": "host", "words": [_word(1, 2)]}]},
            episode_timeline=timeline,
            transcript_fingerprint="sha256:transcript",
            config=SETTINGS,
        )

    assert report["status"] == "error"
    assert "ends at 8.000s" in report["artifacts"][0]["detail"]


def test_unknown_required_currentness_status_fails_closed(tmp_path):
    timeline = Timeline.from_edits(10, [])
    target = {
        "role": "upload_video",
        "path": str(tmp_path / "upload_video.mp4"),
        "clock": "output",
        "required": True,
        "status": "maybe",
        "detail": "unknown",
        "timeline": timeline,
    }

    report = analyze_output_continuity(
        [target],
        {"utterances": [{"speaker": "host", "words": [_word(1, 2)]}]},
        episode_timeline=timeline,
        transcript_fingerprint="sha256:transcript",
        config=SETTINGS,
    )

    assert report["status"] == "error"
    assert report["safe"] is False
    assert report["artifacts"][0]["status"] == "error"


def test_artifact_replaced_during_decode_is_not_certified(tmp_path):
    path = tmp_path / "upload_video.mp4"
    path.write_bytes(b"before")
    timeline = Timeline.from_edits(10, [])
    target = {
        "role": "upload_video",
        "path": str(path),
        "clock": "output",
        "required": True,
        "revision": "sha256:current",
        "status": "current",
        "detail": "current",
        "timeline": timeline,
        "scan_identity": _scan_identity(path),
    }

    def replace_artifact(*args, **kwargs):
        path.write_bytes(b"replacement is longer")
        return _stats(duration=10)

    with patch("lib.audio_qa.decode_audio_windows", side_effect=replace_artifact):
        report = analyze_output_continuity(
            [target],
            {"utterances": [{"speaker": "host", "words": [_word(1, 2)]}]},
            episode_timeline=timeline,
            transcript_fingerprint="sha256:transcript",
            config=SETTINGS,
        )

    assert report["status"] == "error"
    assert "changed while" in report["artifacts"][0]["detail"]


def test_target_resolution_uses_current_selected_repair_not_base_mix(tmp_path):
    episode_dir = tmp_path / "episode"
    (episode_dir / "work").mkdir(parents=True)
    (episode_dir / "shorts").mkdir()
    selected = episode_dir / "work" / "audio_repair_selected.wav"
    selected.write_bytes(b"selected repair")
    base = episode_dir / "work" / "audio_mix.wav"
    base.write_bytes(b"unselected base")
    selected_stat = selected.stat()
    selection = {
        "fingerprint": "sha256:selection",
        "selected_output": {
            "path": str(selected),
            "fingerprint": {
                "size_bytes": selected_stat.st_size,
                "mtime_ns": selected_stat.st_mtime_ns,
            },
        },
    }
    timeline = Timeline.from_edits(10, [])

    with (
        patch("agents.qa.current_audio_selection", return_value=selection),
        patch("agents.qa.current_episode_longform_render", return_value=None),
    ):
        targets = _output_continuity_targets(
            episode_dir, {}, [], {"platforms": {}}, timeline
        )

    master = targets[0]
    assert master["role"] == "selected_audio_master"
    assert master["status"] == "current"
    assert master["path"] == str(selected.resolve())
    assert master["path"] != str(base.resolve())


def test_quality_revision_binds_transcript_mp3_and_render_output(tmp_path):
    config = {"platforms": {"podcast_rss": {"enabled": True}}}
    baseline = quality_revision(tmp_path, {}, config=config)

    (tmp_path / "diarized_transcript.json").write_text('{"words":[]}')
    transcript_revision = quality_revision(tmp_path, {}, config=config)
    assert transcript_revision != baseline

    (tmp_path / "podcast_audio.mp3").write_bytes(b"current mp3")
    podcast_revision = quality_revision(tmp_path, {}, config=config)
    assert podcast_revision != transcript_revision

    (tmp_path / "render_manifest.json").write_text(
        '{"version":1,"shorts":{},"longform":{"output":{"size_bytes":1}}}'
    )
    assert quality_revision(tmp_path, {}, config=config) != podcast_revision

    dormant_revision = quality_revision(tmp_path, {}, config={})
    (tmp_path / "podcast_audio.mp3").write_bytes(b"ignored dormant mp3")
    assert quality_revision(tmp_path, {}, config={}) == dormant_revision


def test_clip_approval_revision_binds_remastered_output_identity():
    clip = {"id": "clip_01", "start_seconds": 1, "end_seconds": 2}
    original = {
        "fingerprint": "sha256:render",
        "output": {"size_bytes": 100, "mtime_ns": 1},
    }
    remastered = {
        **original,
        "output": {"size_bytes": 101, "mtime_ns": 2},
    }

    assert clip_review_revision(clip, original) != clip_review_revision(
        clip, remastered
    )
