"""Guarded restoration of a preserved source master."""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import stat
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from agents import PIPELINE_ORDER
from lib.apfs_clone import (
    CloneSafetyError,
    StaleFileError,
    assert_snapshot,
    clone_to_new_path,
    move_to_new_path,
    snapshot_path,
)
from lib.atomic_write import atomic_write_json
from lib.audio_mix import json_fingerprint
from lib.ffprobe import probe
from lib.paths import get_episodes_dir
from server.routes import clips, delivery, pipeline, require_episode_dir

router = APIRouter(prefix="/api/episodes", tags=["source-recovery"])
EPISODES_DIR = get_episodes_dir()

_SOURCE_NAME = "source_merged.mp4"
_CANDIDATE_NAME = "source_merged_original.mp4"
_RECOVERY_LOCK = threading.Lock()
_SOURCE_DEPENDENT_AGENTS = tuple(PIPELINE_ORDER[PIPELINE_ORDER.index("stitch") + 1 :])

EvidenceMethod = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
EvidenceSummary = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
EvidenceReference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]
TrackName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[^/\\\x00]+$",
    ),
]


class SourceClockEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    method: EvidenceMethod
    anchor_count: int = Field(ge=2, le=10_000)
    track_count: int = Field(ge=0, le=100)
    summary: EvidenceSummary
    fit_r_squared: float = Field(ge=0, le=1)
    artifact_references: list[EvidenceReference] = Field(min_length=1, max_length=20)


class SourceClockFit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    offset_seconds: float
    tempo_factor: float = Field(ge=0.99, le=1.01)
    drift_rate_ppm: float
    sync_track: TrackName | None = None
    evidence: SourceClockEvidence

    @model_validator(mode="after")
    def validate_consistency(self) -> SourceClockFit:
        expected_drift = (self.tempo_factor - 1.0) * 1_000_000
        if not math.isclose(
            self.drift_rate_ppm, expected_drift, rel_tol=0, abs_tol=0.001
        ):
            raise ValueError("drift_rate_ppm does not match tempo_factor")
        if self.sync_track is None:
            if self.evidence.track_count != 0:
                raise ValueError("track_count requires sync_track")
            if (
                self.offset_seconds != 0
                or self.tempo_factor != 1
                or self.drift_rate_ppm != 0
            ):
                raise ValueError("camera-source audio must use an identity clock")
        elif self.evidence.track_count == 0:
            raise ValueError("sync_track requires a positive track_count")
        return self


class SourceRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    expected_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_candidate_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate: Literal["source_merged_original.mp4"]
    source_clock: SourceClockFit


class RecoveryConflict(RuntimeError):
    """The inspected source-recovery state is no longer safe to apply."""


class RecoveryApplyError(RuntimeError):
    """Recovery failed after mutation began and rollback was attempted."""


def _public_identity(snapshot: dict, filename: str) -> dict:
    return {
        "filename": filename,
        "revision": f"sha256:{snapshot['sha256']}",
        "method": "sha256-full/v1",
        "size_bytes": snapshot["size_bytes"],
        "mtime_ns": snapshot["mtime_ns"],
        "ctime_ns": snapshot["ctime_ns"],
        "device": snapshot["device"],
        "inode": snapshot["inode"],
        "mode": snapshot["mode"],
        "uid": snapshot["uid"],
        "gid": snapshot["gid"],
        "flags": snapshot["flags"],
        "xattrs": snapshot["xattrs"],
    }


def _stat_signature(path: Path) -> tuple[int, ...]:
    value = path.stat(follow_symlinks=False)
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        getattr(value, "st_flags", 0),
    )


def _media_details(path: Path) -> dict:
    before = _stat_signature(path)
    data = probe(path)
    try:
        duration = float(data.get("format", {}).get("duration", 0))
        video = next(
            stream
            for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
        )
        fps = round(float(Fraction(video.get("r_frame_rate", "30/1"))), 3)
        properties = {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "codec": video.get("codec_name", ""),
            "pix_fmt": video.get("pix_fmt", ""),
            "fps": fps,
            "color_space": video.get("color_space", ""),
            "color_primaries": video.get("color_primaries", ""),
            "color_transfer": video.get("color_transfer", ""),
        }
    except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError(f"Video properties are invalid: {path.name}") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Media duration is invalid: {path.name}")
    snapshot = snapshot_path(path)
    if _stat_signature(path) != before:
        raise StaleFileError(f"media changed while being inspected: {path.name}")
    return {
        "snapshot": snapshot,
        "duration_seconds": duration,
        "source_properties": properties,
    }


def _read_episode(path: Path) -> tuple[dict, dict, bytes]:
    before = snapshot_path(path)
    try:
        content = path.read_bytes()
        episode = json.loads(content)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("episode.json is unreadable") from error
    if not isinstance(episode, dict):
        raise TypeError("episode.json must contain an object")
    if snapshot_path(path) != before:
        raise StaleFileError("episode.json changed while being read")
    return episode, before, content


def _next_predecessor_name(episode_dir: Path, source_sha256: str) -> str:
    stem = f"source_merged.pre-recovery-{source_sha256[:12]}"
    for suffix in ("", *(f"-{index}" for index in range(1, 1000))):
        name = f"{stem}{suffix}.mp4"
        path = episode_dir / name
        if not path.exists() and not path.is_symlink():
            return name
    raise RecoveryConflict("Could not allocate a unique predecessor filename")


def _pipeline_invalidation(episode: dict) -> tuple[dict, list[str]]:
    pipeline_state = episode.get("pipeline") or {}
    if not isinstance(pipeline_state, dict):
        raise TypeError("episode pipeline state must be an object")
    completed = pipeline_state.get("agents_completed", [])
    if not isinstance(completed, list):
        raise TypeError("completed pipeline agents must be a list")
    invalidated = [agent for agent in _SOURCE_DEPENDENT_AGENTS if agent in completed]
    return pipeline_state, invalidated


def _build_inspection(episode_id: str, episode_dir: Path) -> tuple[dict, dict]:
    episode, episode_snapshot, episode_bytes = _read_episode(
        episode_dir / "episode.json"
    )
    current = _media_details(episode_dir / _SOURCE_NAME)
    candidate = _media_details(episode_dir / _CANDIDATE_NAME)
    current_identity = {
        **_public_identity(current["snapshot"], _SOURCE_NAME),
        "duration_seconds": current["duration_seconds"],
        "source_properties": current["source_properties"],
    }
    candidate_identity = {
        **_public_identity(candidate["snapshot"], _CANDIDATE_NAME),
        "duration_seconds": candidate["duration_seconds"],
        "source_properties": candidate["source_properties"],
    }
    predecessor_name = _next_predecessor_name(
        episode_dir, current["snapshot"]["sha256"]
    )
    _, invalidated_agents = _pipeline_invalidation(episode)
    core = {
        "schema": "cascade.source-recovery-inspection/v1",
        "episode_id": episode_id,
        "episode_revision": _public_identity(episode_snapshot, "episode.json"),
        "current_source": current_identity,
        "candidate_source": candidate_identity,
        "candidate_episode_values": {
            "duration_seconds": candidate["duration_seconds"],
            "source_properties": candidate["source_properties"],
        },
        "preservation_plan": {
            "clone_method": "APFS clone; no byte-copy fallback",
            "candidate_filename": _CANDIDATE_NAME,
            "canonical_filename": _SOURCE_NAME,
            "predecessor_filename": predecessor_name,
            "preserve_candidate": True,
            "preserve_derived_artifacts": True,
            "delete_paths": [],
            "pipeline_agents_to_invalidate": invalidated_agents,
            "derived_artifacts": (
                "retained; source and metadata fingerprints become stale"
            ),
        },
        "already_recovered": (
            current["snapshot"]["sha256"] == candidate["snapshot"]["sha256"]
        ),
        "current_episode_values": {
            key: episode.get(key)
            for key in ("duration_seconds", "source_properties", "audio_sync")
        },
    }
    revision = json_fingerprint(core)
    blockers = []
    if core["already_recovered"]:
        blockers.append("canonical source already matches the preserved candidate")
    inspection = {
        **core,
        "revision": revision,
        "eligible": not blockers,
        "blockers": blockers,
        "apply_binding": {
            "expected_revision": revision,
            "candidate": _CANDIDATE_NAME,
        },
        "candidate_confirmation": {
            "required_post_field": "expected_candidate_revision",
            "revision_to_verify_against_the_external_audit": candidate_identity[
                "revision"
            ],
        },
        "source_clock_contract": {
            "server_infers_fit": False,
            "json_schema": SourceClockFit.model_json_schema(),
            "note": "Submit a reviewed source-clock fit and its evidence.",
        },
    }
    state = {
        "episode": episode,
        "episode_bytes": episode_bytes,
        "episode_snapshot": episode_snapshot,
        "current_snapshot": current["snapshot"],
        "candidate_snapshot": candidate["snapshot"],
    }
    return inspection, state


def _registered_job_reason(episode_id: str, episode_dir: Path) -> str | None:
    worker = pipeline._running.get(episode_id)
    if worker is not None and worker.is_alive():
        return "pipeline"
    if episode_id in delivery._running or episode_id in delivery._video_running:
        return "delivery"
    prefix = f"{episode_dir.resolve()}:"
    if any(key.startswith(prefix) for key in clips._active_render_jobs):
        return "clip render"
    return None


def _audio_sync(episode: dict, fit: SourceClockFit, duration: float) -> dict:
    if fit.sync_track is not None:
        tracks = episode.get("audio_tracks")
        if not isinstance(tracks, list) or not any(
            isinstance(track, dict) and track.get("filename") == fit.sync_track
            for track in tracks
        ):
            raise RecoveryConflict("sync_track is not registered to this episode")
    evidence = fit.evidence.model_dump()
    result = {
        "status": "verified",
        "video_duration": duration,
        "confidence": evidence["fit_r_squared"],
        "consensus_tracks": evidence["track_count"],
        "checkpoint_count": evidence["anchor_count"],
        "good_checkpoint_count": evidence["anchor_count"],
        "offset_seconds": fit.offset_seconds,
        "tempo_factor": fit.tempo_factor,
        "drift_rate_ppm": fit.drift_rate_ppm,
        "drift_total_seconds": round((fit.tempo_factor - 1.0) * duration, 6),
        "r_squared": evidence["fit_r_squared"],
        "drift_status": (
            "identity"
            if fit.offset_seconds == 0 and fit.tempo_factor == 1
            else "applied"
        ),
        "manually_adjusted": True,
        "source_clock_evidence": evidence,
    }
    if fit.sync_track is not None:
        result["sync_track"] = fit.sync_track
    return result


def _invalidate_pipeline(episode: dict) -> list[str]:
    pipeline_state, invalidated = _pipeline_invalidation(episode)
    dependent = set(_SOURCE_DEPENDENT_AGENTS)
    pipeline_state["agents_completed"] = [
        agent
        for agent in pipeline_state.get("agents_completed", [])
        if agent not in dependent
    ]
    errors = pipeline_state.get("errors", {})
    if not isinstance(errors, dict):
        raise TypeError("pipeline errors must be an object")
    pipeline_state["errors"] = {
        name: message for name, message in errors.items() if name not in dependent
    }
    pipeline_state.pop("completed_at", None)
    pipeline_state.pop("current_agent", None)
    episode["pipeline"] = pipeline_state
    episode["status"] = "ready_to_render"
    return invalidated


def _remove_clone(path: Path, inode: int) -> None:
    try:
        if path.stat(follow_symlinks=False).st_ino == inode:
            path.unlink()
    except FileNotFoundError:
        pass


def _rollback_files(
    current: Path,
    temporary: Path,
    cloned: dict,
    installed: dict | None,
    predecessor: dict | None,
) -> None:
    if installed is not None:
        move_to_new_path(installed, temporary)
    if predecessor is not None:
        move_to_new_path(predecessor, current)
    _remove_clone(temporary, cloned["inode"])


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), mode)
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _restore_episode(path: Path, content: bytes, mode: int) -> None:
    _atomic_write_bytes(path, content, mode)
    if path.read_bytes() != content or stat.S_IMODE(path.stat().st_mode) != mode:
        raise OSError("episode.json rollback verification failed")


def _apply_recovery(
    episode_id: str, episode_dir: Path, request: SourceRecoveryRequest
) -> dict:
    inspection, state = _build_inspection(episode_id, episode_dir)
    if request.expected_revision != inspection["revision"]:
        raise RecoveryConflict("Source recovery revision is stale; inspect again")
    if (
        request.expected_candidate_revision
        != inspection["candidate_source"]["revision"]
    ):
        raise RecoveryConflict("Candidate revision does not match the reviewed audit")
    if not inspection["eligible"]:
        raise RecoveryConflict("; ".join(inspection["blockers"]))

    episode = state["episode"]
    updates = inspection["candidate_episode_values"] | {
        "audio_sync": _audio_sync(
            episode,
            request.source_clock,
            inspection["candidate_source"]["duration_seconds"],
        )
    }
    current = episode_dir / _SOURCE_NAME
    episode_path = episode_dir / "episode.json"
    temporary = episode_dir / f".{_SOURCE_NAME}.recovery-{secrets.token_hex(8)}"
    cloned = clone_to_new_path(state["candidate_snapshot"], temporary)
    predecessor_snapshot = None
    installed_snapshot = None
    episode_write_started = False
    with delivery._running_lock, clips._render_jobs_lock:
        try:
            assert_snapshot(episode_path, state["episode_snapshot"])
            reason = _registered_job_reason(episode_id, episode_dir)
            if reason:
                raise RecoveryConflict(
                    f"Cannot recover source while {reason} is active"
                )

            predecessor_name = inspection["preservation_plan"]["predecessor_filename"]
            predecessor_path = episode_dir / predecessor_name
            if predecessor_path.exists() or predecessor_path.is_symlink():
                raise RecoveryConflict(
                    "Planned predecessor filename is no longer unused"
                )
            predecessor_snapshot = move_to_new_path(
                state["current_snapshot"], predecessor_path
            )
            installed_snapshot = move_to_new_path(cloned, current)
            assert_snapshot(episode_path, state["episode_snapshot"])

            episode.update(updates)
            invalidated_agents = _invalidate_pipeline(episode)
            episode["source_recovery"] = {
                "schema": "cascade.source-recovery/v1",
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "inspection_revision": inspection["revision"],
                "candidate": inspection["candidate_source"],
                "installed_source": _public_identity(installed_snapshot, _SOURCE_NAME),
                "prior_source": inspection["current_source"],
                "predecessor_filename": predecessor_name,
                "source_clock": request.source_clock.model_dump(),
                "invalidated_agents": invalidated_agents,
                "system_xattr_changes": cloned["system_xattr_changes"],
            }
            episode_write_started = True
            atomic_write_json(episode_path, episode)
        except BaseException as error:
            rollback_errors = []
            try:
                _rollback_files(
                    current,
                    temporary,
                    cloned,
                    installed_snapshot,
                    predecessor_snapshot,
                )
            except (CloneSafetyError, OSError) as rollback_error:
                rollback_errors.append(f"media: {rollback_error}")
            if episode_write_started:
                try:
                    _restore_episode(
                        episode_path,
                        state["episode_bytes"],
                        state["episode_snapshot"]["mode"],
                    )
                except OSError as rollback_error:
                    rollback_errors.append(f"episode.json: {rollback_error}")
            if rollback_errors:
                raise RecoveryApplyError(
                    "Source recovery failed and rollback failed: "
                    + "; ".join(rollback_errors)
                ) from error
            if isinstance(error, RecoveryConflict) or (
                predecessor_snapshot is None
                and installed_snapshot is None
                and isinstance(error, CloneSafetyError)
            ):
                raise
            raise RecoveryApplyError(f"Source recovery rolled back: {error}") from error

    return {
        "status": "recovered",
        "episode_id": episode_id,
        "source": {
            **_public_identity(installed_snapshot, _SOURCE_NAME),
            **inspection["candidate_episode_values"],
        },
        "preserved_predecessor": {
            **inspection["current_source"],
            "filename": predecessor_name,
        },
        "candidate_preserved": True,
        "derived_artifacts_preserved": True,
        "invalidated_agents": invalidated_agents,
        "system_xattr_changes": cloned["system_xattr_changes"],
    }


@router.get("/{episode_id}/inspection/source-recovery")
async def inspect_source_recovery(episode_id: str) -> dict:
    episode_dir = require_episode_dir(EPISODES_DIR, episode_id)
    try:
        inspection, _ = await asyncio.to_thread(
            _build_inspection, episode_id, episode_dir
        )
        return inspection
    except (
        CloneSafetyError,
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{episode_id}/inspection/source-recovery")
async def recover_source(episode_id: str, request: SourceRecoveryRequest) -> dict:
    episode_dir = require_episode_dir(EPISODES_DIR, episode_id)
    if not _RECOVERY_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another source recovery is active")
    try:
        async with pipeline._pipeline_lock:
            return await asyncio.to_thread(
                _apply_recovery, episode_id, episode_dir, request
            )
    except (
        RecoveryConflict,
        CloneSafetyError,
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RecoveryApplyError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        _RECOVERY_LOCK.release()
