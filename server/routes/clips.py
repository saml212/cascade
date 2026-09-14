"""Clip candidate editing, rendering, and final-review endpoints."""

import asyncio
import copy
import json
import logging
import math
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
from lib.short_variants import (
    BACKGROUND_VARIANT_ID,
    DEFAULT_BACKGROUND_ASSET_ID,
    DISTRIBUTION_RELEASE_FIELD,
    DISTRIBUTION_VARIANT_FIELD,
    background_variant_output,
    background_variant_state,
    distribution_release_revision,
    require_background_variant,
    save_background_variant_approval,
    selected_short_variant_id,
)
from server.routes import require_episode_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes/{episode_id}/clips", tags=["clips"])

EPISODES_DIR = get_episodes_dir()
_render_jobs_lock = threading.Lock()
_active_render_jobs: set[str] = set()
_render_completion_tasks: set[asyncio.Task] = set()
_RENDER_JOBS_PATH = Path("work/clip_render_jobs.json")


def _release_render_task(task: asyncio.Task) -> None:
    _render_completion_tasks.discard(task)
    if not task.cancelled():
        task.exception()


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


class VariantRenderRequest(BaseModel):
    asset_id: str = DEFAULT_BACKGROUND_ASSET_ID


class VariantApprovalRequest(BaseModel):
    expected_revision: str


class DistributionSelectionRequest(BaseModel):
    variant_id: str | None = None
    expected_revision: str


class ReReleaseRequest(DistributionSelectionRequest):
    request_id: UUID
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=500)
    acknowledge_unresolved_history_revision: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )


class ScheduleCancellationRequest(ReReleaseRequest):
    expected_snapshot_revision: str = Field(min_length=1)


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


def variant_render_job_id(clip_id: str, variant_id: str) -> str:
    return f"{clip_id}@{variant_id}"


def _read_render_jobs(ep_dir: Path) -> dict:
    try:
        data = json.loads((ep_dir / _RENDER_JOBS_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 1, "jobs": {}}
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("jobs"), dict)
    ):
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
            audio_repaired=bool(result.get("audio_repaired")),
            video_reencoded=result.get("video_reencoded"),
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


_RELEASE_REQUEST_STRING_FIELDS = (
    "request_id",
    "actor",
    "reason",
    "target_revision",
    "render_fingerprint",
    "receipt_history_revision",
    "revision",
    "created_at",
)


def _valid_release_request(value: object) -> bool:
    if not (
        isinstance(value, dict)
        and all(
            isinstance(value.get(field), str) and bool(value[field])
            for field in _RELEASE_REQUEST_STRING_FIELDS
        )
        and "variant_id" in value
        and value["variant_id"] in {None, BACKGROUND_VARIANT_ID}
    ):
        return False
    if "unresolved_history_acknowledgement" not in value:
        return True
    acknowledgement = value["unresolved_history_acknowledgement"]
    if not (
        value["variant_id"] == BACKGROUND_VARIANT_ID
        and isinstance(acknowledgement, dict)
        and set(acknowledgement) == {"receipt_history_revision", "obligations"}
        and acknowledgement.get("receipt_history_revision")
        == value["receipt_history_revision"]
        and isinstance(acknowledgement.get("obligations"), list)
        and acknowledgement["obligations"]
    ):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("receipt_revision"), str)
        and item.get("artifact_identity") in {"known", "unknown"}
        for item in acknowledgement["obligations"]
    )


