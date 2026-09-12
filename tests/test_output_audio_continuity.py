"""Regression tests for transcript-grounded delivery audio continuity."""

import os
from unittest.mock import patch

import numpy as np
import pytest

from agents.qa import (
    _output_continuity_targets,
    _render_audio_mapping,
    clip_review_revision,
    quality_revision,
)
from lib.audio_qa import (
    OUTPUT_SOURCE_MAPPING_SCHEMA,
    OutputContinuityConfig,
    WindowStats,
    analyze_output_continuity,
    analyze_output_windows,
)
from lib.delivery_video import RENDER_PIPELINE_VERSION, aac_content_timing_proof
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
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "resolved_path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _video_source_mapping(timeline, path):
    timing = aac_content_timing_proof()
    identity = _scan_identity(path)
    return {
        "schema": OUTPUT_SOURCE_MAPPING_SCHEMA,
        "pipeline_version": RENDER_PIPELINE_VERSION,
        "source_intervals": [list(item) for item in timeline.keep_intervals],
        "audio_codec": "aac",
        "audio_sample_rate_hz": timing["sample_rate_hz"],
        "audio_timing": timing,
        "timing_provenance": {
            "method": "render-manifest/v1",
            "media_identity": identity,
            "stream": {
                "codec_name": "aac",
                "sample_rate_hz": timing["sample_rate_hz"],
                "start_pts": 0,
                "time_base": "1/48000",
                "initial_padding": 0,
            },
        },
    }


def _aac_probe_data(sample_rate="48000"):
    return {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": sample_rate,
                "start_pts": 0,
                "time_base": "1/48000",
                "initial_padding": 0,
            }
        ]
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


def test_encoded_video_uses_render_timeline_and_proven_content_offset(tmp_path):
    path = tmp_path / "upload_video.mp4"
    path.write_bytes(b"current")
    editorial_timeline = Timeline(12, [(0, 4), (6, 12)])
    render_timeline = Timeline(12, [(0.01, 3.99), (6.01, 12)])
    frame_seconds = 0.01
    frames = round(10 / frame_seconds)
    rms = np.full((frames, 2), -20.0)
    peak = np.full((frames, 2), 0.1)
    zero = np.zeros((frames, 2))
    stats = WindowStats(frame_seconds, rms, peak, zero)
    first = round(4.21 / frame_seconds)
    last = round(4.62 / frame_seconds)
    stats.rms_dbfs[first:last, :] = -240
    stats.peak[first:last, :] = 0
    stats.zero_fraction[first:last, :] = 1
    target = {
        "role": "upload_video",
        "path": str(path),
        "clock": "output",
        "required": True,
        "revision": "sha256:current",
        "status": "current",
        "detail": "current",
        "timeline": render_timeline,
        "source_mapping": _video_source_mapping(render_timeline, path),
        "scan_identity": _scan_identity(path),
    }
    settings = OutputContinuityConfig(
        frame_seconds=frame_seconds,
        min_issue_seconds=0.3,
        bridge_seconds=0,
    )

    with patch("lib.audio_qa.decode_audio_windows", return_value=stats):
        report = analyze_output_continuity(
            [target],
            {"utterances": [{"speaker": "host", "words": [_word(6.22, 6.60)]}]},
            episode_timeline=editorial_timeline,
            transcript_fingerprint="sha256:transcript",
            config=settings,
        )

    finding = report["findings"][0]
    assert finding["artifact_time"] == {
        "clock": "output",
        "start_seconds": 4.21,
        "end_seconds": 4.62,
        "duration_seconds": 0.41,
    }
    assert finding["source_ranges"][0]["start_seconds"] == pytest.approx(6.218667)
    assert finding["source_ranges"][0]["end_seconds"] == pytest.approx(6.628667)
    assert finding["episode_output_ranges"][0]["start_seconds"] == pytest.approx(
        4.218667
    )
    assert finding["episode_output_ranges"][0]["end_seconds"] == pytest.approx(4.628667)


