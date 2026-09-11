"""Tests for the source-clock audio analysis agent."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from agents.audio_analysis import (
    ANALYSIS_SAMPLE_RATE,
    AudioAnalysisAgent,
    audio_analysis_fingerprint,
)
from lib.audio_mix import CAMERA_AUDIO_TIMELINE_FILTER


class TestAudioAnalysisAgent:
    def _setup_merged(self, episode_dir):
        """Create a dummy source_merged.mp4 (just needs to exist for path checks)."""
        (episode_dir / "source_merged.mp4").write_bytes(b"\x00")

    def test_mono_source(self, tmp_episode_dir, sample_config):
        self._setup_merged(tmp_episode_dir)

        mock_probe = {
            "format": {"duration": "60"},
            "streams": [
                {"codec_type": "audio", "channels": 1, "sample_rate": "48000"},
                {"codec_type": "video", "width": 1920, "height": 1080},
            ],
        }

        agent = AudioAnalysisAgent(tmp_episode_dir, sample_config)
        with patch("agents.audio_analysis.ffprobe", return_value=mock_probe):
            result = agent.execute()

        assert result["classification"] == "mono_source"
        assert result["audio_channels_identical"] is True
        assert result["channels"] == 1

    def test_identical_channels_detection(self, tmp_episode_dir, sample_config):
        self._setup_merged(tmp_episode_dir)

        mock_probe = {
            "format": {"duration": "60"},
            "streams": [
                {"codec_type": "audio", "channels": 2, "sample_rate": "48000"},
                {"codec_type": "video", "width": 1920, "height": 1080},
            ],
        }

        samples = np.random.randint(-32768, 32767, 48000, dtype=np.int16)

        agent = AudioAnalysisAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.audio_analysis.ffprobe", return_value=mock_probe),
            patch.object(
                agent,
                "_extract_channels",
                return_value=(samples.astype(np.float32), samples.astype(np.float32)),
            ),
        ):
            result = agent.execute()

        assert result["classification"] == "audio_channels_identical"
        assert result["audio_channels_identical"] == True
        assert abs(result["correlation"]) > 0.95

    def test_true_stereo_detection(self, tmp_episode_dir, sample_config):
        self._setup_merged(tmp_episode_dir)

        mock_probe = {
            "format": {"duration": "60"},
            "streams": [
                {"codec_type": "audio", "channels": 2, "sample_rate": "48000"},
                {"codec_type": "video", "width": 1920, "height": 1080},
            ],
        }

        # Create very different channel data
        rng = np.random.RandomState(42)
        left = rng.randint(-32768, 32767, 48000).astype(np.float32)
        right = rng.randint(-32768, 32767, 48000).astype(np.float32)

        agent = AudioAnalysisAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.audio_analysis.ffprobe", return_value=mock_probe),
            patch.object(agent, "_extract_channels", return_value=(left, right)),
        ):
            result = agent.execute()

        assert result["classification"] == "true_stereo"
        assert result["audio_channels_identical"] is False

    def test_no_audio_stream_raises(self, tmp_episode_dir, sample_config):
        self._setup_merged(tmp_episode_dir)

        mock_probe = {
            "format": {"duration": "60"},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
        }

        agent = AudioAnalysisAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.audio_analysis.ffprobe", return_value=mock_probe),
            pytest.raises(RuntimeError, match="No audio stream"),
        ):
            agent.execute()

    def test_result_structure(self, tmp_episode_dir, sample_config):
        self._setup_merged(tmp_episode_dir)

        mock_probe = {
            "format": {"duration": "60"},
            "streams": [
                {"codec_type": "audio", "channels": 1, "sample_rate": "48000"},
                {"codec_type": "video", "width": 1920, "height": 1080},
            ],
        }

        agent = AudioAnalysisAgent(tmp_episode_dir, sample_config)
        with patch("agents.audio_analysis.ffprobe", return_value=mock_probe):
            result = agent.execute()

        assert "channels" in result
        assert "sample_rate" in result
        assert "classification" in result
        assert "audio_channels_identical" in result
        assert "correlation" in result
        assert "rms_delta_db" in result

    def test_config_thresholds_respected(self, tmp_episode_dir, sample_config):
        """Test that custom config thresholds are used."""
        self._setup_merged(tmp_episode_dir)
        # Set very strict thresholds
        sample_config["processing"]["max_channel_correlation"] = 0.999
        sample_config["processing"]["max_channel_rms_ratio_delta"] = 0.1

        mock_probe = {
            "format": {"duration": "60"},
            "streams": [
                {"codec_type": "audio", "channels": 2, "sample_rate": "48000"},
                {"codec_type": "video", "width": 1920, "height": 1080},
            ],
        }

        # Moderately different data (enough to exceed strict thresholds)
        rng = np.random.RandomState(42)
        base = rng.randint(-32768, 32767, 48000).astype(np.float32)
        noise = rng.normal(0, 5000, 48000).astype(np.float32)
        left = base
        right = base + noise  # Correlated but with significant noise

        agent = AudioAnalysisAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.audio_analysis.ffprobe", return_value=mock_probe),
            patch.object(agent, "_extract_channels", return_value=(left, right)),
        ):
            result = agent.execute()

        # With strict thresholds, this should be classified as true_stereo
        assert result["classification"] == "true_stereo"

    def test_channel_decode_materializes_source_timeline_and_stays_compact(
        self, tmp_episode_dir, sample_config
    ):
        source = tmp_episode_dir / "source_merged.mp4"
        source.write_bytes(b"source")
        stereo = np.array([[1, 2], [3, 4], [5, 6]], dtype=np.int16)
        completed = MagicMock(stdout=stereo.tobytes())
        agent = AudioAnalysisAgent(tmp_episode_dir, sample_config)

        with patch(
            "agents.audio_analysis.subprocess.run", return_value=completed
        ) as run:
            left, right = agent._extract_channels(source)

        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-af") + 1] == CAMERA_AUDIO_TIMELINE_FILTER
        assert cmd[cmd.index("-ar") + 1] == str(ANALYSIS_SAMPLE_RATE)
        assert left.tolist() == [1, 3, 5]
        assert right.tolist() == [2, 4, 6]

    def test_cache_requires_current_source_and_sync_fingerprint(
        self, tmp_episode_dir, sample_config
    ):
        source = tmp_episode_dir / "source_merged.mp4"
        source.write_bytes(b"source")
        episode = {"audio_sync": {"offset_seconds": 1.0}}
        current = audio_analysis_fingerprint(tmp_episode_dir, episode, sample_config)
        work = tmp_episode_dir / "work"
        AudioAnalysisAgent._publish_channel_cache(
            work,
            current,
            np.ones(10, dtype=np.float32),
            np.ones(10, dtype=np.float32),
        )

        assert AudioAnalysisAgent._load_cached_channels(work, current) is not None
        changed = audio_analysis_fingerprint(
            tmp_episode_dir,
            {"audio_sync": {"offset_seconds": 2.0}},
            sample_config,
        )
        assert AudioAnalysisAgent._load_cached_channels(work, changed) is None
