"""Tests for shared source-clock render infrastructure."""

import json
import shutil
import subprocess
import wave
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from lib.ass import CaptionStyle, generate_ass_from_diarized
from lib.delivery_video import (
    AAC_ENCODER_DELAY_SAMPLES,
    TRANSCRIPT_RENDER_REUSE_VERSION,
    _audio_filter_graph,
    _short_render_fingerprint,
    aac_content_timing_proof,
    build_keep_intervals,
    build_render_segments,
    capture_transcript_render_reuse_proof,
    concat_video_segments,
    current_longform_render,
    current_short_render,
    ffmpeg_executable,
    longform_render_fingerprint,
    longform_trim_reuse_fingerprint,
    migrate_unchanged_short_crop_fingerprints,
    migrate_unchanged_transcript_render_fingerprints,
    mux_timeline_audio,
    prepare_longform_trim_reuse,
    record_longform_render,
    record_short_render,
    render_config_for_episode,
    render_fingerprint,
    render_space_budget,
    render_space_status,
    render_video_segment,
    reusable_terminal_trim_render,
    short_render_fingerprint,
    source_fps,
    staged_render_output,
    video_packet_signature,
)
from lib.loudness import delivery_loudness_policy
from lib.srt import escape_srt_path
from lib.timeline import Timeline, rebase_diarized


def _record_short_in_process(args):
    episode_dir, clip_id = args
    episode_dir = Path(episode_dir)
    output = episode_dir / "shorts" / f"{clip_id}.mp4"
    output.write_bytes(clip_id.encode())
    record_short_render(
        episode_dir,
        clip_id,
        fingerprint=f"fingerprint-{clip_id}",
        timeline=Timeline.from_edits(1),
        media={"duration_seconds": 1},
    )
    return clip_id


_REUSE_PROBE = {
    "format": {"duration": "30"},
    "streams": [{"codec_type": "video", "r_frame_rate": "30/1"}],
}


def _transcript_reuse_inputs(tmp_path):
    (tmp_path / "shorts").mkdir()
    (tmp_path / "subtitles").mkdir()
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio_mix.wav"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    transcript = {
        "utterances": [
            {
                "speaker": "speaker_0",
                "words": [
                    {
                        "word": "alpha",
                        "punctuated_word": "Alpha",
                        "start": 2.0,
                        "end": 2.4,
                    },
                    {
                        "word": "bravo",
                        "punctuated_word": "bravo.",
                        "start": 12.0,
                        "end": 12.5,
                    },
                ],
            }
        ]
    }
    (tmp_path / "diarized_transcript.json").write_text(json.dumps(transcript))
    episode = {
        "crop_config": {"wide_zoom": 1},
        "longform_edits": [],
        "delivery_burn_captions": False,
    }
    config = {"processing": {"use_hardware_accel": False}}
    segments = [
        {"start": 0, "end": 10.001, "speaker": "speaker_0"},
        {"start": 10.001, "end": 30, "speaker": "speaker_1"},
    ]
    clips = [
        {"id": "clip_a", "start_seconds": 1, "end_seconds": 5},
        {"id": "clip_b", "start_seconds": 11, "end_seconds": 15},
    ]
    timeline = Timeline.from_edits(30).quantize("30/1")
    (tmp_path / "upload_video.mp4").write_bytes(b"longform")
    (tmp_path / "subtitles" / "longform.ass").write_text("long captions")
    record_longform_render(
        tmp_path,
        fingerprint=longform_render_fingerprint(
            tmp_path, episode, config, audio, segments
        ),
        render_mode="speaker_cut",
        timeline=timeline,
        media={"duration_seconds": 30},
        captions={
            "path": "subtitles/longform.ass",
            "format": "ass",
            "burned_in": False,
        },
    )
    for clip in clips:
        clip_id = clip["id"]
        (tmp_path / "shorts" / f"{clip_id}.mp4").write_bytes(clip_id.encode())
        (tmp_path / "subtitles" / f"{clip_id}.ass").write_text("captions")
        record_short_render(
            tmp_path,
            clip_id,
            fingerprint=short_render_fingerprint(
                tmp_path, episode, config, audio, segments, clip
            ),
            timeline=timeline.slice(
                clip["start_seconds"], clip["end_seconds"]
            ).quantize("30/1"),
            media={"duration_seconds": 4},
            captions={
                "path": f"subtitles/{clip_id}.ass",
                "format": "ass",
                "burned_in": True,
            },
        )
    return episode, config, audio, segments, clips, transcript


def test_keep_intervals_applies_trims_and_cuts():
    edits = [
        {"type": "trim_start", "seconds": 10},
        {"type": "trim_end", "seconds": 100},
        {"type": "cut", "start_seconds": 30, "end_seconds": 40},
    ]
    assert build_keep_intervals(120, edits) == [(10.0, 30.0), (40.0, 100.0)]


def test_keep_intervals_rejects_unknown_edit():
    with pytest.raises(ValueError, match="Unsupported"):
        build_keep_intervals(100, [{"type": "fade"}])


def test_audio_filter_graph_applies_the_same_interior_cut_ranges():
    graph = _audio_filter_graph([(1, 2), (3, 4)])

    assert "[1:a]asplit=2[ain0][ain1]" in graph
    assert "[ain0]atrim=start=1:end=2" in graph
    assert "[ain1]atrim=start=3:end=4" in graph
    assert "[a0][a1]concat=n=2:v=0:a=1[a]" in graph


