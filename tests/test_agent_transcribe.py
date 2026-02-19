"""Tests for the transcribe agent."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.transcribe import TranscribeAgent


def _mock_deepgram_response():
    return {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "words": [
                                {"word": "Hello", "punctuated_word": "Hello", "start": 0.5, "end": 0.8, "confidence": 0.99, "speaker": 0},
                                {"word": "world", "punctuated_word": "world", "start": 0.9, "end": 1.2, "confidence": 0.98, "speaker": 0},
                                {"word": "this", "punctuated_word": "this", "start": 1.5, "end": 1.7, "confidence": 0.97, "speaker": 1},
                                {"word": "is", "punctuated_word": "is", "start": 1.8, "end": 1.9, "confidence": 0.99, "speaker": 1},
                                {"word": "a", "punctuated_word": "a", "start": 2.0, "end": 2.1, "confidence": 0.99, "speaker": 1},
                                {"word": "test", "punctuated_word": "test.", "start": 2.2, "end": 2.5, "confidence": 0.96, "speaker": 1},
                            ]
                        }
                    ]
                }
            ],
            "utterances": [
                {
                    "speaker": 0, "start": 0.5, "end": 1.2,
                    "transcript": "Hello world",
                    "confidence": 0.985,
                    "words": [
                        {"word": "Hello", "start": 0.5, "end": 0.8, "confidence": 0.99, "speaker": 0},
                        {"word": "world", "start": 0.9, "end": 1.2, "confidence": 0.98, "speaker": 0},
                    ],
                },
                {
                    "speaker": 1, "start": 1.5, "end": 2.5,
                    "transcript": "this is a test",
                    "confidence": 0.977,
                    "words": [
                        {"word": "this", "start": 1.5, "end": 1.7, "confidence": 0.97, "speaker": 1},
                        {"word": "is", "start": 1.8, "end": 1.9, "confidence": 0.99, "speaker": 1},
                        {"word": "a", "start": 2.0, "end": 2.1, "confidence": 0.99, "speaker": 1},
                        {"word": "test", "start": 2.2, "end": 2.5, "confidence": 0.96, "speaker": 1},
                    ],
                },
            ],
        },
    }


class TestTranscribeAgent:
    def test_build_diarized_transcript(self, tmp_episode_dir, sample_config):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        raw = _mock_deepgram_response()
        result = agent._build_diarized_transcript(raw)

        assert "utterances" in result
        assert len(result["utterances"]) == 2
        assert result["utterances"][0]["speaker"] == 0
        assert result["utterances"][0]["text"] == "Hello world"

    def test_diarized_word_timestamps(self, tmp_episode_dir, sample_config):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        raw = _mock_deepgram_response()
        result = agent._build_diarized_transcript(raw)

        words = result["utterances"][0]["words"]
        assert len(words) == 2
        assert words[0]["start"] == 0.5
        assert words[0]["end"] == 0.8

    def test_generate_srt(self, tmp_episode_dir, sample_config):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        raw = _mock_deepgram_response()
        agent._generate_srt(raw)

        srt_path = tmp_episode_dir / "subtitles" / "transcript.srt"
        assert srt_path.exists()
        content = srt_path.read_text()
        assert "Hello" in content
        assert "-->" in content

    def test_srt_empty_words(self, tmp_episode_dir, sample_config):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        raw = {"results": {"channels": [{"alternatives": [{"words": []}]}], "utterances": []}}
        agent._generate_srt(raw)

        srt_path = tmp_episode_dir / "subtitles" / "transcript.srt"
        assert srt_path.exists()
        assert srt_path.read_text() == ""

    def test_format_srt_time(self, tmp_episode_dir, sample_config):
        """Test the fmt_timecode function used by the transcribe agent."""
        from lib.srt import fmt_timecode
        assert fmt_timecode(0) == "00:00:00,000"
        assert fmt_timecode(61.5) == "00:01:01,500"
        assert fmt_timecode(3661.123) == "01:01:01,123"

    @patch("httpx.post")
    @patch("subprocess.run")
    def test_execute_calls_deepgram(self, mock_run, mock_post, tmp_episode_dir, sample_config, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")

        # Create source_merged.mp4
        (tmp_episode_dir / "source_merged.mp4").write_bytes(b"\x00" * 100)

        # Create pre-existing audio file to skip extraction
        work_dir = tmp_episode_dir / "work"
        audio_path = work_dir / "audio.m4a"
        audio_path.write_bytes(b"\x00" * 50)

        # Mock Deepgram response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _mock_deepgram_response()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        result = agent.execute()

        assert result["utterance_count"] == 2
        assert (tmp_episode_dir / "transcript.json").exists()
        assert (tmp_episode_dir / "diarized_transcript.json").exists()
