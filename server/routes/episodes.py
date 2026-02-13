"""Episode endpoints."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/episodes", tags=["episodes"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = Path(os.getenv("CASCADE_OUTPUT_DIR", PROJECT_ROOT / "output"))
EPISODES_DIR = OUTPUT_DIR / "episodes"


class NewEpisodeRequest(BaseModel):
    source_path: Optional[str] = None


def read_episode(episode_id: str) -> dict:
    """Read episode.json for a given episode."""
    ep_dir = EPISODES_DIR / episode_id
    ep_file = ep_dir / "episode.json"
    if not ep_file.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    with open(ep_file) as f:
        return json.load(f)


def write_episode(episode_id: str, data: dict):
    """Write episode.json for a given episode."""
    ep_dir = EPISODES_DIR / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    with open(ep_dir / "episode.json", "w") as f:
        json.dump(data, f, indent=2)


@router.get("/")
async def list_episodes() -> list[dict]:
    """List all episodes with summary info."""
    if not EPISODES_DIR.exists():
        return []

    episodes = []
    for ep_dir in sorted(EPISODES_DIR.iterdir()):
        if not ep_dir.is_dir():
            continue
        ep_file = ep_dir / "episode.json"
        if not ep_file.exists():
            continue
        try:
            with open(ep_file) as f:
                ep = json.load(f)
            episodes.append({
                "episode_id": ep.get("episode_id", ep_dir.name),
                "title": ep.get("title", ep_dir.name),
                "status": ep.get("status", "processing"),
                "duration_seconds": ep.get("duration_seconds"),
                "created_at": ep.get("created_at"),
                "clips": ep.get("clips", []),
            })
        except (json.JSONDecodeError, OSError):
            continue

    return episodes


@router.post("/")
async def create_episode(req: NewEpisodeRequest) -> dict:
    """Trigger a new episode ingest."""
    now = datetime.now(timezone.utc)
    episode_id = f"ep_{now.strftime('%Y-%m-%d')}_{now.strftime('%H%M%S')}"

    episode = {
        "episode_id": episode_id,
        "title": "",
        "status": "processing",
        "source_path": req.source_path,
        "duration_seconds": None,
        "created_at": now.isoformat(),
        "clips": [],
        "pipeline": {
            "started_at": now.isoformat(),
            "completed_at": None,
            "agents_completed": [],
        },
    }

    write_episode(episode_id, episode)

    # Create subdirectories
    ep_dir = EPISODES_DIR / episode_id
    for sub in ["shorts", "subtitles", "metadata", "qa"]:
        (ep_dir / sub).mkdir(parents=True, exist_ok=True)

    return {"episode_id": episode_id, "status": "processing"}


@router.get("/{episode_id}")
async def get_episode(episode_id: str) -> dict:
    """Get full episode detail."""
    ep = read_episode(episode_id)

    # Also load clips.json if it exists
    clips_file = EPISODES_DIR / episode_id / "clips.json"
    if clips_file.exists() and not ep.get("clips"):
        try:
            with open(clips_file) as f:
                clips_data = json.load(f)
            ep["clips"] = clips_data.get("clips", clips_data) if isinstance(clips_data, dict) else clips_data
        except (json.JSONDecodeError, OSError):
            pass

    return ep


@router.post("/{episode_id}/approve")
async def approve_episode(episode_id: str) -> dict:
    """Approve the entire episode batch."""
    ep = read_episode(episode_id)
    ep["status"] = "approved"
    ep["approved_at"] = datetime.now(timezone.utc).isoformat()

    # Mark all pending clips as approved
    for clip in ep.get("clips", []):
        if clip.get("status", "pending") == "pending":
            clip["status"] = "approved"

    # Also update clips.json if it exists
    clips_file = EPISODES_DIR / episode_id / "clips.json"
    if clips_file.exists():
        try:
            with open(clips_file) as f:
                clips_data = json.load(f)
            clips_list = clips_data.get("clips", clips_data) if isinstance(clips_data, dict) else clips_data
            for clip in clips_list:
                if clip.get("status", "pending") == "pending":
                    clip["status"] = "approved"
            with open(clips_file, "w") as f:
                json.dump(clips_data, f, indent=2)
        except (json.JSONDecodeError, OSError):
            pass

    write_episode(episode_id, ep)
    return {"status": "approved", "episode_id": episode_id}