def test_source_fps_preserves_fractional_ffprobe_rate():
    assert source_fps({"r_frame_rate": "30000/1001"}, {}) == "30000/1001"


def test_render_segments_cover_edits_and_switch_speakers_without_overlap():
    timeline = Timeline.from_edits(
        10, [{"type": "cut", "start_seconds": 4, "end_seconds": 6}]
    )
    segments = [
        {"start": 0, "end": 2, "speaker": "A"},
        {"start": 2, "end": 8, "speaker": "B"},
        {"start": 8, "end": 10, "speaker": "A"},
    ]

    rendered = build_render_segments(timeline, segments)

    assert [segment["speaker"] for segment in rendered] == ["A", "B", "B", "A"]
    assert [
        (segment["source_start"], segment["source_end"]) for segment in rendered
    ] == [(0, 2.0), (2.0, 4.0), (6.0, 8.0), (8.0, 10)]
    assert sum(segment["duration"] for segment in rendered) == timeline.duration


def test_render_segments_fill_detection_gaps_with_wide_crop():
    timeline = Timeline.from_edits(5)

    rendered = build_render_segments(timeline, [{"start": 1, "end": 4, "speaker": "A"}])

    assert [segment["speaker"] for segment in rendered] == ["BOTH", "A", "BOTH"]
    assert sum(segment["duration"] for segment in rendered) == 5


def test_disk_estimate_uses_bitrate_cap_and_accounts_for_intermediates():
    encoding = {
        "video_max_bitrate_bps": 16_000_000,
        "audio_bitrate_bps": 192_000,
    }

    budget = render_space_budget(3600, encoding)

    assert budget["output_bytes"] == 7_650_720_000
    assert budget["scratch_bytes"] == 15_120_000_000


def test_space_status_combines_output_and_scratch_on_one_filesystem(tmp_path):
    output = tmp_path / "output"
    scratch = tmp_path / "scratch"
    output.mkdir()
    scratch.mkdir()

    with (
        patch("lib.delivery_video.OUTPUT_RESERVE_BYTES", 1),
        patch("lib.delivery_video.SCRATCH_RESERVE_BYTES", 10),
        patch("lib.delivery_video.render_scratch_root", return_value=scratch),
        patch(
            "lib.delivery_video.shutil.disk_usage",
            return_value=SimpleNamespace(free=15),
        ),
    ):
        status = render_space_status(output, {"output_bytes": 4, "scratch_bytes": 4})

    assert status["output"]["safe"] is True
    assert status["scratch"]["safe"] is True
    assert status["same_filesystem"] is True
    assert status["combined_required_bytes"] == 18
    assert status["safe"] is False
    assert status["failures"] == ["combined_peak_exceeds_free_space"]


def test_episode_render_config_preserves_source_color_without_explicit_opt_in(
    tmp_path,
):
    lut = tmp_path / "grade.cube"
    lut.write_text("LUT_3D_SIZE 2\n")
    config = {"processing": {"lut_path": str(lut)}}

    assert render_config_for_episode({}, config)["processing"]["lut_path"] == ""
    assert render_config_for_episode({"delivery_apply_lut": True}, config)[
        "processing"
    ]["lut_path"] == str(lut)
    assert config["processing"]["lut_path"] == str(lut)


def test_longform_fingerprint_tracks_caption_choice_and_lut_contents(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio.wav"
    transcript = tmp_path / "diarized_transcript.json"
    lut = tmp_path / "grade.cube"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    transcript.write_text('{"utterances": []}')
    lut.write_text("first")
    episode = {"delivery_apply_lut": True}
    config = {"processing": {"lut_path": str(lut)}}

    first = longform_render_fingerprint(tmp_path, episode, config, audio, [])
    config["processing"]["longform_burn_captions"] = True
    burned = longform_render_fingerprint(tmp_path, episode, config, audio, [])
    config["processing"]["video_max_bitrate"] = "12M"
    bounded = longform_render_fingerprint(tmp_path, episode, config, audio, [])
    lut.write_text("other")
    changed_lut = longform_render_fingerprint(tmp_path, episode, config, audio, [])

    assert len({first, burned, bounded, changed_lut}) == 4


def test_short_fingerprint_marks_only_sustained_two_person_overlap(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio.wav"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    episode = {"crop_config": {"speakers": [{}, {}]}}
    config = {"processing": {"shorts_hold_wide_seconds": 3}}
    clip = {"id": "clip_01", "start_seconds": 10, "end_seconds": 20}

    with patch(
        "lib.delivery_video.render_fingerprint",
        side_effect=lambda _paths, state: state,
    ):
        brief = short_render_fingerprint(
            tmp_path,
            episode,
            config,
            audio,
            [{"start": 11, "end": 13, "speaker": "BOTH"}],
            clip,
        )
        sustained = short_render_fingerprint(
            tmp_path,
            episode,
            config,
            audio,
            [{"start": 11, "end": 15, "speaker": "BOTH"}],
            clip,
        )

    assert "shorts_overlap_layout" not in brief
    assert sustained["shorts_overlap_layout"] == "two-person-stack/v1"


def test_short_fingerprint_marks_only_clips_with_overlapping_caption_events(tmp_path):
    (tmp_path / "source_merged.mp4").write_bytes(b"source")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    (tmp_path / "diarized_transcript.json").write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "speaker": 0,
                        "words": [{"word": "main", "start": 10.0, "end": 11.0}],
                    },
                    {
                        "speaker": 1,
                        "words": [{"word": "reply", "start": 10.7, "end": 11.1}],
                    },
                ]
            }
        )
    )

    with patch(
        "lib.delivery_video.render_fingerprint",
        side_effect=lambda _paths, state: state,
    ):
        affected = short_render_fingerprint(
            tmp_path,
            {},
            {},
            audio,
            [],
            {"id": "clip_01", "start_seconds": 10, "end_seconds": 12},
        )
        unaffected = short_render_fingerprint(
            tmp_path,
            {},
            {},
            audio,
            [],
            {"id": "clip_02", "start_seconds": 20, "end_seconds": 22},
        )

    assert affected["caption_timing"] == "single-lane/v1"
    assert "caption_timing" not in unaffected


