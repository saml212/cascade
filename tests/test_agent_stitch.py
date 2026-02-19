"""Tests for the stitch agent."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.stitch import StitchAgent


class TestStitchAgent:
    def _make_ingest_json(self, episode_dir, files):
        with open(episode_dir / "ingest.json", "w") as f:
            json.dump({"files": files}, f)

    def test_no_files_raises(self, tmp_episode_dir, sample_config):
        self._make_ingest_json(tmp_episode_dir, [])
        agent = StitchAgent(tmp_episode_dir, sample_config)
        with pytest.raises(ValueError, match="No files to stitch"):
            agent.execute()

    @patch("subprocess.run")
    @patch("shutil.copy2")
    @patch("agents.stitch.ffprobe")
    def test_single_file_copies(self, mock_probe, mock_copy, mock_run, tmp_episode_dir, sample_config):
        files = [{"dest_path": "/tmp/source/test.MP4", "duration_seconds": 120.0}]
        self._make_ingest_json(tmp_episode_dir, files)

        mock_probe.return_value = {"format": {"duration": "120.0"}, "streams": []}

        # Mock the frame extraction subprocess
        mock_run.return_value = MagicMock(returncode=0)

        agent = StitchAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        mock_copy.assert_called_once()
        assert result["input_count"] == 1

    @patch("subprocess.run")
    @patch("agents.stitch.ffprobe")
    def test_multi_file_uses_ffmpeg(self, mock_probe, mock_run, tmp_episode_dir, sample_config):
        files = [
            {"dest_path": "/tmp/source/a.MP4", "duration_seconds": 60.0},
            {"dest_path": "/tmp/source/b.MP4", "duration_seconds": 60.0},
        ]
        self._make_ingest_json(tmp_episode_dir, files)

        mock_probe.return_value = {"format": {"duration": "120.0"}, "streams": []}
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        agent = StitchAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        assert result["input_count"] == 2
        # Should have called ffmpeg for concat
        assert any("ffmpeg" in str(call) for call in mock_run.call_args_list)

    @patch("subprocess.run")
    @patch("agents.stitch.ffprobe")
    def test_duration_validation_warning(self, mock_probe, mock_run, tmp_episode_dir, sample_config):
        files = [
            {"dest_path": "/tmp/source/a.MP4", "duration_seconds": 60.0},
            {"dest_path": "/tmp/source/b.MP4", "duration_seconds": 60.0},
        ]
        self._make_ingest_json(tmp_episode_dir, files)

        # Return a significantly different duration
        mock_probe.return_value = {"format": {"duration": "100.0"}, "streams": []}
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        agent = StitchAgent(tmp_episode_dir, sample_config)
        # Should not raise, just warn
        result = agent.execute()
        assert result["duration_seconds"] == pytest.approx(100.0, abs=1)

    @patch("subprocess.run")
    @patch("shutil.copy2")
    @patch("agents.stitch.ffprobe")
    def test_result_structure(self, mock_probe, mock_copy, mock_run, tmp_episode_dir, sample_config):
        files = [{"dest_path": "/tmp/source/test.MP4", "duration_seconds": 120.0}]
        self._make_ingest_json(tmp_episode_dir, files)
        mock_probe.return_value = {"format": {"duration": "120.0"}, "streams": []}
        mock_run.return_value = MagicMock(returncode=0)

        agent = StitchAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        assert "output_path" in result
        assert "input_count" in result
        assert "duration_seconds" in result

    @patch("subprocess.run")
    @patch("agents.stitch.ffprobe")
    def test_concat_list_written(self, mock_probe, mock_run, tmp_episode_dir, sample_config):
        files = [
            {"dest_path": "/tmp/source/a.MP4", "duration_seconds": 60.0},
            {"dest_path": "/tmp/source/b.MP4", "duration_seconds": 60.0},
        ]
        self._make_ingest_json(tmp_episode_dir, files)
        mock_probe.return_value = {"format": {"duration": "120.0"}, "streams": []}
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        agent = StitchAgent(tmp_episode_dir, sample_config)
        agent.execute()

        concat_file = tmp_episode_dir / "work" / "concat_list.txt"
        assert concat_file.exists()
        content = concat_file.read_text()
        assert "a.MP4" in content
        assert "b.MP4" in content
