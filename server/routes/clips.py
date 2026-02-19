"""Clip review endpoints."""

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.clips import normalize_clip as _normalize_clip, load_clips as _load_clips_from_dir, save_clips as _save_clips_to_dir
from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes/{episode_id}/clips", tags=["clips"])

EPISODES_DIR = get_episodes_dir()


class ManualClipRequest(BaseModel):
    start_seconds: float
    end_seconds: float


class MetadataUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    hashtags: Optional[str] = None
    start_seconds: Optional[float] = None
    end_seconds: Optional[float] = None
    metadata: Optional[dict] = None


def load_clips(episode_id: str) -> tuple:
    """Load clips from clips.json, falling back to episode.json."""
    ep_dir = EPISODES_DIR / episode_id
    clips_file = ep_dir / "clips.json"
    if not ep_dir.exists():
        return [], clips_file

    clips = _load_clips_from_dir(ep_dir)
    if clips:
        return clips, clips_file

    # Fallback to episode.json
    ep_file = ep_dir / "episode.json"
    if ep_file.exists():
        with open(ep_file) as f:
            ep = json.load(f)
        return [_normalize_clip(c) for c in ep.get("clips", [])], clips_file

    return [], clips_file


def save_clips(clips: list, clips_file: Path):
    """Save clips list to clips.json."""
    _save_clips_to_dir(clips_file.parent, clips)


def find_clip(clips: list, clip_id: str) -> tuple[dict, int]:
    """Find a clip by ID, raise 404 if not found."""
    for i, clip in enumerate(clips):
        if clip.get("id") == clip_id:
            return clip, i
    raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")


@router.get("")
@router.get("/")
async def list_clips(episode_id: str) -> list[dict]:
    """List all clip candidates."""
    logger.info("GET /api/episodes/%s/clips", episode_id)
    clips, _ = load_clips(episode_id)
    return clips


@router.get("/{clip_id}")
async def get_clip(episode_id: str, clip_id: str) -> dict:
    """Get single clip detail."""
    clips, _ = load_clips(episode_id)
    clip, _ = find_clip(clips, clip_id)
    return clip


@router.post("/{clip_id}/approve")
async def approve_clip(episode_id: str, clip_id: str) -> dict:
    """Approve a clip."""
    logger.info("POST /api/episodes/%s/clips/%s/approve", episode_id, clip_id)
    clips, clips_file = load_clips(episode_id)
    clip, idx = find_clip(clips, clip_id)
    clip["status"] = "approved"
    clips[idx] = clip
    save_clips(clips, clips_file)
    return {"status": "approved", "clip_id": clip_id}


@router.post("/{clip_id}/reject")
async def reject_clip(episode_id: str, clip_id: str) -> dict:
    """Reject a clip."""
    logger.info("POST /api/episodes/%s/clips/%s/reject", episode_id, clip_id)
    clips, clips_file = load_clips(episode_id)
    clip, idx = find_clip(clips, clip_id)
    clip["status"] = "rejected"
    clips[idx] = clip
    save_clips(clips, clips_file)
    return {"status": "rejected", "clip_id": clip_id}


@router.post("/{clip_id}/alternative")
async def request_alternative(episode_id: str, clip_id: str) -> dict:
    """Request a Claude-generated alternative clip.

    This is a placeholder — the full implementation will call the Anthropic API
    with the transcript, excluding the rejected clip's time range, and ask for
    a replacement clip suggestion.
    """
    clips, _ = load_clips(episode_id)
    clip, _ = find_clip(clips, clip_id)

    return {
        "message": "Alternative clip requested. This feature requires the full pipeline to be running with a valid ANTHROPIC_API_KEY.",
        "rejected_clip_id": clip_id,
        "excluded_range": {
            "start": clip.get("start"),
            "end": clip.get("end"),
        },
    }


@router.post("/manual")
async def add_manual_clip(episode_id: str, req: ManualClipRequest) -> dict:
    """Add a custom clip by specifying start and end timestamps."""
    if req.end_seconds <= req.start_seconds:
        raise HTTPException(status_code=400, detail="end_seconds must be greater than start_seconds")

    duration = req.end_seconds - req.start_seconds
    if duration < 5 or duration > 300:
        raise HTTPException(status_code=400, detail="Clip duration must be between 5 and 300 seconds")

    clips, clips_file = load_clips(episode_id)

    # Generate clip ID
    existing_ids = {c.get("id", "") for c in clips}
    clip_num = len(clips) + 1
    while f"clip_{clip_num:02d}" in existing_ids:
        clip_num += 1
    clip_id = f"clip_{clip_num:02d}"

    new_clip = {
        "id": clip_id,
        "rank": len(clips) + 1,
        "start": req.start_seconds,
        "end": req.end_seconds,
        "duration": duration,
        "title": f"Custom clip ({int(req.start_seconds // 60)}:{int(req.start_seconds % 60):02d}–{int(req.end_seconds // 60)}:{int(req.end_seconds % 60):02d})",
        "hook_text": "",
        "compelling_reason": "Manually specified by user",
        "virality_score": 0,
        "speaker": "BOTH",
        "status": "pending",
        "manual": True,
    }

    clips.append(new_clip)
    save_clips(clips, clips_file)
    return new_clip


@router.patch("/{clip_id}/metadata")
async def update_clip_metadata(episode_id: str, clip_id: str, update: MetadataUpdate) -> dict:
    """Update clip metadata (title, description, hashtags, time range, per-platform metadata)."""
    clips, clips_file = load_clips(episode_id)
    clip, idx = find_clip(clips, clip_id)

    if update.title is not None:
        clip["title"] = update.title
    if update.description is not None:
        clip["description"] = update.description
    if update.hashtags is not None:
        clip["hashtags"] = update.hashtags
    if update.start_seconds is not None:
        clip["start"] = update.start_seconds
    if update.end_seconds is not None:
        clip["end"] = update.end_seconds
    if update.start_seconds is not None or update.end_seconds is not None:
        clip["duration"] = clip.get("end", 0) - clip.get("start", 0)
    if update.metadata is not None:
        existing_meta = clip.get("metadata", {})
        existing_meta.update(update.metadata)
        clip["metadata"] = existing_meta

    clips[idx] = clip
    save_clips(clips, clips_file)
    return clip
