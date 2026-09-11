"""Tests for source-grounded structured clip mining."""

import json
from unittest.mock import patch

import pytest

from agents.clip_miner import ClipMinerAgent


def _provider_result(clips):
    return (
        {
            "episode_info": {
                "guest_name": "John",
                "guest_title": "Engineer",
                "episode_title": "Generated title",
                "episode_description": "Generated description",
            },
            "clips": clips,
        },
        {
            "provider": "openai",
            "model": "generation-test-model",
            "response_id": "resp_test",
        },
    )


def _candidate(start=30.0, end=90.0, hook="Thanks for having me"):
    return {
        "start_seconds": start,
        "end_seconds": end,
        "title": "Nuclear power",
        "hook_text": hook,
        "compelling_reason": "A complete explanation",
        "virality_score": 8,
    }


@pytest.fixture
def episode_inputs(tmp_episode_dir):
    diarized = {
        "clock": "source",
        "provenance": {
            "raw_transcript_sha256": "raw-sha",
            "corrections_fingerprint": "corrections-sha",
        },
        "utterances": [
            {
                "speaker": 0,
                "start": 0.0,
                "end": 30.0,
                "text": "Welcome to the show.",
                "words": [
                    {
                        "word": "welcome",
                        "punctuated_word": "Welcome",
                        "start": 0.0,
                        "end": 0.5,
                        "speaker": 0,
                    }
                ],
            },
            {
                "speaker": 1,
                "start": 30.0,
                "end": 90.0,
                "text": "Thanks for having me. Let me explain nuclear power.",
                "words": [
                    {
                        "word": word.casefold().rstrip("."),
                        "punctuated_word": word,
                        "start": 31 + index,
                        "end": 31.8 + index,
                        "speaker": 1,
                    }
                    for index, word in enumerate(
                        [
                            "Thanks",
                            "for",
                            "having",
                            "me.",
                            "Let",
                            "me",
                            "explain",
                            "nuclear",
                            "power.",
                        ]
                    )
                ],
            },
        ],
    }
    segments = {
        "clock": "source",
        "segments": [
            {"start": 0.0, "end": 30.0, "speaker": "speaker_0"},
            {"start": 30.0, "end": 120.0, "speaker": "speaker_1"},
        ],
    }
    for name, data in (
        ("diarized_transcript.json", diarized),
        ("segments.json", segments),
        ("stitch.json", {"duration_seconds": 120.0}),
        (
            "episode.json",
            {
                "episode_id": "ep_test",
                "title": "Editorial title",
                "status": "processing",
            },
        ),
    ):
        (tmp_episode_dir / name).write_text(json.dumps(data))
    return tmp_episode_dir


def test_format_transcript_uses_source_clock(episode_inputs, sample_config):
    agent = ClipMinerAgent(episode_inputs, sample_config)
    result = agent._format_transcript(agent.load_json("diarized_transcript.json"))

    assert "[30.000s-90.000s] Speaker 1" in result
    assert "nuclear power" in result


def test_get_dominant_speaker(episode_inputs, sample_config):
    agent = ClipMinerAgent(episode_inputs, sample_config)
    segments = agent.load_json("segments.json")["segments"]

    assert agent._get_dominant_speaker(40.0, 80.0, segments) == "speaker_1"
    assert agent._get_dominant_speaker(0.0, 35.0, segments) == "speaker_0"
    assert agent._get_dominant_speaker(130.0, 140.0, segments) == "BOTH"


def test_snap_to_silence_without_rms_is_unchanged(episode_inputs, sample_config):
    agent = ClipMinerAgent(episode_inputs, sample_config)
    clips = [{"start_seconds": 30.0, "end_seconds": 90.0}]

    assert agent._snap_to_silence(clips, {}) == clips


def test_execute_uses_shared_provider_and_preserves_editorial_episode_title(
    episode_inputs, sample_config
):
    with patch(
        "agents.clip_miner.generate_structured",
        return_value=_provider_result([_candidate()]),
    ) as generate:
        result = ClipMinerAgent(episode_inputs, sample_config).execute()

    request = generate.call_args.kwargs
    assert request["task"] == "podcast_clip_candidates"
    assert "20-90 seconds" in request["prompt"]
    assert result["clips"][0]["hook_grounding"] == "exact_canonical_transcript"
    assert result["clips"][0]["speaker"] == "speaker_1"
    assert result["generation"]["raw_transcript_sha256"] == "raw-sha"
    episode = json.loads((episode_inputs / "episode.json").read_text())
    assert episode["title"] == "Editorial title"
    assert episode["guest_name"] == "John"


