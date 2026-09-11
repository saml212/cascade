"""One read-only contract for reviewing current and previous media."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from agents.pipeline import load_config
from agents.qa import (
    PLATFORM_COPY_FIELDS,
    canonical_release_metadata,
    clip_review_revision,
    editorial_revision,
)
from agents.speaker_cut import current_speaker_segments
from lib.delivery_video import (
    longform_render_fingerprint,
    read_render_manifest,
    render_artifact_state,
    short_render_fingerprint,
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


def _selection_status(clip: dict) -> str:
    status = clip.get("selection_status")
    if status in {"selected", "rejected"}:
        return status
    if clip.get("status") == "approved":
        return "selected"
    if clip.get("status") == "rejected":
        return "rejected"
    return "unselected"


def _expected_fingerprints(
    episode_dir: Path, episode: dict, clips: list[dict], config: dict
) -> tuple[str | None, dict[str, str | None]]:
    audio = episode_dir / "work" / "audio_mix.wav"
    try:
        current_segments = current_speaker_segments(episode_dir, episode, config)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        current_segments = None
    segments = (
        current_segments.get("segments", [])
        if isinstance(current_segments, dict)
        else []
    )
    if not audio.is_file() or not segments:
        return None, {str(clip.get("id")): None for clip in clips}
    try:
        longform = longform_render_fingerprint(
            episode_dir, episode, config, audio, segments
        )
        shorts = {
            str(clip["id"]): short_render_fingerprint(
                episode_dir, episode, config, audio, segments, clip
            )
            for clip in clips
            if clip.get("id")
        }
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None, {str(clip.get("id")): None for clip in clips}
    return longform, shorts


@router.get("/{episode_id}/review")
async def review_state(episode_id: str) -> dict:
    """Return reviewable files, their freshness, copy, and approval state."""
    episode_dir = _episode_dir(episode_id)
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
                    "selection": {"status": _selection_status(clip)},
                    "render": render,
                    "approval": _approval_state(clip, render, copy),
                    "metadata": _metadata_state(copy, destinations),
                    "render_job": render_job_state(episode_dir, clip_id),
                },
            }
        )

    selection_counts = {
        status: sum(_selection_status(clip) == status for clip in clips)
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
