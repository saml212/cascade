"""Tests for the clip miner agent."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.clip_miner import ClipMinerAgent


class TestClipMinerAgent:
    def _setup_inputs(self, episode_dir):
        """Create required input files."""
        diarized = {
            "utterances": [
                {"speaker": 0, "start": 0.0, "end": 30.0, "text": "Welcome to the show", "words": []},
                {"speaker": 1, "start": 30.0, "end": 90.0, "text": "Thanks for having me, let me tell you about nuclear power", "words": []},
                {"speaker": 0, "start": 90.0, "end": 120.0, "text": "That's amazing!", "words": []},
            ]
        }
        segments = {
            "segments": [
                {"start": 0.0, "end": 30.0, "speaker": "L"},
                {"start": 30.0, "end": 90.0, "speaker": "R"},
                {"start": 90.0, "end": 120.0, "speaker": "L"},
            ]
        }
        stitch = {"duration_seconds": 120.0}
        episode = {"episode_id": "ep_test", "title": "", "status": "processing"}

        with open(episode_dir / "diarized_transcript.json", "w") as f:
            json.dump(diarized, f)
        with open(episode_dir / "segments.json", "w") as f:
            json.dump(segments, f)
        with open(episode_dir / "stitch.json", "w") as f:
            json.dump(stitch, f)
        with open(episode_dir / "episode.json", "w") as f:
            json.dump(episode, f)

    def test_format_transcript(self, tmp_episode_dir, sample_config):
        self._setup_inputs(tmp_episode_dir)
        agent = ClipMinerAgent(tmp_episode_dir, sample_config)
        diarized = agent.load_json("diarized_transcript.json")
        result = agent._format_transcript(diarized)

        assert "Speaker 0" in result
        assert "Speaker 1" in result
        assert "nuclear power" in result

    def test_get_dominant_speaker(self, tmp_episode_dir, sample_config):
        self._setup_inputs(tmp_episode_dir)
        agent = ClipMinerAgent(tmp_episode_dir, sample_config)
        segments = [
            {"start": 0.0, "end": 30.0, "speaker": "L"},
            {"start": 30.0, "end": 90.0, "speaker": "R"},
        ]

        # Clip mostly in R segment
        assert agent._get_dominant_speaker(40.0, 80.0, segments) == "R"
        # Clip mostly in L segment
        assert agent._get_dominant_speaker(0.0, 35.0, segments) == "L"
        # No overlap
        assert agent._get_dominant_speaker(100.0, 110.0, segments) == "BOTH"

    def test_snap_to_silence_no_rms(self, tmp_episode_dir, sample_config):
        self._setup_inputs(tmp_episode_dir)
        agent = ClipMinerAgent(tmp_episode_dir, sample_config)
        segments = json.loads((tmp_episode_dir / "segments.json").read_text())

        clips = [{"start_seconds": 30.0, "end_seconds": 90.0}]
        result = agent._snap_to_silence(clips, segments)
        # Without RMS data, should return unchanged
        assert result[0]["start_seconds"] == 30.0

    @patch("anthropic.Anthropic")
    def test_execute_integration(self, mock_anthropic_cls, tmp_episode_dir, sample_config, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        self._setup_inputs(tmp_episode_dir)

        # Mock Claude response (single combined call)
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        combined_response = MagicMock()
        combined_response.content = [MagicMock(text=json.dumps({
            "episode_info": {"guest_name": "John", "guest_title": "Engineer", "episode_title": "Test", "episode_description": "A test"},
            "clips": [
                {
                    "start_seconds": 30.0, "end_seconds": 90.0,
                    "title": "Nuclear Power", "hook_text": "Let me tell you",
                    "compelling_reason": "Great story", "virality_score": 8,
                }
            ]
        }))]

        mock_client.messages.create.return_value = combined_response

        agent = ClipMinerAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        assert result["clip_count"] == 1
        assert (tmp_episode_dir / "clips.json").exists()

    def test_clips_get_ids_and_ranks(self, tmp_episode_dir, sample_config, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        self._setup_inputs(tmp_episode_dir)

        mock_client = MagicMock()
        combined_response = MagicMock()
        combined_response.content = [MagicMock(text=json.dumps({
            "episode_info": {"guest_name": "", "guest_title": "", "episode_title": "", "episode_description": ""},
            "clips": [
                {"start_seconds": 30.0, "end_seconds": 60.0, "title": "A", "hook_text": "", "compelling_reason": "", "virality_score": 7},
                {"start_seconds": 60.0, "end_seconds": 90.0, "title": "B", "hook_text": "", "compelling_reason": "", "virality_score": 6},
            ]
        }))]
        mock_client.messages.create.return_value = combined_response

        with patch("anthropic.Anthropic", return_value=mock_client):
            agent = ClipMinerAgent(tmp_episode_dir, sample_config)
            result = agent.execute()

        clips = result["clips"]
        assert clips[0]["id"] == "clip_01"
        assert clips[0]["rank"] == 1
        assert clips[1]["id"] == "clip_02"
        assert clips[1]["rank"] == 2

    def test_markdown_code_block_parsing(self, tmp_episode_dir, sample_config, monkeypatch):
        """Test that Claude responses wrapped in markdown code blocks are parsed correctly."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        self._setup_inputs(tmp_episode_dir)

        mock_client = MagicMock()
        combined_response = MagicMock()
        combined_response.content = [MagicMock(text='```json\n' + json.dumps({
            "episode_info": {"guest_name": "", "guest_title": "", "episode_title": "", "episode_description": ""},
            "clips": [{"start_seconds": 30.0, "end_seconds": 60.0, "title": "A", "hook_text": "", "compelling_reason": "", "virality_score": 7}]
        }) + '\n```')]
        mock_client.messages.create.return_value = combined_response

        with patch("anthropic.Anthropic", return_value=mock_client):
            agent = ClipMinerAgent(tmp_episode_dir, sample_config)
            result = agent.execute()

        assert result["clip_count"] == 1
