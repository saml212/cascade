"""Tests for LongformRenderAgent — crop filters, edit operations, segment splitting."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.longform_render import LongformRenderAgent


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
        assert "crop=960:" in result_l   # 3840 / (2*2.0)
        assert "crop=1280:" in result_r  # 3840 / (2*1.5)

    def test_both_no_zoom_passthrough(self, agent, crop_config):
        result = agent._get_crop_filter("BOTH", 3840, 2160, crop_config)
        assert result == "scale=1920:1080"

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
        assert "crop=1280:" in spk   # 3840 / (2*1.5)

    def test_out_of_range_speaker_index(self, agent, crop_config):
        result = agent._get_crop_filter("speaker_5", 3840, 2160, crop_config)
        assert "crop=" in result  # still produces a crop (centered)

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
