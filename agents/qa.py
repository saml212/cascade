"""Quality agent for release artifacts and source-audio continuity."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from agents.base import BaseAgent
from agents.transcribe import current_diarized_transcript
from lib.audio_mix import (
    AUDIO_SELECTION_PATH,
    SELECTED_REPAIR_AUDIO_PATH,
    current_audio_selection,
    selected_audio_source,
)
from lib.audio_qa import (
    AUDIO_FINDING_REVIEWS_PATH,
    OUTPUT_CONTINUITY_SCHEMA,
    OUTPUT_CONTINUITY_VERSION,
    OUTPUT_SOURCE_MAPPING_SCHEMA,
    TRANSCRIPT_ANALYSIS_FINGERPRINT_METHOD,
    analyze_episode_audio,
    analyze_output_continuity,
    apply_finding_reviews,
    review_output_revision,
    selected_output_proof_status,
    transcript_analysis_fingerprint,
)
from lib.audio_qa import release_gate as audio_release_gate
from lib.clips import is_selected_clip
from lib.delivery_video import (
    RENDER_PIPELINE_VERSION,
    aac_content_timing_proof,
    current_episode_longform_render,
    current_short_render,
    read_render_manifest,
)
from lib.ffprobe import file_fingerprint, get_audio_stream
from lib.ffprobe import probe as ffprobe
from lib.timeline import Timeline
from lib.transcript_search import clip_boundary_evidence

QUALITY_SCHEMA = "cascade.release-quality/v1"
PUBLISH_PLAN_SCHEMA = "cascade.publish-plan/v1"
QUALITY_REPORT_PATH = Path("qa/qa.json")
AUDIO_REPORT_PATH = Path("qa/audio-quality.json")
PLATFORM_COPY_FIELDS = {
    "youtube": ("title", "description"),
    "tiktok": ("caption",),
    "instagram": ("caption",),
    "x": ("text",),
}
PODCAST_CHANNEL_FIELDS = (
    "title",
    "description",
    "author",
    "artwork_url",
    "language",
    "category",
    "explicit",
    "link",
    "owner_email",
)


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def _file_signature(path: Path) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _private_identity(scope: str, value: object) -> str:
    """Fingerprint private destination data without exposing the source value."""
    encoded = json.dumps(
        {"scope": scope, "value": value},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _upload_post_plan(
    config: dict, episode: dict, environment: Mapping[str, str]
) -> dict:
    platforms = config.get("platforms", {})
    destinations = sorted(
        platform
        for platform in PLATFORM_COPY_FIELDS
        if platforms.get(platform, {}).get("enabled") is True
    )
    plan = {"enabled": bool(destinations), "destinations": destinations}
    if not destinations:
        return plan

    user = environment.get("UPLOAD_POST_USER", "")
    youtube_url = episode.get("youtube_longform_url", "")
    if episode.get("youtube_longform_url_source") == "upload_post_receipt" or (
        not youtube_url and "youtube" in destinations
    ):
        youtube_funnel = {"source": "upload_post_receipt"}
    else:
        youtube_funnel = {
            "source": "supplied",
            "identity": (
                _private_identity("youtube-longform-url", youtube_url)
                if youtube_url
                else None
            ),
        }
    schedule = config.get("schedule", {})
    plan.update(
        account_identity=_private_identity("upload-post-user", user) if user else None,
        schedule={
            "timezone": schedule.get("timezone", "America/Los_Angeles"),
            "shorts_per_day_weekday": schedule.get("shorts_per_day_weekday", 1),
            "shorts_per_day_weekend": schedule.get("shorts_per_day_weekend", 2),
        },
        funnel_identity=_private_identity(
            "upload-post-funnel",
            {
                "youtube": youtube_funnel,
                "spotify": episode.get("spotify_longform_url", ""),
                "channel_handle": config.get("podcast", {}).get("channel_handle", ""),
            },
        ),
    )
    return plan


def _podcast_rss_plan(
    config: dict, episode: dict, environment: Mapping[str, str]
) -> dict:
    platforms = config.get("platforms", {})
    enabled = platforms.get("podcast_rss", {}).get("enabled") is True
    plan = {"enabled": enabled}
    if not enabled:
        return plan

    podcast = config.get("podcast", {})
    r2 = podcast.get("r2", {})
    account = environment.get("CLOUDFLARE_ACCOUNT_ID", "")
    required_channel_fields = (
        "title",
        "description",
        "author",
        "artwork_url",
        "link",
        "owner_email",
    )
    plan.update(
        account_identity=(
            _private_identity("cloudflare-account", account) if account else None
        ),
        destination_identity=_private_identity(
            "podcast-r2-destination",
            {
                "bucket": r2.get("bucket", ""),
                "public_url": str(r2.get("public_url", "")).rstrip("/"),
            },
        ),
        destination_configured=bool(r2.get("bucket") and r2.get("public_url")),
        channel_identity=_private_identity(
            "podcast-channel",
            {field: podcast.get(field) for field in PODCAST_CHANNEL_FIELDS},
        ),
        channel_configured=all(podcast.get(field) for field in required_channel_fields),
        episode_identity=_private_identity(
            "podcast-episode",
            {
                "episode_id": episode.get("episode_id", ""),
                "title": episode.get("episode_name") or episode.get("title", ""),
                "description": episode.get("episode_description")
                or episode.get("description", ""),
                "created_at": episode.get("created_at", ""),
            },
        ),
    )
    return plan


def current_publish_plan(
    config: dict,
    episode: dict,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Return the normalized external destinations covered by publish approval."""
    environment = os.environ if environment is None else environment
    return {
        "schema": PUBLISH_PLAN_SCHEMA,
        "upload_post": _upload_post_plan(config, episode, environment),
        "podcast_rss": _podcast_rss_plan(config, episode, environment),
    }


def canonical_release_metadata(
    episode_dir: str | Path,
    episode: dict | None = None,
    clips: list[dict] | None = None,
) -> dict:
    """Resolve the copy edited by the UI and consumed by publishing."""
    episode_dir = Path(episode_dir)
    episode = (
        episode if episode is not None else _load_json(episode_dir / "episode.json")
    )
    legacy = _load_json(episode_dir / "metadata" / "metadata.json", {})
    if clips is None:
        clip_data = _load_json(episode_dir / "clips.json", {"clips": []})
        clips = clip_data.get("clips", []) if isinstance(clip_data, dict) else clip_data

    legacy_longform = legacy.get("longform", {})
    longform = {
        key: episode[key] if key in episode else legacy_longform.get(key)
        for key in ("title", "description", "tags")
    }
    legacy_clips = {
        str(item["id"]): item
        for item in legacy.get("clips", [])
        if isinstance(item, dict) and item.get("id")
    }
    resolved_clips = []
    for clip in clips:
        if not isinstance(clip, dict) or not clip.get("id"):
            continue
        clip_id = str(clip["id"])
        resolved = dict(legacy_clips.get(clip_id, {}))
        inline = clip.get("metadata") if isinstance(clip.get("metadata"), dict) else {}
        for key, value in inline.items():
            if isinstance(value, dict) and isinstance(resolved.get(key), dict):
                resolved[key] = {**resolved[key], **value}
            else:
                resolved[key] = value
        resolved["id"] = clip_id
        resolved_clips.append(resolved)
    return {
        "longform": longform,
        "clips": resolved_clips,
        "schedule": legacy.get("schedule", []),
    }


