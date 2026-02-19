"""Tests for the speaker cut agent."""

import json
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import patch

from agents.speaker_cut import SpeakerCutAgent


class TestSpeakerCutAgent:
    def _setup_identical_channels(self, episode_dir):
        """Set up audio_analysis.json indicating identical channels."""
        with open(episode_dir / "audio_analysis.json", "w") as f:
            json.dump({
                "audio_channels_identical": True,
                "channels": 2,
                "sample_rate": 48000,
            }, f)
        with open(episode_dir / "stitch.json", "w") as f:
            json.dump({"duration_seconds": 60.0}, f)

    def _setup_stereo(self, episode_dir, sample_rate=48000):
        """Set up audio_analysis.json indicating true stereo."""
        with open(episode_dir / "audio_analysis.json", "w") as f:
            json.dump({
                "audio_channels_identical": False,
                "channels": 2,
                "sample_rate": sample_rate,
            }, f)
        with open(episode_dir / "stitch.json", "w") as f:
            json.dump({"duration_seconds": 10.0}, f)

    def test_identical_channels_single_both_segment(self, tmp_episode_dir, sample_config):
        self._setup_identical_channels(tmp_episode_dir)
        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        assert result["segment_count"] == 1
        assert result["segments"][0]["speaker"] == "BOTH"
        assert result["channels_identical"] is True

    def test_identical_channels_segment_spans_full_duration(self, tmp_episode_dir, sample_config):
        self._setup_identical_channels(tmp_episode_dir)
        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        seg = result["segments"][0]
        assert seg["start"] == 0.0
        assert seg["end"] == 60.0

    def test_stereo_produces_lr_segments(self, tmp_episode_dir, sample_config):
        self._setup_stereo(tmp_episode_dir, sample_rate=1000)

        # Create WAV data: first half loud left, second half loud right
        n_samples = 10000  # 10s at 1000 Hz
        half = n_samples // 2
        left = np.zeros(n_samples, dtype=np.float32)
        right = np.zeros(n_samples, dtype=np.float32)
        left[:half] = np.random.RandomState(1).normal(0, 10000, half).astype(np.float32)
        right[half:] = np.random.RandomState(2).normal(0, 10000, half).astype(np.float32)

        call_count = [0]
        def mock_load(path):
            call_count[0] += 1
            return left if call_count[0] == 1 else right

        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        with patch.object(agent, "_load_wav", side_effect=mock_load):
            result = agent.execute()

        assert result["channels_identical"] is False
        speakers = set(s["speaker"] for s in result["segments"])
        # Should have at least L and R segments
        assert len(speakers) >= 2

    def test_segments_have_required_fields(self, tmp_episode_dir, sample_config):
        self._setup_identical_channels(tmp_episode_dir)
        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        for seg in result["segments"]:
            assert "start" in seg
            assert "end" in seg
            assert "speaker" in seg
            assert "duration" in seg

    def test_segments_json_saved(self, tmp_episode_dir, sample_config):
        self._setup_identical_channels(tmp_episode_dir)
        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        agent.execute()

        assert (tmp_episode_dir / "segments.json").exists()

    def test_absorb_short_segments(self, tmp_episode_dir, sample_config):
        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        segments = [
            {"start": 0.0, "end": 5.0, "speaker": "L"},
            {"start": 5.0, "end": 5.3, "speaker": "R"},  # Too short (< 0.8s)
            {"start": 5.3, "end": 10.0, "speaker": "L"},
        ]
        result = agent._absorb_short_segments(segments, 0.8)
        # Short segment should be absorbed
        assert len(result) < 3

    def test_absorb_keeps_long_segments(self, tmp_episode_dir, sample_config):
        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        segments = [
            {"start": 0.0, "end": 5.0, "speaker": "L"},
            {"start": 5.0, "end": 10.0, "speaker": "R"},
            {"start": 10.0, "end": 15.0, "speaker": "L"},
        ]
        result = agent._absorb_short_segments(segments, 0.8)
        assert len(result) == 3

    def test_absorb_single_segment(self, tmp_episode_dir, sample_config):
        agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
        segments = [{"start": 0.0, "end": 5.0, "speaker": "L"}]
        result = agent._absorb_short_segments(segments, 0.8)
        assert len(result) == 1
