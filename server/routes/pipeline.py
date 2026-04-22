"""Pipeline API endpoints — trigger and monitor the agent pipeline."""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from lib.atomic_write import atomic_write_json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes", tags=["pipeline"])

OUTPUT_DIR = get_episodes_dir()

# Track running pipelines and cancellation
_running = {}  # type: dict
_cancel_requested = set()  # type: set
_pipeline_lock = asyncio.Lock()


class RunPipelineRequest(BaseModel):
    source_path: Optional[str] = None
    audio_path: Optional[str] = None
    agents: Optional[list[str]] = None


class RunAgentRequest(BaseModel):
    source_path: Optional[str] = None


@router.post("/{episode_id}/run-pipeline")
async def run_pipeline_endpoint(episode_id: str, req: RunPipelineRequest) -> dict:
    """Trigger the full pipeline as a background task."""
    logger.info("POST /api/episodes/%s/run-pipeline", episode_id)
    async with _pipeline_lock:
        if episode_id in _running and _running[episode_id].is_alive():
            raise HTTPException(
                status_code=409, detail="Pipeline already running for this episode"
            )

        # Resolve source_path and audio_path: use request value, fall back to episode.json
        source_path = req.source_path
        audio_path = req.audio_path
        if not source_path or not audio_path:
            episode_file = OUTPUT_DIR / episode_id / "episode.json"
            if episode_file.exists():
                with open(episode_file) as f:
                    ep_data = json.load(f)
                if not source_path:
                    source_path = ep_data.get("source_path", "")
                if not audio_path:
                    audio_path = ep_data.get("audio_path")
        if not source_path:
            raise HTTPException(
                status_code=400,
                detail="source_path required (not found in request or episode.json)",
            )

        def _run():
            from agents.pipeline import run_pipeline

            run_pipeline(
                source_path=source_path,
                audio_path=audio_path,
                episode_id=episode_id,
                agents=req.agents,
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _running[episode_id] = thread

    logger.info("Pipeline started for %s", episode_id)
    return {"status": "started", "episode_id": episode_id}


@router.post("/{episode_id}/run-agent/{agent_name}")
async def run_single_agent(
    episode_id: str, agent_name: str, req: RunAgentRequest
) -> dict:
    """Run a single agent for an episode."""
    logger.info("POST /api/episodes/%s/run-agent/%s", episode_id, agent_name)
    from agents import AGENT_REGISTRY
    from agents.pipeline import load_config

    if agent_name not in AGENT_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")

    episode_dir = OUTPUT_DIR / episode_id
    if not episode_dir.exists():
        raise HTTPException(
            status_code=404, detail=f"Episode directory not found: {episode_id}"
        )

    config = load_config()
    agent_cls = AGENT_REGISTRY[agent_name]
    agent = agent_cls(episode_dir, config)

    if agent_name == "ingest" and req.source_path:
        agent.source_path = req.source_path

    result = agent.run()
    return {"status": "completed", "agent": agent_name, "result": result}


@router.get("/{episode_id}/pipeline-status")
async def pipeline_status(episode_id: str) -> dict:
    """Get current pipeline status for an episode."""
    logger.info("GET /api/episodes/%s/pipeline-status", episode_id)
    episode_file = OUTPUT_DIR / episode_id / "episode.json"
    if not episode_file.exists():
        raise HTTPException(status_code=404, detail=f"Episode not found: {episode_id}")

    with open(episode_file) as f:
        episode = json.load(f)

    pipeline = episode.get("pipeline", {})
    is_running = episode_id in _running and _running[episode_id].is_alive()

    # Read progress.json if it exists
    progress = None
    progress_file = OUTPUT_DIR / episode_id / "progress.json"
    if progress_file.exists():
        try:
            with open(progress_file) as pf:
                progress = json.load(pf)
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "episode_id": episode_id,
        "status": episode.get("status", "unknown"),
        "is_running": is_running,
        "current_agent": pipeline.get("current_agent"),
        "agents_completed": pipeline.get("agents_completed", []),
        "agents_requested": pipeline.get("agents_requested"),
        "errors": pipeline.get("errors", {}),
        "progress": progress,
        "started_at": pipeline.get("started_at"),
        "completed_at": pipeline.get("completed_at"),
    }