def test_render_fingerprints_only_track_their_resolved_crop(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio.wav"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    episode = {
        "crop_config": {
            "speakers": [
                {
                    "center_x": 400,
                    "center_y": 500,
                    "zoom": 1.1,
                    "longform_center_x": 450,
                    "longform_center_y": 520,
                    "longform_zoom": 0.8,
                }
            ]
        }
    }
    clip = {"id": "clip_01", "start_seconds": 1, "end_seconds": 3}
    config = {"processing": {}}
    segments = [{"start": 0, "end": 4, "speaker": "speaker_0"}]
    old_long = longform_render_fingerprint(tmp_path, episode, config, audio, segments)
    old_short = short_render_fingerprint(
        tmp_path, episode, config, audio, segments, clip
    )

    longform_edit = json.loads(json.dumps(episode))
    longform_edit["crop_config"]["speakers"][0]["longform_center_y"] = 600
    assert (
        longform_render_fingerprint(tmp_path, longform_edit, config, audio, segments)
        != old_long
    )
    assert (
        short_render_fingerprint(tmp_path, longform_edit, config, audio, segments, clip)
        == old_short
    )

    short_edit = json.loads(json.dumps(episode))
    short_edit["crop_config"]["speakers"][0]["center_y"] = 650
    assert (
        longform_render_fingerprint(tmp_path, short_edit, config, audio, segments)
        == old_long
    )
    assert (
        short_render_fingerprint(tmp_path, short_edit, config, audio, segments, clip)
        != old_short
    )


