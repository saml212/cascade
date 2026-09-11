"""Prepare local, upload-ready podcast audio without publishing it."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agents.pipeline import load_config
from agents.podcast_feed import PodcastFeedAgent
from agents.qa import quality_snapshot
from lib.atomic_write import atomic_write_json
from lib.audio_mix import generate_audio_mix
from lib.delivery_video import build_keep_intervals, render_delivery_video
from lib.ffprobe import get_duration
from lib.loudness import measure_loudness
from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/episodes", tags=["delivery"])

EPISODES_DIR = get_episodes_dir()
STATUS_NAME = "delivery.json"
_running: set[str] = set()
_video_running: set[str] = set()
_running_lock = threading.Lock()


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


_DELIVERY_PROCESSING_KEYS = (
    "audio_enhance",
    "audio_enhance_mode",
    "audio_target_lufs",
    "audio_target_lra",
    "audio_target_tp",
    "audio_highpass_hz",
    "audio_per_speaker_leveling",
    "audio_per_speaker_dynaudnorm",
    "audio_denoise_model",
    "use_hardware_accel",
)


def _source_fingerprint(
    episode_dir: Path, episode: dict, config: dict | None = None
) -> str:
    """Fingerprint metadata and existing media inputs that affect the mix."""
    paths = [episode_dir / "source_merged.mp4"]
    for track in episode.get("audio_tracks", []):
        value = track.get("dest_path") or track.get("path")
        if value:
            paths.append(Path(value))
    inputs = [
        {"path": str(path.resolve()), **_file_stat(path)}
        for path in sorted(set(paths))
        if path.exists()
    ]
    payload = {
        "episode": {
            key: episode.get(key)
            for key in (
                "audio_sync",
                "audio_mix",
                "audio_tracks",
                "crop_config",
                "duration_seconds",
                "longform_edits",
                "source_properties",
            )
        },
        "inputs": inputs,
    }
    if config is not None:
        payload["processing"] = {
            key: config.get("processing", {}).get(key)
            for key in _DELIVERY_PROCESSING_KEYS
        }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _video_fingerprint(
    episode_dir: Path, episode: dict, config: dict, audio: Path
) -> str:
    apply_lut = bool(episode.get("delivery_apply_lut", False))
    payload = {
        "source": _source_fingerprint(episode_dir, episode, config),
        "audio": _file_stat(audio),
        "crop_config": episode.get("crop_config"),
        "edits": episode.get("longform_edits"),
        "apply_lut": apply_lut,
        "lut_path": config.get("processing", {}).get("lut_path") if apply_lut else None,
        "lut_interpolation": (
            config.get("processing", {}).get("lut_interpolation") if apply_lut else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _refresh_status(episode_dir: Path) -> dict:
    """Convert abandoned or changed delivery records into actionable states."""
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
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        episode, config = {}, {}
    if status.get("status") == "preparing":
        with _running_lock:
            active = episode_id in _running
        if not active:
            partial_audio = episode_dir / "podcast_audio.mp3.tmp.mp3"
            partial_audio.unlink(missing_ok=True)
            status = {
                **status,
                "status": "failed",
                "completed_at": _now(),
                "error": "Preparation was interrupted; start it again.",
            }
            _write_status(episode_dir, status)
    elif status.get("status") == "ready":
        audio_path = episode_dir / "podcast_audio.mp3"
        try:
            stored_fingerprint = status.get("source_fingerprint")
            current_fingerprint = _source_fingerprint(episode_dir, episode, config)
            legacy_fingerprint = _source_fingerprint(episode_dir, episode)
            if stored_fingerprint == legacy_fingerprint:
                status["source_fingerprint"] = current_fingerprint
                stored_fingerprint = current_fingerprint
                _write_status(episode_dir, status)
            stale = (
                not audio_path.exists()
                or status.get("output_stat") != _file_stat(audio_path)
                or stored_fingerprint != current_fingerprint
            )
        except (OSError, json.JSONDecodeError):
            stale = True
        if stale:
            status = {
                **status,
                "status": "not_prepared",
                "stale": True,
                "error": "Inputs or prepared audio changed; prepare the episode again.",
            }
            _write_status(episode_dir, status)
    if status.get("video_status") == "preparing":
        with _running_lock:
            video_active = episode_id in _video_running
        if not video_active:
            partial_video = episode_dir / "upload_video.tmp.mp4"
            partial_video.unlink(missing_ok=True)
            status.update(
                video_status="failed",
                video_error="Video preparation was interrupted; start it again.",
            )
            _write_status(episode_dir, status)
    elif status.get("video_status") == "ready":
        video_path = episode_dir / "upload_video.mp4"
        try:
            audio_path = episode_dir / "work" / "audio_mix.wav"
            expected_fingerprint = _video_fingerprint(
                episode_dir, episode, config, audio_path
            )
            video_stale = (
                not video_path.exists()
                or status.get("video_output_stat") != _file_stat(video_path)
                or status.get("video_source_fingerprint") != expected_fingerprint
            )
        except (OSError, json.JSONDecodeError):
            video_stale = True
        if video_stale:
            status.update(
                video_status="not_prepared",
                video_stale=True,
                video_error="Video inputs or output changed; prepare the video again.",
            )
            _write_status(episode_dir, status)
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


def _has_audio_input(episode_dir: Path, episode: dict) -> bool:
    if (episode_dir / "source_merged.mp4").exists():
        return True
    for track in episode.get("audio_tracks", []):
        path = track.get("dest_path") or track.get("path")
        if path and Path(path).exists():
            return True
    return False


def _prepare_delivery(episode_id: str) -> None:
    """Synchronous worker used by the background thread and unit tests."""
    episode_dir = EPISODES_DIR / episode_id
    started_at = _now()
    _write_status(
        episode_dir,
        {"status": "preparing", "episode_id": episode_id, "started_at": started_at},
    )
    try:
        episode = json.loads((episode_dir / "episode.json").read_text())
        config = load_config()
        source_fingerprint = _source_fingerprint(episode_dir, episode, config)
        mix_path = generate_audio_mix(episode_dir, episode, config)
        if not mix_path or not mix_path.exists() or mix_path.stat().st_size <= 44:
            raise RuntimeError("Audio mix could not be generated")

        audio_path = PodcastFeedAgent(episode_dir, config).prepare_local_audio()
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise RuntimeError("Podcast MP3 was not created")

        agent = PodcastFeedAgent(episode_dir, config)
        duration = agent._get_duration(audio_path)
        expected_duration = _source_duration(episode_dir, episode)
        if duration <= 0:
            raise RuntimeError("Podcast MP3 has no measurable duration")
        if not expected_duration:
            raise RuntimeError("Episode has no expected video duration for validation")
        expected_duration = sum(
            end - start
            for start, end in build_keep_intervals(
                float(expected_duration), episode.get("longform_edits", [])
            )
        )
        duration_delta = abs(duration - float(expected_duration))
        if duration_delta > 1.0:
            raise RuntimeError(
                f"Podcast duration is {duration:.3f}s; expected {float(expected_duration):.3f}s "
                f"(difference {duration_delta:.3f}s)"
            )
        loudness = measure_loudness(audio_path)
        if not loudness:
            raise RuntimeError("Podcast MP3 loudness could not be measured")
        target_lufs = float(config.get("processing", {}).get("audio_target_lufs", -16))
        integrated_lufs = float(loudness["integrated_lufs"])
        if abs(integrated_lufs - target_lufs) > 1.0:
            raise RuntimeError(
                f"Podcast loudness is {integrated_lufs:.1f} LUFS; expected "
                f"{target_lufs:.1f} ±1.0 LU"
            )
        true_peak = float(loudness["true_peak_dbfs"])
        if true_peak > -0.5:
            raise RuntimeError(
                f"Podcast true peak is {true_peak:.1f} dBFS; expected at most -0.5 dBFS"
            )

        result = {
            "status": "ready",
            "episode_id": episode_id,
            "started_at": started_at,
            "completed_at": _now(),
            "filename": audio_path.name,
            "download_url": f"/api/episodes/{episode_id}/delivery/audio",
            "size_bytes": audio_path.stat().st_size,
            "duration_seconds": round(duration, 3),
            "expected_duration_seconds": round(float(expected_duration), 3),
            "duration_difference_seconds": round(duration_delta, 3),
            **loudness,
            "target_lufs": target_lufs,
            "source_fingerprint": source_fingerprint,
            "output_stat": _file_stat(audio_path),
            "notes": [
                "Local file only; nothing has been uploaded or published.",
                "Duration, integrated loudness, loudness range, and true peak passed automated checks.",
            ],
        }
        _write_status(episode_dir, result)
    except Exception as exc:
        logger.exception("Delivery preparation failed for %s", episode_id)
        _write_status(
            episode_dir,
            {
                "status": "failed",
                "episode_id": episode_id,
                "started_at": started_at,
                "completed_at": _now(),
                "error": str(exc),
            },
        )
    finally:
        with _running_lock:
            _running.discard(episode_id)


def _prepare_video(episode_id: str) -> None:
    episode_dir = EPISODES_DIR / episode_id
    try:
        status = _refresh_status(episode_dir)
        episode = json.loads((episode_dir / "episode.json").read_text())
        config = load_config()
        # Delivery defaults to source color. A LUT is destructive and is only
        # enabled when an operator explicitly identifies the episode as log.
        if not episode.get("delivery_apply_lut", False):
            config = copy.deepcopy(config)
            config.setdefault("processing", {})["lut_path"] = ""
        audio_path = episode_dir / "work" / "audio_mix.wav"
        source_fingerprint = _video_fingerprint(
            episode_dir, episode, config, audio_path
        )

        def progress(percent: float, detail: str) -> None:
            current = _read_status(episode_dir)
            current.update(
                video_status="preparing",
                video_progress=round(percent, 1),
                video_detail=detail,
                video_error=None,
            )
            _write_status(episode_dir, current)

        video = render_delivery_video(
            episode_dir, episode, config, audio_path, progress=progress
        )
        status = _read_status(episode_dir)
        status.update(
            video_status="ready",
            video_progress=100.0,
            video_completed_at=_now(),
            video_download_url=f"/api/episodes/{episode_id}/delivery/video",
            video_source_fingerprint=source_fingerprint,
            video_output_stat=_file_stat(episode_dir / "upload_video.mp4"),
            video=video,
            video_error=None,
        )
        _write_status(episode_dir, status)
    except Exception as exc:
        logger.exception("Video delivery preparation failed for %s", episode_id)
        status = _read_status(episode_dir)
        status.update(
            video_status="failed",
            video_completed_at=_now(),
            video_error=str(exc),
        )
        _write_status(episode_dir, status)
    finally:
        with _running_lock:
            _video_running.discard(episode_id)


@router.get("/{episode_id}/delivery")
async def delivery_status(episode_id: str) -> dict:
    episode_dir = _episode_dir(episode_id)
    return {**_refresh_status(episode_dir), "quality": quality_snapshot(episode_dir)}


class DeliveryTrimRequest(BaseModel):
    start_seconds: float
    end_seconds: float


class DeliveryVideoRequest(BaseModel):
    apply_lut: bool = False


@router.put("/{episode_id}/delivery/trim")
async def save_delivery_trim(episode_id: str, request: DeliveryTrimRequest) -> dict:
    episode_dir = _episode_dir(episode_id)
    with _running_lock:
        if episode_id in _running or episode_id in _video_running:
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
        status="not_prepared",
        stale=True,
        error="Trim changed; prepare audio again.",
        video_status="not_prepared",
        video_stale=True,
        video_error="Trim changed; prepare video again after audio.",
        trim_start_seconds=round(start, 3),
        trim_end_seconds=round(end, 3),
        source_duration_seconds=float(source_duration),
    )
    _write_status(episode_dir, status)
    return status


@router.post("/{episode_id}/delivery/prepare", status_code=202)
async def prepare_delivery(episode_id: str) -> dict:
    episode_dir = _episode_dir(episode_id)
    episode = json.loads((episode_dir / "episode.json").read_text())
    if not _has_audio_input(episode_dir, episode):
        raise HTTPException(status_code=422, detail="Episode has no usable audio input")

    with _running_lock:
        if episode_id in _running or episode_id in _video_running:
            raise HTTPException(
                status_code=409, detail="Delivery preparation is already running"
            )
        _running.add(episode_id)

    _write_status(
        episode_dir,
        {"status": "preparing", "episode_id": episode_id, "started_at": _now()},
    )
    threading.Thread(
        target=_prepare_delivery,
        args=(episode_id,),
        name=f"delivery-{episode_id}",
        daemon=True,
    ).start()
    return _refresh_status(episode_dir)


@router.get("/{episode_id}/delivery/audio")
async def download_delivery_audio(episode_id: str):
    episode_dir = _episode_dir(episode_id)
    status = _refresh_status(episode_dir)
    audio_path = episode_dir / "podcast_audio.mp3"
    if status.get("status") != "ready" or not audio_path.exists():
        raise HTTPException(
            status_code=404, detail="Prepared podcast audio is not ready"
        )
    return FileResponse(audio_path, media_type="audio/mpeg", filename=audio_path.name)


@router.get("/{episode_id}/delivery/metadata")
async def download_delivery_metadata(episode_id: str):
    episode_dir = _episode_dir(episode_id)
    status = _refresh_status(episode_dir)
    if status.get("status") != "ready":
        raise HTTPException(status_code=404, detail="Delivery metadata is not ready")
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
    metadata_path = episode_dir / "delivery_metadata.json"
    atomic_write_json(metadata_path, metadata)
    return FileResponse(
        metadata_path,
        media_type="application/json",
        filename=f"{episode_id}-delivery.json",
    )


@router.post("/{episode_id}/delivery/video/prepare", status_code=202)
async def prepare_delivery_video(
    episode_id: str, request: DeliveryVideoRequest | None = None
) -> dict:
    request = request or DeliveryVideoRequest()
    episode_dir = _episode_dir(episode_id)
    status = _refresh_status(episode_dir)
    if status.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Prepare and verify audio first")
    if not (episode_dir / "source_merged.mp4").exists():
        raise HTTPException(status_code=422, detail="source_merged.mp4 is required")
    if not (episode_dir / "work" / "audio_mix.wav").exists():
        raise HTTPException(
            status_code=422, detail="Canonical mastered WAV is required"
        )
    with _running_lock:
        if episode_id in _running:
            raise HTTPException(
                status_code=409, detail="Audio preparation is still running"
            )
        if _video_running:
            raise HTTPException(
                status_code=409,
                detail="Another video preparation is already running; wait for it to finish",
            )
        _video_running.add(episode_id)
    episode = json.loads((episode_dir / "episode.json").read_text())
    episode["delivery_apply_lut"] = request.apply_lut
    atomic_write_json(episode_dir / "episode.json", episode)
    status.update(
        video_status="preparing",
        video_progress=0.0,
        video_detail="Checking inputs and disk space",
        video_error=None,
        video_started_at=_now(),
        delivery_apply_lut=request.apply_lut,
    )
    _write_status(episode_dir, status)
    threading.Thread(
        target=_prepare_video,
        args=(episode_id,),
        name=f"delivery-video-{episode_id}",
        daemon=True,
    ).start()
    return status


@router.get("/{episode_id}/delivery/video")
async def download_delivery_video(episode_id: str):
    episode_dir = _episode_dir(episode_id)
    status = _refresh_status(episode_dir)
    video_path = episode_dir / "upload_video.mp4"
    if status.get("video_status") != "ready" or not video_path.exists():
        raise HTTPException(
            status_code=404, detail="Prepared upload video is not ready"
        )
    return FileResponse(video_path, media_type="video/mp4", filename=video_path.name)