@router.post("/{episode_id}/cancel-pipeline")
async def cancel_pipeline(episode_id: str) -> dict:
    """Request cancellation of a running pipeline."""
    logger.info("POST /api/episodes/%s/cancel-pipeline", episode_id)
    async with _pipeline_lock:
        is_running = episode_id in _running and _running[episode_id].is_alive()
        if is_running:
            _cancel_requested.add(episode_id)

    if not is_running:
        # Even if not running, update status if still "processing"
        episode_file = OUTPUT_DIR / episode_id / "episode.json"
        if episode_file.exists():
            with open(episode_file) as f:
                episode = json.load(f)
            if episode.get("status") == "processing":
                episode["status"] = "cancelled"
                episode["pipeline"].pop("current_agent", None)
                atomic_write_json(episode_file, episode)
        return {"status": "not_running", "episode_id": episode_id}

    # Update episode status immediately
    episode_file = OUTPUT_DIR / episode_id / "episode.json"
    if episode_file.exists():
        with open(episode_file) as f:
            episode = json.load(f)
        episode["status"] = "cancelled"
        episode["pipeline"].pop("current_agent", None)
        atomic_write_json(episode_file, episode)

    logger.info("Pipeline cancellation requested for %s", episode_id)
    return {"status": "cancel_requested", "episode_id": episode_id}


