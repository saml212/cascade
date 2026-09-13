"""Tests for LongformRenderAgent — crop filters, edit operations, segment splitting."""

import json
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from agents.longform_render import LongformRenderAgent, repair_longform_audio
from lib.delivery_video import render_space_budget
from lib.encoding import get_video_encoding_policy
from lib.timeline import Timeline


@pytest.fixture
def crop_config():
    """Standard 2-speaker crop config with both legacy and N-speaker fields."""
    return {
        "source_width": 3840,
        "source_height": 2160,
        "speaker_l_center_x": 960,
        "speaker_l_center_y": 1080,
        "speaker_r_center_x": 2880,
        "speaker_r_center_y": 1080,
        "speaker_l_zoom": 1.0,
        "speaker_r_zoom": 1.0,
        "zoom": 1.0,
        "speakers": [
            {"label": "Speaker 0", "center_x": 960, "center_y": 1080, "zoom": 1.0},
            {"label": "Speaker 1", "center_x": 2880, "center_y": 1080, "zoom": 1.0},
        ],
    }


@pytest.fixture
def agent(tmp_episode_dir, sample_config):
    return LongformRenderAgent(tmp_episode_dir, sample_config)


class TestGetCropFilter:
    """Test _get_crop_filter produces correct ffmpeg filter strings."""

    def test_speaker_l_zoom_1(self, agent, crop_config):
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        assert "crop=1920:" in result  # 3840 / (2*1.0)
        assert "scale=1920:1080" in result

    def test_speaker_zoom_2(self, agent, crop_config):
        crop_config["speakers"][0]["zoom"] = 2.0
        crop_config["speaker_l_zoom"] = 2.0
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        assert "crop=960:" in result  # 3840 / (2*2.0)

    def test_per_speaker_zoom(self, agent, crop_config):
        crop_config["speakers"][0]["zoom"] = 2.0
        crop_config["speakers"][1]["zoom"] = 1.5
        result_l = agent._get_crop_filter("speaker_0", 3840, 2160, crop_config)
        result_r = agent._get_crop_filter("speaker_1", 3840, 2160, crop_config)
        assert "crop=960:" in result_l  # 3840 / (2*2.0)
        assert "crop=1280:" in result_r  # 3840 / (2*1.5)

    def test_both_no_zoom_passthrough(self, agent, crop_config):
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result.startswith("scale=1920:1080")
        assert "lanczos" in result

    def test_both_with_wide_zoom(self, agent, crop_config):
        crop_config["wide_zoom"] = 1.2
        crop_config["wide_center_x"] = 1920
        crop_config["wide_center_y"] = 1080
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert "crop=3200:" in result  # 3840 / 1.2 (wide formula)

    def test_wide_is_2x_speaker_at_same_zoom(self, agent, crop_config):
        crop_config["wide_zoom"] = 1.5
        crop_config["wide_center_x"] = 1920
        crop_config["wide_center_y"] = 1080
        crop_config["speakers"][0]["zoom"] = 1.5
        wide = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        spk = agent._get_crop_filter("speaker_0", 3840, 2160, crop_config)
        assert "crop=2560:" in wide  # 3840 / 1.5
        assert "crop=1280:" in spk  # 3840 / (2*1.5)

    def test_out_of_range_speaker_index(self, agent, crop_config):
        with pytest.raises(ValueError, match="Speaker index 5"):
            agent._get_crop_filter("speaker_5", 3840, 2160, crop_config)

    def test_1080p_source(self, agent):
        config = {
            "speakers": [
                {"label": "Speaker 0", "center_x": 480, "center_y": 540, "zoom": 1.0},
            ],
        }
        result = agent._get_crop_filter("speaker_0", 1920, 1080, config)
        assert "crop=960:" in result  # 1920 / (2*1.0)
        assert "scale=1920:1080" in result


def test_longform_uses_configured_1080p_ceiling(agent):
    agent.config["processing"]["output_resolution"] = "1920x1080"

    assert agent._output_dimensions(3840, 2160) == (1920, 1080)
    assert agent._output_dimensions(1280, 720) == (1280, 720)


