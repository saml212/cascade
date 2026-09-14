"""Compatibility endpoint for non-destructive episode trims."""

from __future__ import annotations

import json
import logging
import math
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.ffprobe import get_duration
from lib.paths import get_episodes_dir
from lib.timeline import Timeline
from server.routes import require_episode_dir
from server.routes.delivery import DeliveryTrimRequest, save_delivery_trim

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/episodes/{episode_id}", tags=["trim"])
EPISODES_DIR = get_episodes_dir()


class TrimRequest(BaseModel):
    trim_start_seconds: float = 0.0
    trim_end_seconds: float = 0.0


def _terminal_bounds(episode_dir: Path, duration: float) -> tuple[float, float]:
    try:
        episode = json.loads((episode_dir / "episode.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        episode = {}
    trims = {
        edit.get("type"): edit
        for edit in episode.get("longform_edits", [])
        if edit.get("type") in {"trim_start", "trim_end"}
    }
    return (
        float(trims.get("trim_start", {}).get("seconds", 0)),
        float(trims.get("trim_end", {}).get("seconds", duration)),
    )


@router.post("/trim")
async def trim_episode(episode_id: str, req: TrimRequest) -> dict:
    """Save legacy trim requests as source-clock edit bounds."""
    episode_dir = require_episode_dir(EPISODES_DIR, episode_id)
    source = episode_dir / "source_merged.mp4"
    if not source.is_file():
        raise HTTPException(status_code=404, detail="source_merged.mp4 not found")
    if not math.isfinite(req.trim_start_seconds) or not math.isfinite(
        req.trim_end_seconds
    ):
        raise HTTPException(status_code=400, detail="Trim values must be finite")
    if req.trim_start_seconds < 0 or req.trim_end_seconds < 0:
        raise HTTPException(status_code=400, detail="Trim values must be non-negative")
    try:
        duration = get_duration(source)
    except (subprocess.CalledProcessError, KeyError, ValueError) as exc:
        logger.error("Failed to probe source file for %s: %s", episode_id, exc)
        raise HTTPException(
            status_code=500, detail=f"Could not probe source file: {exc}"
        ) from exc

    start = req.trim_start_seconds
    end = req.trim_end_seconds if req.trim_end_seconds > 0 else duration
    end = min(end, duration)
    if start >= end:
        raise HTTPException(
            status_code=400, detail="Trim start must be before trim end"
        )

    current_start, current_end = _terminal_bounds(episode_dir, duration)
    if math.isclose(start, current_start, abs_tol=0.0005) and math.isclose(
        end, current_end, abs_tol=0.0005
    ):
        return {
            "status": "noop",
            "message": "Trim matched existing bounds — nothing to do.",
            "duration_seconds": end - start,
            "source_unchanged": True,
        }

    status = await save_delivery_trim(
        episode_id,
        DeliveryTrimRequest(start_seconds=start, end_seconds=end),
    )
    episode = json.loads((episode_dir / "episode.json").read_text())
    retained_duration = Timeline.from_edits(
        duration, episode.get("longform_edits", [])
    ).duration
    original = episode_dir / "source_merged_original.mp4"
    return {
        "status": "saved",
        "new_duration": retained_duration,
        "duration_seconds": retained_duration,
        "terminal_range_duration_seconds": end - start,
        "backup_path": str(original) if original.is_file() else None,
        "source_unchanged": True,
        "delivery": status,
    }
