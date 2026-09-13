"""Pipeline API endpoints — trigger and monitor the agent pipeline."""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents.qa import editorial_revision, quality_snapshot
from lib.atomic_write import atomic_write_json
from lib.audio_mix import selected_audio_source
from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes", tags=["pipeline"])

OUTPUT_DIR = get_episodes_dir()

# Track running pipelines and cancellation
_running = {}  # type: dict
_cancel_requested = set()  # type: set
_pipeline_lock = asyncio.Lock()


class _SingleAgentWorker(threading.Thread):
    pass


def _start_pipeline_thread(
    episode_id: str,
    source_path: str,
    agents: list[str] | None,
    *,
    audio_path: str | None = None,
) -> None:
    """Start and register one background pipeline with a single launch contract."""

    def _run() -> None:
        from agents.pipeline import run_pipeline

        run_pipeline(
            source_path=source_path,
            audio_path=audio_path,
            episode_id=episode_id,
            agents=agents,
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _running[episode_id] = thread


async def _unregister_single_agent(
    episode_id: str,
    worker: _SingleAgentWorker,
    *,
    work_complete: bool = False,
) -> None:
    async with _pipeline_lock:
        if _running.get(episode_id) is worker and (
            work_complete or not worker.is_alive()
        ):
            _running.pop(episode_id, None)
            _cancel_requested.discard(episode_id)


def _current_longform_for_approval(episode_dir: Path, episode: dict) -> dict | None:
    """Resolve the canonical current render required by editorial approval."""
    from agents.pipeline import load_config
    from agents.speaker_cut import current_speaker_segments
    from lib.delivery_video import current_longform_render

    config = load_config()
    try:
        segment_document = current_speaker_segments(episode_dir, episode, config)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        return None
    try:
        audio = selected_audio_source(episode_dir, episode, config) or (
            episode_dir / "work" / "audio_mix.wav"
        )
    except ValueError:
        return None
    if not segment_document or not audio.is_file():
        return None
    try:
        return current_longform_render(
            episode_dir,
            episode,
            config,
            audio,
            segment_document.get("segments", []),
        )
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        return None


class RunPipelineRequest(BaseModel):
    source_path: Optional[str] = None
    audio_path: Optional[str] = None
    agents: Optional[list[str]] = None


class ResumePipelineRequest(BaseModel):
    agents: Optional[list[str]] = None


class RunAgentRequest(BaseModel):
    source_path: Optional[str] = None


class ApproveLongformRequest(BaseModel):
    continue_production: bool = True


class ApprovePublishRequest(BaseModel):
    start_publication: bool = True


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


class ApproveLongformResponse(PipelineActionResponse):
    production_started: bool


class ApprovePublishResponse(PipelineActionResponse):
    publication_started: bool
    publication_agents: list[str]


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

        _start_pipeline_thread(
            episode_id,
            source_path,
            req.agents,
            audio_path=audio_path,
        )

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

    outcome = {}
    loop = asyncio.get_running_loop()

    def _run() -> None:
        try:
            outcome["result"] = agent.run()
        except Exception as error:  # noqa: BLE001 - re-raised after the worker joins
            outcome["error"] = error
        finally:
            asyncio.run_coroutine_threadsafe(
                _unregister_single_agent(episode_id, worker, work_complete=True), loop
            )

    worker = _SingleAgentWorker(target=_run, daemon=True)
    async with _pipeline_lock:
        current = _running.get(episode_id)
        if current is not None and current.is_alive():
            raise HTTPException(
                status_code=409, detail="Pipeline already running for this episode"
            )
        _cancel_requested.discard(episode_id)
        _running[episode_id] = worker
        worker.start()
    try:
        await asyncio.to_thread(worker.join)
    finally:
        await _unregister_single_agent(episode_id, worker)

    if "error" in outcome:
        raise outcome["error"]
    result = outcome["result"]
    if agent_name == "qa" and result.get("overall") != "pass":
        failed_checks = [
            check.get("name", "unknown")
            for check in result.get("checks", [])
            if not check.get("pass")
        ]
        raise HTTPException(
            status_code=422,
            detail={
                "message": "QA release gate failed",
                "failed_checks": failed_checks,
                "quality_url": f"/api/episodes/{episode_id}/quality",
            },
        )
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
        worker = _running.get(episode_id)
        is_running = worker is not None and worker.is_alive()
        if is_running and isinstance(worker, _SingleAgentWorker):
            raise HTTPException(
                status_code=409,
                detail="A single-agent run is active and cannot be cancelled",
            )
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
async def resume_pipeline(
    episode_id: str, req: Optional[ResumePipelineRequest] = None
) -> ResumePipelineResponse:
    """Resume pipeline — runs the agents the caller requests, in PIPELINE_ORDER.

    If `agents` is provided in the body, we run only those (filtered to ones
    not already completed). This is what /produce relies on to gate
    paid-API stages: it dispatches subagents to produce clips.json /
    metadata.json, then resumes with an explicit list so the cost-locked
    agents never run automatically. If `agents` is omitted we run every
    remaining local-production agent. Publication agents require an explicit
    request and a current release approval.
    """
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
        from agents.pipeline import EXPLICIT_PUBLICATION_AGENTS

        requested = req.agents if req is not None else None
        if requested is not None:
            unknown = [a for a in requested if a not in PIPELINE_ORDER]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown agent(s): {unknown}",
                )
            # Preserve canonical pipeline order, drop already-completed.
            remaining = [
                a for a in PIPELINE_ORDER if a in requested and a not in completed
            ]
        else:
            remaining = [
                agent
                for agent in PIPELINE_ORDER
                if agent not in completed and agent not in EXPLICIT_PUBLICATION_AGENTS
            ]

        if not remaining:
            return {"status": "already_complete", "episode_id": episode_id}

        _start_pipeline_thread(episode_id, source_path, remaining)

    logger.info("Pipeline resumed for %s with agents: %s", episode_id, remaining)
    return {
        "status": "resumed",
        "episode_id": episode_id,
        "remaining_agents": remaining,
    }


