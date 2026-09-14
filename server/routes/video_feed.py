"""Explicit dry-run and publication routes for the dedicated video RSS feed."""

from __future__ import annotations

import asyncio
import json

import httpx
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException

from agents.pipeline import load_config
from agents.video_feed import VideoFeedAgent
from lib.paths import get_episodes_dir
from server.routes import require_episode_dir
from server.routes.pipeline import _pipeline_lock, _running, _start_pipeline_thread

router = APIRouter(prefix="/api/episodes", tags=["video-feed"])
EPISODES_DIR = get_episodes_dir()


@router.post("/{episode_id}/delivery/video-feed/prepare")
async def prepare_video_feed(episode_id: str) -> dict:
    """Build a local preview from remote history without changing release state."""
    episode_dir = require_episode_dir(EPISODES_DIR, episode_id)
    before = (episode_dir / "episode.json").read_bytes()
    try:
        result = await asyncio.to_thread(
            VideoFeedAgent(episode_dir, load_config()).prepare
        )
    except (ClientError, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if (episode_dir / "episode.json").read_bytes() != before:
        raise HTTPException(
            status_code=500,
            detail="Video feed dry run unexpectedly changed release state",
        )
    return result


@router.post("/{episode_id}/delivery/video-feed/publish", status_code=202)
async def publish_video_feed(episode_id: str) -> dict:
    """Dispatch only the dedicated video-feed publisher for an approved release."""
    episode_dir = require_episode_dir(EPISODES_DIR, episode_id)
    async with _pipeline_lock:
        if episode_id in _running and _running[episode_id].is_alive():
            raise HTTPException(
                status_code=409, detail="Pipeline already running for this episode"
            )
        try:
            episode = json.loads((episode_dir / "episode.json").read_text())
            agent = VideoFeedAgent(episode_dir, load_config())
            await asyncio.to_thread(
                agent._current_inputs, require_publish_approval=True
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _start_pipeline_thread(
            episode_id,
            str(episode.get("source_path", "")),
            ["video_feed"],
        )
    return {
        "status": "video_feed_publishing",
        "episode_id": episode_id,
        "publication_agents": ["video_feed"],
    }
