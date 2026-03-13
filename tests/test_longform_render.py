"""Tests for LongformRenderAgent — crop filters, edit operations, segment splitting."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.longform_render import LongformRenderAgent


@pytest.fixture
def crop_config():
    """Standard 2-speaker crop config."""
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
    }


@pytest.fixture
def agent(tmp_episode_dir, sample_config):
    return LongformRenderAgent(tmp_episode_dir, sample_config)


class TestGetCropFilter:
    """Test _get_crop_filter for various speakers and zoom levels."""

    def test_speaker_l_default_zoom(self, agent, crop_config):
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        assert result.startswith("crop=")
        assert "scale=1920:1080" in result

    def test_speaker_r_default_zoom(self, agent, crop_config):
        result = agent._get_crop_filter("R", 3840, 2160, crop_config)
        assert result.startswith("crop=")
        assert "scale=1920:1080" in result

    def test_both_speaker_no_zoom_passthrough(self, agent, crop_config):
        """BOTH with zoom <= 1.0 should return simple scale (no crop)."""
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result == "scale=1920:1080"

    def test_both_speaker_with_wide_zoom(self, agent, crop_config):
        """BOTH with wide_zoom > 1.0 should apply a crop."""
        crop_config["wide_zoom"] = 1.5
        crop_config["wide_center_x"] = 1920
        crop_config["wide_center_y"] = 1080
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result.startswith("crop=")
        assert "scale=1920:1080" in result

    def test_both_uses_frame_center_when_no_wide_config(self, agent, crop_config):
        """Without wide_center config, should use frame center."""
        crop_config["wide_zoom"] = 1.5
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result.startswith("crop=")

    def test_zoom_2x_halves_crop_width(self, agent, crop_config):
        """zoom=2.0 should produce a crop width of src_w / (2 * 2.0) = src_w/4."""
        crop_config["speaker_l_zoom"] = 2.0
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        # crop_w = 3840 / (2 * 2.0) = 960
        assert "crop=960:" in result

    def test_zoom_1x_gives_half_frame_width(self, agent, crop_config):
        """zoom=1.0 should produce crop_w = src_w / 2."""
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        # crop_w = 3840 / (2 * 1.0) = 1920
        assert "crop=1920:" in result

    def test_crop_clamped_to_frame_bounds_left_edge(self, agent, crop_config):
        """Crop centered at x=0 should be clamped to x=0."""
        crop_config["speaker_l_center_x"] = 0
        crop_config["speaker_l_center_y"] = 1080
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        parts = result.split("crop=")[1].split(",scale=")[0]
        values = parts.split(":")
        x = int(values[2])
        assert x >= 0

    def test_crop_clamped_to_frame_bounds_right_edge(self, agent, crop_config):
        """Crop centered near right edge should not exceed frame."""
        crop_config["speaker_r_center_x"] = 3840
        result = agent._get_crop_filter("R", 3840, 2160, crop_config)
        parts = result.split("crop=")[1].split(",scale=")[0]
        values = parts.split(":")
        crop_w = int(values[0])
        x = int(values[2])
        assert x + crop_w <= 3840

    def test_crop_clamped_to_frame_bounds_top(self, agent, crop_config):
        """Crop centered near top should not go negative."""
        crop_config["speaker_l_center_y"] = 0
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        parts = result.split("crop=")[1].split(",scale=")[0]
        values = parts.split(":")
        y = int(values[3])
        assert y >= 0

    def test_crop_clamped_to_frame_bounds_bottom(self, agent, crop_config):
        """Crop centered near bottom should not exceed frame."""
        crop_config["speaker_l_center_y"] = 2160
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        parts = result.split("crop=")[1].split(",scale=")[0]
        values = parts.split(":")
        crop_h = int(values[1])
        y = int(values[3])
        assert y + crop_h <= 2160

    def test_minimum_crop_dimensions(self, agent, crop_config):
        """Even with extreme zoom, crop should have minimum dimensions."""
        crop_config["speaker_l_zoom"] = 100.0
        result = agent._get_crop_filter("L", 3840, 2160, crop_config)
        parts = result.split("crop=")[1].split(",scale=")[0]
        values = parts.split(":")
        crop_w = int(values[0])
        crop_h = int(values[1])
        assert crop_w >= 64
        assert crop_h >= 36

    def test_per_speaker_zoom_override(self, agent, crop_config):
        """Per-speaker zoom should override global zoom."""
        crop_config["zoom"] = 1.0
        crop_config["speaker_l_zoom"] = 2.0
        crop_config["speaker_r_zoom"] = 1.5
        result_l = agent._get_crop_filter("L", 3840, 2160, crop_config)
        result_r = agent._get_crop_filter("R", 3840, 2160, crop_config)
        # L should have crop_w = 3840 / (2*2.0) = 960
        assert "crop=960:" in result_l
        # R should have crop_w = 3840 / (2*1.5) = 1280
        assert "crop=1280:" in result_r

    def test_1080p_source(self, agent, crop_config):
        """Test with standard 1080p source."""
        config = {
            "speaker_l_center_x": 480,
            "speaker_l_center_y": 540,
            "speaker_r_center_x": 1440,
            "speaker_r_center_y": 540,
            "zoom": 1.0,
        }
        result = agent._get_crop_filter("L", 1920, 1080, config)
        # crop_w = 1920 / 2 = 960
        assert "crop=960:" in result
        assert "scale=1920:1080" in result


class TestWideShot:
    """Test wide shot crop config usage."""

    def test_wide_shot_with_configured_center(self, agent, crop_config):
        crop_config["wide_center_x"] = 1920
        crop_config["wide_center_y"] = 1080
        crop_config["wide_zoom"] = 1.2
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result.startswith("crop=")
        assert "scale=1920:1080" in result

    def test_wide_shot_defaults_to_center(self, agent, crop_config):
        """Without wide_center config, should use frame center."""
        crop_config["wide_zoom"] = 1.5
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result.startswith("crop=")

    def test_wide_zoom_exactly_1_returns_passthrough(self, agent, crop_config):
        crop_config["wide_zoom"] = 1.0
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result == "scale=1920:1080"


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