@router.post("/{episode_id}/resume-pipeline")
async def resume_pipeline(episode_id: str) -> dict:
    """Resume pipeline after crop setup — runs remaining agents."""
    logger.info("POST /api/episodes/%s/resume-pipeline", episode_id)
    async with _pipeline_lock:
        if episode_id in _running and _running[episode_id].is_alive():
            raise HTTPException(
                status_code=409, detail="Pipeline already running for this episode"
            )

        episode_file = OUTPUT_DIR / episode_id / "episode.json"
        if not episode_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Episode not found: {episode_id}"
            )

        with open(episode_file) as f:
            episode = json.load(f)

        completed = set(episode.get("pipeline", {}).get("agents_completed", []))
        source_path = episode.get("source_path", "")

        from agents import PIPELINE_ORDER

        remaining = [a for a in PIPELINE_ORDER if a not in completed]

        if not remaining:
            return {"status": "already_complete", "episode_id": episode_id}

        def _run():
            from agents.pipeline import run_pipeline

            run_pipeline(
                source_path=source_path,
                episode_id=episode_id,
                agents=remaining,
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _running[episode_id] = thread

    logger.info("Pipeline resumed for %s with agents: %s", episode_id, remaining)
    return {
        "status": "resumed",
        "episode_id": episode_id,
        "remaining_agents": remaining,
    }


@router.post("/{episode_id}/auto-approve")
async def auto_approve(episode_id: str) -> dict:
    """Auto-approve all clips for an episode (skip manual review)."""
    logger.info("POST /api/episodes/%s/auto-approve", episode_id)
    episode_file = OUTPUT_DIR / episode_id / "episode.json"
    if not episode_file.exists():
        raise HTTPException(status_code=404, detail=f"Episode not found: {episode_id}")

    with open(episode_file) as f:
        episode = json.load(f)

    # Approve all clips in episode.json
    for clip in episode.get("clips", []):
        if clip.get("status", "pending") == "pending":
            clip["status"] = "approved"

    episode["status"] = "approved"
    episode["approved_at"] = datetime.now(timezone.utc).isoformat()

    atomic_write_json(episode_file, episode)

    # Also approve in clips.json
    clips_file = OUTPUT_DIR / episode_id / "clips.json"
    if clips_file.exists():
        with open(clips_file) as f:
            clips_data = json.load(f)
        for clip in clips_data.get("clips", []):
            if clip.get("status", "pending") == "pending":
                clip["status"] = "approved"
        atomic_write_json(clips_file, clips_data)

    return {"status": "approved", "episode_id": episode_id}


@router.post("/{episode_id}/approve-backup")
async def approve_backup(episode_id: str) -> dict:
    """Approve backup + SD card cleanup, then resume pipeline to run backup agent."""
    logger.info("POST /api/episodes/%s/approve-backup", episode_id)
    async with _pipeline_lock:
        if episode_id in _running and _running[episode_id].is_alive():
            raise HTTPException(
                status_code=409, detail="Pipeline already running for this episode"
            )

        episode_file = OUTPUT_DIR / episode_id / "episode.json"
        if not episode_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Episode not found: {episode_id}"
            )

        with open(episode_file) as f:
            episode = json.load(f)

        episode["backup_approved"] = True
        episode["backup_approved_at"] = datetime.now(timezone.utc).isoformat()
        episode["status"] = "processing"

        atomic_write_json(episode_file, episode)

        # Resume pipeline with just backup
        source_path = episode.get("source_path", "")

        def _run():
            from agents.pipeline import run_pipeline

            run_pipeline(
                source_path=source_path,
                episode_id=episode_id,
                agents=["backup"],
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _running[episode_id] = thread

    logger.info("Backup approved and started for %s", episode_id)
    return {"status": "backup_started", "episode_id": episode_id}


@router.post("/{episode_id}/approve-longform")
async def approve_longform(episode_id: str) -> dict:
    """Approve the longform render — publishes the longform FIRST.

    Fires podcast_feed (triggers Spotify RSS ingest) + publish (uploads
    longform to YouTube via Upload-Post). Shorts do NOT render here — the
    shorts_render agent is hard-gated on episode.json.youtube_longform_url
    being set, which only happens after YouTube returns the processed URL
    (15 min to several hours after submit).

    After this route:
    1. Longform uploads to YouTube (publish.py loops over clips, skips them
       because the shorts/ dir is empty; then uploads longform).
    2. RSS updates, Spotify auto-ingests.
    3. /produce polls for YouTube URL OR Sam pastes it in.
    4. /produce fires resume-pipeline with ["shorts_render", "metadata_gen",
       "thumbnail_gen", "qa"] to produce the shorts (URL now known).
    5. Sam reviews clips + metadata.
    6. /produce fires approve-publish → shorts upload (longform idempotently
       skips because youtube_longform_url is set).
    """
    logger.info("POST /api/episodes/%s/approve-longform", episode_id)
    async with _pipeline_lock:
        if episode_id in _running and _running[episode_id].is_alive():
            raise HTTPException(status_code=409, detail="Pipeline already running")

        episode_file = OUTPUT_DIR / episode_id / "episode.json"
        if not episode_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Episode not found: {episode_id}"
            )

        with open(episode_file) as f:
            episode = json.load(f)

        # Both flags must be set: longform_approved unpauses the
        # awaiting_longform_approval gate, publish_approved passes publish.py's
        # safety gate so the longform upload can fire.
        now = datetime.now(timezone.utc).isoformat()
        episode["longform_approved"] = True
        episode["longform_approved_at"] = now
        episode["publish_approved"] = True
        episode["publish_approved_at"] = now
        episode["status"] = "processing"
        atomic_write_json(episode_file, episode)

        source_path = episode.get("source_path", "")

        def _run():
            from agents.pipeline import run_pipeline

            run_pipeline(
                source_path=source_path,
                episode_id=episode_id,
                agents=["podcast_feed", "publish"],
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _running[episode_id] = thread

    logger.info("Longform approved, publishing longform for %s", episode_id)
    return {"status": "longform_publishing", "episode_id": episode_id}


@router.post("/{episode_id}/approve-publish")
async def approve_publish(episode_id: str) -> dict:
    """Approve publishing the SHORTS (phase 4 of the flow).

    Assumes longform is already live (approve-longform was fired earlier,
    publish_approved is already True, youtube_longform_url is saved to
    episode.json). Shorts are already rendered with metadata.

    Fires publish only — the longform upload block in publish.py is
    idempotent and will skip because youtube_longform_url is set. Shorts
    upload with youtube_first_comment referencing the longform URL.
    """
    logger.info("POST /api/episodes/%s/approve-publish", episode_id)
    async with _pipeline_lock:
        if episode_id in _running and _running[episode_id].is_alive():
            raise HTTPException(
                status_code=409, detail="Pipeline already running for this episode"
            )

        episode_file = OUTPUT_DIR / episode_id / "episode.json"
        if not episode_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Episode not found: {episode_id}"
            )

        with open(episode_file) as f:
            episode = json.load(f)

        # publish_approved should already be set by approve-longform; set
        # defensively in case this route is called directly.
        episode["publish_approved"] = True
        if not episode.get("publish_approved_at"):
            episode["publish_approved_at"] = datetime.now(timezone.utc).isoformat()
        episode["status"] = "processing"

        atomic_write_json(episode_file, episode)

        source_path = episode.get("source_path", "")

        def _run():
            from agents.pipeline import run_pipeline

            run_pipeline(
                source_path=source_path,
                episode_id=episode_id,
                agents=["publish"],
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _running[episode_id] = thread

    logger.info("Shorts publish approved and started for %s", episode_id)
    return {"status": "shorts_publishing", "episode_id": episode_id}
