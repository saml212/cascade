"""Tests for LongformRenderAgent — crop filters, edit operations, segment splitting."""

import json
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from agents.longform_render import LongformRenderAgent
from lib.delivery_video import render_space_budget
from lib.encoding import get_video_encoding_policy


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


class TestApplyEdits:
    """Test _apply_edits with cut, trim_start, trim_end operations."""

    def _make_segments(self):
        return [
            {"start": 0.0, "end": 30.0, "speaker": "L", "duration": 30.0},
            {"start": 30.0, "end": 60.0, "speaker": "R", "duration": 30.0},
            {"start": 60.0, "end": 90.0, "speaker": "L", "duration": 30.0},
            {"start": 90.0, "end": 120.0, "speaker": "R", "duration": 30.0},
        ]

    def test_no_edits_returns_same(self, agent):
        segments = self._make_segments()
        result = agent._apply_edits(segments, [])
        assert len(result) == 4

    def test_trim_start_removes_before(self, agent):
        segments = self._make_segments()
        edits = [{"type": "trim_start", "seconds": 45.0}]
        result = agent._apply_edits(segments, edits)

        # First segment (0-30) is entirely before 45, removed
        # Second segment (30-60) starts before 45, trimmed to 45-60
        assert result[0]["start"] == 45.0
        assert result[0]["end"] == 60.0
        assert len(result) == 3

    def test_trim_end_removes_after(self, agent):
        segments = self._make_segments()
        edits = [{"type": "trim_end", "seconds": 75.0}]
        result = agent._apply_edits(segments, edits)

        # Last segment (90-120) is entirely after 75, removed
        # Third segment (60-90) ends after 75, trimmed to 60-75
        assert result[-1]["end"] == 75.0
        assert len(result) == 3

    def test_cut_removes_middle_segment(self, agent):
        segments = self._make_segments()
        edits = [{"type": "cut", "start_seconds": 30.0, "end_seconds": 60.0}]
        result = agent._apply_edits(segments, edits)

        # Segment 30-60 is entirely within the cut, removed
        assert len(result) == 3
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 30.0
        assert result[1]["start"] == 60.0

    def test_cut_splits_segment_in_middle(self, agent):
        """A cut in the middle of a segment should split it into two."""
        segments = [
            {"start": 0.0, "end": 100.0, "speaker": "L", "duration": 100.0},
        ]
        edits = [{"type": "cut", "start_seconds": 40.0, "end_seconds": 60.0}]
        result = agent._apply_edits(segments, edits)

        assert len(result) == 2
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 40.0
        assert result[0]["duration"] == 40.0
        assert result[1]["start"] == 60.0
        assert result[1]["end"] == 100.0
        assert result[1]["duration"] == 40.0

    def test_cut_trims_start_of_segment(self, agent):
        """Cut overlapping the start of a segment should trim the start."""
        segments = [
            {"start": 50.0, "end": 100.0, "speaker": "R", "duration": 50.0},
        ]
        edits = [{"type": "cut", "start_seconds": 40.0, "end_seconds": 70.0}]
        result = agent._apply_edits(segments, edits)

        assert len(result) == 1
        assert result[0]["start"] == 70.0
        assert result[0]["end"] == 100.0

    def test_cut_trims_end_of_segment(self, agent):
        """Cut overlapping the end of a segment should trim the end."""
        segments = [
            {"start": 0.0, "end": 50.0, "speaker": "L", "duration": 50.0},
        ]
        edits = [{"type": "cut", "start_seconds": 30.0, "end_seconds": 70.0}]
        result = agent._apply_edits(segments, edits)

        assert len(result) == 1
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 30.0

    def test_cut_entire_segment(self, agent):
        """A cut that fully contains a segment should remove it."""
        segments = [
            {"start": 10.0, "end": 20.0, "speaker": "L", "duration": 10.0},
        ]
        edits = [{"type": "cut", "start_seconds": 5.0, "end_seconds": 25.0}]
        result = agent._apply_edits(segments, edits)

        assert len(result) == 0

    def test_tiny_segment_filtering(self, agent):
        """Segments shorter than 0.1s after edits should be filtered out."""
        segments = [
            {"start": 0.0, "end": 100.0, "speaker": "L", "duration": 100.0},
        ]
        # Cut that leaves a tiny sliver at the start
        edits = [{"type": "cut", "start_seconds": 0.05, "end_seconds": 100.0}]
        result = agent._apply_edits(segments, edits)

        # The remaining segment is 0.0-0.05 (0.05s < 0.1s), should be filtered
        assert len(result) == 0

    def test_multiple_edits_applied_sequentially(self, agent):
        segments = self._make_segments()
        edits = [
            {"type": "trim_start", "seconds": 10.0},
            {"type": "trim_end", "seconds": 100.0},
        ]
        result = agent._apply_edits(segments, edits)

        # After trim_start at 10: first segment becomes 10-30
        # After trim_end at 100: last segment (90-120) becomes 90-100
        assert result[0]["start"] == 10.0
        assert result[-1]["end"] == 100.0

    def test_cut_outside_all_segments_no_change(self, agent):
        """A cut entirely outside all segments should leave them unchanged."""
        segments = self._make_segments()
        edits = [{"type": "cut", "start_seconds": 200.0, "end_seconds": 300.0}]
        result = agent._apply_edits(segments, edits)

        assert len(result) == 4

    def test_edit_preserves_speaker(self, agent):
        """Edits should preserve speaker assignment."""
        segments = [
            {"start": 0.0, "end": 100.0, "speaker": "R", "duration": 100.0},
        ]
        edits = [{"type": "trim_start", "seconds": 50.0}]
        result = agent._apply_edits(segments, edits)

        assert result[0]["speaker"] == "R"

    def test_zero_duration_segment_filtered(self, agent):
        """Segments with zero duration (start == end) should be filtered."""
        segments = [
            {"start": 10.0, "end": 10.0, "speaker": "L", "duration": 0.0},
            {"start": 10.0, "end": 50.0, "speaker": "R", "duration": 40.0},
        ]
        result = agent._apply_edits(segments, [])
        assert len(result) == 1
        assert result[0]["speaker"] == "R"

    def test_does_not_mutate_original(self, agent):
        """_apply_edits should not mutate the original segment list."""
        segments = self._make_segments()
        original_starts = [s["start"] for s in segments]
        edits = [{"type": "trim_start", "seconds": 50.0}]
        agent._apply_edits(segments, edits)

        # Original should be unchanged
        assert [s["start"] for s in segments] == original_starts


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
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
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
        patch("agents.longform_render.measure_loudness", return_value=None),
    ):
        result = agent.execute()

    assert "hello" in (tmp_episode_dir / "subtitles" / "longform.ass").read_text()
    assert (render_segments.call_args.args[3] is not None) is burn_captions
    assert result["captions_burned_in"] is burn_captions
    assert result["filename"] == "upload_video.mp4"
    assert [detail for _, detail in progress] == [
        "Joining rendered segments",
        "Muxing canonical audio",
        "Measuring output loudness",
        "Longform render complete",
    ]
    assert all(percent < 100 for percent, _ in progress[:-1])
    assert progress[-1][0] == 100
    budget = render_space_budget(2, get_video_encoding_policy(config, "longform"))
    render_space.assert_called_once_with(tmp_episode_dir, budget)
    assert scratch_space.call_args.args[1] == budget["scratch_bytes"]
