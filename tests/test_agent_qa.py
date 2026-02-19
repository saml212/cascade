"""Tests for the QA agent."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.qa import QAAgent


class TestQAAgent:
    def _setup_full_episode(self, episode_dir, sample_clips):
        """Create all files needed for a passing QA run."""
        # source_merged.mp4
        (episode_dir / "source_merged.mp4").write_bytes(b"\x00" * 100)

        # longform.mp4
        (episode_dir / "longform.mp4").write_bytes(b"\x00" * 100)

        # clips.json
        with open(episode_dir / "clips.json", "w") as f:
            json.dump({"clips": sample_clips}, f)

        # shorts
        for clip in sample_clips:
            (episode_dir / "shorts" / "{}.mp4".format(clip["id"])).write_bytes(b"\x00")

        # SRT
        (episode_dir / "subtitles" / "transcript.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n")

        # Metadata
        metadata = {
            "longform": {"title": "Test"},
            "clips": [{"id": c["id"]} for c in sample_clips],
            "schedule": [{"clip_id": "clip_01", "platform": "youtube"}],
        }
        with open(episode_dir / "metadata" / "metadata.json", "w") as f:
            json.dump(metadata, f)

    def test_all_checks_pass(self, tmp_episode_dir, sample_config, sample_clips):
        self._setup_full_episode(tmp_episode_dir, sample_clips)

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "duration": "3600.0"},
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with patch("agents.qa.ffprobe", return_value=mock_probe):
            result = agent.execute()

        assert result["overall"] == "pass"
        assert result["hard_checks_passed"] == result["hard_checks_total"]

    def test_missing_source_merged(self, tmp_episode_dir, sample_config, sample_clips):
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        (tmp_episode_dir / "source_merged.mp4").unlink()

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "duration": "3600.0"},
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with patch("agents.qa.ffprobe", return_value=mock_probe):
            result = agent.execute()

        assert result["overall"] == "fail"
        failed = [c for c in result["checks"] if not c["pass"]]
        assert any("source_merged" in c["name"] for c in failed)

    def test_missing_shorts_detected(self, tmp_episode_dir, sample_config, sample_clips):
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        # Remove one short
        (tmp_episode_dir / "shorts" / "clip_02.mp4").unlink()

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "duration": "3600.0"},
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with patch("agents.qa.ffprobe", return_value=mock_probe):
            result = agent.execute()

        assert result["overall"] == "fail"
        shorts_check = next(c for c in result["checks"] if c["name"] == "all_shorts_rendered")
        assert not shorts_check["pass"]
        assert "clip_02" in shorts_check["detail"]

    def test_duration_warnings(self, tmp_episode_dir, sample_config):
        """Clips outside configured duration range produce warnings."""
        clips = [
            {"id": "clip_01", "start_seconds": 0, "end_seconds": 10, "duration": 10.0, "status": "pending"},
        ]
        self._setup_full_episode(tmp_episode_dir, clips)

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "duration": "3600.0"},
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with patch("agents.qa.ffprobe", return_value=mock_probe):
            result = agent.execute()

        assert result["warning_count"] > 0

    def test_qa_json_saved(self, tmp_episode_dir, sample_config, sample_clips):
        self._setup_full_episode(tmp_episode_dir, sample_clips)

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "duration": "3600.0"},
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with patch("agents.qa.ffprobe", return_value=mock_probe):
            agent.execute()

        assert (tmp_episode_dir / "qa" / "qa.json").exists()