def release_metadata_issues(
    metadata: dict, clips: list[dict], config: dict
) -> list[dict]:
    """List exact missing release-copy fields for enabled destinations."""
    issues = []
    longform = metadata.get("longform", {})
    missing_longform = [
        field for field in ("title", "description") if not longform.get(field)
    ]
    if missing_longform:
        issues.append(
            {
                "scope": "longform",
                "platform": "youtube",
                "fields": missing_longform,
            }
        )

    by_id = {
        str(item["id"]): item
        for item in metadata.get("clips", [])
        if isinstance(item, dict) and item.get("id")
    }
    enabled = config.get("platforms", {})
    for clip in clips:
        if not isinstance(clip, dict) or not clip.get("id"):
            continue
        clip_id = str(clip["id"])
        copy = by_id.get(clip_id, {})
        for platform, fields in PLATFORM_COPY_FIELDS.items():
            if not enabled.get(platform, {}).get("enabled"):
                continue
            platform_copy = copy.get(platform, {})
            missing = [field for field in fields if not platform_copy.get(field)]
            if missing:
                issues.append(
                    {
                        "scope": "clip",
                        "clip_id": clip_id,
                        "platform": platform,
                        "fields": missing,
                    }
                )
    return issues


def editorial_revision(episode_dir: str | Path, episode: dict | None = None) -> str:
    """Fingerprint inputs a longform editorial approval is reviewing."""
    episode_dir = Path(episode_dir)
    episode = (
        episode if episode is not None else _load_json(episode_dir / "episode.json")
    )
    try:
        selected_audio = selected_audio_source(episode_dir, episode)
        audio_signature: object = _file_signature(
            selected_audio or episode_dir / "work" / "audio_mix.wav"
        )
    except ValueError as exc:
        audio_signature = {"status": "invalid_selection", "reason": str(exc)}
    payload = {
        "source": _file_signature(episode_dir / "source_merged.mp4"),
        "audio_master": audio_signature,
        "release_video": _file_signature(episode_dir / "upload_video.mp4"),
        "render_manifest": read_render_manifest(episode_dir).get("longform"),
        "audio_sync": episode.get("audio_sync"),
        "audio_mix": episode.get("audio_mix"),
        "crop_config": episode.get("crop_config"),
        "longform_edits": episode.get("longform_edits", []),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def quality_revision(
    episode_dir: str | Path,
    episode: dict | None = None,
    *,
    config: dict | None = None,
) -> str:
    """Fingerprint rendered media and copy, excluding human review decisions."""
    episode_dir = Path(episode_dir)
    episode = (
        episode if episode is not None else _load_json(episode_dir / "episode.json")
    )
    clips = _load_json(episode_dir / "clips.json", {"clips": []})
    clip_list = clips.get("clips", []) if isinstance(clips, dict) else clips
    short_records = read_render_manifest(episode_dir).get("shorts", {})
    shorts = {
        str(clip.get("id")): {
            "file": _file_signature(episode_dir / "shorts" / f"{clip.get('id')}.mp4"),
            "render": short_records.get(str(clip.get("id"))),
        }
        for clip in clip_list
        if isinstance(clip, dict) and clip.get("id")
    }
    decision_fields = {
        "status",
        "selection_status",
        "approved_at",
        "approved_revision",
        "approved_render_fingerprint",
    }
    quality_clips = [
        {key: value for key, value in clip.items() if key not in decision_fields}
        for clip in clip_list
        if isinstance(clip, dict)
    ]
    continuity_inputs = {
        name: _file_signature(episode_dir / path)
        for name, path in {
            "raw_transcript": "transcript.json",
            "diarized_transcript": "diarized_transcript.json",
            "transcript_provenance": "transcript_provenance.json",
            "transcript_corrections": "transcript_corrections.json",
        }.items()
    }
    if config and (
        config.get("platforms", {}).get("podcast_rss", {}).get("enabled") is True
    ):
        continuity_inputs.update(
            podcast_audio=_file_signature(episode_dir / "podcast_audio.mp3"),
            podcast_audio_proof=_file_signature(
                episode_dir / "podcast_audio.fingerprint"
            ),
        )
    payload = {
        "editorial_revision": editorial_revision(episode_dir, episode),
        "output_continuity_detector": OUTPUT_CONTINUITY_VERSION,
        "clips": quality_clips,
        "shorts": shorts,
        "output_continuity_inputs": continuity_inputs,
        "metadata": canonical_release_metadata(episode_dir, episode, clip_list),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def release_revision(
    episode_dir: str | Path,
    episode: dict | None = None,
    *,
    config: dict | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Bind final publish approval to quality inputs and clip decisions."""
    episode_dir = Path(episode_dir)
    episode = (
        episode if episode is not None else _load_json(episode_dir / "episode.json")
    )
    if config is None:
        from agents.pipeline import load_config

        config = load_config()
    clips_data = _load_json(episode_dir / "clips.json", {"clips": []})
    clips = clips_data.get("clips", []) if isinstance(clips_data, dict) else clips_data
    decisions = {
        str(clip.get("id")): clip.get("status", "pending")
        for clip in clips
        if isinstance(clip, dict) and clip.get("id")
    }
    payload = {
        "quality_revision": quality_revision(episode_dir, episode, config=config),
        "clip_decisions": decisions,
        "audio_finding_reviews": _file_signature(
            episode_dir / AUDIO_FINDING_REVIEWS_PATH
        ),
        "publish_plan": current_publish_plan(config, episode, environment=environment),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _approval_current(approval: object, revision: str) -> bool:
    return isinstance(approval, dict) and approval.get("revision") == revision


def clip_review_revision(
    clip: dict, render_record: dict, metadata_entry: dict | None = None
) -> str:
    """Bind final clip approval to its rendered pixels, bounds, and copy."""
    ignored = {
        "status",
        "selection_status",
        "approved_at",
        "approved_revision",
        "approved_render_fingerprint",
    }
    copy = {}
    if isinstance(metadata_entry, dict):
        nested = metadata_entry.get("metadata")
        if isinstance(nested, dict):
            copy.update(nested)
        copy.update(
            {key: value for key, value in metadata_entry.items() if key != "metadata"}
        )
    inline = clip.get("metadata")
    if isinstance(inline, dict):
        for key, value in inline.items():
            if isinstance(value, dict) and isinstance(copy.get(key), dict):
                copy[key] = {**copy[key], **value}
            else:
                copy[key] = value
    payload = {
        "clip": {key: value for key, value in clip.items() if key not in ignored},
        "render_fingerprint": render_record.get("fingerprint"),
        "render_output": render_record.get("output"),
        "metadata": copy,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def current_clip_boundary_evidence(
    episode_dir: str | Path,
    episode: dict,
    config: dict,
    clips: list[dict],
) -> dict:
    """Return nonblocking clip-boundary evidence from the current transcript."""
    episode_dir = Path(episode_dir)
    unavailable = clip_boundary_evidence({}, clips)
    try:
        transcript = current_diarized_transcript(episode_dir, episode, config)
        if transcript is None:
            raise ValueError("Current source-clock transcript is unavailable")
        transcript_revision = file_fingerprint(
            episode_dir / "diarized_transcript.json"
        )["id"]
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
        unavailable.update(
            current=False,
            status="unavailable",
            transcript_revision=None,
            detail=str(exc),
        )
        for clip in unavailable["clips"]:
            clip.update(status="unavailable", transcript_revision=None)
        return unavailable

    evidence = clip_boundary_evidence(transcript, clips)
    evidence.update(current=True, transcript_revision=transcript_revision)
    for clip in evidence["clips"]:
        clip["transcript_revision"] = transcript_revision
    return evidence


def _render_status(
    episode_dir: Path, episode: dict, clips: list[dict], config: dict
) -> tuple[dict | None, dict[str, dict]]:
    try:
        audio = selected_audio_source(episode_dir, episode, config) or (
            episode_dir / "work" / "audio_mix.wav"
        )
        segments = _load_json(episode_dir / "segments.json", {}).get("segments", [])
        longform = current_episode_longform_render(episode_dir, episode, config, audio)
        shorts = {}
        for clip in clips:
            if not isinstance(clip, dict) or not clip.get("id"):
                continue
            record = current_short_render(
                episode_dir, episode, config, audio, segments, clip
            )
            if record:
                shorts[str(clip["id"])] = record
        return longform, shorts
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        return None, {}


def _continuity_scan_identity(path: Path) -> dict:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "resolved_path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _verified_render_audio_stream(path: Path, output: dict) -> tuple[dict, dict]:
    """Probe one stat-bound render without trusting a symlink or file race."""
    try:
        resolved = path.resolve(strict=True)
        before = _continuity_scan_identity(resolved)
        if (
            output.get("size_bytes") != before["size_bytes"]
            or output.get("mtime_ns") != before["mtime_ns"]
        ):
            raise ValueError("Video render changed after its manifest was recorded.")
        audio = get_audio_stream(ffprobe(resolved))
        after = _continuity_scan_identity(resolved)
        if (
            path.resolve(strict=True) != resolved
            or after != before
            or audio.get("codec_name") != "aac"
            or str(audio.get("sample_rate")) != "48000"
            or int(audio.get("start_pts", -1)) != 0
            or str(audio.get("time_base")) != "1/48000"
            or int(audio.get("initial_padding", -1)) != 0
        ):
            raise ValueError("Video render AAC timing evidence is invalid or stale.")
    except (
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Video render"):
            raise
        raise ValueError("Video render AAC timing evidence is unavailable.") from exc
    return audio, before


def _render_audio_mapping(
    path: Path, record: dict, interval_field: str, source_duration: float
) -> tuple[Timeline, dict]:
    output = record.get("output") or {}
    timing = output.get("audio_timing")
    expected_timing = aac_content_timing_proof()
    if record.get("pipeline_version") != RENDER_PIPELINE_VERSION:
        raise ValueError("Video render has no current audio timing proof.")
    try:
        timeline = Timeline(source_duration, record[interval_field])
        recorded_duration = float(record["output_duration_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Video render source mapping is unavailable.") from exc
    if not math.isclose(recorded_duration, timeline.duration, abs_tol=0.001):
        raise ValueError("Video render source mapping is stale.")
    if output.get("audio_codec") != "aac":
        raise ValueError("Video render has no current audio timing proof.")
    audio, media_identity = _verified_render_audio_stream(path, output)
    if (
        output.get("audio_sample_rate_hz") == expected_timing["sample_rate_hz"]
        and timing == expected_timing
    ):
        proof_method = "render-manifest/v1"
    elif "audio_sample_rate_hz" not in output and "audio_timing" not in output:
        # source-clock/v3 introduced this exact native-AAC/-use_editlist 0 mux.
        # Its older records omitted timing metadata, so bind the compatibility
        # proof to the current manifest, file identity, and actual AAC stream.
        timing = expected_timing
        proof_method = "source-clock-v3-current-stream/v1"
    else:
        raise ValueError("Video render has no current audio timing proof.")
    return timeline, {
        "schema": OUTPUT_SOURCE_MAPPING_SCHEMA,
        "pipeline_version": RENDER_PIPELINE_VERSION,
        "source_intervals": [list(item) for item in timeline.keep_intervals],
        "audio_codec": "aac",
        "audio_sample_rate_hz": expected_timing["sample_rate_hz"],
        "audio_timing": timing,
        "timing_provenance": {
            "method": proof_method,
            "media_identity": media_identity,
            "stream": {
                "codec_name": audio["codec_name"],
                "sample_rate_hz": int(audio["sample_rate"]),
                "start_pts": int(audio["start_pts"]),
                "time_base": audio["time_base"],
                "initial_padding": int(audio["initial_padding"]),
            },
        },
    }


def _continuity_target(
    path: Path,
    *,
    role: str,
    clock: str,
    timeline: Timeline,
    current: bool,
    proof: object,
    stale_detail: str,
    clip_id: str | None = None,
) -> dict:
    target = {
        "role": role,
        "path": str(path.absolute()),
        "clock": clock,
        "required": True,
        "timeline": timeline,
    }
    if clip_id is not None:
        target["clip_id"] = clip_id
    try:
        scan_identity = _continuity_scan_identity(path)
    except (OSError, RuntimeError):
        target.update(status="missing", detail=f"{path.name} is missing.")
        return target
    if scan_identity["size_bytes"] <= 0 or not path.is_file():
        target.update(status="missing", detail=f"{path.name} is missing.")
        return target
    target["scan_identity"] = scan_identity
    recorded_output = proof.get("output") if isinstance(proof, dict) else None
    if isinstance(proof, dict) and "selected_output" in proof:
        recorded_output = (proof.get("selected_output") or {}).get("fingerprint")
    if isinstance(recorded_output, dict) and (
        recorded_output.get("size_bytes") != scan_identity["size_bytes"]
        or recorded_output.get("mtime_ns") != scan_identity["mtime_ns"]
    ):
        current = False
        stale_detail = "Artifact changed after its currentness proof was recorded."
    target["revision"] = _private_identity(
        "output-continuity-artifact", {"file": scan_identity, "proof": proof}
    )
    target.update(
        status="current" if current else "stale",
        detail="Current artifact is ready for analysis." if current else stale_detail,
    )
    return target


def _output_continuity_targets(
    episode_dir: Path,
    episode: dict,
    clips: list[dict],
    config: dict,
    timeline: Timeline,
) -> list[dict]:
    selected_clips = [
        clip
        for clip in clips
        if isinstance(clip, dict) and clip.get("id") and is_selected_clip(clip)
    ]
    selection_path = episode_dir / AUDIO_SELECTION_PATH
    try:
        selection = current_audio_selection(episode_dir, episode, config)
        selection_error = None
    except ValueError as exc:
        selection = None
        selection_error = str(exc)

    if selection_error is not None:
        master_path = episode_dir / SELECTED_REPAIR_AUDIO_PATH
        master_current = False
        master_proof = {"selection_error": selection_error}
        master_detail = selection_error
    elif selection is not None:
        master_path = Path(selection["selected_output"]["path"])
        master_current = True
        master_proof = selection
        master_detail = "Selected repair audio is stale."
    else:
        master_path = episode_dir / "work" / "audio_mix.wav"
        try:
            master_proof = master_path.with_suffix(".fingerprint").read_text().strip()
        except OSError:
            master_proof = None
        master_current = bool(master_proof) and not selection_path.exists()
        master_detail = "Base audio mix has no current generation proof."

    master = _continuity_target(
        master_path,
        role="selected_audio_master",
        clock="source",
        timeline=timeline,
        current=master_current,
        proof=master_proof,
        stale_detail=master_detail,
    )
    targets = [master]
    master_ready = master["status"] == "current"
    longform_record, short_records = (
        _render_status(episode_dir, episode, selected_clips, config)
        if master_ready
        else (None, {})
    )
    longform_timeline = timeline
    longform_mapping = None
    longform_mapping_error = None
    if longform_record is not None:
        try:
            longform_timeline, longform_mapping = _render_audio_mapping(
                episode_dir / "upload_video.mp4",
                longform_record,
                "keep_intervals",
                timeline.source_duration,
            )
        except ValueError as exc:
            longform_mapping_error = str(exc)
    upload_target = _continuity_target(
        episode_dir / "upload_video.mp4",
        role="upload_video",
        clock="output",
        timeline=longform_timeline,
        current=longform_record is not None and longform_mapping_error is None,
        proof=longform_record,
        stale_detail=longform_mapping_error
        or "Canonical upload video is stale for current release inputs.",
    )
    if longform_mapping is not None:
        upload_target["source_mapping"] = longform_mapping
    if longform_mapping_error is not None and upload_target["status"] != "missing":
        upload_target["status"] = "unavailable"
    targets.append(upload_target)

    for clip in selected_clips:
        clip_id = str(clip["id"])
        try:
            clip_timeline = timeline.slice(
                float(clip.get("start_seconds", clip.get("start"))),
                float(clip.get("end_seconds", clip.get("end"))),
            )
        except (TypeError, ValueError) as exc:
            targets.append(
                {
                    "role": "short",
                    "clip_id": clip_id,
                    "path": str((episode_dir / "shorts" / f"{clip_id}.mp4").resolve()),
                    "clock": "output",
                    "required": True,
                    "status": "unavailable",
                    "detail": str(exc),
                }
            )
            continue
        short_record = short_records.get(clip_id)
        short_mapping = None
        short_mapping_error = None
        if short_record is not None:
            try:
                clip_timeline, short_mapping = _render_audio_mapping(
                    episode_dir / "shorts" / f"{clip_id}.mp4",
                    short_record,
                    "clip_source_intervals",
                    timeline.source_duration,
                )
            except ValueError as exc:
                short_mapping_error = str(exc)
        short_target = _continuity_target(
            episode_dir / "shorts" / f"{clip_id}.mp4",
            role="short",
            clip_id=clip_id,
            clock="output",
            timeline=clip_timeline,
            current=short_record is not None and short_mapping_error is None,
            proof=short_record,
            stale_detail=short_mapping_error
            or "Selected short is stale for current release inputs.",
        )
        if short_mapping is not None:
            short_target["source_mapping"] = short_mapping
        if short_mapping_error is not None and short_target["status"] != "missing":
            short_target["status"] = "unavailable"
        targets.append(short_target)

    podcast_path = episode_dir / "podcast_audio.mp3"
    rss_enabled = (
        config.get("platforms", {}).get("podcast_rss", {}).get("enabled") is True
    )
    if not rss_enabled:
        targets.append(
            {
                "role": "podcast_audio",
                "path": str(podcast_path.resolve()),
                "clock": "output",
                "required": False,
                "status": "not_required",
                "detail": "Podcast RSS delivery is disabled.",
            }
        )
    else:
        try:
            from agents.podcast_feed import current_podcast_audio

            podcast_current = current_podcast_audio(
                episode_dir,
                episode,
                config,
            )
            podcast_error = None
        except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
            podcast_current = None
            podcast_error = str(exc)
        podcast_proof = _load_json(podcast_path.with_suffix(".fingerprint"), {})
        targets.append(
            _continuity_target(
                podcast_path,
                role="podcast_audio",
                clock="output",
                timeline=timeline,
                current=podcast_current is not None,
                proof=podcast_proof,
                stale_detail=podcast_error
                or "Podcast audio is stale for current release inputs.",
            )
        )
    return targets


def analyze_release_audio_continuity(
    episode_dir: Path,
    episode: dict,
    clips: list[dict],
    config: dict,
    timeline: Timeline,
    *,
    ffmpeg_bin: str | Path | None = None,
) -> dict:
    """Analyze only provenance-current release audio against the current transcript."""
    transcript = current_diarized_transcript(episode_dir, episode, config)
    if transcript is None:
        raise ValueError("Current source-clock transcript is unavailable")
    transcript_fingerprint = transcript_analysis_fingerprint(
        episode_dir / "diarized_transcript.json",
        transcript,
        minimum_confidence=0.55,
    )["id"]
    return analyze_output_continuity(
        _output_continuity_targets(episode_dir, episode, clips, config, timeline),
        transcript,
        episode_timeline=timeline,
        transcript_fingerprint=transcript_fingerprint,
        ffmpeg_bin=ffmpeg_bin,
    )


def _review_output(record: dict | None) -> dict | None:
    revision = review_output_revision(record)
    if revision is None or not isinstance(record, dict):
        return None
    output = record.get("output") or {}
    return {
        "revision": revision,
        "render_fingerprint": record.get("fingerprint"),
        "output_stat": {
            "size_bytes": output.get("size_bytes"),
            "mtime_ns": output.get("mtime_ns"),
        },
        "completed_at": record.get("completed_at"),
    }


def _finding_review_window(finding: dict, record: dict) -> dict | None:
    ranges = (finding.get("edited_time") or {}).get("ranges") or []
    if len(ranges) != 1:
        return None
    try:
        start = float(ranges[0]["source_start_seconds"])
        end = float(ranges[0]["source_end_seconds"])
        interval_start, interval_end = next(
            (float(left), float(right))
            for left, right in record.get("keep_intervals", [])
            if float(left) <= start < end <= float(right)
        )
    except (KeyError, StopIteration, TypeError, ValueError):
        return None
    finding_duration = end - start
    if (
        not all(math.isfinite(value) for value in (start, end))
        or finding_duration <= 0
        or finding_duration > 30
    ):
        return None
    preview_start = max(interval_start, start - min(2.0, 30.0 - finding_duration))
    preview_end = min(interval_end, end + min(2.0, 30.0 - (end - preview_start)))
    if preview_end < end or preview_end <= preview_start:
        return None
    return {
        "target": "longform",
        "clock": "source",
        "seconds": round(preview_start, 6),
        "duration_seconds": round(preview_end - preview_start, 6),
    }


def _attach_finding_review_context(
    audio_report: dict,
    current_longform: dict | None,
    episode_id: str,
    *,
    qa_current: bool,
) -> dict:
    output = _review_output(current_longform)
    output_proof_status = selected_output_proof_status(audio_report)
    uses_checked_source = (
        audio_report.get("scope", {})
        .get("selected_mix_provenance", {})
        .get("uses_checked_source_audio")
    )
    for finding in audio_report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        window = (
            _finding_review_window(finding, current_longform)
            if isinstance(current_longform, dict)
            else None
        )
        resolution = (finding.get("resolution") or {}).get("status", "unresolved")
        reason = None
        if not qa_current:
            reason = "Run QA for the current revision before recording a review."
        elif uses_checked_source is not True:
            reason = "This source-channel check does not apply to the selected mix."
        elif output_proof_status != "pass":
            reason = "Run QA with a verified current selected audio master before recording a review."
        elif not audio_report.get("fingerprint") or not finding.get("fingerprint"):
            reason = "This report or finding has no stable review fingerprint."
        elif (finding.get("edited_time") or {}).get("status") == "removed":
            reason = "This finding was removed by the current edit."
        elif (finding.get("edited_time") or {}).get("status") == "split":
            reason = "This finding spans multiple retained ranges and needs separate review evidence."
        elif output is None or window is None:
            reason = "A current rendered output containing this finding is unavailable."
        elif resolution not in {"unresolved", "accepted", "false_positive"}:
            reason = f"This finding is already resolved as {resolution}."
        finding["review"] = {
            "allowed": reason is None,
            "reason": reason,
            "report_fingerprint": audio_report.get("fingerprint"),
            "finding_fingerprint": finding.get("fingerprint"),
            "output_revision": output.get("revision") if output else None,
            "inspection_request": (
                {
                    "method": "GET",
                    "endpoint": f"/api/episodes/{episode_id}/inspection/preview",
                    "query": window,
                }
                if window and qa_current
                else None
            ),
            "decision_endpoint": (
                f"/api/episodes/{episode_id}/audio-qc/findings/"
                f"{finding.get('id')}/review"
            ),
        }
    return audio_report


def _attach_output_continuity_inspection(
    output_continuity: dict, episode_id: str, *, qa_current: bool
) -> dict:
    """Expose bounded canonical inspection requests for hard output findings."""
    output_continuity["current"] = qa_current
    for finding in output_continuity.get("findings", []):
        if not isinstance(finding, dict):
            continue
        finding.pop("inspection_request", None)
        if not qa_current:
            continue
        role = finding.get("role")
        artifact_time = finding.get("artifact_time") or {}
        try:
            start = float(artifact_time["start_seconds"])
            end = float(artifact_time["end_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (start, end)) or end <= start:
            continue
        target = None
        clip_id = None
        clock = str(artifact_time.get("clock", "output"))
        if role == "selected_audio_master":
            target, clock = "longform", "source"
            source_ranges = finding.get("source_ranges") or []
            if len(source_ranges) != 1:
                continue
            try:
                start = float(source_ranges[0]["start_seconds"])
                end = float(source_ranges[0]["end_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
        elif role == "upload_video":
            target = "longform"
        elif role == "short" and finding.get("clip_id"):
            target, clip_id = "short", str(finding["clip_id"])
        if target is None or clock not in {"source", "output"}:
            continue
        padding = 0.0 if clock == "source" else 2.0
        query = {
            "target": target,
            "clock": clock,
            "seconds": round(max(0.0, start - padding), 6),
            "duration_seconds": round(min(30.0, end - start + padding * 2), 6),
        }
        if clip_id:
            query["clip_id"] = clip_id
        finding["inspection_request"] = {
            "method": "GET",
            "endpoint": f"/api/episodes/{episode_id}/inspection/preview",
            "query": query,
        }
    return output_continuity


def quality_snapshot(
    episode_dir: str | Path,
    *,
    include_findings: bool = True,
    config: dict | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Return the single machine-readable quality and release decision."""
    episode_dir = Path(episode_dir)
    if config is None:
        from agents.pipeline import load_config

        config = load_config()
    episode = _load_json(episode_dir / "episode.json")
    clips_data = _load_json(episode_dir / "clips.json", {"clips": []})
    clips = clips_data.get("clips", []) if isinstance(clips_data, dict) else clips_data
    current_longform, current_shorts = _render_status(
        episode_dir, episode, clips, config
    )
    current_editorial_revision = editorial_revision(episode_dir, episode)
    current_quality_revision = quality_revision(episode_dir, episode, config=config)
    publish_plan = current_publish_plan(config, episode, environment=environment)
    current_release_revision = release_revision(
        episode_dir,
        episode,
        config=config,
        environment=environment,
    )
    report = _load_json(episode_dir / QUALITY_REPORT_PATH)
    report_revision = report.get("quality_revision")
    qa_current = bool(report) and report_revision == current_quality_revision
    audio_quality = report.get("audio_quality") or _load_json(
        episode_dir / AUDIO_REPORT_PATH
    )
    audio_quality = apply_finding_reviews(
        audio_quality,
        _load_json(episode_dir / AUDIO_FINDING_REVIEWS_PATH),
        output_revision=review_output_revision(current_longform),
    )
    audio_quality = _attach_finding_review_context(
        audio_quality,
        current_longform,
        episode.get("episode_id", episode_dir.name),
        qa_current=qa_current,
    )
    audio_gate = audio_quality.get("release_gate", {})
    effective_checks = [dict(check) for check in report.get("checks", [])]
    for check in effective_checks:
        if check.get("name") == "audio_continuity":
            check.update(
                {
                    "status": audio_gate.get("status", "unknown"),
                    "pass": audio_gate.get("status") == "pass",
                    "detail": audio_gate.get(
                        "reason", "Audio continuity report unavailable"
                    ),
                }
            )
    effective_overall = (
        (
            "pass"
            if all(check.get("pass") is True for check in effective_checks)
            else "fail"
        )
        if effective_checks
        else report.get("overall")
    )

    blockers: list[dict] = []
    if not report:
        quality_status = "missing"
        blockers.append(
            {
                "code": "quality_report_missing",
                "severity": "error",
                "message": "Run the QA agent for this release revision.",
            }
        )
    elif report_revision != current_quality_revision:
        quality_status = "stale"
        blockers.append(
            {
                "code": "quality_report_stale",
                "severity": "error",
                "message": "Release inputs changed after the last QA run.",
            }
        )
    elif effective_overall != "pass":
        quality_status = "blocked"
        blockers.append(
            {
                "code": "quality_checks_failed",
                "severity": "error",
                "message": "The current QA report contains blocking findings.",
            }
        )
    else:
        quality_status = "passed"

    video = episode_dir / "upload_video.mp4"
    video_ready = bool(current_longform)
    if not video.is_file() or video.stat().st_size == 0:
        video_detail = "Canonical upload_video.mp4 is missing."
    elif not current_longform:
        video_detail = "Canonical upload video is not the current speaker-cut render."
    else:
        video_detail = "Current speaker-cut upload video is present."
    if not video_ready:
        blockers.append(
            {
                "code": "release_video_invalid",
                "severity": "error",
                "message": video_detail,
            }
        )

    metadata = canonical_release_metadata(episode_dir, episode, clips)
    metadata_by_id = {
        item.get("id"): item
        for item in metadata.get("clips", [])
        if isinstance(item, dict) and item.get("id")
    }
    approved = []
    pending = []
    for clip in clips:
        if not isinstance(clip, dict) or not clip.get("id"):
            continue
        status = clip.get("status", "pending")
        if status == "approved":
            approved.append(clip)
        elif status != "rejected":
            pending.append(str(clip["id"]))
    if not clips:
        blockers.append(
            {
                "code": "clips_missing",
                "severity": "error",
                "message": "No clip candidates exist for this release.",
            }
        )
    if pending:
        blockers.append(
            {
                "code": "clips_pending_review",
                "severity": "error",
                "message": f"{len(pending)} clip(s) still need approval or rejection.",
                "clip_ids": pending,
            }
        )
    missing_shorts = [
        str(clip["id"]) for clip in approved if str(clip["id"]) not in current_shorts
    ]
    if missing_shorts:
        blockers.append(
            {
                "code": "approved_shorts_missing",
                "severity": "error",
                "message": f"{len(missing_shorts)} approved short render(s) are missing.",
                "clip_ids": missing_shorts,
            }
        )
    stale_clip_approvals = [
        str(clip["id"])
        for clip in approved
        if str(clip["id"]) in current_shorts
        and (
            clip.get("approved_render_fingerprint")
            != current_shorts[str(clip["id"])].get("fingerprint")
            or clip.get("approved_revision")
            != clip_review_revision(
                clip,
                current_shorts[str(clip["id"])],
                metadata_by_id.get(clip["id"]),
            )
        )
    ]
    if stale_clip_approvals:
        blockers.append(
            {
                "code": "clip_approval_missing_or_stale",
                "severity": "error",
                "message": (
                    f"{len(stale_clip_approvals)} clip approval(s) do not match "
                    "the current render and copy."
                ),
                "clip_ids": stale_clip_approvals,
            }
        )
    try:
        metadata_issues = release_metadata_issues(metadata, approved, config)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        metadata_issues = [
            {
                "scope": "configuration",
                "platform": "unknown",
                "fields": ["platforms"],
            }
        ]
    for issue in metadata_issues:
        fields = ", ".join(issue["fields"])
        if issue["scope"] == "longform":
            code = "longform_metadata_missing"
            message = f"Longform {issue['platform']} copy is missing: {fields}."
        elif issue["scope"] == "clip":
            code = "approved_clip_copy_missing"
            message = (
                f"{issue['clip_id']} {issue['platform']} copy is missing: {fields}."
            )
        else:
            code = "platform_configuration_missing"
            message = "Platform configuration is unavailable."
        blockers.append(
            {
                "code": code,
                "severity": "error",
                "message": message,
                **issue,
            }
        )

    editorial_current = _approval_current(
        episode.get("editorial_approval"), current_editorial_revision
    )
    if not editorial_current:
        blockers.append(
            {
                "code": "editorial_approval_missing_or_stale",
                "severity": "error",
                "message": "Approve the current longform edit before publishing.",
            }
        )

    publish_current = _approval_current(
        episode.get("publish_approval"), current_release_revision
    )
    prerequisites = list(blockers)
    if not publish_current:
        blockers.append(
            {
                "code": "publish_approval_missing_or_stale",
                "severity": "error",
                "message": "Explicit publish approval is required for this release revision.",
            }
        )

    if prerequisites:
        release_status = "blocked"
    elif not publish_current:
        release_status = "awaiting_publish_approval"
    else:
        release_status = "ready"

    repair_candidate = _load_json(
        episode_dir / "qa" / "audio-repair" / "audio-repair-candidate.json"
    )
    repair_plan = _load_json(
        episode_dir / "qa" / "audio-repair" / "audio-repair-plan.json"
    )
    repair_candidate_current = bool(
        repair_candidate
        and repair_candidate.get("source_report_fingerprint")
        == audio_quality.get("fingerprint")
        and repair_candidate.get("repair_plan_fingerprint")
        == repair_plan.get("fingerprint")
        and audio_quality.get("transcript", {}).get("fingerprint", {}).get("method")
        == TRANSCRIPT_ANALYSIS_FINGERPRINT_METHOD
    )
    try:
        audio_selection = current_audio_selection(episode_dir, episode)
    except ValueError as exc:
        audio_selection = {"status": "stale", "detail": str(exc)}
    findings = (
        audio_quality.get("findings", []) if isinstance(audio_quality, dict) else []
    )
    analysis = audio_quality.get("analysis", {})
    output_continuity = report.get("selected_master_output_continuity", {})
    output_continuity = _attach_output_continuity_inspection(
        output_continuity,
        episode.get("episode_id", episode_dir.name),
        qa_current=qa_current,
    )
    if not include_findings:
        analysis = {
            key: analysis.get(key)
            for key in ("status", "finding_count", "window_count")
            if key in analysis
        }
        audio_gate = {
            key: audio_gate.get(key)
            for key in ("status", "safe", "reason")
            if key in audio_gate
        }
        output_continuity = {
            **output_continuity,
            "findings": [],
            "artifacts": [
                {**artifact, "findings": []}
                for artifact in output_continuity.get("artifacts", [])
            ],
        }
    editorial_approval = episode.get("editorial_approval") or {}
    publish_approval = episode.get("publish_approval") or {}
    rendered_short_ids = [
        str(clip["id"])
        for clip in clips
        if isinstance(clip, dict)
        and clip.get("id")
        and str(clip["id"]) in current_shorts
    ]
    return {
        "schema": QUALITY_SCHEMA,
        "episode_id": episode.get("episode_id", episode_dir.name),
        "quality": {
            "status": quality_status,
            "current_revision": current_quality_revision,
            "report_revision": report_revision,
            "generated_at": report.get("generated_at"),
            "overall": effective_overall,
            "checks": effective_checks,
            "skipped_checks": report.get("skipped_checks", []),
        },
        "release_gate": {
            "status": release_status,
            "safe": release_status == "ready",
            "can_approve_publish": not prerequisites,
            "revision": current_release_revision,
            "publish_plan": publish_plan,
            "blockers": blockers,
        },
        "approvals": {
            "editorial": {
                "current": editorial_current,
                "revision": current_editorial_revision,
                "approved_at": editorial_approval.get("approved_at"),
            },
            "publish": {
                "current": publish_current,
                "revision": current_release_revision,
                "approved_at": publish_approval.get("approved_at"),
            },
        },
        "artifacts": {
            "release_video": {
                "ready": video_ready,
                "detail": video_detail,
                "download_url": f"/api/episodes/{episode_dir.name}/delivery/video",
                "review_output": _review_output(current_longform),
            },
            "legacy_longform": {
                "available": (episode_dir / "longform.mp4").is_file(),
                "review_url": f"/media/episodes/{episode_dir.name}/longform.mp4",
                "release_candidate": False,
            },
            "approved_short_count": len(approved),
            "candidate_count": len(clips),
            "rendered_short_count": len(rendered_short_ids),
            "rendered_short_ids": rendered_short_ids,
            "pending_clip_count": len(pending),
            "missing_short_ids": missing_shorts,
        },
        "audio_quality": {
            "report_fingerprint": audio_quality.get("fingerprint"),
            "release_gate": audio_gate,
            "analysis": analysis,
            "finding_count": len(findings),
            "findings": findings if include_findings else [],
            "selected_master_output_continuity": output_continuity,
            "repair_candidate": (
                {
                    "status": repair_candidate.get("status"),
                    "current": repair_candidate_current,
                    "fingerprint": repair_candidate.get("fingerprint"),
                    "verification_status": repair_candidate.get("verification", {}).get(
                        "status"
                    ),
                    "repaired_finding_count": len(
                        repair_candidate.get("repaired_finding_ids", [])
                    ),
                    "unresolved_finding_count": len(
                        repair_candidate.get("unresolved_finding_ids", [])
                    ),
                    "perceptual_review": repair_candidate.get("perceptual_review"),
                    "audio_url": (
                        f"/api/episodes/{episode_dir.name}/audio-qc/"
                        "repair-candidate/audio"
                    ),
                }
                if repair_candidate
                else None
            ),
            "repair_selection": (
                {
                    key: audio_selection.get(key)
                    for key in (
                        "status",
                        "fingerprint",
                        "release_safe",
                        "detail",
                    )
                    if key in audio_selection
                }
                if audio_selection
                else None
            ),
        },
    }


class QAAgent(BaseAgent):
    name = "qa"

    def execute(self) -> dict:
        checks: list[dict] = []
        skipped_checks: list[dict] = []
        warnings: list[dict] = []
        episode = self.load_json_safe("episode.json")
        release_video = self.episode_dir / "upload_video.mp4"
        legacy_video = self.episode_dir / "longform.mp4"

        self.report_progress(1, 3, "Validating release artifacts")
        source = self.episode_dir / "source_merged.mp4"
        source_duration = 0.0
        if source.exists():
            source_probe = ffprobe(source)
            source_duration = float(source_probe.get("format", {}).get("duration", 0))
            has_audio = any(
                stream.get("codec_type") == "audio"
                for stream in source_probe.get("streams", [])
            )
            checks.extend(
                [
                    {
                        "name": "source_merged_exists",
                        "pass": True,
                        "detail": f"Duration: {source_duration:.1f}s",
                    },
                    {
                        "name": "source_merged_duration",
                        "pass": source_duration > 60,
                        "detail": f"{source_duration:.1f}s (min 60s)",
                    },
                    {
                        "name": "source_merged_has_audio",
                        "pass": has_audio,
                        "detail": f"Audio stream: {has_audio}",
                    },
                ]
            )
        else:
            checks.append(
                {
                    "name": "source_merged_exists",
                    "pass": False,
                    "detail": "File not found",
                }
            )

        video = release_video if release_video.exists() else legacy_video
        if video.exists():
            video_probe = ffprobe(video)
            duration = float(video_probe.get("format", {}).get("duration", 0))
            checks.append(
                {
                    "name": "review_video_exists",
                    "pass": True,
                    "detail": (
                        f"{video.name}: {duration:.1f}s, "
                        f"{video.stat().st_size / 1e6:.1f} MB"
                    ),
                }
            )
            if video == legacy_video:
                warnings.append(
                    {
                        "name": "legacy_longform_only",
                        "detail": "longform.mp4 is reviewable but is never a publish candidate.",
                    }
                )
        else:
            checks.append(
                {
                    "name": "review_video_exists",
                    "pass": False,
                    "detail": "upload_video.mp4 and longform.mp4 are missing",
                }
            )

        clips_data = self.load_json_safe("clips.json", {"clips": []})
        clips = clips_data.get("clips", [])
        selected_clips = [clip for clip in clips if is_selected_clip(clip)]
        if clips:
            missing = [
                clip["id"]
                for clip in selected_clips
                if not (self.episode_dir / "shorts" / f"{clip['id']}.mp4").exists()
            ]
            checks.append(
                {
                    "name": "all_shorts_rendered",
                    "pass": not missing,
                    "detail": (
                        f"{len(selected_clips) - len(missing)}/"
                        f"{len(selected_clips)} selected clips rendered"
                    )
                    + (f", missing: {missing}" if missing else ""),
                }
            )
            minimum = self.get_config("processing", "clip_min_seconds", default=30)
            maximum = self.get_config("processing", "clip_max_seconds", default=90)
            for clip in selected_clips:
                duration = clip.get("duration", 0)
                if duration < minimum or duration > maximum:
                    warnings.append(
                        {
                            "name": f"clip_duration_{clip['id']}",
                            "detail": f"{duration:.1f}s (expected {minimum}-{maximum}s)",
                        }
                    )
        else:
            checks.append(
                {
                    "name": "clips_json_has_clips",
                    "pass": False,
                    "detail": "No clips found",
                }
            )

        boundary_evidence = current_clip_boundary_evidence(
            self.episode_dir, episode, self.config, clips
        )
        if boundary_evidence["status"] == "review_required":
            for clip in boundary_evidence["clips"]:
                if not clip["actionable"]:
                    continue
                warnings.append(
                    {
                        "name": f"clip_boundary_{clip['clip_id']}",
                        "detail": (
                            f"{clip['finding_count']} high-confidence transcript word "
                            "interval(s) cross a clip boundary; inspect before approval"
                        ),
                        "evidence": clip,
                    }
                )
        elif clips and boundary_evidence["status"] == "unavailable":
            warnings.append(
                {
                    "name": "clip_boundary_evidence_unavailable",
                    "detail": boundary_evidence["detail"],
                }
            )

        transcript = self.episode_dir / "subtitles" / "transcript.srt"
        checks.append(
            {
                "name": "transcript_srt_exists",
                "pass": transcript.exists(),
                "detail": str(transcript),
            }
        )
        metadata = canonical_release_metadata(self.episode_dir, episode, clips)
        metadata_issues = release_metadata_issues(metadata, selected_clips, self.config)
        metadata_valid = not metadata_issues
        checks.append(
            {
                "name": "metadata_valid",
                "pass": metadata_valid,
                "detail": (
                    "All required release copy is present"
                    if metadata_valid
                    else json.dumps(metadata_issues, separators=(",", ":"))
                ),
            }
        )
        if metadata_valid and not metadata.get("schedule"):
            warnings.append(
                {
                    "name": "metadata_schedule",
                    "detail": "No publish schedule in metadata",
                }
            )

        self.report_progress(2, 3, "Analyzing source and release audio continuity")
        audio_report = {}
        timeline = None
        try:
            timeline = (
                Timeline.from_edits(source_duration, episode.get("longform_edits", []))
                if source_duration > 0
                else None
            )
            audio_report = analyze_episode_audio(
                self.episode_dir,
                timeline=timeline,
                ffmpeg_bin=self.get_config("tools", "ffmpeg", default=None),
            )
            current_review_longform, _ = _render_status(
                self.episode_dir, episode, clips, self.config
            )
            audio_report = apply_finding_reviews(
                audio_report,
                _load_json(self.episode_dir / AUDIO_FINDING_REVIEWS_PATH),
                output_revision=review_output_revision(current_review_longform),
            )
            self.save_json(AUDIO_REPORT_PATH, audio_report)
            gate = audio_release_gate(audio_report)
            target = (
                skipped_checks if gate.get("status") == "not_applicable" else checks
            )
            target.append(
                {
                    "name": "audio_continuity",
                    "status": gate.get("status", "unknown"),
                    "pass": None
                    if gate.get("status") == "not_applicable"
                    else gate.get("status") == "pass",
                    "detail": gate.get("reason", "Audio continuity report unavailable"),
                }
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            checks.append(
                {
                    "name": "audio_continuity",
                    "pass": False,
                    "detail": str(exc),
                }
            )

        output_continuity = {}
        try:
            if timeline is None:
                raise ValueError("Current episode timeline is unavailable")
            output_continuity = analyze_release_audio_continuity(
                self.episode_dir,
                episode,
                clips,
                self.config,
                timeline,
                ffmpeg_bin=self.get_config("tools", "ffmpeg", default=None),
            )
            checks.append(
                {
                    "name": "selected_master_output_continuity",
                    "status": output_continuity.get("status", "unknown"),
                    "pass": output_continuity.get("safe") is True,
                    "detail": output_continuity.get(
                        "detail", "Release audio continuity is unavailable."
                    ),
                }
            )
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
            output_continuity = {
                "schema": OUTPUT_CONTINUITY_SCHEMA,
                "status": "error",
                "safe": False,
                "detail": str(exc),
                "artifacts": [],
                "findings": [],
            }
            checks.append(
                {
                    "name": "selected_master_output_continuity",
                    "status": "error",
                    "pass": False,
                    "detail": str(exc),
                }
            )

        hard_pass = all(check["pass"] for check in checks)
        result = {
            "schema": QUALITY_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "editorial_revision": editorial_revision(self.episode_dir, episode),
            "quality_revision": quality_revision(
                self.episode_dir, episode, config=self.config
            ),
            "release_revision": release_revision(
                self.episode_dir, episode, config=self.config
            ),
            "overall": "pass" if hard_pass else "fail",
            "checks": checks,
            "skipped_checks": skipped_checks,
            "warnings": warnings,
            "hard_checks_passed": sum(1 for check in checks if check["pass"]),
            "hard_checks_total": len(checks),
            "skipped_check_count": len(skipped_checks),
            "warning_count": len(warnings),
            "clip_boundary_evidence": boundary_evidence,
            "audio_quality": audio_report,
            "selected_master_output_continuity": output_continuity,
        }
        self.save_json(QUALITY_REPORT_PATH, result)
        self.report_progress(3, 3, f"QA {result['overall']}")
        self.logger.info(
            "QA: %s — %d/%d checks, %d warnings",
            result["overall"].upper(),
            result["hard_checks_passed"],
            result["hard_checks_total"],
            result["warning_count"],
        )
        return result
