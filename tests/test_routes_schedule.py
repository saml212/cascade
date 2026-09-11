"""The calendar is a proposal and accepts both existing clip manifest shapes."""

import asyncio
import json
from datetime import datetime

from server.routes import schedule


def test_calendar_accepts_list_clip_manifest(tmp_path, monkeypatch):
    episode = tmp_path / "example"
    episode.mkdir()
    (episode / "episode.json").write_text(json.dumps({"episode_id": "example"}))
    (episode / "clips.json").write_text(
        json.dumps(
            [
                {"clip_id": "one", "approved": True, "title": "An approved clip"},
                {"clip_id": "two", "approved": False},
            ]
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_load_config", dict)
    result = asyncio.run(schedule.get_schedule())
    assert result["mode"] == "proposal"
    assert result["total_items"] == 1
    assert result["schedule"][0]["items"][0]["clip_id"] == "one"


def test_calendar_day_uses_configured_timezone(tmp_path, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 00:30 UTC is still the previous afternoon in Los Angeles.
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
