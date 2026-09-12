"""Prepare local, upload-ready podcast audio without publishing it."""

from __future__ import annotations

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

from agents.longform_render import render_longform, repair_longform_audio
from agents.pipeline import load_config
from agents.podcast_feed import (
    PODCAST_AUDIO_PROCESSING_KEYS,
    PodcastFeedAgent,
    current_podcast_audio,
)
from agents.podcast_feed import (
    podcast_source_fingerprint as _source_fingerprint,
)
from agents.qa import quality_snapshot
from agents.speaker_cut import current_speaker_segments
from agents.transcribe import current_diarized_transcript
from lib.atomic_write import atomic_write_json
from lib.audio_mix import (
    audio_selection_settings,
    generate_audio_mix,
    selected_audio_source,
)
from lib.delivery_video import (
    build_keep_intervals,
    current_longform_render,
    longform_render_fingerprint,
    read_render_manifest,
    render_config_for_episode,
    render_space_budget,
    render_space_status,
    require_render_space,
)
from lib.encoding import get_video_encoding_policy
from lib.ffprobe import get_duration
from lib.loudness import delivery_loudness_policy, loudness_status, measure_loudness
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


def _artifact_download_url(episode_id: str, artifact: str, output_stat: dict) -> str:
    revision = f"{int(output_stat['size']):x}-{int(output_stat['mtime_ns']):x}"
    return f"/api/episodes/{episode_id}/delivery/{artifact}?v={revision}"


def _legacy_source_fingerprint(
    episode_dir: Path, episode: dict, config: dict | None = None
) -> str:
    paths = [episode_dir / "source_merged.mp4"]
    paths.extend(
        Path(value)
        for track in episode.get("audio_tracks", [])
        if (value := track.get("dest_path") or track.get("path"))
    )
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
        "inputs": [
            {"path": str(path.resolve()), **_file_stat(path)}
            for path in sorted(set(paths))
            if path.exists()
        ],
    }
    if config is not None:
        payload["processing"] = {
            key: config.get("processing", {}).get(key)
            for key in PODCAST_AUDIO_PROCESSING_KEYS
        }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def migrate_unchanged_delivery_audio_fingerprint(
    episode_dir: Path, old_episode: dict, new_episode: dict, config: dict
) -> bool:
    """Preserve prepared audio identity across edits unrelated to its bytes."""
    if audio_selection_settings(old_episode) != audio_selection_settings(new_episode):
        return False
    try:
        selected_audio = selected_audio_source(episode_dir, new_episode, config)
    except ValueError:
        return False
    status = _read_status(episode_dir)
    stored = status.get("source_fingerprint")
    accepted = {
        _source_fingerprint(episode_dir, old_episode, config),
        _source_fingerprint(episode_dir, old_episode),
    }
    if selected_audio is None:
        accepted.update(
            {
                _legacy_source_fingerprint(episode_dir, old_episode, config),
                _legacy_source_fingerprint(episode_dir, old_episode),
            }
        )
    if status.get("status") != "ready" or stored not in accepted:
        return False
    status["source_fingerprint"] = _source_fingerprint(episode_dir, new_episode, config)
    _write_status(episode_dir, status)
    return True


def _video_fingerprint(
    episode_dir: Path, episode: dict, config: dict, audio: Path
) -> str | None:
    segment_document = current_speaker_segments(episode_dir, episode, config)
    if not segment_document:
        return None
    segments = segment_document.get("segments", [])
    current = current_longform_render(episode_dir, episode, config, audio, segments)
    if current:
        return current["fingerprint"]
    return longform_render_fingerprint(
        episode_dir,
        episode,
        config,
        audio,
        segments,
    )


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


