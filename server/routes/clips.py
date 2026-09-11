"""Clip candidate editing, rendering, and final-review endpoints."""

import asyncio
import json
import logging
import math
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.atomic_write import atomic_write_json
from lib.clips import (
    load_clips as _load_clips_from_dir,
)
from lib.clips import (
    normalize_clip as _normalize_clip,
)
from lib.clips import (
    save_clips as _save_clips_to_dir,
)
from lib.ffprobe import get_duration
from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes/{episode_id}/clips", tags=["clips"])

EPISODES_DIR = get_episodes_dir()
_render_jobs_lock = threading.Lock()
_active_render_jobs: set[str] = set()
_RENDER_JOBS_PATH = Path("work/clip_render_jobs.json")


class ManualClipRequest(BaseModel):
    start_seconds: float
    end_seconds: float


class MetadataUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    hashtags: str | list[str] | None = None
    hook_text: str | None = None
    compelling_reason: str | None = None
    virality_score: float | None = None
    speaker: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    metadata: dict | None = None


class BulkClipRequest(BaseModel):
    clip_ids: list[str] | None = None
    min_score: float | None = None
    max_score: float | None = None


def _finite_number(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise HTTPException(status_code=422, detail=f"{name} must be finite")
    return value


def _source_duration_seconds(ep_dir: Path) -> float | None:
    """Return probed source duration, falling back to the recorded ingest value."""
    source = ep_dir / "source_merged.mp4"
    if source.is_file():
        try:
            duration = float(get_duration(source))
            if math.isfinite(duration) and duration > 0:
                return duration
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        episode = json.loads((ep_dir / "episode.json").read_text())
        duration = float(
            episode.get("audio_sync", {}).get("video_duration")
            or episode.get("duration_seconds", 0)
            or 0
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def _render_job_key(ep_dir: Path, clip_id: str) -> str:
    return f"{ep_dir.resolve()}:{clip_id}"


def _read_render_jobs(ep_dir: Path) -> dict:
    try:
        data = json.loads((ep_dir / _RENDER_JOBS_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 1, "jobs": {}}
    if data.get("version") != 1 or not isinstance(data.get("jobs"), dict):
        return {"version": 1, "jobs": {}}
    return data


def _write_render_job(ep_dir: Path, clip_id: str, state: dict) -> None:
    with _render_jobs_lock:
        data = _read_render_jobs(ep_dir)
        data["jobs"][clip_id] = state
        (ep_dir / _RENDER_JOBS_PATH).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(ep_dir / _RENDER_JOBS_PATH, data)


def render_job_state(ep_dir: Path, clip_id: str) -> dict:
    """Return persisted single-clip render progress for review clients."""
    key = _render_job_key(ep_dir, clip_id)
    with _render_jobs_lock:
        state = dict(_read_render_jobs(ep_dir).get("jobs", {}).get(clip_id, {}))
        active = key in _active_render_jobs
    if active:
        state["status"] = "rendering"
    elif state.get("status") == "rendering":
        state.update(
            status="interrupted",
            error="The server stopped before this render reported completion.",
        )
    if not state:
        return {"status": "idle"}
    return state


def _finish_render_job(
    ep_dir: Path,
    clip_id: str,
    started_at: str,
    *,
    result: dict | None = None,
    error: Exception | None = None,
) -> None:
    state = {
        "status": "failed" if error else "succeeded",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        state["error"] = str(error)
    elif result is not None:
        state.update(
            render_fingerprint=result.get("render", {}).get("fingerprint"),
            reused=bool(result.get("reused")),
        )
    _write_render_job(ep_dir, clip_id, state)


def _validate_clip_bounds(ep_dir: Path, start: float, end: float) -> float:
    start = _finite_number("start_seconds", start)
    end = _finite_number("end_seconds", end)
    if start < 0:
        raise HTTPException(
            status_code=422, detail="start_seconds must be non-negative"
        )
    if end <= start:
        raise HTTPException(
            status_code=400, detail="end_seconds must be greater than start_seconds"
        )
    source_duration = _source_duration_seconds(ep_dir)
    if source_duration is None:
        raise HTTPException(
            status_code=409,
            detail="Source duration is unavailable; clip bounds cannot be validated",
        )
    if end > source_duration + 0.001:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "end_seconds exceeds the source duration",
                "source_duration_seconds": round(source_duration, 6),
            },
        )
    return end - start


def _validate_finite_json(value, path: str = "metadata") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise HTTPException(status_code=422, detail=f"{path} must be finite")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_finite_json(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_finite_json(child, f"{path}[{index}]")


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


def _clear_final_approval(clip: dict) -> None:
    """Demote a changed clip until its current render is reviewed again."""
    if clip.get("status") == "approved":
        clip["status"] = "pending"
        clip["selection_status"] = "selected"
    for field in (
        "approved_at",
        "approved_revision",
        "approved_render_fingerprint",
    ):
        clip.pop(field, None)


def _metadata_entry(ep_dir: Path, clip_id: str) -> dict | None:
    try:
        metadata = json.loads((ep_dir / "metadata" / "metadata.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    for item in metadata.get("clips", []):
        if isinstance(item, dict) and item.get("id") == clip_id:
            return item
    return None


def _current_render(ep_dir: Path, clip: dict) -> dict | None:
    """Return the current validated render record for one clip."""
    from agents.pipeline import load_config
    from lib.audio_mix import selected_audio_source
    from lib.delivery_video import current_short_render

    try:
        episode = json.loads((ep_dir / "episode.json").read_text())
        segments = json.loads((ep_dir / "segments.json").read_text()).get(
            "segments", []
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    config = load_config()
    try:
        audio = selected_audio_source(ep_dir, episode, config) or (
            ep_dir / "work" / "audio_mix.wav"
        )
    except ValueError:
        return None
    if not audio.exists() or not segments:
        return None
    return current_short_render(ep_dir, episode, config, audio, segments, clip)


def _approve_current_render(
    ep_dir: Path, clip: dict, render: dict | None = None
) -> dict:
    """Bind final approval to the exact render and copy being reviewed."""
    from agents.qa import clip_review_revision

    render = render or _current_render(ep_dir, clip)
    if not render:
        raise HTTPException(
            status_code=409,
            detail=f"Clip {clip.get('id')} needs a current render before final approval",
        )
    clip["selection_status"] = "selected"
    clip["status"] = "approved"
    clip["approved_render_fingerprint"] = render["fingerprint"]
    clip["approved_revision"] = clip_review_revision(
        clip, render, _metadata_entry(ep_dir, str(clip["id"]))
    )
    clip["approved_at"] = datetime.now(timezone.utc).isoformat()
    return render


def _matches_bulk(clip: dict, req: BulkClipRequest, *, approve: bool) -> bool:
    if req.clip_ids:
        return clip.get("id") in req.clip_ids
    score = clip.get("virality_score", 0)
    threshold = req.min_score if approve else req.max_score
    if threshold is None:
        return False
    return score >= threshold if approve else score <= threshold


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


@router.post("/bulk/approve")
async def approve_clips(episode_id: str, req: BulkClipRequest) -> dict:
    """Approve selected current renders as one all-or-nothing decision."""
    if req.min_score is not None:
        _finite_number("min_score", req.min_score)
    clips, clips_file = load_clips(episode_id)
    targets = [
        clip
        for clip in clips
        if _matches_bulk(clip, req, approve=True) and clip.get("status") != "rejected"
    ]
    renders = {
        str(clip["id"]): _current_render(EPISODES_DIR / episode_id, clip)
        for clip in targets
    }
    missing = [clip_id for clip_id, render in renders.items() if not render]
    if missing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Every clip needs a current render before final approval",
                "clip_ids": missing,
            },
        )
    for clip in targets:
        _approve_current_render(
            EPISODES_DIR / episode_id, clip, renders[str(clip["id"])]
        )
    if targets:
        save_clips(clips, clips_file)
    approved = [str(clip["id"]) for clip in targets]
    return {"status": "approved", "approved": approved, "count": len(approved)}


@router.post("/bulk/reject")
async def reject_clips(episode_id: str, req: BulkClipRequest) -> dict:
    """Reject matching candidates while preserving prior final approvals."""
    if req.max_score is not None:
        _finite_number("max_score", req.max_score)
    clips, clips_file = load_clips(episode_id)
    targets = [
        clip
        for clip in clips
        if _matches_bulk(clip, req, approve=False) and clip.get("status") != "approved"
    ]
    for clip in targets:
        _clear_final_approval(clip)
        clip["selection_status"] = "rejected"
        clip["status"] = "rejected"
    if targets:
        save_clips(clips, clips_file)
    rejected = [str(clip["id"]) for clip in targets]
    return {"status": "rejected", "rejected": rejected, "count": len(rejected)}


@router.post("/{clip_id}/approve")
async def approve_clip(episode_id: str, clip_id: str) -> dict:
    """Finally approve the exact current render and copy for a clip."""
    logger.info("POST /api/episodes/%s/clips/%s/approve", episode_id, clip_id)
    clips, clips_file = load_clips(episode_id)
    clip, idx = find_clip(clips, clip_id)
    render = _approve_current_render(EPISODES_DIR / episode_id, clip)
    clips[idx] = clip
    save_clips(clips, clips_file)
    return {
        "status": "approved",
        "clip_id": clip_id,
        "approved_revision": clip["approved_revision"],
        "render_fingerprint": render["fingerprint"],
    }


@router.post("/{clip_id}/select")
async def select_clip(episode_id: str, clip_id: str) -> dict:
    """Select a candidate without approving an unseen render for release."""
    clips, clips_file = load_clips(episode_id)
    clip, idx = find_clip(clips, clip_id)
    clip["selection_status"] = "selected"
    if clip.get("status") == "rejected":
        clip["status"] = "pending"
    clips[idx] = clip
    save_clips(clips, clips_file)
    return {
        "status": clip.get("status", "pending"),
        "selection_status": "selected",
        "clip_id": clip_id,
    }


@router.post("/{clip_id}/reject")
async def reject_clip(episode_id: str, clip_id: str) -> dict:
    """Reject a clip."""
    logger.info("POST /api/episodes/%s/clips/%s/reject", episode_id, clip_id)
    clips, clips_file = load_clips(episode_id)
    clip, idx = find_clip(clips, clip_id)
    clip["status"] = "rejected"
    clip["selection_status"] = "rejected"
    _clear_final_approval(clip)
    clip["status"] = "rejected"
    clips[idx] = clip
    save_clips(clips, clips_file)
    return {"status": "rejected", "clip_id": clip_id}


@router.post("/{clip_id}/alternative")
async def request_alternative(episode_id: str, clip_id: str) -> dict:
    """Generate and append one transcript-grounded alternative candidate."""
    clips, _ = load_clips(episode_id)
    find_clip(clips, clip_id)
    ep_dir = EPISODES_DIR / episode_id
    from agents.clip_miner import ClipMinerAgent
    from agents.pipeline import load_config

    try:
        return await asyncio.to_thread(
            ClipMinerAgent(ep_dir, load_config()).generate_alternative, clip_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/manual")
async def add_manual_clip(episode_id: str, req: ManualClipRequest) -> dict:
    """Add a custom clip by specifying start and end timestamps."""
    ep_dir = EPISODES_DIR / episode_id
    if not (ep_dir / "episode.json").is_file():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    duration = _validate_clip_bounds(ep_dir, req.start_seconds, req.end_seconds)
    if duration < 5 or duration > 300:
        raise HTTPException(
            status_code=400, detail="Clip duration must be between 5 and 300 seconds"
        )

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
        "start_seconds": req.start_seconds,
        "end_seconds": req.end_seconds,
        "duration": duration,
        "title": f"Custom clip ({int(req.start_seconds // 60)}:{int(req.start_seconds % 60):02d}–{int(req.end_seconds // 60)}:{int(req.end_seconds % 60):02d})",
        "hook_text": "",
        "compelling_reason": "Manually specified by user",
        "virality_score": 0,
        "speaker": "BOTH",
        "status": "pending",
        "selection_status": "selected",
        "manual": True,
    }

    clips.append(new_clip)
    save_clips(clips, clips_file)
    return new_clip


@router.patch("/{clip_id}/metadata")
async def update_clip_metadata(
    episode_id: str, clip_id: str, update: MetadataUpdate
) -> dict:
    """Update clip metadata (title, description, hashtags, time range, per-platform metadata)."""
    clips, clips_file = load_clips(episode_id)
    clip, idx = find_clip(clips, clip_id)

    if update.virality_score is not None:
        _finite_number("virality_score", update.virality_score)
    if update.metadata is not None:
        _validate_finite_json(update.metadata)
    start = update.start_seconds
    end = update.end_seconds
    next_start = (
        start if start is not None else clip.get("start_seconds", clip.get("start"))
    )
    next_end = end if end is not None else clip.get("end_seconds", clip.get("end"))
    bounds_changed = start is not None or end is not None
    duration = None
    if bounds_changed:
        if next_start is None or next_end is None:
            raise HTTPException(
                status_code=422,
                detail="Both existing or updated clip bounds are required",
            )
        duration = _validate_clip_bounds(clips_file.parent, next_start, next_end)

    changed = False
    for field in (
        "title",
        "description",
        "hashtags",
        "hook_text",
        "compelling_reason",
        "virality_score",
        "speaker",
    ):
        value = getattr(update, field)
        if value is not None and clip.get(field) != value:
            clip[field] = value
            changed = True

    if start is not None:
        changed = (
            changed or clip.get("start") != start or clip.get("start_seconds") != start
        )
        clip["start"] = clip["start_seconds"] = start
    if end is not None:
        changed = changed or clip.get("end") != end or clip.get("end_seconds") != end
        clip["end"] = clip["end_seconds"] = end
    if bounds_changed:
        clip["duration"] = duration

    if update.metadata is not None:
        merged = dict(clip.get("metadata", {}))
        for key, value in update.metadata.items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        if merged != clip.get("metadata", {}):
            clip["metadata"] = merged
            changed = True

    if changed:
        _clear_final_approval(clip)

    clips[idx] = clip
    save_clips(clips, clips_file)
    return clip


@router.delete("/{clip_id}")
async def delete_clip(episode_id: str, clip_id: str) -> dict:
    """Remove one clip candidate from the canonical clip list."""
    clips, clips_file = load_clips(episode_id)
    _, index = find_clip(clips, clip_id)
    del clips[index]
    save_clips(clips, clips_file)
    return {"status": "deleted", "clip_id": clip_id}


@router.post("/{clip_id}/render")
async def render_clip(episode_id: str, clip_id: str) -> dict:
    """Render one exact clip through the public shorts-render adapter."""
    clips, _ = load_clips(episode_id)
    find_clip(clips, clip_id)
    ep_dir = EPISODES_DIR / episode_id
    job_key = _render_job_key(ep_dir, clip_id)
    with _render_jobs_lock:
        if job_key in _active_render_jobs:
            raise HTTPException(
                status_code=409, detail=f"Clip {clip_id} is already rendering"
            )
        _active_render_jobs.add(job_key)
    started_at = datetime.now(timezone.utc).isoformat()
    _write_render_job(
        ep_dir,
        clip_id,
        {"status": "rendering", "started_at": started_at},
    )

    from agents.pipeline import load_config
    from agents.shorts_render import render_single_clip

    try:
        try:
            result = await asyncio.to_thread(
                render_single_clip, ep_dir, load_config(), clip_id
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail=f"Clip {clip_id} not found"
            ) from error
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (OSError, RuntimeError) as error:
            logger.exception("single clip render failed for %s", clip_id)
            raise HTTPException(status_code=500, detail=str(error)) from error

        # Newly produced pixels need review. Reusing the exact current fingerprint
        # keeps an existing final approval valid.
        clips, clips_file = load_clips(episode_id)
        clip, index = find_clip(clips, clip_id)
        fingerprint = result.get("render", {}).get("fingerprint")
        if (
            clip.get("status") == "approved"
            and clip.get("approved_render_fingerprint") != fingerprint
        ):
            _clear_final_approval(clip)
            clips[index] = clip
            save_clips(clips, clips_file)
        _finish_render_job(ep_dir, clip_id, started_at, result=result)
        return result
    except Exception as error:
        _finish_render_job(ep_dir, clip_id, started_at, error=error)
        raise
    finally:
        with _render_jobs_lock:
            _active_render_jobs.discard(job_key)