def test_segment_progress_reserves_completion_for_post_render_phases(
    agent, tmp_episode_dir, crop_config
):
    segments = [
        {"speaker": "speaker_0", "start": 0.0, "end": 1.0, "duration": 1.0},
        {"speaker": "speaker_1", "start": 1.0, "end": 2.0, "duration": 1.0},
    ]
    with (
        patch.object(agent, "_render_segment"),
        patch.object(agent, "report_progress") as report,
    ):
        agent._render_segments(
            tmp_episode_dir / "source.mp4",
            tmp_episode_dir,
            segments,
            None,
            None,
            3840,
            2160,
            crop_config,
            ["-c:v", "libx264"],
            "",
            "30/1",
            1920,
            1080,
        )

    assert [call.args[:2] for call in report.call_args_list] == [(1, 5), (2, 5)]


@pytest.mark.parametrize("burn_captions", [False, True])
def test_longform_writes_sidecar_and_only_burns_captions_when_enabled(
    tmp_episode_dir, sample_config, burn_captions
):
    episode = {
        "crop_config": {"speakers": [{"center_x": 80, "center_y": 45, "zoom": 1}]},
        "longform_edits": [],
        "delivery_apply_lut": False,
    }
    (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
    (tmp_episode_dir / "segments.json").write_text(
        json.dumps({"segments": [{"start": 0, "end": 2, "speaker": "speaker_0"}]})
    )
    (tmp_episode_dir / "diarized_transcript.json").write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "speaker": 0,
                        "words": [{"word": "hello", "start": 0.2, "end": 0.6}],
                    }
                ]
            }
        )
    )
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    output = tmp_episode_dir / "upload_video.mp4"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    output.write_bytes(b"reviewed")
    scratch = tmp_episode_dir / "scratch"
    scratch.mkdir()
    config = json.loads(json.dumps(sample_config))
    config["processing"]["longform_burn_captions"] = burn_captions
    progress = []
    agent = LongformRenderAgent(
        tmp_episode_dir,
        config,
        progress=lambda percent, detail: progress.append((percent, detail)),
    )
    source_probe = {
        "format": {"duration": "2"},
        "streams": [
            {
                "codec_type": "video",
                "width": 160,
                "height": 90,
                "r_frame_rate": "30/1",
            }
        ],
    }

    def fake_concat(_paths, destination, **_kwargs):
        destination.write_bytes(b"video")

    def fake_mux(_video, _audio, destination, _timeline, **_kwargs):
        destination.write_bytes(b"muxed")
        return {
            "duration_seconds": 2,
            "audio_duration_seconds": 2,
            "video_duration_seconds": 2,
            "width": 160,
            "height": 90,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_loudness": {
                "integrated_lufs": -16.0,
                "true_peak_dbfs": -1.5,
            },
        }

    with (
        patch(
            "agents.longform_render.current_speaker_segments",
            return_value={"segments": [{"start": 0, "end": 2, "speaker": "speaker_0"}]},
        ),
        patch(
            "agents.longform_render.current_diarized_transcript",
            return_value={
                "utterances": [
                    {
                        "speaker": 0,
                        "words": [{"word": "hello", "start": 0.2, "end": 0.6}],
                    }
                ]
            },
        ),
        patch("agents.longform_render.generate_audio_mix", return_value=audio),
        patch("agents.longform_render.ffprobe", return_value=source_probe),
        patch(
            "agents.longform_render.get_video_encoder_args",
            return_value=["-c:v", "libx264"],
        ),
        patch("agents.longform_render.require_render_space") as render_space,
        patch(
            "agents.longform_render.render_scratch_dir",
            return_value=nullcontext(scratch),
        ) as scratch_space,
        patch.object(
            agent,
            "_render_segments",
            return_value=[scratch / "segment.mp4"],
        ) as render_segments,
        patch("agents.longform_render.concat_video_segments", side_effect=fake_concat),
        patch("agents.longform_render.mux_timeline_audio", side_effect=fake_mux),
    ):
        result = agent.execute()

    assert "hello" in (tmp_episode_dir / "subtitles" / "longform.ass").read_text()
    assert (render_segments.call_args.args[3] is not None) is burn_captions
    assert result["captions_burned_in"] is burn_captions
    assert result["filename"] == "upload_video.mp4"
    assert output.read_bytes() == b"muxed"
    assert [detail for _, detail in progress] == [
        "Joining rendered segments",
        "Muxing canonical audio",
        "Output audio verified",
        "Longform render complete",
    ]
    assert all(percent < 100 for percent, _ in progress[:-1])
    assert progress[-1][0] == 100
    budget = render_space_budget(2, get_video_encoding_policy(config, "longform"))
    render_space.assert_called_once_with(tmp_episode_dir, budget)
    assert scratch_space.call_args.args[1] == budget["scratch_bytes"]


