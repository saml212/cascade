"""The schedule is a proposal based on current review and publication evidence."""

import asyncio
import json
from datetime import datetime

import pytest

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
                "metadata": {"youtube": {"title": "Canonical clip title"}},
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


@pytest.fixture(autouse=True)
def _current_release_gate(monkeypatch):
    monkeypatch.setattr(
        schedule,
        "quality_snapshot",
        lambda *_args, **_kwargs: {
            "release_gate": {
                "can_approve_publish": True,
                "revision": "sha256:current",
                "blockers": [],
            }
        },
    )


def _fix_now(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat("2099-07-01T16:00:00+00:00")
            return instant.astimezone(tz)

    monkeypatch.setattr(schedule, "datetime", FixedDateTime)


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


def test_exact_episode_schedule_replaces_generated_slot(tmp_path, monkeypatch):
    _fix_now(monkeypatch)
    _episode(
        tmp_path,
        youtube_longform_url="https://youtube.example/video",
        publish_schedule=[
            {
                "clip_id": "clip_01",
                "scheduled_date": "2099-07-04T18:00:00-07:00",
            }
        ],
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    items = [item for day in result["schedule"] for item in day["items"]]

    assert [(item["state"], item["scheduled_date"]) for item in items] == [
        ("planned", "2099-07-04T18:00:00-07:00")
    ]
    assert items[0]["title"] == "Canonical clip title"


def test_published_receipt_is_not_labeled_scheduled():
    assert schedule._receipt_state({"status": "published"}) is None


@pytest.mark.parametrize(
    ("receipt_status", "expected_state"),
    [("submitted", "scheduled"), ("failed", "failed"), ("unknown", "unknown")],
)
def test_receipt_date_and_state_override_plan(
    tmp_path, monkeypatch, receipt_status, expected_state
):
    _fix_now(monkeypatch)
    episode = _episode(
        tmp_path,
        youtube_longform_url="https://youtube.example/video",
        publish_schedule=[
            {
                "clip_id": "clip_01",
                "scheduled_date": "2099-07-04T18:00:00-07:00",
            }
        ],
    )
    (episode / "publish.json").write_text(
        json.dumps(
            {
                "release_revision": "sha256:old",
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": receipt_status,
                        "scheduled": True,
                        "scheduled_date": "2099-07-05T09:00:00-07:00",
                        "platforms": ["youtube", "instagram"],
                        "job_id": "job-01",
                        "request_id": "request-01",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "review_state", _review(approved=False))
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    items = [item for day in result["schedule"] for item in day["items"]]

    assert len(items) == 1
    assert items[0]["state"] == expected_state
    assert items[0]["scheduled_date"] == "2099-07-05T09:00:00-07:00"
    assert items[0]["planned_date"] == "2099-07-04T18:00:00-07:00"
    assert items[0]["job_id"] == "job-01"
    assert items[0]["current_release"] is False


def test_qa_blocked_clip_is_held_out_of_suggestions(tmp_path, monkeypatch):
    _episode(tmp_path, youtube_longform_url="https://youtube.example/video")
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)
    monkeypatch.setattr(
        schedule,
        "quality_snapshot",
        lambda *_args, **_kwargs: {
            "release_gate": {
                "can_approve_publish": False,
                "blockers": [{"message": "Quality review is stale."}],
            }
        },
    )

    result = asyncio.run(schedule.get_schedule())

    assert result["total_items"] == 0
    assert all(not day["items"] for day in result["schedule"])
    assert result["held_items"][0]["episode_id"] == "example"
    assert result["held_items"][0]["blockers"] == ["Quality review is stale."]