@router.post("/{episode_id}/auto-approve")
async def auto_approve(episode_id: str) -> PipelineActionResponse:
    """Approve all exact current short renders as one atomic review decision."""
    logger.info("POST /api/episodes/%s/auto-approve", episode_id)
    episode_file = OUTPUT_DIR / episode_id / "episode.json"
    if not episode_file.exists():
        raise HTTPException(status_code=404, detail=f"Episode not found: {episode_id}")
    from server.routes.clips import BulkClipRequest, approve_clips, load_clips

    clips, _ = load_clips(episode_id)
    await approve_clips(
        episode_id,
        BulkClipRequest(
            clip_ids=[
                str(clip["id"])
                for clip in clips
                if clip.get("id") and clip.get("status") != "rejected"
            ]
        ),
    )
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

        _start_pipeline_thread(episode_id, source_path, ["backup"])

    logger.info("Backup approved and started for %s", episode_id)
    return {"status": "backup_started", "episode_id": episode_id}


@router.post("/{episode_id}/approve-longform")
async def approve_longform(
    episode_id: str, request: ApproveLongformRequest | None = None
) -> ApproveLongformResponse:
    """Approve the current edit, optionally continuing local production."""
    request = request or ApproveLongformRequest()
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

        if not _current_longform_for_approval(episode_file.parent, episode):
            raise HTTPException(
                status_code=409,
                detail="A current speaker-cut longform render is required before approval",
            )

        now = datetime.now(timezone.utc).isoformat()
        episode["longform_approved"] = True
        episode["longform_approved_at"] = now
        episode["editorial_approval"] = {
            "revision": editorial_revision(episode_file.parent, episode),
            "approved_at": now,
        }
        episode.pop("publish_approved", None)
        episode.pop("publish_approved_at", None)
        episode.pop("publish_approval", None)
        if request.continue_production:
            episode["status"] = "processing"
        atomic_write_json(episode_file, episode)

        if request.continue_production:
            _start_pipeline_thread(
                episode_id,
                episode.get("source_path", ""),
                [
                    "clip_miner",
                    "shorts_render",
                    "metadata_gen",
                    "thumbnail_gen",
                    "qa",
                ],
            )

    logger.info(
        "Longform approved for %s; production_started=%s",
        episode_id,
        request.continue_production,
    )
    return {
        "status": "approved",
        "episode_id": episode_id,
        "production_started": request.continue_production,
    }