def publication_change_lock(
    ep_dir: Path,
    clip_id: str,
    release_request: object = Ellipsis,
) -> dict:
    """Return whether prior remote work locks this clip's selected version."""
    from agents.publish import short_rerelease_state, validated_short_receipts

    unverifiable = {
        "change_locked": True,
        "change_lock_reason": (
            "Publication receipt history cannot be verified. Repair it before "
            "changing this clip's distribution version."
        ),
        "re_release_allowed": False,
        "re_release_reason": "Publication receipt history cannot be verified.",
        "re_release_request_consumed": None,
        "re_release_history_revision": None,
        "unresolved_receipt_obligations": [],
        "unresolved_history_acknowledgement_allowed": False,
    }
    has_release_request = release_request is not Ellipsis
    if has_release_request and not _valid_release_request(release_request):
        return unverifiable
    request_id = release_request["request_id"] if has_release_request else None
    try:
        publish = json.loads((ep_dir / "publish.json").read_text())
    except FileNotFoundError:
        if has_release_request:
            return unverifiable
        return {
            "change_locked": False,
            "change_lock_reason": None,
            "re_release_allowed": False,
            "re_release_reason": "No prior remote submission requires a re-release.",
            "re_release_request_consumed": None,
            "re_release_history_revision": None,
            "unresolved_receipt_obligations": [],
            "unresolved_history_acknowledgement_allowed": False,
        }
    except (json.JSONDecodeError, OSError):
        return unverifiable
    if not isinstance(publish, dict):
        return unverifiable
    try:
        receipts = validated_short_receipts(publish)
    except ValueError:
        return unverifiable
    profile_username = publish.get("profile_username") or os.getenv(
        "UPLOAD_POST_USER", ""
    )
    rerelease = short_rerelease_state(
        publish,
        clip_id,
        profile_username=profile_username
        if isinstance(profile_username, str)
        else None,
    )
    locked = any(receipt["clip_id"] == clip_id for receipt in receipts)
    if has_release_request and not locked:
        return unverifiable
    request_consumed = (
        any(
            receipt["clip_id"] == clip_id
            and receipt.get("rerelease_request_id") == request_id
            for receipt in receipts
        )
        if request_id
        else None
    )
    return {
        "change_locked": locked,
        "change_lock_reason": (
            "A historical or current publication receipt exists for this clip. "
            "Prepare an explicit re-release identity before changing its "
            "distribution version."
            if locked
            else None
        ),
        "re_release_allowed": rerelease["allowed"] if locked else False,
        "re_release_reason": (
            rerelease["reason"]
            if locked
            else "No prior remote submission requires a re-release."
        ),
        "re_release_request_consumed": request_consumed,
        "re_release_history_revision": rerelease.get("history_revision"),
        "unresolved_receipt_obligations": rerelease.get(
            "unresolved_receipt_obligations", []
        ),
        "unresolved_history_acknowledgement_allowed": rerelease.get(
            "unresolved_history_acknowledgement_allowed", False
        ),
    }


