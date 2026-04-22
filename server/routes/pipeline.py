"""Pipeline API endpoints — trigger and monitor the agent pipeline."""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from lib.atomic_write import atomic_write_json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
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


# ── Response models ─────────────────────────────────────────────────────────
# Typed responses so the frontend can read the contract from the Pydantic
# model instead of inferring it from handler bodies.


class PipelineActionResponse(BaseModel):
    """Generic response for pipeline-action endpoints that start work."""

    status: str  # e.g. "started", "resumed", "cancel_requested", "approved",
    # "backup_started", "longform_publishing", "shorts_publishing", "not_running",
    # "already_complete"
    episode_id: str


class ResumePipelineResponse(PipelineActionResponse):
    """Resume endpoint additionally reports the remaining agent list."""

    remaining_agents: Optional[list[str]] = None


class RunAgentResponse(BaseModel):
    status: str  # "completed"
    agent: str
    result: dict


class PipelineStatusResponse(BaseModel):
    episode_id: str
    status: str
    is_running: bool
    current_agent: Optional[str] = None
    agents_completed: list[str] = []
    agents_requested: Optional[list[str]] = None
    errors: dict = {}
    progress: Optional[dict] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@router.post("/{episode_id}/run-pipeline")
async def run_pipeline_endpoint(
    episode_id: str, req: RunPipelineRequest
) -> PipelineActionResponse:
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
) -> RunAgentResponse:
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
async def pipeline_status(episode_id: str) -> PipelineStatusResponse:
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
async def cancel_pipeline(episode_id: str) -> PipelineActionResponse:
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
async def resume_pipeline(episode_id: str) -> ResumePipelineResponse:
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
async def auto_approve(episode_id: str) -> PipelineActionResponse:
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
async def approve_backup(episode_id: str) -> PipelineActionResponse:
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
async def approve_longform(episode_id: str) -> PipelineActionResponse:
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
async def approve_publish(episode_id: str) -> PipelineActionResponse:
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


# ── Upload-Post URL polling ─────────────────────────────────────────────────
# After longform is submitted via Upload-Post, YouTube takes 15 min to several
# hours to process before the public URL is returned. Rather than force Sam to
# paste the URL by hand, this endpoint queries Upload-Post's status API for
# any pending request IDs on the episode and PATCHes episode.json with the
# URLs once they're live. Frontend polls this on a cadence.


class CheckUploadUrlsResponse(BaseModel):
    longform: dict = {}  # {"status": "pending" | "live" | "failed", "url": str | None}
    shorts: list[dict] = []  # per-clip {"clip_id": ..., "status": ..., "url": ...}


