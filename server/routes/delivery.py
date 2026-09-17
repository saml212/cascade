"""Prepare and serve the canonical release video."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from agents.longform_render import render_longform, repair_longform_audio
from agents.pipeline import load_config
from agents.qa import quality_snapshot
from agents.speaker_cut import current_speaker_segments
from agents.transcribe import current_diarized_transcript
from lib.atomic_write import atomic_write_json
from lib.audio_mix import selected_audio_source
from lib.delivery_video import (
    build_keep_intervals,
    current_longform_render,
    read_render_manifest,
    render_config_for_episode,
    render_space_budget,
    render_space_status,
    require_render_space,
)
from lib.encoding import get_video_encoding_policy
from lib.ffprobe import get_duration
from lib.loudness import delivery_loudness_policy, loudness_status
from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/episodes", tags=["delivery"])

EPISODES_DIR = get_episodes_dir()
STATUS_NAME = "delivery.json"
_video_running: set[str] = set()
_running_lock = threading.Lock()


def video_preparation_active(episode_id: str) -> bool:
    """Return whether this process has a live video preparation worker."""
    with _running_lock:
        return episode_id in _video_running


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_path(episode_dir: Path) -> Path:
    return episode_dir / STATUS_NAME


def _source_duration(episode_dir: Path, episode: dict) -> float | None:
    """Prefer the actual merged artifact; fall back to ingest metadata."""
    source = episode_dir / "source_merged.mp4"
    if source.exists():
        try:
            duration = get_duration(source)
            if duration > 0:
                return duration
        except (OSError, subprocess.SubprocessError, ValueError):
            logger.warning("Could not probe source duration for %s", episode_dir.name)
    value = episode.get("audio_sync", {}).get("video_duration") or episode.get(
        "duration_seconds"
    )
    return float(value) if value else None


def _read_status(episode_dir: Path) -> dict:
    path = _status_path(episode_dir)
    if not path.exists():
        return {"status": "not_prepared", "episode_id": episode_dir.name}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "status": "failed",
            "episode_id": episode_dir.name,
            "error": "Delivery status file is unreadable",
        }


def _file_stat(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _artifact_download_url(episode_id: str, artifact: str, output_stat: dict) -> str:
    revision = f"{int(output_stat['size']):x}-{int(output_stat['mtime_ns']):x}"
    return f"/api/episodes/{episode_id}/delivery/{artifact}?v={revision}"


def _selected_audio_review_artifact(
    episode_dir: Path, episode: dict, config: dict
) -> dict | None:
    """Describe an existing full-length audio reference without rendering it."""
    selected = selected_audio_source(episode_dir, episode, config)
    if selected is not None:
        return {
            "path": selected,
            "provenance": {
                "kind": "selected_repair",
                "currentness": "current",
                "clock": "source",
                "editorial_cuts_applied": False,
            },
        }
    base_mix = episode_dir / "work" / "audio_mix.wav"
    if not base_mix.is_file():
        return None
    return {
        "path": base_mix,
        "provenance": {
            "kind": "base_mix",
            "currentness": "unverified",
            "clock": "source",
            "editorial_cuts_applied": False,
        },
    }


def _selected_audio_review_fields(
    episode_dir: Path, episode: dict, config: dict
) -> dict:
    try:
        artifact = _selected_audio_review_artifact(episode_dir, episode, config)
    except (OSError, TypeError, ValueError) as exc:
        return {"selected_audio_review_error": str(exc)}
    if artifact is None:
        return {}
    audio = artifact["path"]
    output_stat = _file_stat(audio)
    return {
        "selected_audio_download_url": _artifact_download_url(
            episode_dir.name, "selected-audio", output_stat
        ),
        "selected_audio": {
            "filename": audio.name,
            "size_bytes": output_stat["size"],
            "provenance": artifact["provenance"],
        },
    }


def _current_video_record(
    episode_dir: Path, episode: dict, config: dict
) -> dict | None:
    """Return the current manifest-backed video using the selected audio source."""
    audio = selected_audio_source(episode_dir, episode, config) or (
        episode_dir / "work" / "audio_mix.wav"
    )
    segment_document = current_speaker_segments(episode_dir, episode, config)
    segments = segment_document.get("segments", []) if segment_document else []
    if not audio.is_file() or not segments:
        return None
    return current_longform_render(episode_dir, episode, config, audio, segments)


def _video_audio_status(
    episode_dir: Path, config: dict, record: dict | None = None
) -> dict:
    """Classify recorded final AAC evidence without decoding on status polls."""
    record = record or read_render_manifest(episode_dir).get("longform", {})
    return loudness_status(
        record.get("output", {}).get("audio_loudness"),
        delivery_loudness_policy(config, "longform"),
    )


def current_delivery_video_fields(
    episode_dir: Path, episode: dict, config: dict
) -> dict:
    """Return read-only delivery fields only for the current proven video."""
    try:
        record = _current_video_record(episode_dir, episode, config)
        if record is None:
            return {"video_status": "not_prepared"}
        video_path = episode_dir / "upload_video.mp4"
        output_stat = _file_stat(video_path)
        recorded_output = record.get("output", {})
        if (
            recorded_output.get("size_bytes") != output_stat["size"]
            or recorded_output.get("mtime_ns") != output_stat["mtime_ns"]
        ):
            return {"video_status": "not_prepared"}
        video_audio = _video_audio_status(episode_dir, config, record)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        return {"video_status": "not_prepared"}

    fields = {
        "video_completed_at": record.get("completed_at"),
        "video_download_url": _artifact_download_url(
            episode_dir.name, "video", output_stat
        ),
        "video_source_fingerprint": record["fingerprint"],
        "video_output_stat": output_stat,
        "video": {
            "filename": video_path.name,
            "render_mode": record.get("render_mode"),
            "render_fingerprint": record["fingerprint"],
            **record.get("output", {}),
        },
        "video_audio": video_audio,
        "video_stale": False,
    }
    if not video_audio["safe"]:
        return {
            **fields,
            "video_status": "not_prepared",
            "video_repair_required": True,
            "video_error": (
                "; ".join(video_audio.get("errors", []))
                or video_audio.get("error")
                or "Encoded video audio needs repair."
            ),
        }
    return {
        **fields,
        "video_status": "ready",
        "video_progress": 100.0,
        "video_repair_required": False,
        "video_error": None,
    }


def _recover_current_video_status(
    status: dict, episode_dir: Path, episode: dict, config: dict
) -> None:
    """Expose a current proven render without disturbing active preparation."""
    if video_preparation_active(episode_dir.name):
        return
    current = current_delivery_video_fields(episode_dir, episode, config)
    if status.get("video_status") is None or current.get("video_status") == "ready":
        status.update(current)


def _video_preflight(
    episode_dir: Path,
    episode: dict,
    config: dict,
    source_duration: float | None = None,
) -> dict:
    """Return the exact encoding and peak-storage decision used by rendering."""
    source_duration = source_duration or _source_duration(episode_dir, episode)
    if not source_duration:
        raise ValueError("Source duration is unavailable")
    duration = sum(
        end - start
        for start, end in build_keep_intervals(
            float(source_duration), episode.get("longform_edits", [])
        )
    )
    if duration <= 0:
        raise ValueError("No retained source material remains after edits")
    resolved = render_config_for_episode(episode, config)
    encoding = get_video_encoding_policy(resolved, "longform")
    budget = render_space_budget(duration, encoding)
    storage = render_space_status(episode_dir, budget)
    return {
        "schema": "cascade.video-preflight/v1",
        "safe": storage["safe"],
        "profile": "longform",
        "duration_seconds": round(duration, 3),
        "encoding": encoding,
        "budget": budget,
        "storage": storage,
    }


def _refresh_status(episode_dir: Path) -> dict:
    """Return current video state while preserving historical delivery fields."""
    status = _read_status(episode_dir)
    episode_id = episode_dir.name
    try:
        episode = json.loads((episode_dir / "episode.json").read_text())
        config = load_config()
        source_duration = _source_duration(episode_dir, episode)
        trims = {edit.get("type"): edit for edit in episode.get("longform_edits", [])}
        status.update(
            source_duration_seconds=source_duration,
            trim_start_seconds=float(trims.get("trim_start", {}).get("seconds", 0)),
            trim_end_seconds=float(
                trims.get("trim_end", {}).get("seconds", source_duration or 0)
            ),
            delivery_apply_lut=bool(episode.get("delivery_apply_lut", False)),
            delivery_burn_captions=bool(episode.get("delivery_burn_captions", False)),
        )
        for key in (
            "selected_audio_download_url",
            "selected_audio",
            "selected_audio_review_error",
        ):
            status.pop(key, None)
        status.update(_selected_audio_review_fields(episode_dir, episode, config))
        try:
            status["video_preflight"] = _video_preflight(
                episode_dir, episode, config, source_duration
            )
        except (OSError, TypeError, ValueError) as exc:
            status["video_preflight"] = {
                "schema": "cascade.video-preflight/v1",
                "safe": False,
                "error": str(exc),
            }
        _recover_current_video_status(status, episode_dir, episode, config)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        episode, config = {}, {}
    if status.get("video_status") == "preparing":
        if not video_preparation_active(episode_id):
            status.update(
                video_status="failed",
                video_error="Video preparation was interrupted; start it again.",
            )
    elif status.get("video_status") == "ready":
        current_video = current_delivery_video_fields(episode_dir, episode, config)
        status.update(current_video)
        if current_video.get("video_status") != "ready" and not current_video.get(
            "video_repair_required"
        ):
            status.update(
                video_stale=True,
                video_repair_required=False,
                video_error="Video inputs or output changed; prepare the video again.",
            )
    return status


def _write_status(episode_dir: Path, status: dict) -> None:
    atomic_write_json(_status_path(episode_dir), status)


def _episode_dir(episode_id: str) -> Path:
    episode_dir = (EPISODES_DIR / episode_id).resolve()
    if episode_dir.parent != EPISODES_DIR.resolve():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    if not (episode_dir / "episode.json").exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return episode_dir


def _prepare_video(episode_id: str, *, repair_audio: bool = False) -> None:
    episode_dir = EPISODES_DIR / episode_id
    try:
        status = _refresh_status(episode_dir)
        episode = json.loads((episode_dir / "episode.json").read_text())
        config = render_config_for_episode(episode, load_config())

        def progress(percent: float, detail: str) -> None:
            current = _read_status(episode_dir)
            current.update(
                video_status="preparing",
                video_progress=round(percent, 1),
                video_detail=detail,
                video_error=None,
            )
            _write_status(episode_dir, current)

        operation = repair_longform_audio if repair_audio else render_longform
        video = operation(episode_dir, config, progress=progress)
        status = _read_status(episode_dir)
        video_output_stat = _file_stat(episode_dir / "upload_video.mp4")
        status.update(
            video_status="ready",
            video_progress=100.0,
            video_completed_at=_now(),
            video_download_url=_artifact_download_url(
                episode_id, "video", video_output_stat
            ),
            video_source_fingerprint=video["render_fingerprint"],
            video_output_stat=video_output_stat,
            video=video,
            video_operation="repair_audio" if repair_audio else "render",
            video_repair_required=False,
            video_error=None,
        )
        _write_status(episode_dir, status)
    except Exception as exc:
        logger.exception("Video delivery preparation failed for %s", episode_id)
        status = _read_status(episode_dir)
        status.update(
            video_status="failed",
            video_completed_at=_now(),
            video_repair_required=repair_audio,
            video_error=str(exc),
        )
        _write_status(episode_dir, status)
    finally:
        with _running_lock:
            _video_running.discard(episode_id)


@router.get("/{episode_id}/delivery")
async def delivery_status(episode_id: str) -> dict:
    episode_dir = _episode_dir(episode_id)
    return await asyncio.to_thread(_delivery_status, episode_dir)


def _delivery_status(episode_dir: Path) -> dict:
    """Build the validated status document without blocking the API event loop."""
    return {**_refresh_status(episode_dir), "quality": quality_snapshot(episode_dir)}


class DeliveryTrimRequest(BaseModel):
    start_seconds: float
    end_seconds: float


class DeliveryVideoRequest(BaseModel):
    apply_lut: bool = False
    burn_captions: bool = False


def _start_video_job(
    episode_id: str,
    episode_dir: Path,
    status: dict,
    *,
    repair_audio: bool,
) -> dict:
    """Start one serialized longform render or packet-preserving audio repair."""
    with _running_lock:
        if _video_running:
            raise HTTPException(
                status_code=409,
                detail="Another video preparation is already running; wait for it to finish",
            )
        _video_running.add(episode_id)
    operation = "repair_audio" if repair_audio else "render"
    status.update(
        video_status="preparing",
        video_progress=0.0,
        video_detail=(
            "Checking current render and audio"
            if repair_audio
            else "Checking inputs and disk space"
        ),
        video_error=None,
        video_repair_required=False,
        video_started_at=_now(),
        video_operation=operation,
    )
    worker = threading.Thread(
        target=_prepare_video,
        args=(episode_id,),
        kwargs={"repair_audio": repair_audio},
        name=f"delivery-video-{operation}-{episode_id}",
        daemon=True,
    )
    try:
        _write_status(episode_dir, status)
        worker.start()
    except BaseException:
        with _running_lock:
            _video_running.discard(episode_id)
        raise
    return status


@router.put("/{episode_id}/delivery/trim")
async def save_delivery_trim(episode_id: str, request: DeliveryTrimRequest) -> dict:
    episode_dir = _episode_dir(episode_id)
    with _running_lock:
        if episode_id in _video_running:
            raise HTTPException(
                status_code=409, detail="Cannot change trim while preparing"
            )
    episode = json.loads((episode_dir / "episode.json").read_text())
    source_duration = _source_duration(episode_dir, episode)
    if not source_duration:
        raise HTTPException(status_code=422, detail="Source duration is unavailable")
    start = request.start_seconds
    end = request.end_seconds
    # The UI displays milliseconds, so a displayed source end can round up by
    # less than 0.0005s. Accept that presentation delta and clamp to the probe.
    if end <= float(source_duration) + 0.001:
        end = min(end, float(source_duration))
    if not (0 <= start < end <= float(source_duration)):
        raise HTTPException(
            status_code=422,
            detail=f"Trim must satisfy 0 <= start < end <= {float(source_duration):.3f}",
        )
    interior = [
        edit
        for edit in episode.get("longform_edits", [])
        if edit.get("type") not in {"trim_start", "trim_end"}
    ]
    episode["longform_edits"] = [
        *interior,
        {"type": "trim_start", "seconds": round(start, 3)},
        {"type": "trim_end", "seconds": round(end, 3)},
    ]
    atomic_write_json(episode_dir / "episode.json", episode)
    status = _read_status(episode_dir)
    status.update(
        video_status="not_prepared",
        video_stale=True,
        video_repair_required=False,
        video_error="Trim changed; prepare the video again.",
        trim_start_seconds=round(start, 3),
        trim_end_seconds=round(end, 3),
        source_duration_seconds=float(source_duration),
    )
    _write_status(episode_dir, status)
    return status


@router.post("/{episode_id}/delivery/prepare", deprecated=True)
async def prepare_delivery(episode_id: str) -> dict:
    _episode_dir(episode_id)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "podcast_audio_retired",
            "message": "Audio-only podcast preparation is retired.",
        },
    )


@router.get("/{episode_id}/delivery/audio", deprecated=True)
async def download_delivery_audio(episode_id: str):
    """Serve a retained historical MP3 without regenerating or mutating it."""
    episode_dir = _episode_dir(episode_id)
    audio_path = episode_dir / "podcast_audio.mp3"
    if not audio_path.is_file():
        raise HTTPException(
            status_code=404, detail="Historical podcast audio not found"
        )
    return FileResponse(audio_path, media_type="audio/mpeg", filename=audio_path.name)


@router.get("/{episode_id}/delivery/selected-audio")
async def download_selected_audio(episode_id: str):
    """Serve an existing source-clock audio reference without rendering it."""
    episode_dir = _episode_dir(episode_id)
    try:
        episode = json.loads((episode_dir / "episode.json").read_text())
        artifact = _selected_audio_review_artifact(episode_dir, episode, load_config())
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if artifact is None:
        raise HTTPException(status_code=404, detail="Selected audio master not found")
    audio_path = artifact["path"]
    return FileResponse(audio_path, media_type="audio/wav", filename=audio_path.name)


@router.get("/{episode_id}/delivery/metadata", deprecated=True)
async def download_delivery_metadata(episode_id: str):
    """Build a read-only metadata export for retained historical artifacts."""
    episode_dir = _episode_dir(episode_id)
    status = await asyncio.to_thread(_refresh_status, episode_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    metadata = {
        "episode_id": episode_id,
        "title": episode.get("episode_name") or episode.get("title") or "",
        "description": episode.get("episode_description")
        or episode.get("description")
        or "",
        "guest_name": episode.get("guest_name", ""),
        "audio": {
            key: status.get(key)
            for key in (
                "filename",
                "size_bytes",
                "duration_seconds",
                "integrated_lufs",
                "true_peak_dbfs",
                "loudness_range_lu",
            )
        },
        "video": status.get("video"),
        "trim": {
            "start_seconds": status.get("trim_start_seconds"),
            "end_seconds": status.get("trim_end_seconds"),
        },
        "notes": status.get("notes", []),
    }
    return Response(
        json.dumps(metadata, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{episode_id}-delivery.json"'
        },
    )


@router.post("/{episode_id}/delivery/video/prepare", status_code=202)
async def prepare_delivery_video(
    episode_id: str, request: DeliveryVideoRequest | None = None
) -> dict:
    request = request or DeliveryVideoRequest()
    episode_dir = _episode_dir(episode_id)
    status = _refresh_status(episode_dir)
    if not (episode_dir / "source_merged.mp4").exists():
        raise HTTPException(status_code=422, detail="source_merged.mp4 is required")
    episode = json.loads((episode_dir / "episode.json").read_text())
    if not episode.get("crop_config"):
        raise HTTPException(status_code=422, detail="Complete crop setup first")
    episode["delivery_apply_lut"] = request.apply_lut
    episode["delivery_burn_captions"] = request.burn_captions
    try:
        config = render_config_for_episode(episode, load_config())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        audio_path = selected_audio_source(episode_dir, episode, config) or (
            episode_dir / "work" / "audio_mix.wav"
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not audio_path.is_file():
        raise HTTPException(
            status_code=422, detail="Canonical audio source is required"
        )
    if not current_speaker_segments(episode_dir, episode, config):
        raise HTTPException(
            status_code=422,
            detail="Run current speaker analysis before preparing speaker-cut video",
        )
    if not current_diarized_transcript(episode_dir, episode, config):
        raise HTTPException(
            status_code=422,
            detail="Run current transcription before preparing video subtitles",
        )
    try:
        preflight = _video_preflight(episode_dir, episode, config)
        require_render_space(episode_dir, preflight["budget"])
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    atomic_write_json(episode_dir / "episode.json", episode)
    status.update(
        delivery_apply_lut=request.apply_lut,
        delivery_burn_captions=request.burn_captions,
        video_preflight=preflight,
    )
    return _start_video_job(episode_id, episode_dir, status, repair_audio=False)


@router.post("/{episode_id}/delivery/video/repair-audio", status_code=202)
async def repair_delivery_video_audio(episode_id: str) -> dict:
    """Normalize a current longform's AAC while preserving its video packets."""
    episode_dir = _episode_dir(episode_id)
    status = _refresh_status(episode_dir)
    try:
        episode = json.loads((episode_dir / "episode.json").read_text())
        config = render_config_for_episode(episode, load_config())
        current = _current_video_record(episode_dir, episode, config)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if current is None:
        raise HTTPException(
            status_code=409,
            detail="Audio repair requires a current manifest-backed longform render",
        )
    status["video_audio"] = _video_audio_status(episode_dir, config)
    return _start_video_job(episode_id, episode_dir, status, repair_audio=True)


@router.get("/{episode_id}/delivery/video")
async def download_delivery_video(episode_id: str):
    episode_dir = _episode_dir(episode_id)
    status = await asyncio.to_thread(_refresh_status, episode_dir)
    video_path = episode_dir / "upload_video.mp4"
    if status.get("video_status") != "ready" or not video_path.exists():
        raise HTTPException(
            status_code=404, detail="Prepared upload video is not ready"
        )
    return FileResponse(video_path, media_type="video/mp4", filename=video_path.name)
