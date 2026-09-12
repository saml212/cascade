"""Read-only release proposals from current approvals and publication evidence."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tomllib
from fastapi import APIRouter, HTTPException

from lib.paths import get_episodes_dir
from server.routes.review import review_state

router = APIRouter(prefix="/api", tags=["schedule"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RECORDED_PUBLICATION_STATES = {"submitted", "published", "already_submitted"}


def _load_config() -> dict:
    for path in [
        PROJECT_ROOT / "config" / "config.toml",
        PROJECT_ROOT / "config.toml",
    ]:
        if path.exists():
            with path.open("rb") as handle:
                return tomllib.load(handle)
    return {}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _publication_evidence(ep_dir: Path, episode: dict) -> list[dict]:
    """Return recorded external actions without upgrading submissions to publishes."""
    episode_id = str(episode.get("episode_id", ep_dir.name))
    name = episode.get("name") or episode.get("guest_name") or episode_id
    records = []

    podcast = _read_json(ep_dir / "podcast_feed.json", {})
    audio_url = podcast.get("audio_url")
    feed_url = podcast.get("feed_url")
    if audio_url or feed_url:
        records.append(
            {
                "episode_id": episode_id,
                "name": name,
                "content_type": "podcast_audio",
                "destination": "podcast_rss",
                "status": "published" if audio_url else "recorded",
                "url": audio_url,
                "feed_url": feed_url,
                "evidence_source": "podcast_feed.json",
            }
        )

    youtube_url = episode.get("youtube_longform_url")
    if isinstance(youtube_url, str) and youtube_url.strip():
        records.append(
            {
                "episode_id": episode_id,
                "name": name,
                "content_type": "longform",
                "destination": "youtube",
                "status": "published",
                "url": youtube_url.strip(),
                "evidence_source": "episode.json",
            }
        )

    publish = _read_json(ep_dir / "publish.json", {})
    longform = publish.get("longform")
    if (
        isinstance(longform, dict)
        and longform.get("status") in _RECORDED_PUBLICATION_STATES
    ):
        records.append(
            {
                "episode_id": episode_id,
                "name": name,
                "content_type": "longform",
                "destination": longform.get("platform") or "unknown",
                "status": longform["status"],
                "request_id": longform.get("request_id"),
                "evidence_source": "publish.json",
            }
        )
    shorts = publish.get("shorts", [])
    if not isinstance(shorts, list):
        shorts = []
    for short in shorts:
        if (
            not isinstance(short, dict)
            or not short.get("clip_id")
            or short.get("status") not in _RECORDED_PUBLICATION_STATES
        ):
            continue
        destinations = short.get("platforms")
        if not isinstance(destinations, list) or not destinations:
            destinations = ["unknown"]
        records.append(
            {
                "episode_id": episode_id,
                "name": name,
                "content_type": "short",
                "clip_id": str(short["clip_id"]),
                "destinations": [str(value) for value in destinations],
                "status": short["status"],
                "request_id": short.get("request_id"),
                "evidence_source": "publish.json",
            }
        )
    return records


def _youtube_longform_recorded(records: list[dict]) -> bool:
    return any(
        record["content_type"] == "longform"
        and record.get("destination") in {"youtube", "unknown"}
        for record in records
    )


def _short_publication_recorded(records: list[dict], clip_id: str) -> bool:
    return any(
        record["content_type"] == "short" and record.get("clip_id") == clip_id
        for record in records
    )


async def _get_approved_items(
    episodes_dir: Path,
) -> tuple[list[dict], list[dict]]:
    """Collect only currently approved, current, unsubmitted release items."""
    items = []
    publication_evidence = []
    if not episodes_dir.exists():
        return items, publication_evidence

    for ep_dir in sorted(episodes_dir.iterdir()):
        episode = _read_json(ep_dir / "episode.json", {})
        if not episode:
            continue
        episode_id = str(episode.get("episode_id", ep_dir.name))
        name = episode.get("name") or episode.get("guest_name") or episode_id
        evidence = _publication_evidence(ep_dir, episode)
        publication_evidence.extend(evidence)
        try:
            review = await review_state(episode_id)
        except (HTTPException, OSError, TypeError, ValueError):
            continue

        enabled_destinations = [
            str(destination["key"])
            for destination in review.get("enabled_destinations", [])
            if isinstance(destination, dict) and destination.get("key")
        ]
        longform = review.get("longform", {})
        canonical = longform.get("canonical_render", {})
        approval = longform.get("approval", {})
        if (
            "youtube" in enabled_destinations
            and canonical.get("current") is True
            and approval.get("current") is True
            and not _youtube_longform_recorded(evidence)
        ):
            items.append(
                {
                    "type": "longform",
                    "episode_id": episode_id,
                    "name": name,
                    "title": episode.get("title") or name,
                    "destination": "youtube",
                }
            )

        for clip in review.get("clips", []):
            if not isinstance(clip, dict) or not clip.get("id"):
                continue
            clip_id = str(clip["id"])
            clip_review = clip.get("review", {})
            if (
                not enabled_destinations
                or clip_review.get("selection", {}).get("status") != "selected"
                or clip_review.get("render", {}).get("current") is not True
                or clip_review.get("approval", {}).get("current") is not True
                or _short_publication_recorded(evidence, clip_id)
            ):
                continue
            items.append(
                {
                    "type": "short",
                    "episode_id": episode_id,
                    "clip_id": clip_id,
                    "name": name,
                    "title": clip.get("title") or f"Clip {clip_id}",
                    "destinations": enabled_destinations,
                }
            )

    return items, publication_evidence


@router.get("/schedule")
async def get_schedule():
    """Build a seven-day proposal; this endpoint performs no external action."""
    config = _load_config()
    sched_cfg = config.get("schedule", {})
    weekday_limit = sched_cfg.get("shorts_per_day_weekday", 1)
    weekend_limit = sched_cfg.get("shorts_per_day_weekend", 2)
    longform_delay = sched_cfg.get("longform_delay_days", 0)
    tz_name = sched_cfg.get("timezone", "America/Los_Angeles")

    items, publication_evidence = await _get_approved_items(get_episodes_dir())
    longforms = [item for item in items if item["type"] == "longform"]
    shorts = [item for item in items if item["type"] == "short"]

    today = datetime.now(ZoneInfo(tz_name)).date()
    days = []
    short_idx = 0
    for offset in range(7):
        date = today + timedelta(days=offset)
        is_weekend = date.weekday() >= 4
        limit = weekend_limit if is_weekend else weekday_limit
        day = {
            "date": date.isoformat(),
            "day_name": date.strftime("%A"),
            "items": [],
        }
        if longforms and offset >= longform_delay:
            longform = longforms.pop(0)
            day["items"].append({**longform, "scheduled_date": date.isoformat()})
        while (
            short_idx < len(shorts)
            and sum(item["type"] == "short" for item in day["items"]) < limit
        ):
            short = shorts[short_idx]
            day["items"].append({**short, "scheduled_date": date.isoformat()})
            short_idx += 1
        days.append(day)

    return {
        "schedule": days,
        "total_items": len(items),
        "unscheduled_shorts": len(shorts) - short_idx,
        "unscheduled_longforms": len(longforms),
        "publication_evidence": publication_evidence,
        "mode": "proposal",
        "timezone": tz_name,
    }
