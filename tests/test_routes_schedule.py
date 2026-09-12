"""The schedule is a proposal based on current review and publication evidence."""

import asyncio
import json
from datetime import datetime

from server.routes import schedule


def _episode(root, episode_id="example", **episode):
    path = root / episode_id
    path.mkdir()
    (path / "episode.json").write_text(
        json.dumps({"episode_id": episode_id, "guest_name": "Guest", **episode})
    )
    return path


def _review(*, approved=True):
    state = {
        "enabled_destinations": [{"key": "youtube"}, {"key": "instagram"}],
        "longform": {
            "canonical_render": {"current": approved},
            "approval": {"current": approved},
        },
        "clips": [
            {
                "id": "clip_01",
                "title": "A current clip",
                "review": {
                    "selection": {"status": "selected"},
                    "render": {"current": approved},
                    "approval": {"current": approved},
                },
            }
        ],
    }

    async def load(_episode_id):
        return state

    return load


def test_calendar_ignores_legacy_files_and_boolean_approval(tmp_path, monkeypatch):
    episode = _episode(tmp_path, status="ready_for_review")
    (episode / "longform.mp4").write_bytes(b"legacy")
    (episode / "clips.json").write_text(
        json.dumps([{"clip_id": "one", "approved": True}])
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "review_state", _review(approved=False))
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())

    assert result["mode"] == "proposal"
    assert result["total_items"] == 0


def test_calendar_proposes_only_current_revision_bound_approvals(tmp_path, monkeypatch):
    _episode(tmp_path, title="A current episode")
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    items = [item for day in result["schedule"] for item in day["items"]]

    assert result["total_items"] == 2
    assert items[0]["type"] == "longform"
    assert items[0]["destination"] == "youtube"
    assert items[1]["type"] == "short"
    assert items[1]["destinations"] == ["youtube", "instagram"]


def test_calendar_surfaces_rss_without_claiming_youtube_publication(
    tmp_path, monkeypatch
):
    episode = _episode(tmp_path)
    (episode / "podcast_feed.json").write_text(
        json.dumps(
            {
                "audio_url": "https://media.example/audio.mp3",
                "feed_url": "https://media.example/feed.xml",
            }
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "review_state", _review(approved=False))
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())

    assert result["total_items"] == 0
    assert result["publication_evidence"] == [
        {
            "episode_id": "example",
            "name": "Guest",
            "content_type": "podcast_audio",
            "destination": "podcast_rss",
            "status": "published",
            "url": "https://media.example/audio.mp3",
            "feed_url": "https://media.example/feed.xml",
            "evidence_source": "podcast_feed.json",
        }
    ]


def test_recorded_submissions_block_ambiguous_duplicate_proposals(
    tmp_path, monkeypatch
):
    episode = _episode(tmp_path)
    (episode / "publish.json").write_text(
        json.dumps(
            {
                "longform": {"status": "submitted", "request_id": "long-1"},
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": "submitted",
                        "platforms": ["youtube", "instagram"],
                        "request_id": "short-1",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())

    assert result["total_items"] == 0
    assert [item["status"] for item in result["publication_evidence"]] == [
        "submitted",
        "submitted",
    ]
    assert result["publication_evidence"][0]["destination"] == "unknown"


def test_calendar_day_uses_configured_timezone(tmp_path, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat("2026-09-11T00:30:00+00:00")
            return instant.astimezone(tz)

    monkeypatch.setattr(schedule, "datetime", FixedDateTime)
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(
        schedule,
        "_load_config",
        lambda: {"schedule": {"timezone": "America/Los_Angeles"}},
    )
    result = asyncio.run(schedule.get_schedule())
    assert result["schedule"][0]["date"] == "2026-09-10"