def _distribution_state(ep_dir: Path, clip: dict) -> dict:
    from agents.pipeline import load_config
    from agents.qa import short_distribution_state

    try:
        episode = json.loads((ep_dir / "episode.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            status_code=409, detail="Episode state is unavailable."
        ) from exc
    return short_distribution_state(
        ep_dir,
        episode,
        load_config(),
        clip,
        _current_render(ep_dir, clip),
        _metadata_entry(ep_dir, str(clip["id"])),
    )


def _public_distribution(state: dict, change_lock: dict | None = None) -> dict:
    change_lock = change_lock or {
        "change_locked": False,
        "change_lock_reason": None,
        "re_release_allowed": False,
        "re_release_reason": "No prior remote submission requires a re-release.",
        "re_release_request_consumed": None,
        "re_release_history_revision": None,
        "unresolved_receipt_obligations": [],
        "unresolved_history_acknowledgement_allowed": False,
    }
    return {
        key: state[key]
        for key in (
            "version",
            "variant_id",
            "label",
            "current",
            "approval_current",
            "revision",
        )
    } | {
        "re_release_request": state.get("re_release_request"),
        **change_lock,
    }


def _current_render(ep_dir: Path, clip: dict) -> dict | None:
    """Return the current validated render record for one clip."""
    from agents.pipeline import load_config
    from agents.speaker_cut import current_speaker_segments
    from lib.audio_mix import selected_audio_source
    from lib.delivery_video import current_short_render

    try:
        episode = json.loads((ep_dir / "episode.json").read_text())
        config = load_config()
        plan = current_speaker_segments(ep_dir, episode, config)
        audio = selected_audio_source(ep_dir, episode, config) or (
            ep_dir / "work" / "audio_mix.wav"
        )
        segments = plan.get("segments", []) if isinstance(plan, dict) else []
        if not audio.exists() or not segments:
            return None
        return current_short_render(ep_dir, episode, config, audio, segments, clip)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        return None


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


def _select_clip_distribution_locked(
    episode_id: str, clip_id: str, req: DistributionSelectionRequest
) -> dict:
    from agents.publish import publication_lock

    with publication_lock(EPISODES_DIR):
        return _select_clip_distribution_unlocked(episode_id, clip_id, req)


def _select_clip_distribution_unlocked(
    episode_id: str, clip_id: str, req: DistributionSelectionRequest
) -> dict:
    clips, clips_file = load_clips(episode_id)
    clip, index = find_clip(clips, clip_id)
    try:
        current_variant_id = selected_short_variant_id(clip)
    except KeyError:
        current_variant_id = clip.get(DISTRIBUTION_VARIANT_FIELD)
    change_lock = (
        publication_change_lock(
            clips_file.parent, clip_id, clip[DISTRIBUTION_RELEASE_FIELD]
        )
        if DISTRIBUTION_RELEASE_FIELD in clip
        else publication_change_lock(clips_file.parent, clip_id)
    )
    if current_variant_id != req.variant_id and change_lock["change_locked"]:
        raise HTTPException(
            status_code=409,
            detail=change_lock["change_lock_reason"],
        )

    candidate = dict(clip)
    if req.variant_id is None:
        candidate.pop(DISTRIBUTION_VARIANT_FIELD, None)
    else:
        candidate[DISTRIBUTION_VARIANT_FIELD] = req.variant_id
    state = _distribution_state(clips_file.parent, candidate)
    if req.expected_revision != state["revision"]:
        raise HTTPException(
            status_code=409,
            detail="The selected render or its copy changed; refresh before selecting it.",
        )
    if state["current"] is not True:
        raise HTTPException(
            status_code=409,
            detail="The selected distribution version needs a current render.",
        )
    if state["approval_current"] is not True:
        raise HTTPException(
            status_code=409,
            detail="Approve this exact distribution version and copy before selecting it.",
        )

    clips[index] = candidate
    save_clips(clips, clips_file)
    return {
        "status": "selected",
        "clip_id": clip_id,
        "distribution": _public_distribution(state, change_lock),
    }


@router.put("/{clip_id}/distribution")
async def select_clip_distribution(
    episode_id: str, clip_id: str, req: DistributionSelectionRequest
) -> dict:
    """Select one independently approved short version for distribution."""
    if req.variant_id is not None:
        try:
            require_background_variant(req.variant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    return await asyncio.to_thread(
        _select_clip_distribution_locked, episode_id, clip_id, req
    )


def _rerelease_target(
    ep_dir: Path, clip: dict, req: ReReleaseRequest
) -> tuple[dict, dict]:
    candidate = dict(clip)
    if req.variant_id is None:
        candidate.pop(DISTRIBUTION_VARIANT_FIELD, None)
    else:
        try:
            require_background_variant(req.variant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
        candidate[DISTRIBUTION_VARIANT_FIELD] = req.variant_id
    state = _distribution_state(ep_dir, candidate)
    if req.expected_revision != state["revision"]:
        raise HTTPException(
            status_code=409,
            detail="The target render or its copy changed; refresh before re-releasing it.",
        )
    if state["current"] is not True or state["approval_current"] is not True:
        raise HTTPException(
            status_code=409,
            detail="The target version must be current and separately approved.",
        )
    if (
        not isinstance(state.get("render_fingerprint"), str)
        or not state["render_fingerprint"]
    ):
        raise HTTPException(
            status_code=409,
            detail="The target version has no verifiable render fingerprint.",
        )
    return candidate, state


def _prepare_clip_rerelease_locked(
    episode_id: str, clip_id: str, req: ReReleaseRequest
) -> dict:
    from agents.publish import (
        publication_lock,
        short_receipt_history_revision,
        short_rerelease_state,
        validated_short_receipts,
    )

    with publication_lock(EPISODES_DIR):
        clips, clips_file = load_clips(episode_id)
        clip, index = find_clip(clips, clip_id)
        try:
            publish = json.loads((clips_file.parent / "publish.json").read_text())
            if not isinstance(publish, dict):
                raise TypeError
            receipts = validated_short_receipts(publish)
        except (
            FileNotFoundError,
            json.JSONDecodeError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise HTTPException(
                status_code=409,
                detail="Publication receipt history cannot be verified.",
            ) from exc

        candidate, state = _rerelease_target(clips_file.parent, clip, req)

        request_id = str(req.request_id)
        actor = req.actor.strip()
        reason = req.reason.strip()
        if not actor or len(reason) < 3:
            raise HTTPException(
                status_code=422, detail="Actor and reason are required."
            )
        existing = clip.get(DISTRIBUTION_RELEASE_FIELD)
        if DISTRIBUTION_RELEASE_FIELD in clip and not _valid_release_request(existing):
            raise HTTPException(
                status_code=409,
                detail="The existing re-release identity cannot be verified.",
            )
        if isinstance(existing, dict) and existing.get("request_id") == request_id:
            parent_revision = short_receipt_history_revision(
                publish, clip_id, exclude_request_id=request_id
            )
            acknowledgement = existing.get("unresolved_history_acknowledgement")
            acknowledged_revision = (
                acknowledgement.get("receipt_history_revision")
                if isinstance(acknowledgement, dict)
                else None
            )
            expected_inputs = {
                "request_id": request_id,
                "actor": actor,
                "reason": reason,
                "variant_id": req.variant_id,
                "target_revision": state["revision"],
                "render_fingerprint": state["render_fingerprint"],
                "receipt_history_revision": parent_revision,
            }
            expected = distribution_release_revision(
                **expected_inputs,
                unresolved_history_acknowledgement=acknowledgement,
            )
            if (
                req.acknowledge_unresolved_history_revision != acknowledged_revision
                or existing.get("revision") != expected
                or any(
                    existing.get(field) != value
                    for field, value in expected_inputs.items()
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="This re-release request ID is already bound to different inputs.",
                )
            return {
                "status": "already_prepared",
                "clip_id": clip_id,
                "requires_publish_approval": True,
                "distribution": _public_distribution(
                    {**state, "re_release_request": existing},
                    publication_change_lock(clips_file.parent, clip_id, existing),
                ),
            }
        if isinstance(existing, dict) and not any(
            receipt.get("clip_id") == clip_id
            and receipt.get("rerelease_request_id") == existing.get("request_id")
            for receipt in receipts
        ):
            raise HTTPException(
                status_code=409,
                detail="A different re-release is already prepared and not yet recorded.",
            )

        profile_username = publish.get("profile_username") or os.getenv(
            "UPLOAD_POST_USER", ""
        )
        eligibility = short_rerelease_state(
            publish,
            clip_id,
            profile_username=(
                profile_username if isinstance(profile_username, str) else None
            ),
        )
        acknowledgement = None
        if eligibility["allowed"] is not True:
            if req.acknowledge_unresolved_history_revision is None:
                raise HTTPException(status_code=409, detail=eligibility["reason"])
            if (
                eligibility.get("unresolved_history_acknowledgement_allowed")
                is not True
                or req.variant_id != BACKGROUND_VARIANT_ID
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Only pre-schema unresolved history can be acknowledged "
                        "for a current approved Motion replacement."
                    ),
                )
            if (
                req.acknowledge_unresolved_history_revision
                != eligibility["history_revision"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Unresolved receipt history changed; refresh before re-release.",
                )
            acknowledgement = {
                "receipt_history_revision": eligibility["history_revision"],
                "obligations": eligibility["unresolved_receipt_obligations"],
            }
        elif req.acknowledge_unresolved_history_revision is not None:
            raise HTTPException(
                status_code=409,
                detail="There is no unresolved legacy history to acknowledge.",
            )
        cancellation_request = eligibility.get("cancellation_request")
        if cancellation_request is not None and cancellation_request != {
            "request_id": request_id,
            "actor": actor,
            "reason": reason,
            "variant_id": req.variant_id,
            "target_revision": state["revision"],
            "render_fingerprint": state["render_fingerprint"],
        }:
            raise HTTPException(
                status_code=409,
                detail="Use the exact request and target bound to the cancellation.",
            )
        authorization = {
            "request_id": request_id,
            "actor": actor,
            "reason": reason,
            "variant_id": req.variant_id,
            "target_revision": state["revision"],
            "render_fingerprint": state["render_fingerprint"],
            "receipt_history_revision": eligibility["history_revision"],
        }
        authorization["revision"] = distribution_release_revision(
            request_id=request_id,
            actor=actor,
            reason=reason,
            variant_id=req.variant_id,
            target_revision=state["revision"],
            render_fingerprint=state["render_fingerprint"],
            receipt_history_revision=eligibility["history_revision"],
            unresolved_history_acknowledgement=acknowledgement,
        )
        if acknowledgement is not None:
            authorization["unresolved_history_acknowledgement"] = acknowledgement
        authorization["created_at"] = datetime.now(timezone.utc).isoformat()
        candidate[DISTRIBUTION_RELEASE_FIELD] = authorization
        clips[index] = candidate
        save_clips(clips, clips_file)
        return {
            "status": "prepared",
            "clip_id": clip_id,
            "requires_publish_approval": True,
            "distribution": _public_distribution(
                {**state, "re_release_request": authorization},
                publication_change_lock(clips_file.parent, clip_id, authorization),
            ),
        }


@router.post("/{clip_id}/re-release")
async def prepare_clip_rerelease(
    episode_id: str, clip_id: str, req: ReReleaseRequest
) -> dict:
    """Prepare a new receipt-bound release without rewriting prior receipts."""
    return await asyncio.to_thread(
        _prepare_clip_rerelease_locked, episode_id, clip_id, req
    )


def _load_cancellation_publish(ep_dir: Path) -> tuple[dict, list[dict]]:
    from agents.publish import validated_short_receipts

    try:
        publish = json.loads((ep_dir / "publish.json").read_text())
        if not isinstance(publish, dict):
            raise TypeError
        return publish, validated_short_receipts(publish)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=409, detail="Publication history cannot be verified."
        ) from exc


def _upload_post_job_evidence(api_key: str, profile: str, job_id: str) -> dict:
    headers = {"Authorization": f"Apikey {api_key}"}
    try:
        with httpx.Client(timeout=10) as client:
            status_response = client.get(
                "https://api.upload-post.com/api/uploadposts/status",
                params={"job_id": job_id},
                headers=headers,
            )
            if status_response.status_code == 404:
                status_missing, status = True, None
            else:
                status_response.raise_for_status()
                status_missing, status = False, status_response.json()
                if not isinstance(status, dict):
                    raise TypeError
            history_response = client.get(
                "https://api.upload-post.com/api/uploadposts/history",
                params={
                    "job_id": job_id,
                    "profile_username": profile,
                    "limit": 100,
                    "page": 1,
                },
                headers=headers,
            )
            history_response.raise_for_status()
            history = history_response.json()
            if not isinstance(history, dict):
                raise TypeError
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise RuntimeError("Cannot verify Upload-Post status/history") from exc
    return {"status_not_found": status_missing, "status": status, "history": history}


def _delete_upload_post_schedule(api_key: str, job_id: str) -> dict:
    try:
        response = httpx.delete(
            f"https://api.upload-post.com/api/uploadposts/schedule/{job_id}",
            headers={"Authorization": f"Apikey {api_key}"},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return {"error": f"Cancellation outcome is unknown: {exc}"}
    try:
        body = response.json()
    except ValueError:
        body = None
    return {
        "http_status": response.status_code,
        **({"response": body} if isinstance(body, dict) else {}),
    }


def _cancellation_context(
    episode_id: str, clip_id: str, req: ReReleaseRequest, publish: dict
) -> dict:
    from agents.pipeline import load_config
    from agents.publish import (
        PublishAgent,
        cancellable_scheduled_receipt,
        cancellation_snapshot,
        schedule_cancellation_provider_safe,
    )

    clips, clips_file = load_clips(episode_id)
    clip, _ = find_clip(clips, clip_id)
    _, state = _rerelease_target(clips_file.parent, clip, req)
    existing = clip.get(DISTRIBUTION_RELEASE_FIELD)
    if DISTRIBUTION_RELEASE_FIELD in clip and not _valid_release_request(existing):
        raise HTTPException(status_code=409, detail="Re-release identity is malformed.")
    if isinstance(existing, dict) and not any(
        receipt.get("clip_id") == clip_id
        and receipt.get("rerelease_request_id") == existing.get("request_id")
        for receipt in publish.get("shorts", [])
    ):
        raise HTTPException(status_code=409, detail="A re-release is already prepared.")
    profile = publish.get("profile_username")
    api_key = os.getenv("UPLOAD_POST_API_KEY", "")
    if (
        not isinstance(profile, str)
        or not profile
        or profile != os.getenv("UPLOAD_POST_USER", "")
        or not api_key
    ):
        raise HTTPException(
            status_code=409, detail="Exact provider account is unavailable."
        )
    agent = PublishAgent(clips_file.parent, load_config())
    try:
        receipt, remote, history_revision = cancellable_scheduled_receipt(
            publish,
            clip_id,
            agent._remote_schedule(api_key, profile),
            profile_username=profile,
        )
        evidence = _upload_post_job_evidence(api_key, profile, receipt["job_id"])
        safe, reason = schedule_cancellation_provider_safe(
            receipt, evidence, profile_username=profile, after_delete=False
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not safe:
        raise HTTPException(status_code=409, detail=reason)
    target = {
        "variant_id": req.variant_id,
        "revision": state["revision"],
        "render_fingerprint": state["render_fingerprint"],
    }
    return {
        "receipt": receipt,
        "snapshot": cancellation_snapshot(
            receipt, history_revision, profile, target, remote
        ),
        "pre_delete": {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "evidence": evidence,
        },
        "api_key": api_key,
    }


def _cancellation_body(req: ReReleaseRequest, revision: str) -> dict:
    return {
        "variant_id": req.variant_id,
        "expected_revision": req.expected_revision,
        "request_id": str(req.request_id),
        "actor": req.actor.strip(),
        "reason": req.reason.strip(),
        "expected_snapshot_revision": revision,
    }


def _cancellation_response(episode_id: str, clip_id: str, operation: dict) -> dict:
    target = operation["snapshot"]["target"]
    return {
        "status": "cancelled",
        "clip_id": clip_id,
        "operation_id": operation["operation_id"],
        "replacement_safe": True,
        "prior_receipt_preserved": True,
        "next": {
            "method": "POST",
            "path": f"/api/episodes/{episode_id}/clips/{clip_id}/re-release",
            "body": {
                "variant_id": target["variant_id"],
                "expected_revision": target["revision"],
                "request_id": operation["operation_id"],
                "actor": operation["actor"],
                "reason": operation["reason"],
            },
        },
    }


def _preview_schedule_cancellation_locked(
    episode_id: str, clip_id: str, req: ReReleaseRequest
) -> dict:
    from agents.publish import publication_lock

    if not req.actor.strip() or len(req.reason.strip()) < 3:
        raise HTTPException(status_code=422, detail="Actor and reason are required.")
    with publication_lock(EPISODES_DIR):
        ep_dir = require_episode_dir(EPISODES_DIR, episode_id)
        publish, _ = _load_cancellation_publish(ep_dir)
        context = _cancellation_context(episode_id, clip_id, req, publish)
        receipt, snapshot = context["receipt"], context["snapshot"]
        return {
            "status": "cancellable",
            "clip_id": clip_id,
            "replacement_safe": False,
            "target": snapshot["target"],
            "receipt": {
                "revision": snapshot["receipt_revision"],
                "job_id": receipt["job_id"],
                "external_id": receipt["external_id"],
                "profile_username": snapshot["profile_username"],
                "scheduled_date": receipt["scheduled_date"],
                "platforms": receipt["platforms"],
            },
            "remote_job": snapshot["remote_job"],
            "snapshot_revision": snapshot["revision"],
            "execute": _cancellation_body(req, snapshot["revision"]),
        }


@router.post("/{clip_id}/schedule-cancellation/preview")
async def preview_schedule_cancellation(
    episode_id: str, clip_id: str, req: ReReleaseRequest
) -> dict:
    return await asyncio.to_thread(
        _preview_schedule_cancellation_locked, episode_id, clip_id, req
    )


def _post_delete_cancellation(
    ep_dir: Path, publish: dict, receipts: list[dict], index: int, api_key: str
) -> dict:
    from agents.pipeline import load_config
    from agents.publish import PublishAgent, schedule_cancellation_provider_safe

    receipt = receipts[index]
    operation = receipt["schedule_cancellation"]
    original = receipt["pre_cancellation_receipt"]
    profile = operation["snapshot"]["profile_username"]
    calendar = evidence = None
    try:
        calendar = PublishAgent(ep_dir, load_config())._remote_schedule(
            api_key, profile
        )
        if not isinstance(calendar, list) or any(
            not isinstance(item, dict) for item in calendar
        ):
            raise ValueError("Provider calendar is malformed")
        absent = not any(
            item.get("job_id") == original["job_id"]
            or item.get("external_id") == original["external_id"]
            for item in calendar
        )
        evidence = _upload_post_job_evidence(api_key, profile, original["job_id"])
        safe, reason = schedule_cancellation_provider_safe(
            original, evidence, profile_username=profile, after_delete=True
        )
    except (RuntimeError, ValueError) as exc:
        absent, safe, reason = False, False, str(exc)
    operation["post_delete"] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "calendar": calendar,
        "evidence": evidence,
    }
    if not absent or not safe:
        atomic_write_json(ep_dir / "publish.json", publish)
        raise HTTPException(status_code=409, detail=reason or "Job remains scheduled.")
    completed = datetime.now(timezone.utc).isoformat()
    destinations = {
        platform: {"state": "cancelled"} for platform in original["platforms"]
    }
    event = {
        "observed_at": completed,
        "previous_status": original.get("status"),
        "status": "cancelled",
        "provider_status": "cancelled",
        "profile_username": profile,
        "evidence_source": "schedule_cancellation",
        "terminal_destinations": destinations,
    }
    operation.update(state="cancelled", completed_at=completed, terminal_event=event)
    receipts[index] = {
        **receipt,
        "status": "cancelled",
        "scheduled": False,
        "terminal_destinations": destinations,
        "status_history": [*original.get("status_history", []), event],
    }
    publish["shorts"] = receipts
    atomic_write_json(ep_dir / "publish.json", publish)
    return receipts[index]


def _cancel_schedule_locked(
    episode_id: str, clip_id: str, req: ScheduleCancellationRequest
) -> dict:
    from agents.publish import (
        SCHEDULE_CANCELLATION_SCHEMA,
        publication_lock,
        validated_schedule_cancellation,
    )

    actor, reason = req.actor.strip(), req.reason.strip()
    if not actor or len(reason) < 3:
        raise HTTPException(status_code=422, detail="Actor and reason are required.")
    with publication_lock(EPISODES_DIR):
        ep_dir = require_episode_dir(EPISODES_DIR, episode_id)
        publish, receipts = _load_cancellation_publish(ep_dir)
        matches = [
            (index, receipt, receipt.get("schedule_cancellation"))
            for index, receipt in enumerate(receipts)
            if isinstance(receipt.get("schedule_cancellation"), dict)
            and receipt["schedule_cancellation"].get("operation_id")
            == str(req.request_id)
        ]
        if len(matches) > 1:
            raise HTTPException(
                status_code=409, detail="Cancellation ID is duplicated."
            )
        if matches:
            index, receipt, operation = matches[0]
            if receipt.get("clip_id") != clip_id or operation.get("clip_id") != clip_id:
                raise HTTPException(
                    status_code=409,
                    detail="Cancellation ID belongs to a different clip.",
                )
            target = operation.get("snapshot", {}).get("target", {})
            if validated_schedule_cancellation(receipt) is None or (
                operation.get("actor"),
                operation.get("reason"),
                operation.get("snapshot", {}).get("revision"),
                target.get("variant_id"),
                target.get("revision"),
            ) != (
                actor,
                reason,
                req.expected_snapshot_revision,
                req.variant_id,
                req.expected_revision,
            ):
                raise HTTPException(
                    status_code=409, detail="Cancellation inputs changed."
                )
            if operation["state"] == "cancelled":
                return _cancellation_response(episode_id, clip_id, operation)
            if operation["state"] != "delete_confirmed":
                raise HTTPException(
                    status_code=409,
                    detail="Prior outcome is unresolved; do not send another DELETE.",
                )
        else:
            context = _cancellation_context(episode_id, clip_id, req, publish)
            snapshot, receipt = context["snapshot"], context["receipt"]
            if req.expected_snapshot_revision != snapshot["revision"]:
                raise HTTPException(
                    status_code=409, detail="Snapshot changed; preview again."
                )
            current_clips, _ = load_clips(episode_id)
            current_clip, _ = find_clip(current_clips, clip_id)
            _, current_target = _rerelease_target(ep_dir, current_clip, req)
            if any(
                current_target.get(key) != snapshot["target"].get(key)
                for key in ("variant_id", "revision", "render_fingerprint")
            ):
                raise HTTPException(
                    status_code=409,
                    detail="The target changed during provider verification; preview again.",
                )
            index = receipts.index(receipt)
            original = copy.deepcopy(receipt)
            operation = {
                "schema": SCHEDULE_CANCELLATION_SCHEMA,
                "operation_id": str(req.request_id),
                "clip_id": clip_id,
                "actor": actor,
                "reason": reason,
                "state": "delete_started",
                "snapshot": snapshot,
                "pre_delete": context["pre_delete"],
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            receipts[index] = {
                **receipt,
                "pre_cancellation_receipt": original,
                "schedule_cancellation": operation,
            }
            publish["shorts"] = receipts
            atomic_write_json(ep_dir / "publish.json", publish)
            outcome = _delete_upload_post_schedule(
                context["api_key"], original["job_id"]
            )
            operation["delete"] = {
                "job_id": original["job_id"],
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                **outcome,
            }
            confirmed = (
                outcome.get("http_status") == 200
                and isinstance(outcome.get("response"), dict)
                and outcome["response"].get("success") is True
            )
            if not confirmed:
                operation["state"] = "outcome_uncertain"
                atomic_write_json(ep_dir / "publish.json", publish)
                raise HTTPException(
                    status_code=409,
                    detail="Cancellation is unconfirmed; do not send another DELETE.",
                )
            operation.update(state="delete_confirmed")
            operation["delete"]["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(ep_dir / "publish.json", publish)
        api_key = os.getenv("UPLOAD_POST_API_KEY", "")
        profile = os.getenv("UPLOAD_POST_USER", "")
        if not api_key or profile != operation["snapshot"]["profile_username"]:
            raise HTTPException(
                status_code=409,
                detail="The exact Upload-Post account is unavailable.",
            )
        receipt = _post_delete_cancellation(ep_dir, publish, receipts, index, api_key)
        operation = validated_schedule_cancellation(receipt)
        if operation is None:
            raise HTTPException(
                status_code=409, detail="Cancellation proof is invalid."
            )
        return _cancellation_response(episode_id, clip_id, operation)


@router.post("/{clip_id}/schedule-cancellation")
async def cancel_clip_schedule(
    episode_id: str, clip_id: str, req: ScheduleCancellationRequest
) -> dict:
    return await asyncio.to_thread(_cancel_schedule_locked, episode_id, clip_id, req)


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


async def _run_clip_render_operation(
    episode_id: str,
    clip_id: str,
    *,
    repair_audio: bool = False,
    variant_id: str | None = None,
    asset_id: str | None = None,
) -> dict:
    """Run one serialized clip media operation and persist its review state."""
    from agents.pipeline import load_config
    from agents.shorts_render import (
        render_single_clip,
        render_single_clip_variant,
        repair_single_clip_audio,
    )

    clips, _ = load_clips(episode_id)
    find_clip(clips, clip_id)
    ep_dir = EPISODES_DIR / episode_id
    job_id = variant_render_job_id(clip_id, variant_id) if variant_id else clip_id
    job_keys = {
        _render_job_key(ep_dir, clip_id),
        _render_job_key(ep_dir, job_id),
    }
    with _render_jobs_lock:
        if any(key in _active_render_jobs for key in job_keys):
            raise HTTPException(
                status_code=409, detail=f"Clip {clip_id} is already rendering"
            )
        _active_render_jobs.update(job_keys)
    started_at = datetime.now(timezone.utc).isoformat()

    async def complete() -> dict:
        job_started = False
        try:
            try:
                _write_render_job(
                    ep_dir,
                    job_id,
                    {
                        "status": "rendering",
                        "operation": (
                            "render_variant"
                            if variant_id
                            else "repair_audio"
                            if repair_audio
                            else "render"
                        ),
                        "started_at": started_at,
                    },
                )
                job_started = True
                if variant_id:
                    operation = render_single_clip_variant
                    arguments = (
                        ep_dir,
                        load_config(),
                        clip_id,
                        variant_id,
                        asset_id or DEFAULT_BACKGROUND_ASSET_ID,
                    )
                else:
                    operation = (
                        repair_single_clip_audio if repair_audio else render_single_clip
                    )
                    arguments = (ep_dir, load_config(), clip_id)
                result = await asyncio.to_thread(operation, *arguments)
            except KeyError as error:
                detail = (
                    str(error).strip("'") if variant_id else f"Clip {clip_id} not found"
                )
                raise HTTPException(status_code=404, detail=detail) from error
            except (FileNotFoundError, ValueError) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (OSError, RuntimeError) as error:
                logger.exception("single clip media operation failed for %s", clip_id)
                raise HTTPException(status_code=500, detail=str(error)) from error

            # A variant never mutates or approves its canonical base clip.
            if not variant_id:
                current_clips, clips_file = load_clips(episode_id)
                clip, index = find_clip(current_clips, clip_id)
                fingerprint = result.get("render", {}).get("fingerprint")
                if clip.get("status") == "approved" and (
                    result.get("audio_repaired")
                    or clip.get("approved_render_fingerprint") != fingerprint
                ):
                    _clear_final_approval(clip)
                    current_clips[index] = clip
                    save_clips(current_clips, clips_file)
            _finish_render_job(ep_dir, job_id, started_at, result=result)
            return result
        except Exception as error:
            if job_started:
                _finish_render_job(ep_dir, job_id, started_at, error=error)
            raise
        finally:
            with _render_jobs_lock:
                _active_render_jobs.difference_update(job_keys)

    completion = asyncio.create_task(complete())
    _render_completion_tasks.add(completion)
    completion.add_done_callback(_release_render_task)
    return await asyncio.shield(completion)


@router.post("/{clip_id}/render")
async def render_clip(episode_id: str, clip_id: str) -> dict:
    """Render one exact clip through the public shorts-render adapter."""
    return await _run_clip_render_operation(episode_id, clip_id)


@router.post("/{clip_id}/repair-audio")
async def repair_clip_audio(episode_id: str, clip_id: str) -> dict:
    """Normalize one current short while copying its reviewed video packets."""
    return await _run_clip_render_operation(episode_id, clip_id, repair_audio=True)


@router.post("/{clip_id}/variants/{variant_id}/render")
async def render_clip_variant(
    episode_id: str,
    clip_id: str,
    variant_id: str,
    req: VariantRenderRequest | None = None,
) -> dict:
    """Render one optional review variant without changing the canonical short."""
    try:
        require_background_variant(variant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    return await _run_clip_render_operation(
        episode_id,
        clip_id,
        variant_id=variant_id,
        asset_id=req.asset_id if req else DEFAULT_BACKGROUND_ASSET_ID,
    )


@router.post("/{clip_id}/variants/{variant_id}/approve")
async def approve_clip_variant(
    episode_id: str, clip_id: str, variant_id: str, req: VariantApprovalRequest
) -> dict:
    """Approve the exact current variant pixels and current clip copy."""
    try:
        require_background_variant(variant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    clips, _ = load_clips(episode_id)
    clip, _ = find_clip(clips, clip_id)
    ep_dir = EPISODES_DIR / episode_id

    from agents.pipeline import load_config
    from agents.qa import clip_review_revision
    from lib.delivery_video import render_config_for_episode, render_output_lock
    from lib.encoding import get_video_encoding_policy

    with render_output_lock(background_variant_output(ep_dir, clip_id)):
        base_record = _current_render(ep_dir, clip)
        try:
            episode = json.loads((ep_dir / "episode.json").read_text())
            encoding = get_video_encoding_policy(
                render_config_for_episode(episode, load_config()), "shorts"
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record, render = background_variant_state(
            ep_dir, clip_id, base_record=base_record, encoding=encoding
        )
        revision = clip_review_revision(clip, record, _metadata_entry(ep_dir, clip_id))
        if req.expected_revision != revision:
            raise HTTPException(
                status_code=409,
                detail="The variant or its copy changed; refresh before approving.",
            )
        if not render["current"]:
            raise HTTPException(
                status_code=409,
                detail="This variant needs a current render before approval.",
            )
        if save_background_variant_approval(ep_dir, clip_id, record, revision) is None:
            raise HTTPException(
                status_code=409,
                detail="The variant changed while approval was being saved.",
            )
    return {
        "status": "approved",
        "clip_id": clip_id,
        "variant_id": variant_id,
        "approved_revision": revision,
    }
