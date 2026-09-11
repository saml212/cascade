"""Tests for the transcribe agent — multichannel and mono fallback modes."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from agents.transcribe import (
    CAMERA_AUDIO_CACHE_VERSION,
    TranscribeAgent,
    _raw_is_multichannel,
    analyze_transcript_coverage,
    current_diarized_transcript,
    export_logical_track_window,
    remap_transcript_timestamps,
    repair_existing_transcript,
    transcribe_logical_track_window,
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
    def test_single_channel_field_is_still_mono(self):
        raw = {
            "metadata": {"channels": 1},
            "results": {"utterances": [{"channel": 0, "speaker": 0}]},
        }
        assert _raw_is_multichannel(raw) is False

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
            Path(cmd[-1]).write_bytes(b"aac")
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
        metadata = json.loads(
            (tmp_episode_dir / "work" / "audio.m4a.fingerprint.json").read_text()
        )
        assert metadata["version"] == CAMERA_AUDIO_CACHE_VERSION
        assert metadata["clock"] == "source"

    @patch("agents.transcribe.subprocess.run")
    @patch("httpx.post")
    def test_mono_fallback(
        self, mock_post, run, tmp_episode_dir, sample_config, monkeypatch
    ):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
        (tmp_episode_dir / "source_merged.mp4").write_bytes(b"\x00" * 100)
        with open(tmp_episode_dir / "episode.json", "w") as f:
            json.dump({"episode_id": "test", "duration_seconds": 60}, f)

        def create_audio(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"audio")
            return MagicMock(returncode=0)

        run.side_effect = create_audio

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


def _write_activity(ep_dir, channel_levels):
    (ep_dir / "segments.json").write_text(
        json.dumps(
            {
                "clock": "source",
                "fingerprint": "segments-current",
                "track_mapping": [
                    {
                        "speaker": f"speaker_{index}",
                        "logical_track": track,
                    }
                    for index, track in enumerate((1, 3, 2))
                ],
            }
        )
    )
    (ep_dir / "work" / "rms_meta.json").write_text(
        json.dumps(
            {
                "clock": "source",
                "fingerprint": "segments-current",
                "frame_seconds": 0.1,
            }
        )
    )
    for index, level in enumerate(channel_levels):
        np.save(ep_dir / "work" / f"speaker_{index}_rms_db.npy", np.full(30, level))


def _multichannel_episode(ep_dir):
    tracks = []
    for track in (1, 3, 2):
        source = ep_dir / "audio" / f"session_Tr{track}.WAV"
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(bytes([track]))
        tracks.append(
            {
                "track_number": track,
                "track_type": "input",
                "dest_path": str(source),
                "filename": source.name,
            }
        )
    return {
        "duration_seconds": 3.0,
        "audio_sync": {
            "video_duration": 3.0,
            "offset_seconds": -0.2,
            "tempo_factor": 1.0001,
            "r_squared": 0.99,
        },
        "audio_tracks": tracks,
        "crop_config": {
            "speakers": [
                {"label": "Host", "track": 1},
                {"label": "Arnold", "track": 3},
                {"label": "Guest", "track": 2},
            ]
        },
    }


class TestCanonicalRepair:
    def test_maps_mono_diarization_ids_to_source_speakers(
        self, tmp_episode_dir, sample_config
    ):
        (tmp_episode_dir / "segments.json").write_text(
            json.dumps(
                {
                    "clock": "source",
                    "track_mapping": [
                        {
                            "speaker": "speaker_0",
                            "person": "Host",
                            "camera_channel": "left",
                        },
                        {
                            "speaker": "speaker_1",
                            "person": "Guest",
                            "camera_channel": "right",
                        },
                    ],
                    "segments": [
                        {"speaker": "speaker_0", "start": 0.0, "end": 4.0},
                        {"speaker": "speaker_1", "start": 4.0, "end": 10.0},
                    ],
                }
            )
        )
        raw = {
            "results": {
                "utterances": [
                    {"speaker": 7, "start": 0.5, "end": 3.5},
                    {"speaker": 5, "start": 4.5, "end": 9.5},
                    {"speaker": 9, "start": 6.0, "end": 7.0},
                ]
            }
        }

        speaker_map = TranscribeAgent(
            tmp_episode_dir, sample_config
        )._infer_diarized_speaker_map(raw)

        assert [entry["index"] for entry in speaker_map] == [5, 7, 9]
        assert [entry["label"] for entry in speaker_map] == [
            "Guest",
            "Host",
            "Guest",
        ]
        assert all(entry["mapping_confidence"] == 1.0 for entry in speaker_map)
        assert speaker_map[0]["mapping_collision"] is True
        assert speaker_map[1]["mapping_collision"] is False

    def test_deduplicates_bleed_by_source_activity_and_preserves_real_overlap(
        self, tmp_episode_dir, sample_config
    ):
        episode = _multichannel_episode(tmp_episode_dir)
        (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
        historic_map = [
            {"index": 0, "label": "Speaker 0", "track": 1},
            {"index": 1, "label": "Speaker 1", "track": 3},
            {"index": 2, "label": "Speaker 2", "track": 2},
        ]
        (tmp_episode_dir / "diarized_transcript.json").write_text(
            json.dumps({"speaker_map": historic_map, "utterances": []})
        )
        raw = {
            "results": {
                "utterances": [
                    {
                        "channel": 0,
                        "start": 0.0,
                        "end": 1.0,
                        "transcript": "We row crew.",
                        "confidence": 0.8,
                        "words": [
                            {"word": "we", "start": 0.0, "end": 0.2, "confidence": 0.9},
                            {
                                "word": "row",
                                "start": 0.3,
                                "end": 0.5,
                                "confidence": 0.7,
                            },
                            {
                                "word": "crew",
                                "start": 0.6,
                                "end": 1.0,
                                "confidence": 0.9,
                            },
                        ],
                    },
                    {
                        "channel": 1,
                        "start": 0.01,
                        "end": 1.01,
                        "transcript": "We rode crew.",
                        "confidence": 0.9,
                        "words": [
                            {
                                "word": "we",
                                "start": 0.01,
                                "end": 0.21,
                                "confidence": 0.95,
                            },
                            {
                                "word": "rode",
                                "start": 0.31,
                                "end": 0.51,
                                "confidence": 0.8,
                            },
                            {
                                "word": "crew",
                                "start": 0.61,
                                "end": 1.01,
                                "confidence": 0.95,
                            },
                        ],
                    },
                    {
                        "channel": 2,
                        "start": 0.35,
                        "end": 0.55,
                        "transcript": "Yes.",
                        "confidence": 0.98,
                        "words": [
                            {
                                "word": "yes",
                                "start": 0.35,
                                "end": 0.55,
                                "confidence": 0.98,
                            }
                        ],
                    },
                ]
            }
        }
        raw_text = json.dumps(raw, separators=(",", ":"))
        (tmp_episode_dir / "transcript.json").write_text(raw_text)
        _write_activity(tmp_episode_dir, (0.0, 20.0, 18.0))

        result = repair_existing_transcript(tmp_episode_dir, sample_config)
        repaired = json.loads(
            (tmp_episode_dir / "diarized_transcript.json").read_text()
        )
        words = [
            word for utterance in repaired["utterances"] for word in utterance["words"]
        ]

        assert [word["word"] for word in words] == ["we", "rode", "yes", "crew"]
        assert [word["speaker"] for word in words] == [1, 1, 2, 1]
        assert repaired["canonicalization"]["removed_duplicate_words"] == 3
        assert repaired["canonicalization"]["ambiguous_variant_events"] == 0
        assert words[1]["suspect"] is True
        assert words[1]["suspect_reasons"] == ["channel_variant"]
        assert words[1]["alternatives"][0]["word"] == "row"
        assert words[0]["id"] == "word_000000"
        assert repaired["speaker_map"][1]["label"] == "Arnold"
        assert repaired["speaker_map"][1]["logical_track"] == 3
        assert result["word_count"] == 4
        assert (tmp_episode_dir / "transcript.json").read_text() == raw_text
        assert "Yes." in (tmp_episode_dir / "subtitles" / "transcript.srt").read_text()
        assert (
            current_diarized_transcript(tmp_episode_dir, episode, sample_config)
            == repaired
        )

        (tmp_episode_dir / "transcript_corrections.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "clock": "source",
                    "raw_transcript_sha256": hashlib.sha256(
                        raw_text.encode()
                    ).hexdigest(),
                    "operations": [
                        {
                            "id": "verified_overlap",
                            "op": "replace_range",
                            "start": 0.25,
                            "end": 0.58,
                            "replace_speakers": [1],
                            "reason": "independent close-mic ASR",
                            "words": [
                                {
                                    "word": "rowed",
                                    "start": 0.31,
                                    "end": 0.51,
                                    "speaker": 1,
                                    "confidence": 0.99,
                                }
                            ],
                        }
                    ],
                }
            )
        )
        repair_existing_transcript(tmp_episode_dir, sample_config)
        corrected = json.loads(
            (tmp_episode_dir / "diarized_transcript.json").read_text()
        )
        corrected_words = [
            word for utterance in corrected["utterances"] for word in utterance["words"]
        ]
        assert [word["word"] for word in corrected_words] == [
            "we",
            "rowed",
            "yes",
            "crew",
        ]
        assert corrected_words[1]["id"] == "verified_overlap_000"
        assert corrected_words[2]["speaker"] == 2
        assert corrected["canonicalization"]["corrections_applied"] == [
            "verified_overlap"
        ]
        assert (
            current_diarized_transcript(tmp_episode_dir, episode, sample_config)
            == corrected
        )

        episode["audio_sync"]["offset_seconds"] = 0.5
        assert (
            current_diarized_transcript(tmp_episode_dir, episode, sample_config) is None
        )

    @patch("agents.transcribe.httpx.post")
    @patch("agents.transcribe.subprocess.run")
    def test_repaired_response_is_reused_without_audio_or_network(
        self, run, post, tmp_episode_dir, sample_config
    ):
        (tmp_episode_dir / "source_merged.mp4").write_bytes(b"source")
        (tmp_episode_dir / "episode.json").write_text(
            json.dumps({"duration_seconds": 2.0})
        )
        (tmp_episode_dir / "transcript.json").write_text(json.dumps(MONO_RESPONSE))
        repair_existing_transcript(tmp_episode_dir, sample_config)

        result = TranscribeAgent(tmp_episode_dir, sample_config).execute()

        assert result["reused_raw"] is True
        run.assert_not_called()
        post.assert_not_called()


class TestMultichannelPreparation:
    @patch.object(TranscribeAgent, "_request_deepgram")
    @patch("agents.transcribe.subprocess.run")
    def test_bounded_asr_is_source_clocked_and_cached(
        self, run, request, tmp_episode_dir, sample_config
    ):
        episode = _multichannel_episode(tmp_episode_dir)
        episode["duration_seconds"] = 30.0
        episode["audio_sync"]["video_duration"] = 30.0
        (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))

        def create_audio(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"bounded flac")
            return MagicMock(returncode=0, stderr="")

        run.side_effect = create_audio
        request.return_value = {
            "metadata": {"duration": 5.0},
            "results": {
                "utterances": [
                    {
                        "start": 0.5,
                        "end": 1.0,
                        "words": [{"word": "hello", "start": 0.5, "end": 1.0}],
                    }
                ]
            },
        }
        output_dir = tmp_episode_dir / "qa/bounded-asr"

        first = transcribe_logical_track_window(
            tmp_episode_dir, sample_config, 1, 10.0, 15.0, output_dir
        )
        second = transcribe_logical_track_window(
            tmp_episode_dir, sample_config, 1, 10.0, 15.0, output_dir
        )

        assert first == second
        request.assert_called_once()
        source = json.loads(Path(first["source_response"]["path"]).read_text())
        assert source["results"]["utterances"][0]["start"] == 10.5
        assert source["results"]["utterances"][0]["words"][0]["end"] == 11.0
        assert source["metadata"]["duration"] == 5.0
        assert source["metadata"]["clock"] == "source"
        assert first["review_only"] is True
        assert first["applied_to_canonical"] is False

    @patch("agents.transcribe.subprocess.run")
    def test_exports_bounded_source_clock_logical_track(self, run, tmp_episode_dir):
        episode = _multichannel_episode(tmp_episode_dir)
        episode["duration_seconds"] = 30.0
        episode["audio_sync"]["video_duration"] = 30.0
        first = Path(episode["audio_tracks"][0]["dest_path"])
        second = first.with_name("later_Tr1.WAV")
        second.write_bytes(b"later")
        episode["audio_tracks"].insert(
            1,
            {
                "track_number": 1,
                "track_type": "input",
                "dest_path": str(second),
                "filename": second.name,
            },
        )

        def create_audio(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"bounded flac")
            return MagicMock(returncode=0, stderr="")

        run.side_effect = create_audio
        output = tmp_episode_dir / "qa/review.flac"
        first_result = export_logical_track_window(
            tmp_episode_dir, episode, 1, 5.0, 12.0, output
        )
        second_result = export_logical_track_window(
            tmp_episode_dir, episode, 1, 5.0, 12.0, output
        )

        assert first_result == second_result
        assert run.call_count == 1
        command = run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        assert "[part0][part1]concat=n=2:v=0:a=1[joined]" in graph
        assert graph.index("concat=n=2") < graph.index("adelay=200")
        assert graph.index("atempo=1.00010000") < graph.index(
            "atrim=start=5.000000:end=12.000000"
        )
        assert first_result["source_window"]["duration_seconds"] == 7.0
        assert first_result["source_files"][0]["path"] == str(first.resolve())
        assert (
            first_result["audio"]["sha256"]
            == hashlib.sha256(b"bounded flac").hexdigest()
        )

    def test_reports_untranscribed_logical_mic_activity(
        self, tmp_episode_dir, sample_config
    ):
        episode = {
            "duration_seconds": 10.0,
            "crop_config": {"speakers": [{"label": "Guest", "track": 2}]},
        }
        (tmp_episode_dir / "segments.json").write_text(
            json.dumps(
                {
                    "clock": "source",
                    "fingerprint": "current-speaker-analysis",
                    "track_mapping": [
                        {
                            "speaker": "speaker_0",
                            "person": "Guest",
                            "logical_track": 2,
                        }
                    ],
                    "segments": [
                        {
                            "speaker": "speaker_0",
                            "start": 0.0,
                            "end": 10.0,
                        }
                    ],
                }
            )
        )
        (tmp_episode_dir / "work/rms_meta.json").write_text(
            json.dumps(
                {
                    "clock": "source",
                    "fingerprint": "current-speaker-analysis",
                    "frame_seconds": 0.1,
                }
            )
        )
        levels = np.full(100, -70.0)
        levels[20:40] = -20.0
        levels[70:100] = -18.0
        np.save(tmp_episode_dir / "work/speaker_0_rms_db.npy", levels)
        words = [
            {
                "word": "covered",
                "start": 2.0,
                "end": 3.8,
                "speaker": 7,
            }
        ]
        (tmp_episode_dir / "diarized_transcript.json").write_text(
            json.dumps(
                {
                    "clock": "source",
                    "speaker_map": [
                        {"index": 7, "person": "Guest", "logical_track": 2}
                    ],
                    "utterances": [{"speaker": 7, "words": words}],
                }
            )
        )
        (tmp_episode_dir / "transcript_provenance.json").write_text(
            json.dumps({"clock": "source", "raw_transcript_sha256": "raw"})
        )

        report = analyze_transcript_coverage(tmp_episode_dir, episode, sample_config)

        assert report["status"] == "review_required"
        assert report["summary"]["finding_count"] == 1
        finding = report["findings"][0]
        assert finding["logical_track"] == 2
        assert finding["people"] == ["Guest"]
        assert finding["source_window"] == {
            "start": 7.0,
            "end": 10.0,
            "duration_seconds": 3.0,
        }
        assert finding["evidence"]["active_seconds"] == 3.0

    @patch("agents.transcribe.subprocess.run")
    def test_concatenates_repeated_logical_track_before_sync(
        self, run, tmp_episode_dir, sample_config
    ):
        episode = _multichannel_episode(tmp_episode_dir)
        first = Path(episode["audio_tracks"][0]["dest_path"])
        second = first.with_name("later_Tr1.WAV")
        second.write_bytes(b"later")
        episode["audio_tracks"].insert(
            1,
            {
                "track_number": 1,
                "track_type": "input",
                "dest_path": str(second),
                "filename": second.name,
            },
        )
        (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))

        def create_audio(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"flac")
            return MagicMock(returncode=0, stderr="")

        run.side_effect = create_audio
        agent = TranscribeAgent(tmp_episode_dir, sample_config)
        output, channel_map = agent._prepare_multichannel_audio(episode)

        assert output.is_file()
        assert channel_map[0]["source_files"] == [first.name, second.name]
        command = run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        assert "[c0_0][c0_1]concat=n=2:v=0:a=1[joined0]" in graph
        assert graph.index("concat=n=2") < graph.index("[joined0]asetpts")
        assert "adelay=200" in graph
        assert "atempo=1.00010000" in graph

    def test_historic_asr_channel_order_wins_over_crop_reordering(
        self, tmp_episode_dir, sample_config
    ):
        episode = _multichannel_episode(tmp_episode_dir)
        episode["crop_config"]["speakers"].reverse()
        (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
        (tmp_episode_dir / "diarized_transcript.json").write_text(
            json.dumps(
                {
                    "speaker_map": [
                        {"index": 0, "track": 1},
                        {"index": 1, "track": 3},
                        {"index": 2, "track": 2},
                    ]
                }
            )
        )

        channel_map = TranscribeAgent(
            tmp_episode_dir, sample_config
        )._resolve_channel_map(episode)

        assert [entry["logical_track"] for entry in channel_map] == [1, 3, 2]
        assert [entry["label"] for entry in channel_map] == ["Host", "Arnold", "Guest"]