def test_unverifiable_hook_is_replaced_with_canonical_words(
    episode_inputs, sample_config
):
    with patch(
        "agents.clip_miner.generate_structured",
        return_value=_provider_result([_candidate(hook="An invented quotation")]),
    ):
        result = ClipMinerAgent(episode_inputs, sample_config).execute()

    clip = result["clips"][0]
    assert clip["proposed_hook_text"] == "An invented quotation"
    assert clip["hook_text"].startswith("Thanks for having me.")
    assert clip["hook_grounding"] == "replaced_with_canonical_transcript"


def test_existing_candidates_are_never_overwritten(episode_inputs, sample_config):
    stored = {"clips": [{"id": "clip_07", "title": "Editorial choice"}]}
    (episode_inputs / "clips.json").write_text(json.dumps(stored))

    with patch("agents.clip_miner.generate_structured") as generate:
        result = ClipMinerAgent(episode_inputs, sample_config).execute()

    generate.assert_not_called()
    assert result["_status"] == "skipped"
    assert json.loads((episode_inputs / "clips.json").read_text()) == stored


def test_alternative_appends_without_rewriting_other_candidates(
    episode_inputs, sample_config
):
    original = {
        "clips": [
            {
                "id": "clip_01",
                "rank": 1,
                "start_seconds": 0,
                "end_seconds": 25,
                "title": "Keep me",
                "status": "approved",
            },
            {
                "id": "clip_04",
                "rank": 4,
                "start_seconds": 25,
                "end_seconds": 30,
                "title": "Replace me",
                "status": "pending",
            },
        ]
    }
    (episode_inputs / "clips.json").write_text(json.dumps(original))
    generated = _candidate(start=30, end=90)

    with patch(
        "agents.clip_miner.generate_structured",
        return_value=_provider_result([generated]),
    ) as generate:
        result = ClipMinerAgent(episode_inputs, sample_config).generate_alternative(
            "clip_04"
        )

    stored = json.loads((episode_inputs / "clips.json").read_text())["clips"]
    assert [(clip["id"], clip["title"]) for clip in stored[:2]] == [
        ("clip_01", "Keep me"),
        ("clip_04", "Replace me"),
    ]
    assert stored[1]["selection_status"] == "rejected"
    assert result["alternative"]["id"] == "clip_05"
    assert result["alternative"]["alternative_for"] == "clip_04"
    assert result["alternative"]["selection_status"] == "selected"
    assert "0.000s to 25.000s" in generate.call_args.kwargs["prompt"]


def test_alternative_refuses_to_overwrite_concurrent_editorial_change(
    episode_inputs, sample_config
):
    stored = {
        "clips": [
            {
                "id": "clip_01",
                "rank": 1,
                "start_seconds": 0,
                "end_seconds": 25,
                "title": "Original title",
                "status": "pending",
            }
        ]
    }
    path = episode_inputs / "clips.json"
    path.write_text(json.dumps(stored))

    def concurrent_edit(*_args, **_kwargs):
        changed = json.loads(path.read_text())
        changed["clips"][0]["title"] = "Edited while generation ran"
        path.write_text(json.dumps(changed))
        return _provider_result([_candidate(start=30, end=90)])

    with (
        patch("agents.clip_miner.generate_structured", side_effect=concurrent_edit),
        pytest.raises(ValueError, match="changed while generating"),
    ):
        ClipMinerAgent(episode_inputs, sample_config).generate_alternative("clip_01")

    assert json.loads(path.read_text())["clips"][0]["title"] == (
        "Edited while generation ran"
    )


def test_rejects_non_source_clock_inputs(episode_inputs, sample_config):
    transcript = json.loads((episode_inputs / "diarized_transcript.json").read_text())
    transcript["clock"] = "edited"
    (episode_inputs / "diarized_transcript.json").write_text(json.dumps(transcript))

    with pytest.raises(ValueError, match="source-clock"):
        ClipMinerAgent(episode_inputs, sample_config).execute()
