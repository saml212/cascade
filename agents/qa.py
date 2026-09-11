"""Quality agent for release artifacts and source-audio continuity."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from agents.base import BaseAgent
from lib.audio_qa import analyze_episode_audio, release_gate as audio_release_gate
from lib.delivery_video import (
    current_episode_longform_render,
    current_short_render,
    read_render_manifest,
)
from lib.ffprobe import probe as ffprobe
from lib.timeline import Timeline

QUALITY_SCHEMA = "cascade.release-quality/v1"
QUALITY_REPORT_PATH = Path("qa/qa.json")
AUDIO_REPORT_PATH = Path("qa/audio-quality.json")
PLATFORM_COPY_FIELDS = {
    "youtube": ("title", "description"),
    "tiktok": ("caption",),
    "instagram": ("caption",),
    "x": ("text",),
}


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
    payload = {
        "source": _file_signature(episode_dir / "source_merged.mp4"),
        "audio_master": _file_signature(episode_dir / "work" / "audio_mix.wav"),
        "release_video": _file_signature(episode_dir / "upload_video.mp4"),
        "render_manifest": read_render_manifest(episode_dir).get("longform"),
        "audio_sync": episode.get("audio_sync"),
        "audio_mix": episode.get("audio_mix"),
        "crop_config": episode.get("crop_config"),
        "longform_edits": episode.get("longform_edits", []),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def quality_revision(episode_dir: str | Path, episode: dict | None = None) -> str:
    """Fingerprint rendered media and copy, excluding human review decisions."""
    episode_dir = Path(episode_dir)
    episode = (
        episode if episode is not None else _load_json(episode_dir / "episode.json")
    )
    clips = _load_json(episode_dir / "clips.json", {"clips": []})
    clip_list = clips.get("clips", []) if isinstance(clips, dict) else clips
    shorts = {
        str(clip.get("id")): _file_signature(
            episode_dir / "shorts" / f"{clip.get('id')}.mp4"
        )
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
    payload = {
        "editorial_revision": editorial_revision(episode_dir, episode),
        "clips": quality_clips,
        "shorts": shorts,
        "metadata": canonical_release_metadata(episode_dir, episode, clip_list),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def release_revision(episode_dir: str | Path, episode: dict | None = None) -> str:
    """Bind final publish approval to quality inputs and clip decisions."""
    episode_dir = Path(episode_dir)
    episode = (
        episode if episode is not None else _load_json(episode_dir / "episode.json")
    )
    clips_data = _load_json(episode_dir / "clips.json", {"clips": []})
    clips = clips_data.get("clips", []) if isinstance(clips_data, dict) else clips_data
    decisions = {
        str(clip.get("id")): clip.get("status", "pending")
        for clip in clips
        if isinstance(clip, dict) and clip.get("id")
    }
    payload = {
        "quality_revision": quality_revision(episode_dir, episode),
        "clip_decisions": decisions,
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
        "metadata": copy,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _render_status(
    episode_dir: Path, episode: dict, clips: list[dict]
) -> tuple[dict | None, dict[str, dict]]:
    try:
        from agents.pipeline import load_config

        config = load_config()
        audio = episode_dir / "work" / "audio_mix.wav"
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


def quality_snapshot(episode_dir: str | Path, *, include_findings: bool = True) -> dict:
    """Return the single machine-readable quality and release decision."""
    episode_dir = Path(episode_dir)
    episode = _load_json(episode_dir / "episode.json")
    clips_data = _load_json(episode_dir / "clips.json", {"clips": []})
    clips = clips_data.get("clips", []) if isinstance(clips_data, dict) else clips_data
    current_longform, current_shorts = _render_status(episode_dir, episode, clips)
    current_editorial_revision = editorial_revision(episode_dir, episode)
    current_quality_revision = quality_revision(episode_dir, episode)
    current_release_revision = release_revision(episode_dir, episode)
    report = _load_json(episode_dir / QUALITY_REPORT_PATH)
    report_revision = report.get("quality_revision")

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
    elif report.get("overall") != "pass":
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
        from agents.pipeline import load_config

        metadata_issues = release_metadata_issues(metadata, approved, load_config())
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

    audio_quality = report.get("audio_quality") or _load_json(
        episode_dir / AUDIO_REPORT_PATH
    )
    findings = (
        audio_quality.get("findings", []) if isinstance(audio_quality, dict) else []
    )
    analysis = audio_quality.get("analysis", {})
    audio_gate = audio_quality.get("release_gate", {})
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
            "overall": report.get("overall"),
            "checks": report.get("checks", []),
        },
        "release_gate": {
            "status": release_status,
            "safe": release_status == "ready",
            "can_approve_publish": not prerequisites,
            "revision": current_release_revision,
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
            "release_gate": audio_gate,
            "analysis": analysis,
            "finding_count": len(findings),
            "findings": findings if include_findings else [],
        },
    }


class QAAgent(BaseAgent):
    name = "qa"

    def execute(self) -> dict:
        checks: list[dict] = []
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
        if clips:
            missing = [
                clip["id"]
                for clip in clips
                if not (self.episode_dir / "shorts" / f"{clip['id']}.mp4").exists()
            ]
            checks.append(
                {
                    "name": "all_shorts_rendered",
                    "pass": not missing,
                    "detail": f"{len(clips) - len(missing)}/{len(clips)} rendered"
                    + (f", missing: {missing}" if missing else ""),
                }
            )
            minimum = self.get_config("processing", "clip_min_seconds", default=30)
            maximum = self.get_config("processing", "clip_max_seconds", default=90)
            for clip in clips:
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

        transcript = self.episode_dir / "subtitles" / "transcript.srt"
        checks.append(
            {
                "name": "transcript_srt_exists",
                "pass": transcript.exists(),
                "detail": str(transcript),
            }
        )
        metadata = canonical_release_metadata(self.episode_dir, episode, clips)
        metadata_issues = release_metadata_issues(metadata, clips, self.config)
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

        self.report_progress(2, 3, "Analyzing source-channel continuity")
        audio_report = {}
        try:
            timeline = (
                Timeline.from_edits(source_duration, episode.get("longform_edits", []))
                if source_duration > 0
                else None
            )
            audio_report = analyze_episode_audio(
                self.episode_dir,
                timeline=timeline,
                report_path=self.episode_dir / AUDIO_REPORT_PATH,
                ffmpeg_bin=self.get_config("tools", "ffmpeg", default=None),
            )
            gate = audio_release_gate(audio_report)
            checks.append(
                {
                    "name": "audio_continuity",
                    "pass": gate.get("status") == "pass",
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

        hard_pass = all(check["pass"] for check in checks)
        result = {
            "schema": QUALITY_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "editorial_revision": editorial_revision(self.episode_dir, episode),
            "quality_revision": quality_revision(self.episode_dir, episode),
            "release_revision": release_revision(self.episode_dir, episode),
            "overall": "pass" if hard_pass else "fail",
            "checks": checks,
            "warnings": warnings,
            "hard_checks_passed": sum(1 for check in checks if check["pass"]),
            "hard_checks_total": len(checks),
            "warning_count": len(warnings),
            "audio_quality": audio_report,
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