def test_longform_reuses_only_verified_terminal_prefix_without_rendering_segments(
    tmp_episode_dir, sample_config
):
    episode = {
        "crop_config": {"speakers": [{"center_x": 80, "center_y": 45, "zoom": 1}]},
        "longform_edits": [{"type": "trim_end", "seconds": 2}],
        "delivery_apply_lut": False,
    }
    (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
    (tmp_episode_dir / "diarized_transcript.json").write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "speaker": 0,
                        "words": [{"word": "hello", "start": 0.2, "end": 0.6}],
                    }
                ]
            }
        )
    )
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    output = tmp_episode_dir / "upload_video.mp4"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    output.write_bytes(b"verified prior render")
    segments = [{"start": 0, "end": 3, "speaker": "speaker_0"}]
    progress = []
    agent = LongformRenderAgent(
        tmp_episode_dir,
        sample_config,
        progress=lambda percent, detail: progress.append((percent, detail)),
    )
    source_probe = {
        "format": {"duration": "3"},
        "streams": [
            {
                "codec_type": "video",
                "width": 160,
                "height": 90,
                "r_frame_rate": "30/1",
            }
        ],
    }
    prior = {
        "fingerprint": "verified-prior-fingerprint",
        "output": {"size_bytes": output.stat().st_size},
    }

    def fake_mux(video, _audio, destination, timeline, **_kwargs):
        assert video == output
        assert destination != output
        assert timeline.keep_intervals == ((0.0, 2.0),)
        destination.write_bytes(b"shortened mux")
        return {
            "duration_seconds": 2,
            "audio_duration_seconds": 2,
            "video_duration_seconds": 2,
            "width": 160,
            "height": 90,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_loudness": {
                "integrated_lufs": -16.0,
                "true_peak_dbfs": -1.5,
            },
        }

    with (
        patch(
            "agents.longform_render.current_speaker_segments",
            return_value={"segments": segments},
        ),
        patch(
            "agents.longform_render.current_diarized_transcript",
            return_value={
                "utterances": [
                    {
                        "speaker": 0,
                        "words": [{"word": "hello", "start": 0.2, "end": 0.6}],
                    }
                ]
            },
        ),
        patch("agents.longform_render.generate_audio_mix", return_value=audio),
        patch("agents.longform_render.ffprobe", return_value=source_probe),
        patch("agents.longform_render.current_longform_render", return_value=None),
        patch(
            "agents.longform_render.reusable_terminal_trim_render",
            return_value=prior,
        ),
        patch(
            "agents.longform_render.get_video_encoder_args",
            return_value=["-c:v", "libx264"],
        ),
        patch("agents.longform_render.require_render_space") as render_space,
        patch.object(agent, "_render_segments") as render_segments,
        patch("agents.longform_render.mux_timeline_audio", side_effect=fake_mux),
    ):
        result = agent.execute()

    render_segments.assert_not_called()
    render_space.assert_called_once_with(
        tmp_episode_dir,
        {"output_bytes": len(b"verified prior render"), "scratch_bytes": 0},
    )
    assert result["reused"] is True
    assert output.read_bytes() == b"shortened mux"
    assert result["manifest"]["provenance"]["video_reuse"] == {
        "mode": "verified_terminal_prefix",
        "source_render_fingerprint": "verified-prior-fingerprint",
        "source_output_size_bytes": len(b"verified prior render"),
    }
    assert [detail for _, detail in progress] == [
        "Reusing verified video pixels and muxing canonical audio",
        "Output audio verified",
        "Recording verified render manifest",
        "Longform render complete",
    ]
    assert all(percent < 100 for percent, _ in progress[:-1])