@router.post("/{episode_id}/approve-publish")
async def approve_publish(
    episode_id: str, request: ApprovePublishRequest | None = None
) -> ApprovePublishResponse:
    """Approve the current release, optionally starting legacy publishers."""
    request = request or ApprovePublishRequest()
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

        snapshot = quality_snapshot(episode_file.parent)
        gate = snapshot["release_gate"]
        if not gate["can_approve_publish"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Release prerequisites are not satisfied",
                    "quality_url": f"/api/episodes/{episode_id}/quality",
                    "blockers": gate["blockers"],
                },
            )
        now = datetime.now(timezone.utc).isoformat()
        plan = gate["publish_plan"]
        publication_agents = []
        if plan["upload_post"]["enabled"]:
            publication_agents.append("publish")
        if plan["podcast_rss"]["enabled"]:
            publication_agents.append("podcast_feed")
        video_rss_plan = plan.get("video_podcast_rss", {"enabled": False})
        if not publication_agents and not video_rss_plan.get("enabled"):
            raise HTTPException(
                status_code=409,
                detail="No publication destinations are enabled",
            )
        if request.start_publication and not publication_agents:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Video RSS publication uses its dedicated endpoint; record approval "
                    "with start_publication=false first"
                ),
            )
        configuration_blockers = []
        if plan["upload_post"].get("enabled") and not plan["upload_post"].get(
            "account_identity"
        ):
            configuration_blockers.append("UPLOAD_POST_USER is not configured")
        rss_plan = plan["podcast_rss"]
        if rss_plan.get("enabled") and not rss_plan.get("account_identity"):
            configuration_blockers.append("CLOUDFLARE_ACCOUNT_ID is not configured")
        if rss_plan.get("enabled") and not rss_plan.get("destination_configured"):
            configuration_blockers.append("Podcast R2 destination is not configured")
        if rss_plan.get("enabled") and not rss_plan.get("channel_configured"):
            configuration_blockers.append("Podcast channel metadata is incomplete")
        if video_rss_plan.get("enabled") and not video_rss_plan.get("account_identity"):
            configuration_blockers.append("CLOUDFLARE_ACCOUNT_ID is not configured")
        if video_rss_plan.get("enabled") and not video_rss_plan.get(
            "destination_configured"
        ):
            configuration_blockers.append(
                "Video podcast R2 destination is not configured"
            )
        if video_rss_plan.get("enabled") and not video_rss_plan.get(
            "channel_configured"
        ):
            configuration_blockers.append(
                "Video podcast channel metadata is incomplete"
            )
        if video_rss_plan.get("enabled") and not video_rss_plan.get(
            "episode_configured"
        ):
            configuration_blockers.append(
                "Video podcast episode title, description, or explicit flag is incomplete"
            )
        if configuration_blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Publication destinations are not ready",
                    "blockers": configuration_blockers,
                },
            )

        episode.pop("publish_approved", None)
        episode.pop("publish_approved_at", None)
        episode["publish_approval"] = {
            "revision": gate["revision"],
            "approved_at": now,
            "plan": plan,
        }
        if request.start_publication:
            episode["status"] = "processing"

        atomic_write_json(episode_file, episode)

        if request.start_publication:
            _start_pipeline_thread(
                episode_id, episode.get("source_path", ""), publication_agents
            )

    logger.info(
        "Publication approved for %s; started=%s agents=%s",
        episode_id,
        request.start_publication,
        publication_agents,
    )
    return {
        "status": "shorts_publishing" if request.start_publication else "approved",
        "episode_id": episode_id,
        "publication_started": request.start_publication,
        "publication_agents": publication_agents if request.start_publication else [],
    }


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
                episode["youtube_longform_url_source"] = "upload_post_receipt"
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
