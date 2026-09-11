"""Tests for the real-footage thumbnail agent adapter."""

import json
from unittest.mock import patch

from agents.thumbnail_gen import ThumbnailGenAgent


def test_agent_uses_explicit_episode_headline_and_writes_provenance(
    tmp_episode_dir, sample_config
):
    (tmp_episode_dir / "source_merged.mp4").write_bytes(b"video")
    (tmp_episode_dir / "episode.json").write_text(
        json.dumps(
            {
                "title": "Editorial episode title",
                "thumbnail_headline": "15 Years on the 38 Geary",
                "thumbnail_frame_seconds": 321.5,
                "thumbnail_title_position": "bottom",
            }
        )
    )
    generated = {
        "version": "source-frame-title/v1",
        "clock": "source",
        "headline": "15 YEARS ON THE 38 GEARY",
    }

    with patch(
        "agents.thumbnail_gen.render_episode_thumbnail", return_value=generated
    ) as render:
        result = ThumbnailGenAgent(tmp_episode_dir, sample_config).execute()

    assert result == generated
    assert render.call_args.args == (
        tmp_episode_dir / "source_merged.mp4",
        tmp_episode_dir / "thumbnails" / "longform.jpg",
        "15 Years on the 38 Geary",
    )
    assert render.call_args.kwargs["at_seconds"] == 321.5
    assert render.call_args.kwargs["title_position"] == "bottom"
    assert json.loads((tmp_episode_dir / "thumbnail_gen.json").read_text()) == generated


def test_agent_requires_a_reviewable_headline(tmp_episode_dir, sample_config):
    (tmp_episode_dir / "source_merged.mp4").write_bytes(b"video")
    agent = ThumbnailGenAgent(tmp_episode_dir, sample_config)

    try:
        agent.execute()
    except ValueError as exc:
        assert "thumbnail_headline" in str(exc)
    else:
        raise AssertionError("empty headline should fail before rendering")
