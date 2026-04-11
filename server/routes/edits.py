"""Edits API — transcript-driven editing operations.

Endpoints:
    GET    /api/episodes/{id}/edits             list current edits
    POST   /api/episodes/{id}/edits             append a cut, trim_start, or trim_end
    DELETE /api/episodes/{id}/edits/{idx}       remove edit at index
    DELETE /api/episodes/{id}/edits             clear all edits
    POST   /api/episodes/{id}/edits/find        search transcript, return cut proposals
    POST   /api/episodes/{id}/edits/apply       trigger longform_render with current edits
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.editor import (
    add_cut,
    add_trim_start,
    add_trim_end,
    clear_edits,
    list_edits,
    remove_edit,
    total_time_removed,
    find_and_propose_cut,
)
from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes/{episode_id}/edits", tags=["edits"])

EPISODES_DIR = get_episodes_dir()


# ---------- Request/response models ----------

class AddEditRequest(BaseModel):
    type: str  # "cut" | "trim_start" | "trim_end"
    start_seconds: Optional[float] = None
    end_seconds: Optional[float] = None
    seconds: Optional[float] = None
    reason: str = ""


class FindRequest(BaseModel):
    query: str
    max_results: int = 5


# ---------- Helpers ----------

def _ep_dir(episode_id: str) -> Path:
    ep = EPISODES_DIR / episode_id
    if not ep.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return ep


# ---------- Endpoints ----------

@router.get("")
async def list_episode_edits(episode_id: str):
    """List the current edit list for an episode."""
    ep_dir = _ep_dir(episode_id)
    edits = list_edits(ep_dir)
    return {
        "episode_id": episode_id,
        "edits": edits,
        "count": len(edits),
        "total_time_removed_seconds": round(total_time_removed(edits), 3),
    }


@router.post("")
async def add_edit(episode_id: str, req: AddEditRequest):
    """Append a new edit (cut, trim_start, or trim_end)."""
    ep_dir = _ep_dir(episode_id)
    try:
        if req.type == "cut":
            if req.start_seconds is None or req.end_seconds is None:
                raise HTTPException(400, "cut requires start_seconds and end_seconds")
            edit = add_cut(ep_dir, req.start_seconds, req.end_seconds, req.reason)
        elif req.type == "trim_start":
            if req.seconds is None:
                raise HTTPException(400, "trim_start requires seconds")
            edit = add_trim_start(ep_dir, req.seconds, req.reason)
        elif req.type == "trim_end":
            if req.seconds is None:
                raise HTTPException(400, "trim_end requires seconds")
            edit = add_trim_end(ep_dir, req.seconds, req.reason)
        else:
            raise HTTPException(400, f"Unknown edit type: {req.type}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"edit": edit, "edits": list_edits(ep_dir)}


@router.delete("/{index}")
async def delete_edit(episode_id: str, index: int):
    """Remove the edit at the given index."""
    ep_dir = _ep_dir(episode_id)
    removed = remove_edit(ep_dir, index)
    if removed is None:
        raise HTTPException(404, f"No edit at index {index}")
    return {"removed": removed, "edits": list_edits(ep_dir)}


@router.delete("")
async def clear_episode_edits(episode_id: str):
    """Remove ALL edits for an episode."""
    ep_dir = _ep_dir(episode_id)
    n = clear_edits(ep_dir)
    return {"cleared": n, "edits": []}


@router.post("/find")
async def find_in_transcript(episode_id: str, req: FindRequest):
    """Search the diarized transcript for `query`. Returns cut proposals.

    Each proposal has start/end timestamps (sentence-aligned), context, and
    score. The user picks one and POSTs to /edits to commit it.
    """
    ep_dir = _ep_dir(episode_id)
    try:
        proposals = find_and_propose_cut(ep_dir, req.query, max_results=req.max_results)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))

    return {
        "episode_id": episode_id,
        "query": req.query,
        "proposals": proposals,
        "count": len(proposals),
    }


@router.post("/apply")
async def apply_edits(episode_id: str):
    """Trigger longform_render to apply the current edit list.

    Cleans up cached longform render intermediates so the new edits actually
    take effect, then spawns longform_render in a background thread.
    """
    import threading

    from agents.pipeline import run_pipeline

    ep_dir = _ep_dir(episode_id)

    # Clear cached longform render intermediates so the next render uses
    # the current edits, not stale segment files.
    work_dir = ep_dir / "work"
    if work_dir.exists():
        for pattern in ("longform_seg_*.mp4", "longform_raw.mp4", "longform_concat.txt"):
            for f in work_dir.glob(pattern):
                try:
                    f.unlink()
                except OSError:
                    pass

    # Read source/audio paths from episode.json so the pipeline can resume
    import json
    ep_file = ep_dir / "episode.json"
    with open(ep_file) as f:
        ep_data = json.load(f)
    source_path = ep_data.get("source_path", "")
    audio_path = ep_data.get("audio_path")

    def _run():
        run_pipeline(
            source_path=source_path,
            audio_path=audio_path,
            episode_id=episode_id,
            agents=["longform_render"],
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {
        "episode_id": episode_id,
        "status": "started",
        "agents": ["longform_render"],
    }