def test_terminal_prefix_verification_failure_preserves_reviewed_output(
    agent, tmp_episode_dir
):
    output = tmp_episode_dir / "upload_video.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    caption = tmp_episode_dir / "subtitles" / "longform.ass"
    output.write_bytes(b"reviewed master")
    audio.write_bytes(b"audio")
    caption.write_text("captions")

    def fake_mux(_video, _audio, destination, _timeline, **_kwargs):
        destination.write_bytes(b"unverified replacement")
        raise RuntimeError("verification failed")

    with (
        patch("agents.longform_render.require_render_space"),
        patch("agents.longform_render.mux_timeline_audio", side_effect=fake_mux),
        pytest.raises(RuntimeError, match="verification failed"),
    ):
        agent._reuse_terminal_prefix(
            {"longform_edits": [{"type": "trim_end", "seconds": 2}]},
            audio,
            [{"start": 0, "end": 3, "speaker": "speaker_0"}],
            Timeline.from_edits(3, [{"type": "trim_end", "seconds": 2}]),
            "new-fingerprint",
            caption,
            {"path": "subtitles/longform.ass", "burned_in": False},
            [{"start": 0, "end": 2, "speaker": "speaker_0"}],
            {"audio_bitrate": "192k"},
            ["-c:v", "libx264"],
            {
                "fingerprint": "old-fingerprint",
                "output": {"size_bytes": len(b"reviewed master")},
            },
        )

    assert output.read_bytes() == b"reviewed master"
    assert not list(tmp_episode_dir.glob(".upload_video-trim-reuse-*.mp4"))


