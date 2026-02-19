"""Tests for the ingest agent."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.ingest import IngestAgent


def _mock_ffprobe(duration=100.0, creation_time="2026-01-01T10:00:00Z"):
    """Return a mock ffprobe result dict."""
    return {
        "format": {
            "duration": str(duration),
            "tags": {"creation_time": creation_time},
        },
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080},
            {"codec_type": "audio", "channels": 2, "sample_rate": "48000"},
        ],
    }


class TestIngestAgent:
    def test_no_source_path_raises(self, tmp_episode_dir, sample_config):
        agent = IngestAgent(tmp_episode_dir, sample_config)
        with pytest.raises(ValueError, match="source_path not set"):
            agent.execute()

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_single_file_ingest(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=120.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)
        result = agent.execute()

        assert result["file_count"] == 1
        assert result["total_duration_seconds"] == pytest.approx(120.0, abs=1)

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_directory_ingest_filters_resource_forks(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_dir = tmp_path / "DCIM"
        source_dir.mkdir()
        (source_dir / "MVI_0001.MP4").write_bytes(b"\x00" * 100)
        (source_dir / "MVI_0002.MP4").write_bytes(b"\x00" * 100)
        (source_dir / "._MVI_0001.MP4").write_bytes(b"\x00" * 50)  # Resource fork

        mock_probe.return_value = _mock_ffprobe(duration=60.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_dir)
        result = agent.execute()

        # Should only have 2 files (resource fork filtered)
        assert result["file_count"] == 2

    @patch("agents.ingest.ffprobe")
    def test_empty_directory_raises(self, mock_probe, tmp_episode_dir, sample_config, tmp_path):
        source_dir = tmp_path / "empty"
        source_dir.mkdir()

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_dir)

        with pytest.raises(FileNotFoundError, match="No MP4 files"):
            agent.execute()

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_duration_validation_mismatch(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)

        # Return different durations on first and second call
        mock_probe.side_effect = [
            _mock_ffprobe(duration=120.0),  # Source probe
            _mock_ffprobe(duration=50.0),   # Copy validation probe (big mismatch)
        ]

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)

        with pytest.raises(RuntimeError, match="Duration mismatch"):
            agent.execute()

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_files_sorted_by_creation_time(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_dir = tmp_path / "DCIM"
        source_dir.mkdir()
        (source_dir / "MVI_0002.MP4").write_bytes(b"\x00" * 100)
        (source_dir / "MVI_0001.MP4").write_bytes(b"\x00" * 100)

        # Return different creation times based on which file is probed
        def side_effect(path):
            if "0001" in str(path):
                return _mock_ffprobe(duration=60.0, creation_time="2026-01-01T10:00:00Z")
            else:
                return _mock_ffprobe(duration=60.0, creation_time="2026-01-01T11:00:00Z")

        mock_probe.side_effect = side_effect

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_dir)
        result = agent.execute()

        # First file should be the one with earlier creation time
        assert "0001" in result["files"][0]["filename"]

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_result_structure(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)
        mock_probe.return_value = _mock_ffprobe(duration=120.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)
        result = agent.execute()

        assert "files" in result
        assert "file_count" in result
        assert "total_duration_seconds" in result
        assert "total_size_bytes" in result

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_dest_path_set(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)
        mock_probe.return_value = _mock_ffprobe(duration=120.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)
        result = agent.execute()

        assert result["files"][0]["dest_path"] is not None
        assert result["files"][0]["copy_validated"] is True
