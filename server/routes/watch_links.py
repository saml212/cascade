"""Read-only exact-episode watch-link endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from agents.pipeline import load_config
from lib.paths import get_episodes_dir
from links.episode_hub import build_episode_watch_document, render_episode_page
from server.routes import require_episode_dir

router = APIRouter(prefix="/api/episodes", tags=["watch-links"])
EPISODES_DIR = get_episodes_dir()


def _watch_document(episode_id: str) -> dict:
    episode_dir = require_episode_dir(EPISODES_DIR, episode_id)
    try:
        return build_episode_watch_document(episode_dir, load_config())
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{episode_id}/watch-links")
async def watch_links(episode_id: str) -> dict:
    """Expose current revision-bound destinations without changing state."""
    return await asyncio.to_thread(_watch_document, episode_id)


@router.get("/{episode_id}/watch-page", response_class=HTMLResponse)
async def watch_page(episode_id: str) -> HTMLResponse:
    """Preview HTML for the current API link document."""
    document = await asyncio.to_thread(_watch_document, episode_id)
    return HTMLResponse(render_episode_page(document))