@router.post("/{episode_id}/check-upload-urls")
async def check_upload_urls(episode_id: str) -> CheckUploadUrlsResponse:
    """Poll Upload-Post for any pending request_ids on this episode and
    update episode.json.youtube_longform_url when the URL becomes available.

    Returns a per-submission status so the frontend can show "YouTube is
    still processing..." vs "Live on YouTube" without additional calls.
    """
    import os

    import httpx

    ep_dir = OUTPUT_DIR / episode_id
    episode_file = ep_dir / "episode.json"
    publish_file = ep_dir / "publish.json"

    if not episode_file.exists():
        raise HTTPException(status_code=404, detail=f"Episode not found: {episode_id}")
    if not publish_file.exists():
        return CheckUploadUrlsResponse(
            longform={"status": "not_submitted", "url": None}, shorts=[]
        )

    api_key = os.getenv("UPLOAD_POST_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500, detail="UPLOAD_POST_API_KEY not set in environment"
        )

    with open(publish_file) as f:
        publish_data = json.load(f)
    with open(episode_file) as f:
        episode = json.load(f)

    result = CheckUploadUrlsResponse()
    status_url = "https://api.upload-post.com/api/uploadposts/status"

    def _extract_youtube_url(resp_data: dict) -> Optional[str]:
        """Upload-Post's response shape varies; try several known paths."""
        # Direct URL at top level
        for key in ("video_url", "youtube_url", "url"):
            if resp_data.get(key):
                return resp_data[key]
        # Per-platform nested
        platforms = resp_data.get("platforms", {})
        if isinstance(platforms, dict):
            yt = platforms.get("youtube", {})
            if isinstance(yt, dict):
                for key in ("video_url", "url", "post_url"):
                    if yt.get(key):
                        return yt[key]
        return None

    # Longform check
    longform_res = publish_data.get("longform") or {}
    longform_status = longform_res.get("status")
    longform_request_id = longform_res.get("request_id")
    existing_url = episode.get("youtube_longform_url", "")

    if existing_url:
        result.longform = {"status": "live", "url": existing_url}
    elif longform_status == "submitted" and longform_request_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    status_url,
                    params={"request_id": longform_request_id},
                    headers={"Authorization": f"Apikey {api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            logger.warning(
                "Upload-Post status check failed for %s longform: %s",
                episode_id,
                e,
            )
            result.longform = {"status": "pending", "url": None, "error": str(e)}
        else:
            url = _extract_youtube_url(data)
            if url:
                episode["youtube_longform_url"] = url
                episode["youtube_longform_url_captured_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                atomic_write_json(episode_file, episode)
                result.longform = {"status": "live", "url": url}
            else:
                result.longform = {
                    "status": "pending",
                    "url": None,
                    "upload_post_state": data.get("status") or data.get("state"),
                }
    else:
        result.longform = {"status": longform_status or "not_submitted", "url": None}

    # Per-clip checks (best-effort; failures don't error the endpoint)
    for clip_result in publish_data.get("shorts", []):
        clip_id = clip_result.get("clip_id", "")
        clip_request_id = clip_result.get("request_id")
        if clip_result.get("status") != "submitted" or not clip_request_id:
            result.shorts.append(
                {
                    "clip_id": clip_id,
                    "status": clip_result.get("status", "unknown"),
                    "url": None,
                }
            )
            continue
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    status_url,
                    params={"request_id": clip_request_id},
                    headers={"Authorization": f"Apikey {api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
            url = _extract_youtube_url(data)
            result.shorts.append(
                {
                    "clip_id": clip_id,
                    "status": "live" if url else "pending",
                    "url": url,
                }
            )
        except httpx.HTTPError as e:
            result.shorts.append(
                {"clip_id": clip_id, "status": "pending", "url": None, "error": str(e)}
            )

    return result


# ── SSE event stream ────────────────────────────────────────────────────────
# Frontend's frontend/src/lib/events.ts expects to swap from polling to SSE
# in one line when this endpoint lands. Emits server-sent events of shape:
#   kind: "status" | "progress" | "agent_start" | "agent_done" | "agent_error"
# Implementation: watches episode.json + progress.json mtimes and re-reads
# on change. Lightweight fs polling inside the server so the frontend doesn't
# poll via HTTP.


@router.get("/{episode_id}/events")
async def pipeline_events(episode_id: str) -> StreamingResponse:
    """Server-sent-events stream of pipeline state transitions.

    Watches `<episode_dir>/episode.json` and `<episode_dir>/progress.json`
    for mtime changes and emits events. Keeps the connection open until
    the client disconnects. Emits an initial `status` event immediately
    so clients hydrate without a separate fetch.
    """
    ep_dir = OUTPUT_DIR / episode_id
    episode_file = ep_dir / "episode.json"
    progress_file = ep_dir / "progress.json"
    if not episode_file.exists():
        raise HTTPException(status_code=404, detail=f"Episode not found: {episode_id}")

    async def _event_stream():
        last_status: Optional[str] = None
        last_completed: list[str] = []
        last_progress_mtime: float = 0.0
        last_episode_mtime: float = 0.0

        def _emit(kind: str, data: dict) -> str:
            payload = json.dumps({"kind": kind, **data}, default=str)
            return f"event: {kind}\ndata: {payload}\n\n"

        try:
            while True:
                # Episode.json change detection
                try:
                    ep_mtime = episode_file.stat().st_mtime
                except FileNotFoundError:
                    yield _emit("error", {"detail": "episode.json vanished"})
                    break

                if ep_mtime != last_episode_mtime:
                    last_episode_mtime = ep_mtime
                    try:
                        with open(episode_file) as f:
                            episode = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        await asyncio.sleep(1)
                        continue

                    status = episode.get("status", "unknown")
                    completed = list(
                        episode.get("pipeline", {}).get("agents_completed", [])
                    )

                    if status != last_status:
                        yield _emit("status", {"status": status})
                        last_status = status

                    # Detect newly-completed agents vs previous snapshot
                    new_done = [a for a in completed if a not in last_completed]
                    for agent in new_done:
                        yield _emit("agent_done", {"agent": agent})
                    last_completed = completed

                    # Current agent (if any) as agent_start
                    current = episode.get("pipeline", {}).get("current_agent")
                    if current and current not in completed:
                        yield _emit("agent_start", {"agent": current})

                # Progress.json change detection (separate file, updated by
                # agents mid-run)
                if progress_file.exists():
                    try:
                        pg_mtime = progress_file.stat().st_mtime
                    except FileNotFoundError:
                        pg_mtime = 0
                    if pg_mtime and pg_mtime != last_progress_mtime:
                        last_progress_mtime = pg_mtime
                        try:
                            with open(progress_file) as f:
                                progress = json.load(f)
                            yield _emit("progress", progress)
                        except (json.JSONDecodeError, OSError):
                            pass

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )
