"""Schedule route — compute publish calendar from approved episodes and config."""

import json
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter

from lib.paths import get_episodes_dir

router = APIRouter(prefix="/api", tags=["schedule"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_config() -> dict:
    for p in [PROJECT_ROOT / "config" / "config.toml", PROJECT_ROOT / "config.toml"]:
        if p.exists():
            with open(p, "rb") as f:
                return tomllib.load(f)
    return {}


def _get_approved_items(episodes_dir: Path) -> list[dict]:
    """Collect approved but unpublished clips and longforms."""
    items = []
    if not episodes_dir.exists():
        return items

    for ep_dir in sorted(episodes_dir.iterdir()):
        ep_file = ep_dir / "episode.json"
        if not ep_file.exists():
            continue
        try:
            with open(ep_file) as f:
                ep = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        ep_id = ep.get("episode_id", ep_dir.name)
        ep_name = ep.get("name", ep.get("guest_name", ep_id))
        published = ep.get("published", {})

        # Check longform
        if ep.get("status") in ("approved", "ready_for_review"):
            longform_path = ep_dir / "longform.mp4"
            if longform_path.exists() and not published.get("longform"):
                items.append({
                    "type": "longform",
                    "episode_id": ep_id,
                    "name": ep_name,
                    "title": ep.get("metadata", {}).get("longform", {}).get("title", ep_name),
                })

        # Check shorts
        clips_file = ep_dir / "clips.json"
        if clips_file.exists():
            try:
                with open(clips_file) as f:
                    clips_data = json.load(f)
                clips = clips_data.get("clips", clips_data if isinstance(clips_data, list) else [])
            except (json.JSONDecodeError, OSError):
                clips = []

            for clip in clips:
                clip_id = clip.get("clip_id", clip.get("id", ""))
                if clip.get("approved") and not published.get(f"short_{clip_id}"):
                    items.append({
                        "type": "short",
                        "episode_id": ep_id,
                        "clip_id": clip_id,
                        "name": ep_name,
                        "title": clip.get("title", f"Clip {clip_id}"),
                    })

    return items


@router.get("/schedule")
async def get_schedule():
    """Build a 7-day publish calendar from approved content and config rules."""
    config = _load_config()
    sched_cfg = config.get("schedule", {})
    weekday_limit = sched_cfg.get("shorts_per_day_weekday", 1)
    weekend_limit = sched_cfg.get("shorts_per_day_weekend", 2)
    longform_delay = sched_cfg.get("longform_delay_days", 0)
    tz_name = sched_cfg.get("timezone", "America/Los_Angeles")

    episodes_dir = get_episodes_dir()
    items = _get_approved_items(episodes_dir)

    # Separate longforms and shorts
    longforms = [i for i in items if i["type"] == "longform"]
    shorts = [i for i in items if i["type"] == "short"]

    # Build 7-day calendar starting today
    today = datetime.now().date()
    days = []
    short_idx = 0

    for offset in range(7):
        date = today + timedelta(days=offset)
        weekday = date.weekday()  # 0=Mon, 6=Sun
        is_weekend = weekday >= 4  # Fri-Sun
        limit = weekend_limit if is_weekend else weekday_limit

        day = {
            "date": date.isoformat(),
            "day_name": date.strftime("%A"),
            "items": [],
        }

        # Schedule longform on first available day after delay
        if longforms and offset >= longform_delay:
            lf = longforms.pop(0)
            day["items"].append({**lf, "scheduled_date": date.isoformat()})

        # Fill shorts up to daily limit
        while short_idx < len(shorts) and len([i for i in day["items"] if i["type"] == "short"]) < limit:
            short = shorts[short_idx]
            day["items"].append({**short, "scheduled_date": date.isoformat()})
            short_idx += 1

        days.append(day)

    # Count unscheduled
    unscheduled_shorts = len(shorts) - short_idx
    unscheduled_longforms = len(longforms)

    return {
        "schedule": days,
        "total_items": len(items),
        "unscheduled_shorts": unscheduled_shorts,
        "unscheduled_longforms": unscheduled_longforms,
    }