@pytest.mark.parametrize("proof_state", ["missing", "stale"])
def test_current_video_without_exact_source_mapping_proof_fails_closed(
    tmp_path, proof_state
):
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
    if proof_state == "stale":
        target["source_mapping"] = {
            **_video_source_mapping(timeline, path),
            "source_intervals": [[0, 9]],
        }

    with patch("lib.audio_qa.decode_audio_windows") as decode:
        report = analyze_output_continuity(
            [target],
            {"utterances": [{"speaker": "host", "words": [_word(1, 2)]}]},
            episode_timeline=timeline,
            transcript_fingerprint="sha256:transcript",
            config=SETTINGS,
        )

    assert report["status"] == "error"
    assert report["safe"] is False
    assert "source mapping proof" in report["artifacts"][0]["detail"]
    decode.assert_not_called()


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
        "source_mapping": _video_source_mapping(timeline, path),
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
    original_stat = path.stat()
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
        "source_mapping": _video_source_mapping(timeline, path),
        "scan_identity": _scan_identity(path),
    }

    def replace_artifact(*args, **kwargs):
        replacement = tmp_path / "replacement.mp4"
        replacement.write_bytes(b"after!")
        os.utime(
            replacement,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        replacement.replace(path)
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


def test_legacy_video_mapping_rejects_symlink_retarget_during_probe(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    logical = tmp_path / "upload_video.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"other")
    logical.symlink_to(first)
    stat = logical.stat()
    record = {
        "pipeline_version": RENDER_PIPELINE_VERSION,
        "keep_intervals": [[0, 10]],
        "output_duration_seconds": 10,
        "output": {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "audio_codec": "aac",
        },
    }

    def retarget(_path):
        logical.unlink()
        logical.symlink_to(second)
        return _aac_probe_data()

    with (
        patch("agents.qa.ffprobe", side_effect=retarget),
        pytest.raises(ValueError, match="timing evidence"),
    ):
        _render_audio_mapping(logical, record, "keep_intervals", 10)


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


def test_video_targets_use_exact_manifest_source_intervals(tmp_path):
    episode_dir = tmp_path / "episode"
    (episode_dir / "work").mkdir(parents=True)
    (episode_dir / "shorts").mkdir()
    selected = episode_dir / "work" / "audio_repair_selected.wav"
    upload = episode_dir / "upload_video.mp4"
    short = episode_dir / "shorts" / "clip_01.mp4"
    selected.write_bytes(b"selected")
    upload.write_bytes(b"longform")
    short.write_bytes(b"short")
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
    timing = aac_content_timing_proof()

    def record(path, field, intervals, *, include_timing):
        stat = path.stat()
        render_timeline = Timeline(12, intervals)
        output = {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "audio_codec": "aac",
        }
        if include_timing:
            output.update(
                audio_sample_rate_hz=timing["sample_rate_hz"],
                audio_timing=timing,
            )
        return {
            "pipeline_version": RENDER_PIPELINE_VERSION,
            field: intervals,
            "output_duration_seconds": round(render_timeline.duration, 3),
            "output": output,
        }

    longform_intervals = [[0.01, 3.99], [6.01, 12]]
    short_intervals = [[6.21, 7.19]]
    longform = record(
        upload, "keep_intervals", longform_intervals, include_timing=False
    )
    short_record = record(
        short, "clip_source_intervals", short_intervals, include_timing=True
    )
    editorial = Timeline(12, [(0, 4), (6, 12)])
    clips = [
        {
            "id": "clip_01",
            "start_seconds": 6.2,
            "end_seconds": 7.2,
            "selection_status": "selected",
        }
    ]

    with (
        patch("agents.qa.current_audio_selection", return_value=selection),
        patch(
            "agents.qa._render_status",
            return_value=(longform, {"clip_01": short_record}),
        ),
        patch("agents.qa.ffprobe", return_value=_aac_probe_data()),
    ):
        targets = _output_continuity_targets(
            episode_dir, {}, clips, {"platforms": {}}, editorial
        )

    upload_target = next(item for item in targets if item["role"] == "upload_video")
    short_target = next(item for item in targets if item["role"] == "short")
    assert upload_target["status"] == "current"
    assert list(upload_target["timeline"].keep_intervals) == [
        tuple(item) for item in longform_intervals
    ]
    assert upload_target["source_mapping"]["audio_timing"] == timing
    assert (
        upload_target["source_mapping"]["timing_provenance"]["method"]
        == "source-clock-v3-current-stream/v1"
    )
    assert short_target["status"] == "current"
    assert list(short_target["timeline"].keep_intervals) == [
        tuple(item) for item in short_intervals
    ]
    assert (
        short_target["source_mapping"]["timing_provenance"]["method"]
        == "render-manifest/v1"
    )


@pytest.mark.parametrize(
    ("record_changes", "output_changes", "stream_rate"),
    [
        ({"pipeline_version": "source-clock/v2"}, {}, "48000"),
        ({"output_duration_seconds": 9}, {}, "48000"),
        ({}, {"size_bytes": 999}, "48000"),
        ({}, {}, "44100"),
    ],
)
def test_legacy_video_mapping_rejects_unknown_or_stale_evidence(
    tmp_path, record_changes, output_changes, stream_rate
):
    path = tmp_path / "upload_video.mp4"
    path.write_bytes(b"current")
    stat = path.stat()
    record = {
        "pipeline_version": RENDER_PIPELINE_VERSION,
        "keep_intervals": [[0, 10]],
        "output_duration_seconds": 10,
        "output": {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "audio_codec": "aac",
            **output_changes,
        },
        **record_changes,
    }

    with (
        patch("agents.qa.ffprobe", return_value=_aac_probe_data(stream_rate)),
        pytest.raises(ValueError, match="Video render"),
    ):
        _render_audio_mapping(path, record, "keep_intervals", 10)


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