@pytest.mark.parametrize("record_failure", [False, True])
def test_audio_repair_is_atomic_with_manifest_record(
    tmp_episode_dir, sample_config, record_failure
):
    episode = {
        "crop_config": {"speakers": [{"center_x": 80, "center_y": 45, "zoom": 1}]},
        "longform_edits": [],
        "delivery_apply_lut": False,
    }
    (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
    (tmp_episode_dir / "diarized_transcript.json").write_text(
        json.dumps({"utterances": []})
    )
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    output = tmp_episode_dir / "upload_video.mp4"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    output.write_bytes(b"unsafe audio, verified video")
    manifest_path = tmp_episode_dir / "render_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "clock": "source",
                "shorts": {},
                "longform": {"fingerprint": "prior-record"},
            }
        )
    )
    original_output = output.read_bytes()
    original_manifest = manifest_path.read_bytes()
    current = {
        "fingerprint": "current-fingerprint",
        "render_mode": "speaker_cut",
        "keep_intervals": [[0.0, 2.0]],
        "captions": {"path": "subtitles/longform.ass", "burned_in": False},
        "provenance": {"color_grade": "source"},
        "output": {
            "size_bytes": output.stat().st_size,
            "audio_loudness": {
                "integrated_lufs": -16.4,
                "true_peak_dbfs": 0.9,
            },
        },
    }
    source_probe = {
        "format": {"duration": "2"},
        "streams": [
            {
                "codec_type": "video",
                "width": 160,
                "height": 90,
                "r_frame_rate": "30/1",
            }
        ],
    }

    def fake_mux(video, _audio, destination, timeline, **kwargs):
        assert video == output
        assert destination != output
        assert timeline.keep_intervals == ((0.0, 2.0),)
        assert kwargs["loudness_policy"]["target_lufs"] == -16
        assert kwargs["verify_video_copy"] is True
        destination.write_bytes(b"same video packets, repaired audio")
        return {
            "duration_seconds": 2,
            "audio_duration_seconds": 2,
            "video_duration_seconds": 2,
            "width": 160,
            "height": 90,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_loudness": {
                "integrated_lufs": -16.0,
                "true_peak_dbfs": -1.4,
            },
            "video_copy_verification": {
                "status": "pass",
                "input": {"sha256": "same", "packet_count": 60},
                "output": {"sha256": "same", "packet_count": 60},
            },
        }

    record = (
        patch(
            "agents.longform_render.record_longform_render",
            side_effect=RuntimeError("manifest write failed"),
        )
        if record_failure
        else nullcontext()
    )
    with (
        patch(
            "agents.longform_render.current_speaker_segments",
            return_value={"segments": [{"start": 0, "end": 2, "speaker": "speaker_0"}]},
        ),
        patch(
            "agents.longform_render.current_diarized_transcript",
            return_value={"utterances": []},
        ),
        patch("agents.longform_render.generate_audio_mix", return_value=audio),
        patch("agents.longform_render.ffprobe", return_value=source_probe),
        patch(
            "agents.longform_render.longform_render_fingerprint",
            return_value="current-fingerprint",
        ),
        patch("agents.longform_render.current_longform_render", return_value=current),
        patch("agents.longform_render.require_render_space"),
        patch("agents.longform_render.mux_timeline_audio", side_effect=fake_mux),
        record,
    ):
        if record_failure:
            with pytest.raises(RuntimeError, match="manifest write failed"):
                repair_longform_audio(tmp_episode_dir, sample_config)
        else:
            result = repair_longform_audio(tmp_episode_dir, sample_config)

    if record_failure:
        assert output.read_bytes() == original_output
        assert manifest_path.read_bytes() == original_manifest
        return

    assert output.read_bytes() == b"same video packets, repaired audio"
    assert result["audio_repaired"] is True
    assert result["video_reencoded"] is False
    assert result["render_fingerprint"] == "current-fingerprint"
    assert (
        result["manifest"]["provenance"]["audio_remaster"]["video_reencoded"] is False
    )
    assert (
        result["manifest"]["provenance"]["audio_remaster"]["video_copy_verification"][
            "status"
        ]
        == "pass"
    )


def test_audio_repair_refuses_to_render_pixels_when_longform_is_not_current(
    tmp_episode_dir, sample_config
):
    episode = {
        "crop_config": {"speakers": [{"center_x": 80, "center_y": 45, "zoom": 1}]},
        "longform_edits": [],
        "delivery_apply_lut": False,
    }
    (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
    (tmp_episode_dir / "diarized_transcript.json").write_text('{"utterances": []}')
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    output = tmp_episode_dir / "upload_video.mp4"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    output.write_bytes(b"previous")
    source_probe = {
        "format": {"duration": "2"},
        "streams": [
            {
                "codec_type": "video",
                "width": 160,
                "height": 90,
                "r_frame_rate": "30/1",
            }
        ],
    }

    with (
        patch(
            "agents.longform_render.current_speaker_segments",
            return_value={"segments": [{"start": 0, "end": 2, "speaker": "speaker_0"}]},
        ),
        patch(
            "agents.longform_render.current_diarized_transcript",
            return_value={"utterances": []},
        ),
        patch("agents.longform_render.generate_audio_mix", return_value=audio),
        patch("agents.longform_render.ffprobe", return_value=source_probe),
        patch("agents.longform_render.current_longform_render", return_value=None),
        patch.object(LongformRenderAgent, "_render_segments") as render_segments,
        pytest.raises(RuntimeError, match="current manifest-backed"),
    ):
        repair_longform_audio(tmp_episode_dir, sample_config)

    render_segments.assert_not_called()
    assert output.read_bytes() == b"previous"
