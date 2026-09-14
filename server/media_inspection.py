"""Bounded, source-clock media inspection with verified artifact inputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agents.audio_analysis import audio_analysis_fingerprint
from agents.speaker_cut import current_speaker_segments, strict_bool
from agents.transcribe import current_diarized_transcript, export_logical_track_window
from lib.audio_mix import CAMERA_AUDIO_TIMELINE_FILTER, selected_audio_source
from lib.delivery_video import (
    current_longform_render,
    current_short_render,
    ffmpeg_executable,
    source_fps,
)
from lib.encoding import get_video_encoding_policy
from lib.ffprobe import file_fingerprint, probe
from lib.short_variants import (
    background_variant_state,
    require_background_variant,
)
from lib.timeline import Timeline

INSPECTION_VERSION = "source-clock/v1"
MAX_PREVIEW_SECONDS = 30.0


@dataclass(frozen=True)
class InspectionTarget:
    path: Path
    timeline: Timeline
    source_duration: float
    media_duration: float
    fingerprint: str
    preserve_audio_timestamps: bool


@dataclass(frozen=True)
class InspectionAsset:
    path: Path
    payload: dict


def file_revision(path: Path) -> str:
    return file_fingerprint(path)["id"]


def media_revision(path: Path) -> str:
    stat = path.stat()
    payload = {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def camera_channel_evidence(episode_dir: Path, episode: dict, config: dict) -> dict:
    """Report whether camera channels are current and independently useful."""
    try:
        analysis = json.loads((episode_dir / "audio_analysis.json").read_text())
        current = analysis.get("fingerprint") == audio_analysis_fingerprint(
            episode_dir, episode, config
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        analysis, current = {}, False
    try:
        identical = (
            strict_bool(analysis["audio_channels_identical"])
            if current and "audio_channels_identical" in analysis
            else None
        )
    except (TypeError, ValueError):
        identical = None
    return {
        "analysis_current": current,
        "channel_count": analysis.get("channels") if current else None,
        "relationship": (
            "dual_mono"
            if identical is True
            else "distinct"
            if identical is False
            else "unknown"
        ),
        "usable_for_speaker_separation": (
            False if identical is True else True if identical is False else None
        ),
    }


def resolve_target(
    episode_dir: Path,
    episode: dict,
    config: dict,
    target: Literal["source", "longform", "short", "short_variant"],
    *,
    clip: dict | None = None,
    variant_id: str | None = None,
) -> InspectionTarget:
    """Resolve a source or revision-current render without mutating artifacts."""
    if target != "short_variant" and variant_id is not None:
        raise ValueError("variant_id is only valid for a short_variant target")
    source = episode_dir / "source_merged.mp4"
    source_duration, fps = _video(source, episode)
    source_timeline = Timeline.from_edits(source_duration).quantize(fps)
    if target == "source":
        return InspectionTarget(
            path=source,
            timeline=source_timeline,
            source_duration=source_duration,
            media_duration=source_duration,
            fingerprint=media_revision(source),
            preserve_audio_timestamps=True,
        )

    audio = selected_audio_source(episode_dir, episode, config) or (
        episode_dir / "work" / "audio_mix.wav"
    )
    segment_document = current_speaker_segments(episode_dir, episode, config)
    transcript = current_diarized_transcript(episode_dir, episode, config)
    if not audio.is_file() or segment_document is None or transcript is None:
        raise ValueError(
            "Current transcript, shot plan, and canonical audio are required"
        )
    segments = segment_document.get("segments", [])
    if not segments:
        raise ValueError("Current shot plan has no segments")
    episode_timeline = Timeline.from_edits(
        source_duration, episode.get("longform_edits", [])
    ).quantize(fps)

    if target == "longform":
        if clip is not None:
            raise ValueError("clip is only valid for a short target")
        record = current_longform_render(episode_dir, episode, config, audio, segments)
        path = episode_dir / "upload_video.mp4"
        timeline = episode_timeline
    elif target in {"short", "short_variant"}:
        if not isinstance(clip, dict) or not clip.get("id"):
            raise ValueError("A stored clip is required for a short target")
        base_record = current_short_render(
            episode_dir, episode, config, audio, segments, clip
        )
        timeline = episode_timeline.slice(
            float(clip["start_seconds"]), float(clip["end_seconds"])
        ).quantize(fps)
        if target == "short_variant":
            require_background_variant(str(variant_id))
            record, state = background_variant_state(
                episode_dir,
                str(clip["id"]),
                base_record=base_record,
                encoding=get_video_encoding_policy(config, "shorts"),
            )
            if not state["current"]:
                raise ValueError("Current short_variant render is unavailable")
            path = episode_dir / state["path"]
        else:
            if variant_id is not None:
                raise ValueError("variant_id is only valid for a short_variant target")
            record = base_record
            path = episode_dir / "shorts" / f"{clip['id']}.mp4"
    else:
        raise ValueError(f"Unsupported inspection target: {target}")
    if record is None:
        raise ValueError(f"Current {target} render is unavailable")
    media_duration, _ = _video(path, episode)
    return InspectionTarget(
        path=path,
        timeline=timeline,
        source_duration=source_duration,
        media_duration=media_duration,
        fingerprint=str(record["fingerprint"]),
        preserve_audio_timestamps=False,
    )


def map_timestamp(
    timeline: Timeline, clock: Literal["source", "output"], seconds: float
) -> tuple[float, float]:
    """Return source and artifact-output timestamps for one retained point."""
    seconds = finite_number(seconds, "seconds")
    if seconds < 0:
        raise ValueError("seconds cannot be negative")
    if clock == "source":
        output = timeline.source_to_output(seconds)
        if output is None or output >= timeline.duration - 1e-9:
            raise ValueError("Source timestamp is outside retained target content")
        return seconds, output
    if clock != "output":
        raise ValueError(f"Unsupported clock: {clock}")
    if seconds >= timeline.duration - 1e-9:
        raise ValueError("Output timestamp is outside target duration")
    return timeline.output_to_source(seconds), seconds


def source_ranges(
    timeline: Timeline, output_start: float, output_end: float
) -> list[dict]:
    """Map an artifact-output range onto retained source-clock ranges."""
    ranges = []
    for span in timeline.spans:
        start = max(output_start, span.output_start)
        end = min(output_end, span.output_end)
        if end > start:
            ranges.append(
                {
                    "start_seconds": round(
                        span.source_start + start - span.output_start, 6
                    ),
                    "end_seconds": round(
                        span.source_start + end - span.output_start, 6
                    ),
                }
            )
    return ranges


def inspect_media_window(
    target: InspectionTarget,
    cache_dir: Path,
    *,
    kind: Literal["frame", "preview"],
    clock: Literal["source", "output"],
    seconds: float,
    duration: float = 0.0,
) -> InspectionAsset:
    """Map and materialize one finite media inspection request."""
    source_seconds, output_seconds = map_timestamp(target.timeline, clock, seconds)
    requested_duration = (
        finite_number(duration, "duration_seconds") if kind == "preview" else None
    )
    if output_seconds >= target.media_duration - 1e-9:
        raise ValueError("Timestamp is outside the artifact")
    if requested_duration is not None:
        if not 0 < requested_duration <= MAX_PREVIEW_SECONDS:
            raise ValueError(
                f"duration_seconds must be between 0 and {MAX_PREVIEW_SECONDS:g}"
            )
        available = min(
            target.timeline.duration - output_seconds,
            target.media_duration - output_seconds,
        )
        duration = min(requested_duration, available)
    asset, cached = render_cached_inspection(
        target,
        cache_dir,
        kind=kind,
        start=output_seconds,
        duration=duration,
    )
    mapping = {
        "source_seconds": round(source_seconds, 6),
        "output_seconds": round(output_seconds, 6),
    }
    if kind == "preview":
        mapping["source_ranges"] = source_ranges(
            target.timeline, output_seconds, output_seconds + duration
        )
    return InspectionAsset(
        path=asset,
        payload={
            "kind": kind,
            "requested": {
                "clock": clock,
                "seconds": seconds,
                **(
                    {"duration_seconds": requested_duration}
                    if requested_duration is not None
                    else {}
                ),
            },
            "mapping": mapping,
            "timeline": {
                "clock": "source",
                "source_duration_seconds": round(target.source_duration, 6),
                "output_duration_seconds": round(target.timeline.duration, 6),
            },
            "artifact": {
                "current": True,
                "fingerprint": target.fingerprint,
                "duration_seconds": round(target.media_duration, 6),
            },
            "asset": {
                "media_type": "image/jpeg" if kind == "frame" else "video/mp4",
                "cached": cached,
                **(
                    {"duration_seconds": round(duration, 6)}
                    if kind == "preview"
                    else {}
                ),
            },
        },
    )


def inspect_audio_window(
    episode_dir: Path,
    episode: dict,
    config: dict,
    *,
    source_kind: Literal["recorder", "camera"],
    start: float,
    duration: float,
    logical_track: int | None = None,
    channel: Literal["left", "right"] | None = None,
) -> InspectionAsset:
    """Export one finite source-clock recorder track or camera channel."""
    start = finite_number(start, "start_seconds")
    duration = finite_number(duration, "duration_seconds")
    if start < 0 or not 0 < duration <= MAX_PREVIEW_SECONDS:
        raise ValueError(
            f"Audio window must be non-negative and at most {MAX_PREVIEW_SECONDS:g} seconds"
        )
    selector = logical_track if source_kind == "recorder" else channel
    digest = hashlib.sha256(
        f"{source_kind}:{selector}:{start:.6f}:{duration:.6f}".encode()
    ).hexdigest()
    cache_dir = episode_dir / "work" / "media_inspection"
    requested = cache_dir / "audio_requests" / f"{digest}.flac"
    manifest = export_logical_track_window(
        episode_dir,
        episode,
        logical_track,
        start,
        start + duration,
        requested,
        source_kind=source_kind,
        channel=channel,
    )
    asset = cache_dir / f"{manifest['audio']['sha256']}.flac"
    try:
        os.link(requested, asset)
    except FileExistsError:
        pass
    return InspectionAsset(
        path=asset,
        payload={
            "clock": "source",
            "source": manifest["source"],
            "source_window": manifest["source_window"],
            "fingerprint": manifest["fingerprint"],
            "channel_evidence": (
                camera_channel_evidence(episode_dir, episode, config)
                if source_kind == "camera"
                else None
            ),
            "asset": {
                "media_type": "audio/flac",
                "size_bytes": manifest["audio"]["size_bytes"],
            },
        },
    )


def render_cached_inspection(
    target: InspectionTarget,
    cache_dir: Path,
    *,
    kind: Literal["frame", "preview"],
    start: float,
    duration: float = 0.0,
    runner: Callable = subprocess.run,
) -> tuple[Path, bool]:
    """Render one frame or at most 30 seconds, atomically and by content hash."""
    start = finite_number(start, "start")
    duration = finite_number(duration, "duration")
    if start < 0 or (kind == "preview" and not 0 < duration <= MAX_PREVIEW_SECONDS):
        raise ValueError("Inspection window is outside the bounded range")
    stat = target.path.stat()
    payload = {
        "version": INSPECTION_VERSION,
        "kind": kind,
        "media": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "fingerprint": target.fingerprint,
        "keep_intervals": list(target.timeline.keep_intervals),
        "start": round(start, 6),
        "duration": round(duration, 6),
        "preserve_audio_timestamps": target.preserve_audio_timestamps,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    suffix = ".jpg" if kind == "frame" else ".mp4"
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{digest}{suffix}"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination, True

    with tempfile.NamedTemporaryFile(
        prefix=f".{digest}.", suffix=suffix, dir=cache_dir, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        runner(
            _inspection_command(
                target.path,
                temporary,
                kind=kind,
                start=start,
                duration=duration,
                preserve_audio_timestamps=target.preserve_audio_timestamps,
            ),
            capture_output=True,
            text=True,
            check=True,
            timeout=max(30.0, duration * 4),
        )
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError("Inspection render produced an empty file")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, False


def finite_number(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _video(path: Path, episode: dict) -> tuple[float, str]:
    details = probe(path)
    stream = next(
        (
            item
            for item in details.get("streams", [])
            if item.get("codec_type") == "video"
        ),
        None,
    )
    if stream is None:
        raise ValueError(f"{path.name} has no video stream")
    duration = finite_number(details.get("format", {}).get("duration"), "duration")
    if duration <= 0:
        raise ValueError(f"{path.name} has no duration")
    return duration, source_fps(stream, episode)


def _inspection_command(
    source: Path,
    destination: Path,
    *,
    kind: str,
    start: float,
    duration: float,
    preserve_audio_timestamps: bool,
) -> list[str]:
    coarse_seek = max(0.0, start - 5.0)
    command = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        str(coarse_seek),
        "-i",
        str(source),
        "-ss",
        str(start - coarse_seek),
    ]
    scale = "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if kind == "frame":
        return [
            *command,
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            scale,
            "-q:v",
            "2",
            str(destination),
        ]
    command.extend(
        [
            "-t",
            str(duration),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            scale,
        ]
    )
    if preserve_audio_timestamps:
        command.extend(["-af", CAMERA_AUDIO_TIMELINE_FILTER])
    return [
        *command,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "2M",
        "-maxrate",
        "3M",
        "-bufsize",
        "4M",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(destination),
    ]
