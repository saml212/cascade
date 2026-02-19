"""Publishing and schedule endpoints."""
# TODO: Implement publishing system (next priority)

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from lib.paths import get_project_root, get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["publish"])

PROJECT_ROOT = get_project_root()
DATA_DIR = PROJECT_ROOT / "data"
EPISODES_DIR = get_episodes_dir()


@router.get("/schedule")
async def get_schedule() -> dict:
    """Get the full publish schedule."""
    logger.info("GET /api/schedule")
    # Try data/schedule.json first
    schedule_file = DATA_DIR / "schedule.json"
    if schedule_file.exists():
        try:
            with open(schedule_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Aggregate from episode directories
    schedule_items = []
    if EPISODES_DIR.exists():
        for ep_dir in sorted(EPISODES_DIR.iterdir()):
            if not ep_dir.is_dir():
                continue
            ep_file = ep_dir / "episode.json"
            if not ep_file.exists():
                continue
            try:
                with open(ep_file) as f:
                    ep = json.load(f)
                sched = ep.get("schedule", {})
                if sched:
                    schedule_items.append({
                        "episode_id": ep.get("episode_id", ep_dir.name),
                        "schedule": sched,
                    })
            except (json.JSONDecodeError, OSError):
                continue

    return {
        "schedule": [],
        "episodes": schedule_items,
        "pattern": {
            "weekday": "1 clip/day (Mon–Thu)",
            "weekend": "2 clips/day (Fri–Sun)",
            "total": "10 clips over 7 days",
        },
    }


@router.post("/schedule/{episode_id}/publish")
async def trigger_publish(episode_id: str) -> dict:
    """Trigger manual publish for an episode.

    Placeholder — the full implementation will invoke the Publisher Agent
    to push approved clips to YouTube, TikTok, and Instagram.
    """
    logger.info("POST /api/schedule/%s/publish", episode_id)
    ep_dir = EPISODES_DIR / episode_id
    if not ep_dir.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")

    return {
        "message": "Publishing triggered. This feature requires platform API keys to be configured.",
        "episode_id": episode_id,
    }
