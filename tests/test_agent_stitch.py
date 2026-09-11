"""Tests for the stitch agent."""

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.stitch import StitchAgent


class TestStitchAgent:
    def _make_ingest_json(self, episode_dir, files, **extra):
        with open(episode_dir / "ingest.json", "w") as f:
            json.dump({"files": files, **extra}, f)

    def test_no_files_raises(self, tmp_episode_dir, sample_config):
        self._make_ingest_json(tmp_episode_dir, [])
        agent = StitchAgent(tmp_episode_dir, sample_config)
        with pytest.raises(ValueError, match="No files to stitch"):
            agent.execute()

    @patch("subprocess.run")
    @patch("os.symlink")
    @patch("agents.stitch.ffprobe")
    def test_single_file_symlinks(
        self, mock_probe, mock_symlink, mock_run, tmp_episode_dir, sample_config
    ):
        files = [{"dest_path": "/tmp/source/test.MP4", "duration_seconds": 120.0}]
        self._make_ingest_json(tmp_episode_dir, files)

        mock_probe.return_value = {"format": {"duration": "120.0"}, "streams": []}

        # Mock the frame extraction subprocess
        mock_run.return_value = MagicMock(returncode=0)

        agent = StitchAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        mock_symlink.assert_called_once()
        assert result["input_count"] == 1

    @patch("subprocess.run")
    @patch("agents.stitch.ffprobe")
    def test_multi_file_uses_ffmpeg(
        self, mock_probe, mock_run, tmp_episode_dir, sample_config
    ):
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
    def test_duration_validation_warning(
        self, mock_probe, mock_run, tmp_episode_dir, sample_config
    ):
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
    @patch("os.symlink")
    @patch("agents.stitch.ffprobe")
    def test_result_structure(
        self, mock_probe, mock_symlink, mock_run, tmp_episode_dir, sample_config
    ):
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
    def test_concat_list_written(
        self, mock_probe, mock_run, tmp_episode_dir, sample_config
    ):
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

    @patch("subprocess.run")
    @patch("agents.stitch.ffprobe")
    def test_stitch_repairs_dji_sequence_order(
        self, mock_probe, mock_run, tmp_episode_dir, sample_config
    ):
        files = [
            {
                "filename": f"DJI_2026042912{i:02d}00_{sequence:04d}_D.MP4",
                "dest_path": f"/tmp/{sequence:04d}.MP4",
                "duration_seconds": 60,
            }
            for i, sequence in enumerate((6, 7, 9, 8))
        ]
        self._make_ingest_json(tmp_episode_dir, files)
        mock_probe.return_value = {"format": {"duration": "240"}, "streams": []}
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        StitchAgent(tmp_episode_dir, sample_config).execute()

        content = (tmp_episode_dir / "work" / "concat_list.txt").read_text()
        positions = [content.index(f"/{sequence:04d}.MP4") for sequence in (6, 7, 8, 9)]
        assert positions == sorted(positions)

    @patch("subprocess.run")
    @patch("agents.stitch.ffprobe")
    def test_stitch_preserves_authoritative_order(
        self, mock_probe, mock_run, tmp_episode_dir, sample_config
    ):
        files = [
            {
                "filename": f"DJI_20260429120000_{sequence:04d}_D.MP4",
                "dest_path": f"/tmp/{sequence:04d}.MP4",
                "duration_seconds": 60,
            }
            for sequence in (9, 8)
        ]
        self._make_ingest_json(tmp_episode_dir, files, source_order_authoritative=True)
        mock_probe.return_value = {"format": {"duration": "120"}, "streams": []}
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        StitchAgent(tmp_episode_dir, sample_config).execute()

        content = (tmp_episode_dir / "work" / "concat_list.txt").read_text()
        assert content.index("/0009.MP4") < content.index("/0008.MP4")
