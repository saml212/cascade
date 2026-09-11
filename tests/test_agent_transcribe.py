"""Tests for the transcribe agent — multichannel and mono fallback modes."""

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.transcribe import (
    CAMERA_AUDIO_CACHE_VERSION,
    TranscribeAgent,
    remap_transcript_timestamps,
)

# -- Fixtures ----------------------------------------------------------------

MONO_RESPONSE = {
    "results": {
        "channels": [
            {
                "alternatives": [
                    {
                        "words": [
                            {
                                "word": "Hello",
                                "punctuated_word": "Hello",
                                "start": 0.5,
                                "end": 0.8,
                                "confidence": 0.99,
                                "speaker": 0,
                            },
                            {
                                "word": "world",
                                "punctuated_word": "world",
                                "start": 0.9,
                                "end": 1.2,
                                "confidence": 0.98,
                                "speaker": 0,
                            },
                            {
                                "word": "test",
                                "punctuated_word": "test.",
                                "start": 1.5,
                                "end": 1.8,
                                "confidence": 0.97,
                                "speaker": 1,
                            },
                        ]
                    }
                ]
            }
        ],
        "utterances": [
            {
                "speaker": 0,
                "start": 0.5,
                "end": 1.2,
                "transcript": "Hello world",
                "confidence": 0.985,
                "words": [
                    {
                        "word": "Hello",
                        "start": 0.5,
                        "end": 0.8,
                        "confidence": 0.99,
                        "speaker": 0,
                    },
                    {
                        "word": "world",
                        "start": 0.9,
                        "end": 1.2,
                        "confidence": 0.98,
                        "speaker": 0,
                    },
                ],
            },
            {
                "speaker": 1,
                "start": 1.5,
                "end": 1.8,
                "transcript": "test",
                "confidence": 0.97,
                "words": [
                    {
                        "word": "test",
                        "start": 1.5,
                        "end": 1.8,
                        "confidence": 0.97,
                        "speaker": 1,
                    }
                ],
            },
        ],
    },
}

MC_RESPONSE = {
    "results": {
        "channels": [
            {
                "alternatives": [
                    {
                        "words": [
                            {
                                "word": "Welcome",
                                "punctuated_word": "Welcome",
                                "start": 0.5,
                                "end": 0.8,
                            }
                        ]
                    }
                ]
            },
            {
                "alternatives": [
                    {
                        "words": [
                            {
                                "word": "Thanks",
                                "punctuated_word": "Thanks",
                                "start": 2.0,
                                "end": 2.3,
                            }
                        ]
                    }
                ]
            },
            {
                "alternatives": [
                    {
                        "words": [
                            {
                                "word": "Yeah",
                                "punctuated_word": "Yeah,",
                                "start": 3.5,
                                "end": 3.7,
                            }
                        ]
                    }
                ]
            },
        ],
        "utterances": [
            {
                "channel": 0,
                "start": 0.5,
                "end": 0.8,
                "transcript": "Welcome",
                "confidence": 0.99,
                "words": [
                    {"word": "Welcome", "start": 0.5, "end": 0.8, "confidence": 0.99}
                ],
            },
            {
                "channel": 1,
                "start": 2.0,
                "end": 2.3,
                "transcript": "Thanks",
                "confidence": 0.97,
                "words": [
                    {"word": "Thanks", "start": 2.0, "end": 2.3, "confidence": 0.97}
                ],
            },
            {
                "channel": 2,
                "start": 3.5,
                "end": 3.7,
                "transcript": "Yeah,",
                "confidence": 0.96,
                "words": [
                    {"word": "Yeah", "start": 3.5, "end": 3.7, "confidence": 0.96}
                ],
            },
        ],
    },
}

MC_CHANNEL_MAP = [
    {"index": 0, "label": "Speaker 0", "track": 1},
    {"index": 1, "label": "Speaker 1", "track": 2},
    {"index": 2, "label": "Speaker 2", "track": 4},
]

# -- Tests -------------------------------------------------------------------