def test_verified_legacy_short_migrates_after_longform_only_crop_edit(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio.wav"
    output = tmp_path / "shorts" / "clip_01.mp4"
    output.parent.mkdir()
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    output.write_bytes(b"render")
    old_episode = {
        "crop_config": {
            "speakers": [
                {
                    "center_x": 400,
                    "center_y": 500,
                    "zoom": 1.1,
                    "longform_center_x": 450,
                    "longform_center_y": 520,
                    "longform_zoom": 0.8,
                }
            ]
        }
    }
    new_episode = json.loads(json.dumps(old_episode))
    new_episode["crop_config"]["speakers"][0]["longform_center_y"] = 600
    clip = {
        "id": "clip_01",
        "start_seconds": 1,
        "end_seconds": 3,
        "status": "approved",
        "approved_render_fingerprint": "reviewed-old-fingerprint",
    }
    config = {"processing": {}}
    segments = [{"start": 0, "end": 4, "speaker": "speaker_0"}]
    legacy = _short_render_fingerprint(
        tmp_path,
        old_episode,
        config,
        audio,
        segments,
        clip,
        legacy_crop=True,
    )
    record_short_render(
        tmp_path,
        "clip_01",
        fingerprint=legacy,
        timeline=Timeline.from_edits(4).slice(1, 3),
        media={"duration_seconds": 2},
    )

    migrated = migrate_unchanged_short_crop_fingerprints(
        tmp_path,
        old_episode,
        new_episode,
        config,
        audio,
        segments,
        [clip],
    )

    assert migrated == ["clip_01"]
    assert current_short_render(tmp_path, new_episode, config, audio, segments, clip)
    manifest = json.loads((tmp_path / "render_manifest.json").read_text())
    record = manifest["shorts"]["clip_01"]
    assert record["fingerprint_migration"]["from"] == legacy
    assert clip["approved_render_fingerprint"] == "reviewed-old-fingerprint"


def test_transcript_reuse_preserves_only_unaffected_render_decisions(tmp_path):
    episode, config, audio, segments, clips, transcript = _transcript_reuse_inputs(
        tmp_path
    )
    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        proof = capture_transcript_render_reuse_proof(
            tmp_path, episode, config, audio, segments, clips
        )

    original_manifest = json.loads((tmp_path / "render_manifest.json").read_text())
    output_stats = {
        path: path.stat()
        for path in [
            tmp_path / "upload_video.mp4",
            tmp_path / "shorts" / "clip_a.mp4",
            tmp_path / "shorts" / "clip_b.mp4",
        ]
    }
    transcript["utterances"][0]["words"][0]["punctuated_word"] = "Corrected alpha"
    (tmp_path / "diarized_transcript.json").write_text(json.dumps(transcript))
    quantized_same_segments = [
        {"start": 0, "end": 10.002, "speaker": "speaker_0"},
        {"start": 10.002, "end": 30, "speaker": "speaker_1"},
    ]

    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        result = migrate_unchanged_transcript_render_fingerprints(
            tmp_path,
            episode,
            config,
            audio,
            quantized_same_segments,
            clips,
            proof,
        )

    assert result == {
        "version": TRANSCRIPT_RENDER_REUSE_VERSION,
        "manifest_updated": True,
        "longform_migrated": True,
        "shorts_migrated": ["clip_b"],
    }
    manifest = json.loads((tmp_path / "render_manifest.json").read_text())
    assert (
        manifest["shorts"]["clip_a"]["fingerprint"]
        == original_manifest["shorts"]["clip_a"]["fingerprint"]
    )
    for scope, record in (
        ("longform", manifest["longform"]),
        ("clip_b", manifest["shorts"]["clip_b"]),
    ):
        migration = record["fingerprint_migration"]
        assert migration["from"] != migration["to"] == record["fingerprint"]
        assert migration["decision"]
        assert scope in {"longform", "clip_b"}
    assert current_longform_render(
        tmp_path, episode, config, audio, quantized_same_segments
    )
    assert current_short_render(
        tmp_path, episode, config, audio, quantized_same_segments, clips[1]
    )
    assert (
        current_short_render(
            tmp_path, episode, config, audio, quantized_same_segments, clips[0]
        )
        is None
    )
    for path, before in output_stats.items():
        after = path.stat()
        assert (after.st_size, after.st_mtime_ns) == (
            before.st_size,
            before.st_mtime_ns,
        )


def test_transcript_reuse_rejects_changed_speaker_cut_and_stale_record(tmp_path):
    episode, config, audio, segments, clips, _ = _transcript_reuse_inputs(tmp_path)
    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        proof = capture_transcript_render_reuse_proof(
            tmp_path, episode, config, audio, segments, clips
        )
    changed = [{"start": 0, "end": 30, "speaker": "speaker_1"}]
    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        result = migrate_unchanged_transcript_render_fingerprints(
            tmp_path, episode, config, audio, changed, clips, proof
        )
    assert result["longform_migrated"] is False

    manifest_path = tmp_path / "render_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["longform"]["fingerprint"] = "already-stale"
    manifest_path.write_text(json.dumps(manifest))
    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        stale_proof = capture_transcript_render_reuse_proof(
            tmp_path, episode, config, audio, segments, []
        )
    assert stale_proof["renders"] == []


@pytest.mark.parametrize("changed_input", ["crop", "source", "audio", "encoding"])
def test_transcript_reuse_rejects_changed_static_input(tmp_path, changed_input):
    episode, config, audio, segments, clips, transcript = _transcript_reuse_inputs(
        tmp_path
    )
    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        proof = capture_transcript_render_reuse_proof(
            tmp_path, episode, config, audio, segments, clips
        )
    transcript["utterances"][0]["words"][0]["punctuated_word"] = "Corrected alpha"
    (tmp_path / "diarized_transcript.json").write_text(json.dumps(transcript))
    if changed_input == "crop":
        episode = {
            **episode,
            "crop_config": {"wide_zoom": 1.2, "speaker_l_center_x": 50},
        }
    elif changed_input == "source":
        (tmp_path / "source_merged.mp4").write_bytes(b"changed source")
    elif changed_input == "audio":
        audio.write_bytes(b"changed audio")
    else:
        config = {
            "processing": {
                "use_hardware_accel": False,
                "video_bitrate": "8M",
                "shorts_video_bitrate": "8M",
            }
        }

    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        result = migrate_unchanged_transcript_render_fingerprints(
            tmp_path, episode, config, audio, segments, clips, proof
        )
    assert result == {
        "version": TRANSCRIPT_RENDER_REUSE_VERSION,
        "manifest_updated": False,
        "longform_migrated": False,
        "shorts_migrated": [],
    }


def test_transcript_reuse_manifest_write_failure_propagates_without_partial_write(
    tmp_path,
):
    episode, config, audio, segments, clips, transcript = _transcript_reuse_inputs(
        tmp_path
    )
    with patch("lib.delivery_video.probe", return_value=_REUSE_PROBE):
        proof = capture_transcript_render_reuse_proof(
            tmp_path, episode, config, audio, segments, clips
        )
    transcript["utterances"][0]["words"][0]["punctuated_word"] = "Corrected alpha"
    (tmp_path / "diarized_transcript.json").write_text(json.dumps(transcript))
    manifest_path = tmp_path / "render_manifest.json"
    before = manifest_path.read_bytes()

    with (
        patch("lib.delivery_video.probe", return_value=_REUSE_PROBE),
        patch(
            "lib.delivery_video.atomic_write_json", side_effect=OSError("write failed")
        ),
        pytest.raises(OSError, match="write failed"),
    ):
        migrate_unchanged_transcript_render_fingerprints(
            tmp_path, episode, config, audio, segments, clips, proof
        )
    assert manifest_path.read_bytes() == before


def test_fingerprint_marks_missing_inputs_without_raising(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    available = render_fingerprint([source], {})

    source.unlink()

    assert render_fingerprint([source], {}) != available


def test_mux_uses_a_unique_atomic_temp_for_each_attempt(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    output = tmp_path / "result.mp4"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    destinations = []

    def runner(command, **_kwargs):
        destination = Path(command[-1])
        destinations.append(destination)
        destination.write_bytes(b"render")

    with (
        patch("lib.delivery_video.probe", return_value={"format": {"duration": "1"}}),
        patch(
            "lib.delivery_video.validate_av_output",
            return_value={"duration_seconds": 1},
        ),
    ):
        mux_timeline_audio(video, audio, output, Timeline.from_edits(1), runner=runner)
        mux_timeline_audio(video, audio, output, Timeline.from_edits(1), runner=runner)

    assert destinations[0] != destinations[1]
    assert all(destination.parent == tmp_path for destination in destinations)
    assert not any(destination.exists() for destination in destinations)


def test_mux_two_pass_normalizes_retained_timeline_and_records_final_proof(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    output = tmp_path / "result.mp4"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    commands = []
    analysis = (
        '{"input_i":"-19.0","input_lra":"4.0","input_tp":"-3.0",'
        '"input_thresh":"-29.0","target_offset":"0.0"}'
    )

    def runner(command, **_kwargs):
        commands.append(command)
        if command[-1] == "-":
            return SimpleNamespace(returncode=0, stderr=analysis)
        Path(command[-1]).write_bytes(b"mastered")
        return SimpleNamespace(returncode=0, stderr="")

    final = {
        "integrated_lufs": -16.0,
        "true_peak_dbfs": -1.4,
        "loudness_range_lu": 4.0,
    }
    with (
        patch("lib.delivery_video.probe", return_value={"format": {"duration": "5"}}),
        patch(
            "lib.delivery_video.validate_av_output",
            return_value={"duration_seconds": 3},
        ),
        patch("lib.delivery_video.measure_loudness", return_value=final),
    ):
        media = mux_timeline_audio(
            video,
            audio,
            output,
            Timeline(5, [(1, 2), (3, 5)]),
            loudness_policy=delivery_loudness_policy({}, "shorts"),
            runner=runner,
        )

    analysis_graph = commands[0][commands[0].index("-filter_complex") + 1]
    render_graph = commands[1][commands[1].index("-filter_complex") + 1]
    assert "atrim=start=1.0:end=2.0" in analysis_graph
    assert "print_format=json" in analysis_graph
    assert "measured_I=-19.0" in render_graph
    assert commands[1][commands[1].index("-map") + 3] == "[mastered]"
    assert media["audio_loudness"]["verification"]["safe"] is True
    assert media["audio_mastering"]["method"] == "ffmpeg-loudnorm-two-pass/v1"
    assert media["audio_timing"] == aac_content_timing_proof()
    assert output.read_bytes() == b"mastered"


def test_mux_loudness_failure_preserves_existing_output(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    output = tmp_path / "result.mp4"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    output.write_bytes(b"reviewed")
    analysis = (
        '{"input_i":"-19.0","input_lra":"4.0","input_tp":"-3.0",'
        '"input_thresh":"-29.0","target_offset":"0.0"}'
    )

    def runner(command, **_kwargs):
        if command[-1] == "-":
            return SimpleNamespace(returncode=0, stderr=analysis)
        Path(command[-1]).write_bytes(b"unsafe")
        return SimpleNamespace(returncode=0, stderr="")

    with (
        patch("lib.delivery_video.probe", return_value={"format": {"duration": "1"}}),
        patch(
            "lib.delivery_video.validate_av_output",
            return_value={"duration_seconds": 1},
        ),
        patch(
            "lib.delivery_video.measure_loudness",
            return_value={"integrated_lufs": -16, "true_peak_dbfs": 0.9},
        ),
        pytest.raises(RuntimeError, match="true peak 0.9"),
    ):
        mux_timeline_audio(
            video,
            audio,
            output,
            Timeline.from_edits(1),
            loudness_policy=delivery_loudness_policy({}, "longform"),
            runner=runner,
        )

    assert output.read_bytes() == b"reviewed"


def test_staged_render_output_preserves_reviewed_file_when_verification_fails(
    tmp_path,
):
    output = tmp_path / "upload_video.mp4"
    output.write_bytes(b"reviewed")

    with (
        pytest.raises(RuntimeError, match="verification failed"),
        staged_render_output(output) as staged,
    ):
        staged.write_bytes(b"unverified")
        raise RuntimeError("verification failed")

    assert output.read_bytes() == b"reviewed"
    assert not list(tmp_path.glob(".upload_video-*.mp4"))


def test_video_packet_signature_ignores_remux_timestamp_offsets():
    first = """#extradata 0, 39, aaa
0, 0, 0, 512, 10, one
0, 512, 512, 512, 20, two
"""
    shifted = """#extradata 0, 39, aaa
0, 840, 840, 512, 10, one
0, 1352, 1352, 512, 20, two
"""

    def signature(output):
        return video_packet_signature(
            Path("video.mp4"),
            runner=lambda *_args, **_kwargs: SimpleNamespace(stdout=output),
        )

    assert signature(first) == signature(shifted)
    assert signature(first)["packet_count"] == 2


def test_mux_rejects_video_packet_change_before_replacing_output(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    output = tmp_path / "result.mp4"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    output.write_bytes(b"reviewed")
    signatures = iter(
        [
            "0, 0, 0, 512, 10, original\n",
            "0, 0, 0, 512, 10, changed\n",
        ]
    )

    def runner(command, **_kwargs):
        if "framehash" in command:
            return SimpleNamespace(stdout=next(signatures))
        Path(command[-1]).write_bytes(b"remuxed")
        return SimpleNamespace(returncode=0, stderr="")

    with (
        patch("lib.delivery_video.probe", return_value={"format": {"duration": "1"}}),
        patch(
            "lib.delivery_video.validate_av_output",
            return_value={"duration_seconds": 1},
        ),
        pytest.raises(RuntimeError, match="changed or truncated"),
    ):
        mux_timeline_audio(
            video,
            audio,
            output,
            Timeline.from_edits(1),
            verify_video_copy=True,
            runner=runner,
        )

    assert output.read_bytes() == b"reviewed"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_mux_can_atomically_shorten_its_video_input_in_place(tmp_path):
    output = tmp_path / "upload_video.mp4"
    audio = tmp_path / "audio.wav"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-bf",
            "0",
            "-use_editlist",
            "0",
            "-c:a",
            "aac",
            output,
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=3",
            audio,
        ],
        check=True,
    )
    timeline = Timeline.from_edits(3, [{"type": "trim_end", "seconds": 2}]).quantize(
        "30/1"
    )

    media = mux_timeline_audio(output, audio, output, timeline)

    assert output.is_file()
    assert media["duration_seconds"] == pytest.approx(2, abs=0.1)
    assert media["audio_duration_seconds"] == pytest.approx(2, abs=0.1)
    assert media["video_duration_seconds"] == pytest.approx(2, abs=0.1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_mux_normalizes_aac_and_copies_video_packets(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30:duration=3",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            video,
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-af",
            "volume=0.08",
            "-c:a",
            "pcm_s24le",
            audio,
        ],
        check=True,
    )

    def video_hash(path):
        result = subprocess.run(
            [
                ffmpeg_executable(),
                "-v",
                "error",
                "-i",
                path,
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "md5",
                "-",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    before = video_hash(video)
    media = mux_timeline_audio(
        video,
        audio,
        output,
        Timeline.from_edits(3),
        loudness_policy=delivery_loudness_policy({}, "shorts"),
        verify_video_copy=True,
    )

    assert video_hash(output) == before
    assert media["video_copy_verification"]["status"] == "pass"
    assert (
        media["video_copy_verification"]["input"]
        == media["video_copy_verification"]["output"]
    )
    assert media["audio_loudness"]["integrated_lufs"] == pytest.approx(-16, abs=0.5)
    assert media["audio_loudness"]["true_peak_dbfs"] <= -1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_mux_preserves_fractional_frame_rate_video_tail(tmp_path):
    video = tmp_path / "fractional-video.mp4"
    audio = tmp_path / "audio.wav"
    output = tmp_path / "output.mp4"
    duration = 1.11
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=96x54:rate=30000/1001:duration={duration}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-bf",
            "0",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            video,
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:a",
            "pcm_s16le",
            audio,
        ],
        check=True,
    )

    before = video_packet_signature(video)
    media = mux_timeline_audio(
        video,
        audio,
        output,
        Timeline.from_edits(duration),
        verify_video_copy=True,
    )

    assert video_packet_signature(output) == before
    assert media["video_copy_verification"]["status"] == "pass"
    assert media["video_copy_verification"]["output"]["packet_count"] == 34


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_mux_records_native_aac_content_delay_across_an_edit(tmp_path):
    sample_rate = 48_000
    source_samples = np.random.default_rng(42).integers(
        -12_000, 12_001, size=2 * sample_rate, dtype=np.int16
    )
    audio = tmp_path / "source.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(source_samples.astype("<i2").tobytes())

    video = tmp_path / "video.mp4"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x54:rate=30:duration=1.3",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            video,
        ],
        check=True,
    )
    timeline = Timeline(2, [(0.2, 0.8), (1.2, 1.8)])
    rendered = tmp_path / "rendered.mp4"

    media = mux_timeline_audio(video, audio, rendered, timeline)
    decoded = np.frombuffer(
        subprocess.run(
            [
                ffmpeg_executable(),
                "-v",
                "error",
                "-i",
                rendered,
                "-map",
                "0:a:0",
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout,
        dtype="<f4",
    )
    expected = (
        np.concatenate(
            (
                source_samples[round(0.2 * sample_rate) : round(0.8 * sample_rate)],
                source_samples[round(1.2 * sample_rate) : round(1.8 * sample_rate)],
            )
        ).astype(np.float32)
        / 32768
    )

    comparison_length = expected.size - 2048
    reference = expected[:comparison_length]
    correlations = []
    for lag in range(900, 1151):
        observed = decoded[lag : lag + comparison_length]
        correlations.append(
            float(np.dot(reference, observed))
            / float(np.linalg.norm(reference) * np.linalg.norm(observed))
        )
    best_lag = 900 + int(np.argmax(correlations))

    assert best_lag == AAC_ENCODER_DELAY_SAMPLES
    assert correlations[best_lag - 900] > 0.9
    assert media["audio_timing"] == aac_content_timing_proof()


def test_manifest_updates_survive_multiple_writer_processes(tmp_path):
    (tmp_path / "shorts").mkdir()
    clip_ids = [f"clip_{index:02d}" for index in range(8)]
    with ProcessPoolExecutor(max_workers=4) as executor:
        assert sorted(
            executor.map(
                _record_short_in_process,
                [(str(tmp_path), clip_id) for clip_id in clip_ids],
            )
        ) == sorted(clip_ids)

    manifest = json.loads((tmp_path / "render_manifest.json").read_text())
    assert sorted(manifest["shorts"]) == clip_ids


def test_longform_manifest_rejects_changed_segments(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio_mix.wav"
    output = tmp_path / "upload_video.mp4"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    output.write_bytes(b"render")
    episode = {"crop_config": {"wide_zoom": 1}, "longform_edits": []}
    config = {"processing": {"video_crf": 22}}
    segments = [{"start": 0, "end": 5, "speaker": "A"}]
    fingerprint = longform_render_fingerprint(
        tmp_path, episode, config, audio, segments
    )
    timeline = Timeline.from_edits(5)

    record_longform_render(
        tmp_path,
        fingerprint=fingerprint,
        render_mode="speaker_cut",
        timeline=timeline,
        media={"duration_seconds": 5},
    )

    assert current_longform_render(tmp_path, episode, config, audio, segments)
    changed = [{"start": 0, "end": 5, "speaker": "B"}]
    assert current_longform_render(tmp_path, episode, config, audio, changed) is None


def test_terminal_trim_reuse_requires_explicit_current_proof_and_exact_inputs(tmp_path):
    source = tmp_path / "source_merged.mp4"
    transcript = tmp_path / "diarized_transcript.json"
    audio = tmp_path / "audio_mix.wav"
    output = tmp_path / "upload_video.mp4"
    source.write_bytes(b"source")
    transcript.write_text('{"utterances":[]}')
    audio.write_bytes(b"audio")
    output.write_bytes(b"render")
    episode = {
        "crop_config": {"wide_zoom": 1},
        "longform_edits": [
            {"type": "trim_start", "seconds": 1},
            {"type": "trim_end", "seconds": 9},
        ],
    }
    config = {"processing": {"video_crf": 22}}
    segments = [{"start": 0, "end": 10, "speaker": "A"}]
    timeline = Timeline.from_edits(10, episode["longform_edits"])
    fingerprint = longform_render_fingerprint(
        tmp_path, episode, config, audio, segments
    )
    record_longform_render(
        tmp_path,
        fingerprint=fingerprint,
        render_mode="speaker_cut",
        timeline=timeline,
        media={"duration_seconds": 8},
    )

    shortened = {
        **episode,
        "longform_edits": [
            {"type": "trim_start", "seconds": 1},
            {"type": "trim_end", "seconds": 8},
        ],
    }
    shortened_timeline = Timeline.from_edits(10, shortened["longform_edits"])
    assert (
        reusable_terminal_trim_render(
            tmp_path, shortened, config, audio, segments, shortened_timeline
        )
        is None
    )

    prepared = prepare_longform_trim_reuse(tmp_path, episode, config, audio, segments)
    assert prepared is not None
    proof = prepared["provenance"]["terminal_trim_reuse"]
    assert proof["input_fingerprint"] == longform_trim_reuse_fingerprint(
        tmp_path, episode, config, audio, segments
    )
    assert proof["source_intervals"] == [[1.0, 9.0]]
    assert reusable_terminal_trim_render(
        tmp_path, shortened, config, audio, segments, shortened_timeline
    )

    changed_crop = {
        **shortened,
        "crop_config": {"wide_zoom": 1.1},
    }
    assert (
        reusable_terminal_trim_render(
            tmp_path, changed_crop, config, audio, segments, shortened_timeline
        )
        is None
    )
    changed_middle = {
        **episode,
        "longform_edits": [
            {"type": "trim_start", "seconds": 1},
            {"type": "cut", "start_seconds": 4, "end_seconds": 5},
            {"type": "trim_end", "seconds": 8},
        ],
    }
    assert (
        reusable_terminal_trim_render(
            tmp_path,
            changed_middle,
            config,
            audio,
            segments,
            Timeline.from_edits(10, changed_middle["longform_edits"]),
        )
        is None
    )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_segmented_render_keeps_audio_video_crops_and_captions_on_one_timeline(
    tmp_path,
):
    source = tmp_path / "source.mp4"
    audio = tmp_path / "audio.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=160x180:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=160x180:rate=30:duration=6",
            "-filter_complex",
            "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ],
        check=True,
    )
    inputs = []
    for frequency in (440, 550, 660, 770, 880, 990):
        inputs.extend(
            ["-f", "lavfi", "-i", f"sine={frequency}:sample_rate=48000:duration=1"]
        )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            "[0:a][1:a][2:a][3:a][4:a][5:a]concat=n=6:v=0:a=1[a]",
            "-map",
            "[a]",
            "-c:a",
            "pcm_s24le",
            str(audio),
        ],
        check=True,
    )

    timeline = Timeline.from_edits(
        6,
        [
            {"type": "trim_start", "seconds": 1},
            {"type": "cut", "start_seconds": 3, "end_seconds": 4},
        ],
    )
    speakers = [
        {"start": 0, "end": 3, "speaker": "left"},
        {"start": 3, "end": 6, "speaker": "right"},
    ]
    render_segments = build_render_segments(timeline, speakers)
    captions = rebase_diarized(
        {
            "utterances": [
                {
                    "speaker": 0,
                    "words": [
                        {"word": "left", "start": 1.2, "end": 1.5},
                        {"word": "removed", "start": 3.2, "end": 3.5},
                        {"word": "right", "start": 4.2, "end": 4.5},
                    ],
                }
            ]
        },
        timeline,
    )
    segment_paths = []
    for index, segment in enumerate(render_segments):
        segment_timeline = Timeline(
            timeline.duration, [(segment["start"], segment["end"])]
        )
        ass = tmp_path / f"segment_{index}.ass"
        generate_ass_from_diarized(
            rebase_diarized(captions, segment_timeline),
            0,
            segment["duration"],
            ass,
            CaptionStyle(font_size=24, margin_v=20, play_res_x=160, play_res_y=180),
        )
        crop_x = 0 if segment["speaker"] == "left" else 160
        segment_path = tmp_path / f"segment_{index}.mp4"
        render_video_segment(
            source,
            segment_path,
            source_start=segment["source_start"],
            source_end=segment["source_end"],
            video_filter=(
                f"crop=160:180:{crop_x}:0,format=yuv420p,"
                f"subtitles='{escape_srt_path(ass)}'"
            ),
            encoder_args=["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20"],
            fps=30,
        )
        segment_paths.append(segment_path)

    video_only = tmp_path / "video_only.mp4"
    output = tmp_path / "output.mp4"
    concat_video_segments(segment_paths, video_only)
    media = mux_timeline_audio(video_only, audio, output, timeline)

    def pixel_at(timestamp):
        return np.frombuffer(
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(output),
                    "-vf",
                    "crop=16:16:8:8,scale=1:1",
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-",
                ],
                capture_output=True,
                check=True,
            ).stdout,
            dtype=np.uint8,
        )

    def frequency_at(timestamp):
        samples = np.frombuffer(
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(timestamp),
                    "-t",
                    "0.4",
                    "-i",
                    str(output),
                    "-vn",
                    "-f",
                    "f32le",
                    "-ac",
                    "1",
                    "-ar",
                    "8000",
                    "-",
                ],
                capture_output=True,
                check=True,
            ).stdout,
            dtype="<f4",
        )
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        frequencies = np.fft.rfftfreq(len(samples), 1 / 8000)
        return frequencies[int(np.argmax(spectrum))]

    assert pixel_at(0.5)[0] > pixel_at(0.5)[2]
    assert pixel_at(2.5)[2] > pixel_at(2.5)[0]
    assert frequency_at(0.25) == pytest.approx(550, abs=10)
    assert frequency_at(2.25) == pytest.approx(880, abs=10)
    assert "removed" not in "".join(path.read_text() for path in tmp_path.glob("*.ass"))
    assert media["duration_seconds"] == pytest.approx(4, abs=0.1)
    assert media["audio_duration_seconds"] == pytest.approx(
        media["video_duration_seconds"], abs=0.1
    )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_fractional_fps_hundred_cut_fixture_has_no_cumulative_av_drift(tmp_path):
    rate = "30000/1001"
    frame_duration = 1001 / 30000
    source_duration = 200 * frame_duration
    source = tmp_path / "fractional_source.mp4"
    audio = tmp_path / "fractional_audio.wav"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=green:size=64x36:rate={rate}:duration={source_duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=990:sample_rate=48000:duration={source_duration - 6}",
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "[a]",
            "-c:a",
            "pcm_s24le",
            str(audio),
        ],
        check=True,
    )

    cuts = [
        {
            "type": "cut",
            "start_seconds": (frame_index + 1) * frame_duration,
            "end_seconds": (frame_index + 2) * frame_duration,
        }
        for frame_index in range(0, 200, 2)
    ]
    timeline = Timeline.from_edits(source_duration, cuts).quantize(rate)
    render_segments = build_render_segments(
        timeline,
        [
            {
                "start": start,
                "end": end,
                "speaker": "A" if index % 2 == 0 else "B",
            }
            for index, (start, end) in enumerate(timeline.keep_intervals)
        ],
        frame_rate=rate,
    )
    assert len(render_segments) == 100

    paths = []
    for index, segment in enumerate(render_segments):
        path = tmp_path / f"fractional_{index:03d}.mp4"
        render_video_segment(
            source,
            path,
            source_start=segment["source_start"],
            source_end=segment["source_end"],
            video_filter="format=yuv420p",
            encoder_args=["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"],
            fps=rate,
        )
        paths.append(path)

    video_only = tmp_path / "fractional_video.mp4"
    output = tmp_path / "fractional_output.mp4"
    concat_video_segments(paths, video_only)
    media = mux_timeline_audio(video_only, audio, output, timeline)

    samples = np.frombuffer(
        subprocess.run(
            [
                ffmpeg_executable(),
                "-v",
                "error",
                "-ss",
                "3.1",
                "-t",
                "0.2",
                "-i",
                str(output),
                "-vn",
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout,
        dtype="<f4",
    )
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    frequencies = np.fft.rfftfreq(len(samples), 1 / 8000)
    dominant = frequencies[int(np.argmax(spectrum))]

    expected_duration = 100 * frame_duration
    assert timeline.duration == pytest.approx(expected_duration, abs=1e-9)
    assert media["video_duration_seconds"] == pytest.approx(expected_duration, abs=0.05)
    assert media["audio_duration_seconds"] == pytest.approx(
        media["video_duration_seconds"], abs=0.05
    )
    assert dominant == pytest.approx(990, abs=15)