def _video_audio_status(episode_dir: Path, config: dict) -> dict:
    """Classify recorded final AAC evidence without decoding on status polls."""
    record = read_render_manifest(episode_dir).get("longform", {})
    return loudness_status(
        record.get("output", {}).get("audio_loudness"),
        delivery_loudness_policy(config, "longform"),
    )


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
            delivery_burn_captions=bool(episode.get("delivery_burn_captions", False)),
        )
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
        audio_input_error = None
        try:
            output_stat = _file_stat(episode_dir / "podcast_audio.mp3")
            status["download_url"] = _artifact_download_url(
                episode_id, "audio", output_stat
            )
            stored_fingerprint = status.get("source_fingerprint")
            current_fingerprint = _source_fingerprint(episode_dir, episode, config)
            audio_path = current_podcast_audio(
                episode_dir, episode, config, verify_content=False
            )
            stale = (
                audio_path is None
                or status.get("output_stat") != output_stat
                or stored_fingerprint != current_fingerprint
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            stale = True
            audio_input_error = str(exc)
        if stale:
            status = {
                **status,
                "status": "not_prepared",
                "stale": True,
                "error": audio_input_error
                or "Inputs or prepared audio changed; prepare the episode again.",
            }
            _write_status(episode_dir, status)
    if status.get("video_status") == "preparing":
        with _running_lock:
            video_active = episode_id in _video_running
        if not video_active:
            for partial_video in episode_dir.glob(".upload_video-*.mp4"):
                partial_video.unlink(missing_ok=True)
            (episode_dir / "upload_video.tmp.mp4").unlink(missing_ok=True)
            status.update(
                video_status="failed",
                video_error="Video preparation was interrupted; start it again.",
            )
            _write_status(episode_dir, status)
    elif status.get("video_status") == "ready":
        video_path = episode_dir / "upload_video.mp4"
        video_input_error = None
        try:
            video_output_stat = _file_stat(video_path)
            status["video_download_url"] = _artifact_download_url(
                episode_id, "video", video_output_stat
            )
            audio_path = selected_audio_source(episode_dir, episode, config) or (
                episode_dir / "work" / "audio_mix.wav"
            )
            expected_fingerprint = _video_fingerprint(
                episode_dir, episode, config, audio_path
            )
            video_stale = (
                not video_path.exists()
                or not expected_fingerprint
                or status.get("video_output_stat") != video_output_stat
                or status.get("video_source_fingerprint") != expected_fingerprint
            )
            video_audio = _video_audio_status(episode_dir, config)
            status["video_audio"] = video_audio
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            video_stale = True
            video_input_error = str(exc)
        if video_stale:
            status.update(
                video_status="not_prepared",
                video_stale=True,
                video_repair_required=False,
                video_error=video_input_error
                or "Video inputs or output changed; prepare the video again.",
            )
            _write_status(episode_dir, status)
        elif not video_audio["safe"]:
            status.update(
                video_status="not_prepared",
                video_stale=False,
                video_repair_required=True,
                video_error=(
                    "; ".join(video_audio.get("errors", []))
                    or video_audio.get("error")
                    or "Encoded video audio needs repair."
                ),
            )
            _write_status(episode_dir, status)
        else:
            status["video_repair_required"] = False
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
        if (
            current_podcast_audio(episode_dir, episode, config, verify_content=False)
            != audio_path
        ):
            raise RuntimeError("Podcast MP3 proof is missing or stale")

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

        output_stat = _file_stat(audio_path)
        result = {
            "status": "ready",
            "episode_id": episode_id,
            "started_at": started_at,
            "completed_at": _now(),
            "filename": audio_path.name,
            "download_url": _artifact_download_url(
                episode_id, "audio", output_stat
            ),
            "size_bytes": audio_path.stat().st_size,
            "duration_seconds": round(duration, 3),
            "expected_duration_seconds": round(float(expected_duration), 3),
            "duration_difference_seconds": round(duration_delta, 3),
            **loudness,
            "target_lufs": target_lufs,
            "source_fingerprint": source_fingerprint,
            "output_stat": output_stat,
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
    """Start one serialized longform render or audio-only repair job."""
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
    _write_status(episode_dir, status)
    threading.Thread(
        target=_prepare_video,
        args=(episode_id,),
        kwargs={"repair_audio": repair_audio},
        name=f"delivery-video-{operation}-{episode_id}",
        daemon=True,
    ).start()
    return status


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
        video_repair_required=False,
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
        raise HTTPException(
            status_code=409,
            detail=status.get("error") or "Prepare and verify audio first",
        )
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
    status = _refresh_status(episode_dir)
    video_path = episode_dir / "upload_video.mp4"
    if status.get("video_status") != "ready" or not video_path.exists():
        raise HTTPException(
            status_code=404, detail="Prepared upload video is not ready"
        )
    return FileResponse(video_path, media_type="video/mp4", filename=video_path.name)
