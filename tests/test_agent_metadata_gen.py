"""Tests for gap-filling release metadata generation."""

import json
from unittest.mock import patch

import pytest

from agents.metadata_gen import MetadataGenAgent


def _platform_copy(prefix: str) -> dict:
    return {
        "youtube": {"title": f"{prefix} YouTube", "description": "YT body"},
        "tiktok": {"caption": f"{prefix} TikTok", "hashtags": ["#Local"]},
        "instagram": {"caption": f"{prefix} Instagram", "hashtags": ["#Local"]},
        "x": {"text": f"{prefix} X"},
    }


def _inputs(tmp_episode_dir, metadata: dict | None = None):
    episode = {
        "title": "Editorial episode title",
        "description": "Editorial episode description",
        "tags": ["editorial"],
    }
    clips = {
        "clip_count": 2,
        "generation": {"response_id": "original"},
        "clips": [
            {
                "id": "clip_01",
                "start_seconds": 10,
                "end_seconds": 20,
                "selection_status": "selected",
                "metadata": metadata if metadata is not None else _platform_copy("Edit"),
            },
            {
                "id": "clip_02",
                "start_seconds": 30,
                "end_seconds": 40,
                "selection_status": "rejected",
                "status": "rejected",
            },
        ],
    }
    diarized = {
        "clock": "source",
        "provenance": {
            "raw_transcript_sha256": "raw-sha",
            "corrections_fingerprint": "correction-sha",
        },
        "utterances": [
            {
                "start": 10,
                "end": 20,
                "words": [
                    {
                        "word": "grounded",
                        "punctuated_word": "Grounded",
                        "start": 10,
                        "end": 11,
                    }
                ],
            }
        ],
    }
    (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
    (tmp_episode_dir / "clips.json").write_text(json.dumps(clips))
    (tmp_episode_dir / "diarized_transcript.json").write_text(json.dumps(diarized))
    return episode, clips


def test_complete_editorial_copy_is_preserved_without_generation(
    tmp_episode_dir, sample_config
):
    sample_config["platforms"] = {
        platform: {"enabled": True}
        for platform in ("youtube", "tiktok", "instagram", "x")
    }
    episode, clips = _inputs(tmp_episode_dir)
    episode_bytes = (tmp_episode_dir / "episode.json").read_bytes()
    clip_bytes = (tmp_episode_dir / "clips.json").read_bytes()

    with patch("agents.metadata_gen.generate_structured") as generate:
        result = MetadataGenAgent(tmp_episode_dir, sample_config).execute()

    generate.assert_not_called()
    assert not result["generated"]
    assert (tmp_episode_dir / "episode.json").read_bytes() == episode_bytes
    assert (tmp_episode_dir / "clips.json").read_bytes() == clip_bytes
    stored = json.loads(
        (tmp_episode_dir / "metadata" / "metadata.json").read_text()
    )
    assert stored["longform"]["title"] == episode["title"]
    assert stored["clips"] == [{"id": "clip_01", **clips["clips"][0]["metadata"]}]
    assert stored["generation"]["provider"] == "existing_editorial"


def test_generation_fills_only_missing_fields_and_preserves_container_metadata(
    tmp_episode_dir, sample_config
):
    sample_config["platforms"] = {"youtube": {"enabled": True}}
    locked = _platform_copy("Editorial")
    locked.pop("youtube")
    _, clips = _inputs(tmp_episode_dir, locked)
    generated = {
        "longform": {
            "title": "Generated episode",
            "description": "Generated episode body",
            "tags": ["generated"],
        },
        "clips": [{"id": "clip_01", **_platform_copy("Generated")}],
    }
    provenance = {"provider": "claude_cli", "model": "sonnet"}

    with patch(
        "agents.metadata_gen.generate_structured",
        return_value=(generated, provenance),
    ) as generate:
        result = MetadataGenAgent(tmp_episode_dir, sample_config).execute()

    assert result["generated"]
    request = generate.call_args.kwargs
    assert request["task"] == "podcast_release_metadata"
    assert "Grounded" in request["prompt"]
    stored_clips = json.loads((tmp_episode_dir / "clips.json").read_text())
    assert stored_clips["clip_count"] == clips["clip_count"]
    assert stored_clips["generation"] == clips["generation"]
    stored = stored_clips["clips"][0]["metadata"]
    assert stored["youtube"]["title"] == "Generated YouTube"
    assert stored["tiktok"]["caption"] == "Editorial TikTok"
    episode = json.loads((tmp_episode_dir / "episode.json").read_text())
    assert episode["title"] == "Editorial episode title"


def test_missing_copy_rejects_non_source_clock_transcript(
    tmp_episode_dir, sample_config
):
    sample_config["platforms"] = {"youtube": {"enabled": True}}
    locked = _platform_copy("Editorial")
    locked.pop("youtube")
    _inputs(tmp_episode_dir, locked)
    diarized_path = tmp_episode_dir / "diarized_transcript.json"
    diarized = json.loads(diarized_path.read_text())
    diarized["clock"] = "edited"
    diarized_path.write_text(json.dumps(diarized))

    with pytest.raises(ValueError, match="source-clock"):
        MetadataGenAgent(tmp_episode_dir, sample_config).execute()