class TestBuildDiarizedTranscript:
    def test_piecewise_timestamp_remap_updates_nested_timing_and_metadata(self):
        raw = {
            "metadata": {"duration": 20.0},
            "results": {
                "utterances": [
                    {
                        "start": 9.0,
                        "end": 16.0,
                        "words": [
                            {"start": 9.5, "end": 10.0},
                            {"start": 15.0, "end": 16.0},
                        ],
                    }
                ]
            },
        }
        mapped = remap_transcript_timestamps(raw, [(10.0, 2.0), (15.0, 3.0)])

        assert mapped["results"]["utterances"][0]["start"] == 9.0
        assert mapped["results"]["utterances"][0]["end"] == 19.0
        assert mapped["results"]["utterances"][0]["words"][0]["end"] == 12.0
        assert mapped["results"]["utterances"][0]["words"][1]["start"] == 18.0
        assert mapped["metadata"]["duration"] == 23.0
        assert raw["metadata"]["duration"] == 20.0

    def test_mono_mode(self, tmp_episode_dir, sample_config):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        result = agent._build_diarized_transcript(MONO_RESPONSE)

        assert result["mode"] == "diarized"
        assert "speaker_map" not in result
        assert len(result["utterances"]) == 2
        assert result["utterances"][0]["speaker"] == 0
        assert result["utterances"][0]["text"] == "Hello world"
        assert result["utterances"][0]["words"][0]["start"] == 0.5
        assert result["utterances"][1]["speaker"] == 1

    def test_multichannel_mode(self, tmp_episode_dir, sample_config):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        result = agent._build_diarized_transcript(
            MC_RESPONSE, multichannel=True, channel_map=MC_CHANNEL_MAP
        )

        assert result["mode"] == "multichannel"
        assert result["speaker_map"] == MC_CHANNEL_MAP
        assert len(result["utterances"]) == 3
        assert [u["speaker"] for u in result["utterances"]] == [0, 1, 2]
        assert result["utterances"][2]["text"] == "Yeah,"
        # Words inherit utterance speaker
        assert all(w["speaker"] == 1 for w in result["utterances"][1]["words"])


@pytest.mark.parametrize(
    "multichannel,raw,expected_words",
    [
        (False, MONO_RESPONSE, ["Hello", "world", "test"]),
        (True, MC_RESPONSE, ["Welcome", "Thanks", "Yeah"]),
    ],
)
class TestGenerateSrt:
    def test_srt_content(
        self, multichannel, raw, expected_words, tmp_episode_dir, sample_config
    ):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        agent._generate_srt(raw, multichannel=multichannel)
        content = (tmp_episode_dir / "subtitles" / "transcript.srt").read_text()
        assert "-->" in content
        for word in expected_words:
            assert word in content


class TestGenerateSrtEmpty:
    def test_empty_produces_empty(self, tmp_episode_dir, sample_config):
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        agent._generate_srt(
            {"results": {"channels": [{"alternatives": [{"words": []}]}]}}
        )
        assert (tmp_episode_dir / "subtitles" / "transcript.srt").read_text() == ""


class TestExecute:
    @patch("agents.transcribe.httpx.post")
    @patch("agents.transcribe.subprocess.run")
    def test_camera_extraction_materializes_timestamp_gaps(
        self, run, post, tmp_episode_dir, sample_config, monkeypatch
    ):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
        (tmp_episode_dir / "source_merged.mp4").write_bytes(b"source")
        (tmp_episode_dir / "episode.json").write_text(
            json.dumps({"episode_id": "test", "duration_seconds": 60})
        )

        def create_audio(cmd, **kwargs):
            (tmp_episode_dir / "work" / "audio.m4a").write_bytes(b"aac")
            return MagicMock(returncode=0)

        run.side_effect = create_audio
        response = MagicMock()
        response.json.return_value = MONO_RESPONSE
        post.return_value = response

        TranscribeAgent(tmp_episode_dir, sample_config).execute()

        cmd = run.call_args.args[0]
        assert (
            cmd[cmd.index("-af") + 1]
            == "aresample=async=1000:min_hard_comp=0.001:first_pts=0"
        )
        assert (
            tmp_episode_dir / "work" / f"audio.m4a.{CAMERA_AUDIO_CACHE_VERSION}"
        ).exists()

    @patch("httpx.post")
    def test_mono_fallback(
        self, mock_post, tmp_episode_dir, sample_config, monkeypatch
    ):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
        (tmp_episode_dir / "source_merged.mp4").write_bytes(b"\x00" * 100)
        (tmp_episode_dir / "work" / "audio.m4a").write_bytes(b"\x00" * 50)
        (
            tmp_episode_dir / "work" / f"audio.m4a.{CAMERA_AUDIO_CACHE_VERSION}"
        ).write_text(CAMERA_AUDIO_CACHE_VERSION)
        with open(tmp_episode_dir / "episode.json", "w") as f:
            json.dump({"episode_id": "test", "duration_seconds": 60}, f)

        mock_resp = MagicMock()
        mock_resp.json.return_value = MONO_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = TranscribeAgent(tmp_episode_dir, sample_config).execute()
        assert result["mode"] == "diarized"
        assert result["utterance_count"] == 2
        assert (tmp_episode_dir / "diarized_transcript.json").exists()

        params = mock_post.call_args.kwargs.get("params") or mock_post.call_args[1].get(
            "params"
        )
        assert params["diarize"] == "true"
        assert "multichannel" not in params
