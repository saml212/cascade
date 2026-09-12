"""One read-only contract for reviewing current and previous media."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from agents.pipeline import load_config
from agents.qa import (
    PLATFORM_COPY_FIELDS,
    canonical_release_metadata,
    clip_review_revision,
    current_clip_boundary_evidence,
    editorial_revision,
)
from agents.speaker_cut import current_speaker_segments
from agents.transcribe import current_diarized_transcript
from lib.audio_mix import selected_audio_source
from lib.clips import clip_selection_status
from lib.delivery_video import (
    current_longform_render,
    current_short_render,
    longform_render_fingerprint,
    read_render_manifest,
    render_artifact_state,
    short_render_fingerprint,
)
from lib.media_inspection import (
    InspectionTarget,
    file_revision,
    inspect_audio_window,
    inspect_media_window,
    resolve_target,
)
from lib.paths import get_episodes_dir
from server.routes.clips import render_job_state

router = APIRouter(prefix="/api/episodes", tags=["review"])

PLATFORM_LABELS = {
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "x": "X",
}
_CLIP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _episode_dir(episode_id: str) -> Path:
    root = get_episodes_dir().resolve()
    episode_dir = (root / episode_id).resolve()
    if episode_dir.parent != root or not (episode_dir / "episode.json").is_file():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return episode_dir


def _with_media_url(state: dict, episode_id: str) -> dict:
    state = dict(state)
    if state["playable"]:
        path = "/".join(quote(part, safe="") for part in state["path"].split("/"))
        state["url"] = f"/media/episodes/{quote(episode_id, safe='')}/{path}"
        state["download_url"] = state["url"]
    else:
        state["url"] = None
        state["download_url"] = None
    return state


def _enabled_destinations(config: dict) -> list[dict]:
    configured = config.get("platforms", {})
    return [
        {
            "key": key,
            "label": PLATFORM_LABELS.get(key, key.replace("_", " ").title()),
            "required_fields": list(fields),
        }
        for key, fields in PLATFORM_COPY_FIELDS.items()
        if configured.get(key, {}).get("enabled") is True
    ]


def _has_copy(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _metadata_state(copy: dict, destinations: list[dict]) -> dict:
    states = []
    for destination in destinations:
        platform_copy = copy.get(destination["key"], {})
        if not isinstance(platform_copy, dict):
            platform_copy = {}
        missing = [
            field
            for field in destination["required_fields"]
            if not _has_copy(platform_copy.get(field))
        ]
        states.append(
            {
                **destination,
                "complete": not missing,
                "missing_fields": missing,
            }
        )
    complete_count = sum(item["complete"] for item in states)
    return {
        "complete": complete_count == len(states),
        "enabled_destination_count": len(states),
        "complete_destination_count": complete_count,
        "destinations": states,
    }


def _approval_state(clip: dict, render: dict, metadata: dict) -> dict:
    if clip.get("status") == "rejected":
        return {"status": "rejected", "current": False}
    if clip.get("status") != "approved":
        return {"status": "unapproved", "current": False}
    if not render["current"]:
        return {"status": "stale", "current": False}
    current = clip.get("approved_render_fingerprint") == render[
        "recorded_fingerprint"
    ] and clip.get("approved_revision") == clip_review_revision(clip, render, metadata)
    return {"status": "current" if current else "stale", "current": current}


def _expected_fingerprints(
    episode_dir: Path, episode: dict, clips: list[dict], config: dict
) -> tuple[str | None, dict[str, str | None]]:
    audio = None
    try:
        audio = selected_audio_source(episode_dir, episode, config) or (
            episode_dir / "work" / "audio_mix.wav"
        )
        current_segments = current_speaker_segments(episode_dir, episode, config)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        current_segments = None
    segments = (
        current_segments.get("segments", [])
        if isinstance(current_segments, dict)
        else []
    )
    if audio is None or not audio.is_file() or not segments:
        return None, {str(clip.get("id")): None for clip in clips}
    try:
        current_longform = current_longform_render(
            episode_dir, episode, config, audio, segments
        )
        longform = (
            current_longform["fingerprint"]
            if current_longform
            else longform_render_fingerprint(
                episode_dir, episode, config, audio, segments
            )
        )
        shorts = {}
        for clip in clips:
            if not clip.get("id"):
                continue
            current_short = current_short_render(
                episode_dir, episode, config, audio, segments, clip
            )
            shorts[str(clip["id"])] = (
                current_short["fingerprint"]
                if current_short
                else short_render_fingerprint(
                    episode_dir, episode, config, audio, segments, clip
                )
            )
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None, {str(clip.get("id")): None for clip in clips}
    return longform, shorts


def _boundary_evidence_with_inspection(
    episode_id: str,
    episode_dir: Path,
    episode: dict,
    clips: list[dict],
    config: dict,
) -> dict:
    evidence = current_clip_boundary_evidence(episode_dir, episode, config, clips)
    endpoint = f"/api/episodes/{quote(episode_id, safe='')}/inspection/preview"
    for clip in evidence["clips"]:
        for finding in clip["findings"]:
            finding["inspection_request"] = {
                "method": "GET",
                "endpoint": endpoint,
                "query": {
                    "target": "source",
                    "clock": "source",
                    "seconds": round(
                        max(0.0, float(finding["boundary_seconds"]) - 2.0), 3
                    ),
                    "duration_seconds": 4.0,
                },
            }
    return evidence


def episode_review_state(episode_dir: Path) -> dict:
    """Build the canonical current review state for routes and local agents."""
    episode_dir = Path(episode_dir)
    episode_id = episode_dir.name
    episode = _read_json(episode_dir / "episode.json", {})
    clips_data = _read_json(episode_dir / "clips.json", {"clips": []})
    clips = clips_data.get("clips", []) if isinstance(clips_data, dict) else clips_data
    clips = [clip for clip in clips if isinstance(clip, dict) and clip.get("id")]
    config = load_config()
    destinations = _enabled_destinations(config)
    metadata = canonical_release_metadata(episode_dir, episode, clips)
    metadata_by_id = {
        str(item["id"]): item
        for item in metadata.get("clips", [])
        if isinstance(item, dict) and item.get("id")
    }
    manifest = read_render_manifest(episode_dir)
    expected_longform, expected_shorts = _expected_fingerprints(
        episode_dir, episode, clips, config
    )
    canonical = _with_media_url(
        render_artifact_state(
            episode_dir,
            Path("upload_video.mp4"),
            manifest.get("longform"),
            expected_fingerprint=expected_longform,
            expected_mode="speaker_cut",
        ),
        episode_id,
    )
    legacy = _with_media_url(
        render_artifact_state(
            episode_dir,
            Path("longform.mp4"),
            None,
            expected_fingerprint=None,
            expected_mode=None,
        ),
        episode_id,
    )
    preferred = canonical if canonical["playable"] else legacy
    approval_revision = editorial_revision(episode_dir, episode)
    approval = episode.get("editorial_approval")
    approval_current = (
        canonical["current"]
        and isinstance(approval, dict)
        and approval.get("revision") == approval_revision
    )

    reviewed_clips = []
    short_records = manifest.get("shorts", {})
    for clip in clips:
        clip_id = str(clip["id"])
        copy = metadata_by_id.get(clip_id, {"id": clip_id})
        render = _with_media_url(
            render_artifact_state(
                episode_dir,
                Path("shorts") / f"{clip_id}.mp4",
                short_records.get(clip_id),
                expected_fingerprint=expected_shorts.get(clip_id),
                expected_mode="speaker_cut_short",
            ),
            episode_id,
        )
        reviewed_clips.append(
            {
                **clip,
                "metadata": {key: value for key, value in copy.items() if key != "id"},
                "review": {
                    "selection": {"status": clip_selection_status(clip)},
                    "render": render,
                    "approval": _approval_state(clip, render, copy),
                    "metadata": _metadata_state(copy, destinations),
                    "render_job": render_job_state(episode_dir, clip_id),
                },
            }
        )

    selection_counts = {
        status: sum(clip_selection_status(clip) == status for clip in clips)
        for status in ("selected", "unselected", "rejected")
    }
    return {
        "schema": "cascade.review/v1",
        "episode_id": episode_id,
        "clock": "source",
        "enabled_destinations": destinations,
        "clip_summary": {
            "candidate_count": len(clips),
            **{f"{status}_count": count for status, count in selection_counts.items()},
        },
        "longform": {
            "render": preferred,
            "canonical_render": canonical,
            "legacy_render": legacy,
            "approval": {
                "status": "current"
                if approval_current
                else "stale"
                if approval
                else "unapproved",
                "current": approval_current,
                "revision": approval_revision,
            },
            "source_preview_url": f"/api/episodes/{quote(episode_id, safe='')}/video-preview",
        },
        "clips": reviewed_clips,
    }


@router.get("/{episode_id}/review")
async def review_state(episode_id: str) -> dict:
    """Return reviewable files, their freshness, copy, and approval state."""
    return episode_review_state(_episode_dir(episode_id))


def _stored_clips(episode_dir: Path) -> list[dict]:
    payload = _read_json(episode_dir / "clips.json", {"clips": []})
    clips = payload.get("clips", []) if isinstance(payload, dict) else payload
    return [clip for clip in clips if isinstance(clip, dict) and clip.get("id")]


async def _inspection_target(
    episode_id: str,
    target: Literal["source", "longform", "short"],
    clip_id: str | None,
) -> tuple[Path, InspectionTarget]:
    episode_dir = _episode_dir(episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    clip = None
    if target == "short":
        if not clip_id or not _CLIP_ID.fullmatch(clip_id):
            raise HTTPException(status_code=422, detail="A valid clip_id is required")
        clip = next(
            (item for item in _stored_clips(episode_dir) if item["id"] == clip_id),
            None,
        )
        if clip is None:
            raise HTTPException(status_code=404, detail=f"Unknown clip: {clip_id}")
    elif clip_id is not None:
        raise HTTPException(
            status_code=422, detail="clip_id is only valid for a short target"
        )
    try:
        resolved = await asyncio.to_thread(
            resolve_target,
            episode_dir,
            episode,
            load_config(),
            target,
            clip=clip,
        )
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return episode_dir, resolved


async def _create_inspection_asset(
    episode_id: str,
    target: Literal["source", "longform", "short"],
    clip_id: str | None,
    clock: Literal["source", "output"],
    seconds: float,
    *,
    kind: Literal["frame", "preview"],
    duration: float = 0.0,
) -> dict:
    episode_dir, resolved = await _inspection_target(episode_id, target, clip_id)
    try:
        result = await asyncio.to_thread(
            inspect_media_window,
            resolved,
            episode_dir / "work" / "media_inspection",
            kind=kind,
            clock=clock,
            seconds=seconds,
            duration=duration,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(
            status_code=502, detail="Could not generate inspection media"
        ) from exc
    return {
        "schema": "cascade.media-inspection/v1",
        "episode_id": episode_id,
        "target": target,
        "clip_id": clip_id,
        **result.payload,
        "asset": {
            **result.payload["asset"],
            "url": _inspection_url(episode_id, result.path),
        },
    }


@router.get("/{episode_id}/inspection/frame")
async def inspection_frame(
    episode_id: str,
    target: Literal["source", "longform", "short"] = "source",
    clock: Literal["source", "output"] = "source",
    seconds: float = 0.0,
    clip_id: str | None = None,
) -> dict:
    return await _create_inspection_asset(
        episode_id, target, clip_id, clock, seconds, kind="frame"
    )


@router.get("/{episode_id}/inspection/preview")
async def inspection_preview(
    episode_id: str,
    target: Literal["source", "longform", "short"] = "source",
    clock: Literal["source", "output"] = "source",
    seconds: float = 0.0,
    duration_seconds: float = 10.0,
    clip_id: str | None = None,
) -> dict:
    return await _create_inspection_asset(
        episode_id,
        target,
        clip_id,
        clock,
        seconds,
        kind="preview",
        duration=duration_seconds,
    )


@router.get("/{episode_id}/inspection/audio-preview")
async def inspection_audio_preview(
    episode_id: str,
    source_kind: Literal["recorder", "camera"],
    start_seconds: float,
    duration_seconds: float = 10.0,
    logical_track: int | None = None,
    channel: Literal["left", "right"] | None = None,
) -> dict:
    episode_dir = _episode_dir(episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    try:
        result = await asyncio.to_thread(
            inspect_audio_window,
            episode_dir,
            episode,
            load_config(),
            source_kind=source_kind,
            start=start_seconds,
            duration=duration_seconds,
            logical_track=logical_track,
            channel=channel,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=502, detail="Could not export audio window"
        ) from exc
    return {
        "schema": "cascade.audio-inspection/v1",
        "episode_id": episode_id,
        **result.payload,
        "asset": {
            **result.payload["asset"],
            "url": _inspection_url(episode_id, result.path),
        },
    }


@router.get("/{episode_id}/inspection/transcript")
async def inspection_transcript(episode_id: str) -> dict:
    return await _current_inspection_document(
        episode_id,
        "diarized_transcript.json",
        "cascade.transcript/v1",
        "transcript",
        current_diarized_transcript,
    )


@router.get("/{episode_id}/inspection/shot-plan")
async def inspection_shot_plan(episode_id: str) -> dict:
    return await _current_inspection_document(
        episode_id,
        "segments.json",
        "cascade.shot-plan/v1",
        "shot_plan",
        current_speaker_segments,
    )


@router.get("/{episode_id}/inspection/clip-boundaries")
async def inspection_clip_boundaries(episode_id: str) -> dict:
    episode_dir = _episode_dir(episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    clips = _stored_clips(episode_dir)
    return _boundary_evidence_with_inspection(
        episode_id, episode_dir, episode, clips, load_config()
    )


async def _current_inspection_document(
    episode_id: str,
    filename: str,
    schema: str,
    key: str,
    loader,
) -> dict:
    episode_dir = _episode_dir(episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    document = await asyncio.to_thread(loader, episode_dir, episode, load_config())
    if document is None:
        label = key.replace("_", " ")
        raise HTTPException(status_code=409, detail=f"Current {label} is unavailable")
    return {
        "schema": schema,
        "episode_id": episode_id,
        "clock": "source",
        "current": True,
        "revision": await asyncio.to_thread(file_revision, episode_dir / filename),
        key: document,
    }


def _inspection_url(episode_id: str, path: Path) -> str:
    return (
        f"/media/episodes/{quote(episode_id, safe='')}/work/media_inspection/"
        f"{quote(path.name, safe='')}"
    )
