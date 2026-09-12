"""Source-clock audio continuity checks and grounded recovery previews."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from lib.atomic_write import atomic_write_json
from lib.audio_mix import (
    CAMERA_AUDIO_TIMELINE_FILTER,
    audio_selection_settings,
    current_audio_selection,
    json_fingerprint,
)
from lib.ffprobe import file_fingerprint, get_audio_stream, media_fingerprint
from lib.ffprobe import probe as ffprobe

REPORT_SCHEMA = "cascade.audio-quality/v1"
OUTPUT_CONTINUITY_SCHEMA = "cascade.output-audio-continuity/v1"
DETECTOR_VERSION = "1.2"
OUTPUT_CONTINUITY_VERSION = "1"
PREVIEW_ALGORITHM_VERSION = "5"
TRANSCRIPT_ANALYSIS_FINGERPRINT_METHOD = "sha256-audio-word-timing-speaker/v1"
REPAIR_FADE_SECONDS = 0.08
REPAIR_FRAME_GUARD_SECONDS = 0.03


@dataclass(frozen=True)
class AudioQAConfig:
    """Thresholds for source-channel continuity analysis."""

    sample_rate: int = 8000
    frame_seconds: float = 0.02
    min_issue_seconds: float = 0.30
    bridge_seconds: float = 0.04
    context_seconds: float = 1.50
    exact_zero_peak: float = 1e-8
    near_zero_dbfs: float = -78.0
    survivor_floor_dbfs: float = -58.0
    collapse_ceiling_dbfs: float = -42.0
    collapse_delta_db: float = 14.0
    context_active_dbfs: float = -40.0
    transcript_confidence: float = 0.55
    speaker_affinity_db: float = 3.0
    speaker_activity_percentile: float = 60.0
    preview_padding_seconds: float = 2.0


@dataclass(frozen=True)
class WindowStats:
    """Compact signal measurements on the preserved source clock."""

    frame_seconds: float
    rms_dbfs: np.ndarray
    peak: np.ndarray
    zero_fraction: np.ndarray
    channel_delta_peak: np.ndarray | None = None

    @property
    def duration(self) -> float:
        return len(self.rms_dbfs) * self.frame_seconds


@dataclass(frozen=True)
class OutputContinuityConfig:
    """Thresholds for whole-output silence over current transcript speech."""

    sample_rate: int = 8000
    frame_seconds: float = 0.02
    min_issue_seconds: float = 0.30
    bridge_seconds: float = 0.04
    exact_zero_peak: float = 1e-8
    near_zero_dbfs: float = -60.0
    transcript_confidence: float = 0.55
    maximum_required_speech_overlap_seconds: float = 0.20
    minimum_required_speech_overlap_ratio: float = 0.50
    duration_tolerance_seconds: float = 0.12


def analyze_episode_audio(
    episode_dir: str | Path,
    *,
    timeline: Any | None = None,
    report_path: str | Path | None = None,
    ffmpeg_bin: str | Path | None = None,
    config: AudioQAConfig | None = None,
) -> dict:
    """Analyze current source media and return a JSON-ready quality report.

    The detector always decodes ``source_merged.mp4`` itself. Cached channel
    WAVs and arrays are deliberately ignored because they may have collapsed
    packet timestamp gaps.
    """
    episode_dir = Path(episode_dir)
    source = episode_dir / "source_merged.mp4"
    if not source.is_file():
        raise FileNotFoundError(f"Source media not found: {source}")

    settings = config or AudioQAConfig()
    media_probe = ffprobe(source)
    try:
        audio_stream = get_audio_stream(media_probe)
    except ValueError as exc:
        raise ValueError(f"Source media has no audio stream: {source}") from exc

    source_duration = float(
        audio_stream.get("duration")
        or media_probe.get("format", {}).get("duration")
        or 0
    )
    if source_duration <= 0:
        raise ValueError(f"Source audio has no usable duration: {source}")

    episode = _read_json(episode_dir / "episode.json") or {}
    if timeline is None:
        timeline = _episode_timeline(source_duration, episode)
    transcript_path, transcript = _load_episode_transcript(episode_dir)
    transcript_fingerprint = (
        transcript_analysis_fingerprint(
            transcript_path, transcript, settings.transcript_confidence
        )
        if transcript_path is not None and transcript is not None
        else None
    )
    fingerprint = media_fingerprint(source, media_probe)
    decoder = resolve_ffmpeg(ffmpeg_bin)
    mix_provenance = _mix_provenance(
        episode,
        episode_dir=episode_dir,
        source_fingerprint=fingerprint,
    )
    repair_selection = current_audio_selection(episode_dir, episode)

    channels = int(audio_stream.get("channels") or 0)
    if channels < 2:
        findings: list[dict] = []
        analysis = {
            "status": "not_applicable",
            "reason": "Channel-dropout analysis requires at least two source channels.",
            "decoded_duration_seconds": None,
            "window_count": 0,
            "suppressed_candidate_count": 0,
        }
        speaker_channels: dict[str, dict] = {}
    else:
        stats = decode_audio_windows(source, decoder, settings)
        findings, analysis, speaker_channels = analyze_windows(
            stats,
            transcript=transcript,
            timeline=timeline,
            source_fingerprint=fingerprint["id"],
            episode_id=episode_dir.name,
            config=settings,
        )
    _apply_repair_resolutions(findings, repair_selection)

    report = {
        "schema": REPORT_SCHEMA,
        "revision": 1,
        "episode_id": episode.get("episode_id", episode_dir.name),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(source.resolve()),
            "duration_seconds": round(source_duration, 6),
            "channels": channels,
            "sample_rate": int(audio_stream.get("sample_rate") or 0),
            "codec": audio_stream.get("codec_name"),
            "fingerprint": fingerprint,
        },
        "transcript": {
            "path": str(transcript_path.resolve()) if transcript_path else None,
            "available": transcript is not None,
            "clock": "source",
            "fingerprint": transcript_fingerprint,
        },
        "scope": {
            "check": "source_channel_continuity",
            "checked_inputs": [
                {
                    "role": "source_audio",
                    "path": str(source.resolve()),
                    "fingerprint": fingerprint["id"],
                },
                *(
                    [
                        {
                            "role": "transcript",
                            "path": str(transcript_path.resolve()),
                            "fingerprint": transcript_fingerprint["id"],
                            "content_fingerprint": transcript_fingerprint[
                                "content_fingerprint"
                            ],
                        }
                    ]
                    if transcript_path and transcript_fingerprint
                    else []
                ),
                {
                    "role": "source_timeline_edits",
                    "value": episode.get("longform_edits", []),
                },
            ],
            "outputs_checked": [],
            "selected_mix_provenance": mix_provenance,
            "limitations": [
                "This report classifies source-channel evidence; it does not prove mastered or rendered output continuity.",
                "Diarized speaker labels are acoustic clusters, not persisted person identities.",
                "Candidate findings require review or output-level repair evidence before resolution.",
                *(
                    [
                        "Some active source ranges contain duplicated stereo samples and have no distinct alternate recovery channel."
                    ]
                    if any(
                        item.get("status") == "duplicated_samples"
                        for item in analysis.get("channel_relationship", {}).get(
                            "ranges", []
                        )
                    )
                    else []
                ),
            ],
        },
        "detector": {
            "version": DETECTOR_VERSION,
            "decoder": str(decoder),
            "timeline_filter": CAMERA_AUDIO_TIMELINE_FILTER,
            "settings": asdict(settings),
        },
        "analysis": analysis,
        "speaker_channel_evidence": speaker_channels,
        "findings": findings,
    }
    report["fingerprint"] = _report_fingerprint(
        fingerprint["id"],
        transcript_fingerprint["id"] if transcript_fingerprint else None,
        episode.get("longform_edits", []),
        mix_provenance,
        settings,
        findings,
    )
    if repair_selection is not None:
        report["scope"]["outputs_checked"] = [
            _selected_repair_output_proof(report, repair_selection)
        ]
    report["release_gate"] = release_gate(report)

    if report_path is not None:
        destination = Path(report_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(destination, report)
    return report


def _apply_repair_resolutions(findings: list[dict], selection: dict | None) -> None:
    """Bind repaired finding resolutions to the selected candidate revision."""
    if selection is None:
        return
    selected = {
        item.get("id"): item.get("fingerprint")
        for item in selection.get("repaired_findings", [])
        if isinstance(item, dict)
    }
    for finding in findings:
        fingerprint = finding.get("fingerprint") or finding.get("id")
        if selected.get(finding.get("id")) != fingerprint:
            continue
        finding["resolution"] = {
            "status": "repaired",
            "finding_fingerprint": fingerprint,
            "evidence": {
                "audio_selection_fingerprint": selection.get("fingerprint"),
                "candidate_manifest_fingerprint": selection.get(
                    "candidate_manifest_fingerprint"
                ),
                "synthetic_audio_used": False,
            },
        }


def _selected_repair_output_proof(report: dict, selection: dict) -> dict:
    """Project candidate verification onto the current report and selected bytes."""
    current = {
        (finding.get("id"), finding.get("fingerprint") or finding.get("id"))
        for finding in report.get("findings", [])
    }
    selected = {
        (item.get("id"), item.get("fingerprint"))
        for item in selection.get("repaired_findings", [])
        if isinstance(item, dict)
    }
    bindings_current = bool(selected) and selected.issubset(current)
    verification = json.loads(json.dumps(selection.get("verification") or {}))
    verification["status"] = (
        "pass" if bindings_current and verification.get("status") == "pass" else "stale"
    )
    verification["repair_binding_status"] = "current" if bindings_current else "stale"
    verification["repaired_findings"] = [
        {"id": finding_id, "fingerprint": fingerprint}
        for finding_id, fingerprint in sorted(selected)
    ]
    verification["repaired_finding_ids"] = sorted(
        finding_id for finding_id, _ in selected
    )
    verification["excluded_finding_ids"] = []
    return {
        "role": "selected_audio_master",
        "status": verification["status"],
        "source_report_fingerprint": report.get("fingerprint"),
        "selected_mix_fingerprint": report.get("scope", {})
        .get("selected_mix_provenance", {})
        .get("fingerprint"),
        "fingerprint": selection.get("selected_output", {}).get("fingerprint"),
        "audio_selection_fingerprint": selection.get("fingerprint"),
        "candidate_manifest_fingerprint": selection.get(
            "candidate_manifest_fingerprint"
        ),
        "verification": verification,
    }


def decode_audio_windows(
    source: str | Path,
    ffmpeg_bin: str | Path | None = None,
    config: AudioQAConfig | None = None,
) -> WindowStats:
    """Stream timestamp-preserved stereo PCM into compact analysis windows."""
    source = Path(source)
    settings = config or AudioQAConfig()
    decoder = resolve_ffmpeg(ffmpeg_bin)
    samples_per_frame = round(settings.sample_rate * settings.frame_seconds)
    if samples_per_frame <= 0:
        raise ValueError("Audio QA frame size must contain at least one sample")

    command = [
        str(decoder),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        (
            f"{CAMERA_AUDIO_TIMELINE_FILTER},"
            f"aformat=sample_fmts=flt:sample_rates={settings.sample_rate}:"
            "channel_layouts=stereo"
        ),
        "-ac",
        "2",
        "-ar",
        str(settings.sample_rate),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]

    rms_blocks: list[np.ndarray] = []
    peak_blocks: list[np.ndarray] = []
    zero_blocks: list[np.ndarray] = []
    delta_blocks: list[np.ndarray] = []
    frames_per_block = max(1, round(10 / settings.frame_seconds))
    block_bytes = frames_per_block * samples_per_frame * 2 * 4

    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
        if process.stdout is None:
            raise RuntimeError("FFmpeg did not expose decoded audio")
        pending = bytearray()
        while True:
            chunk = process.stdout.read(block_bytes - len(pending))
            if chunk:
                pending.extend(chunk)
            if len(pending) == block_bytes or (not chunk and pending):
                values = np.frombuffer(bytes(pending), dtype="<f4")
                complete_values = (
                    len(values) // (samples_per_frame * 2) * samples_per_frame * 2
                )
                if complete_values:
                    frames = values[:complete_values].reshape(-1, samples_per_frame, 2)
                    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
                    rms_blocks.append(20 * np.log10(np.maximum(rms, 1e-12)))
                    peak_blocks.append(np.max(np.abs(frames), axis=1))
                    zero_blocks.append(
                        np.mean(np.abs(frames) <= settings.exact_zero_peak, axis=1)
                    )
                    delta_blocks.append(
                        np.max(np.abs(frames[:, :, 0] - frames[:, :, 1]), axis=1)
                    )
                pending.clear()
            if not chunk:
                break

        return_code = process.wait()
        if return_code:
            stderr.seek(0)
            detail = stderr.read().decode(errors="replace")[-1000:]
            raise RuntimeError(f"Audio QA decode failed: {detail}")

    empty = np.empty((0, 2), dtype=np.float64)
    return WindowStats(
        frame_seconds=settings.frame_seconds,
        rms_dbfs=np.concatenate(rms_blocks) if rms_blocks else empty,
        peak=np.concatenate(peak_blocks) if peak_blocks else empty,
        zero_fraction=np.concatenate(zero_blocks) if zero_blocks else empty,
        channel_delta_peak=(
            np.concatenate(delta_blocks) if delta_blocks else np.empty(0)
        ),
    )


def analyze_output_continuity(
    targets: list[dict],
    transcript: dict,
    *,
    episode_timeline: Any,
    transcript_fingerprint: str,
    ffmpeg_bin: str | Path | None = None,
    config: OutputContinuityConfig | None = None,
) -> dict:
    """Check current selected and delivery artifacts for whole-output silence."""
    settings = config or OutputContinuityConfig()
    words = _transcript_words(transcript, settings.transcript_confidence)
    if not words:
        raise ValueError("Current transcript has no timed speech for output continuity")

    decoder_config = AudioQAConfig(
        sample_rate=settings.sample_rate,
        frame_seconds=settings.frame_seconds,
        min_issue_seconds=settings.min_issue_seconds,
        bridge_seconds=settings.bridge_seconds,
        exact_zero_peak=settings.exact_zero_peak,
    )
    artifacts = []
    all_findings = []
    for target in targets:
        artifact = {
            key: target[key]
            for key in (
                "role",
                "clip_id",
                "path",
                "clock",
                "required",
                "revision",
                "status",
                "detail",
            )
            if key in target
        }
        artifact["currentness"] = target.get("status")
        if target.get("status") != "current":
            if target.get("status") not in {
                "missing",
                "stale",
                "unavailable",
                "not_required",
            } or (
                target.get("required", True) and target.get("status") == "not_required"
            ):
                artifact.update(
                    status="error",
                    detail="Artifact currentness status is invalid.",
                )
            artifact["findings"] = []
            artifacts.append(artifact)
            continue

        timeline = target.get("timeline")
        if timeline is None:
            artifact.update(
                status="error",
                detail="Artifact timeline is unavailable.",
                findings=[],
            )
            artifacts.append(artifact)
            continue
        path = Path(target["path"])
        if target.get("scan_identity") != _scan_identity(path):
            artifact.update(
                status="stale",
                detail="Artifact changed after its currentness proof was resolved.",
                findings=[],
            )
            artifacts.append(artifact)
            continue
        try:
            stats = decode_audio_windows(path, ffmpeg_bin, decoder_config)
            expected_duration = (
                timeline.source_duration
                if target["clock"] == "source"
                else timeline.duration
            )
            if stats.duration + settings.duration_tolerance_seconds < expected_duration:
                raise ValueError(
                    f"Decoded audio ends at {stats.duration:.3f}s; "
                    f"expected at least {expected_duration:.3f}s."
                )
            findings = analyze_output_windows(
                stats,
                words=words,
                timeline=timeline,
                episode_timeline=episode_timeline,
                artifact_clock=target["clock"],
                role=target["role"],
                revision=target.get("revision"),
                config=settings,
            )
            if target.get("scan_identity") != _scan_identity(path):
                raise ValueError("Artifact changed while output continuity was decoded")
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
            artifact.update(status="error", detail=str(exc), findings=[])
        else:
            artifact.update(
                status="failed" if findings else "pass",
                detail=(
                    f"{len(findings)} speech-overlapping silence span(s) detected."
                    if findings
                    else "No speech-overlapping whole-output silence detected."
                ),
                decoded_duration_seconds=round(stats.duration, 6),
                expected_duration_seconds=round(
                    expected_duration,
                    6,
                ),
                findings=findings,
            )
            all_findings.extend(findings)
        artifacts.append(artifact)

    blocking = [
        artifact
        for artifact in artifacts
        if artifact.get("required", True) and artifact.get("status") != "pass"
    ]
    precedence = ("failed", "error", "stale", "missing", "unavailable")
    status = (
        next(
            (
                candidate
                for candidate in precedence
                if any(artifact.get("status") == candidate for artifact in blocking)
            ),
            "error",
        )
        if blocking
        else "pass"
    )
    detail = (
        "All current selected and delivery audio passed continuity analysis."
        if status == "pass"
        else "; ".join(
            "{}{}: {} ({})".format(
                artifact.get("role", "artifact"),
                f"/{artifact['clip_id']}" if artifact.get("clip_id") else "",
                artifact.get("status", "unknown"),
                artifact.get("detail", "no detail"),
            )
            for artifact in blocking
        )
    )
    report = {
        "schema": OUTPUT_CONTINUITY_SCHEMA,
        "detector_version": OUTPUT_CONTINUITY_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "safe": status == "pass",
        "detail": detail,
        "transcript_fingerprint": transcript_fingerprint,
        "timeline": {
            "keep_intervals": [list(item) for item in episode_timeline.keep_intervals],
            "output_duration_seconds": episode_timeline.duration,
        },
        "settings": asdict(settings),
        "artifacts": artifacts,
        "findings": all_findings,
    }
    report["fingerprint"] = json_fingerprint(
        {
            key: report[key]
            for key in (
                "schema",
                "detector_version",
                "status",
                "transcript_fingerprint",
                "timeline",
                "settings",
                "artifacts",
            )
        }
    )
    return report


def _scan_identity(path: Path) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def analyze_output_windows(
    stats: WindowStats,
    *,
    words: list[dict],
    timeline: Any,
    episode_timeline: Any,
    artifact_clock: str,
    role: str,
    revision: str | None = None,
    config: OutputContinuityConfig | None = None,
) -> list[dict]:
    """Return transcript-grounded spans where every output channel is quiet."""
    settings = config or OutputContinuityConfig(frame_seconds=stats.frame_seconds)
    if artifact_clock not in {"source", "output"}:
        raise ValueError(f"Unsupported artifact clock: {artifact_clock}")
    if stats.rms_dbfs.ndim != 2 or stats.rms_dbfs.shape[1] != 2:
        raise ValueError("Output continuity statistics must contain two channels")
    if not (stats.rms_dbfs.shape == stats.peak.shape == stats.zero_fraction.shape):
        raise ValueError("Output continuity statistics have inconsistent shapes")

    projected = timeline.project(words, output_clock=artifact_clock == "output")
    exact = np.max(stats.peak, axis=1) <= settings.exact_zero_peak
    near = np.max(stats.rms_dbfs, axis=1) <= settings.near_zero_dbfs
    findings = []
    for start, end in _mask_spans(exact | near, stats.frame_seconds, settings):
        start_seconds = start * stats.frame_seconds
        end_seconds = end * stats.frame_seconds
        overlap_words = [
            word
            for word in projected
            if word["end"] > start_seconds and word["start"] < end_seconds
        ]
        speech_overlap = _interval_overlap_seconds(
            start_seconds, end_seconds, overlap_words
        )
        required_overlap = min(
            settings.maximum_required_speech_overlap_seconds,
            (end_seconds - start_seconds)
            * settings.minimum_required_speech_overlap_ratio,
        )
        if speech_overlap + 1e-9 < required_overlap:
            continue

        source_ranges = _artifact_source_ranges(
            timeline, start_seconds, end_seconds, artifact_clock
        )
        episode_output_ranges = _episode_output_ranges(episode_timeline, source_ranges)
        kind = "digital_zero" if bool(np.all(exact[start:end])) else "near_zero"
        identity = [
            OUTPUT_CONTINUITY_VERSION,
            role,
            revision,
            kind,
            round(start_seconds, 6),
            round(end_seconds, 6),
        ]
        finding = {
            "id": "oc_"
            + hashlib.sha256(
                json.dumps(identity, separators=(",", ":"), default=str).encode()
            ).hexdigest()[:16],
            "kind": kind,
            "classification": "speech_overlapping_whole_output_silence",
            "severity": "error",
            "role": role,
            "artifact_time": {
                "clock": artifact_clock,
                "start_seconds": round(start_seconds, 6),
                "end_seconds": round(end_seconds, 6),
                "duration_seconds": round(end_seconds - start_seconds, 6),
            },
            "source_ranges": source_ranges,
            "episode_output_ranges": episode_output_ranges,
            "evidence": {
                "maximum_channel_median_dbfs": _round_finite(
                    float(np.median(np.max(stats.rms_dbfs[start:end], axis=1)))
                ),
                "minimum_channel_zero_sample_fraction": round(
                    float(np.mean(np.min(stats.zero_fraction[start:end], axis=1))),
                    6,
                ),
                "speech_overlap_seconds": round(speech_overlap, 6),
                "required_speech_overlap_seconds": round(required_overlap, 6),
                "transcript_word_count": len(overlap_words),
                "transcript_excerpt": " ".join(
                    word["word"] for word in overlap_words[:12] if word["word"]
                ),
            },
            "resolution": {"status": "unresolved"},
        }
        finding["fingerprint"] = json_fingerprint(
            {
                **finding,
                "evidence": {
                    key: value
                    for key, value in finding["evidence"].items()
                    if key != "transcript_excerpt"
                },
            }
        )
        findings.append(finding)
    return findings


def _interval_overlap_seconds(start: float, end: float, words: list[dict]) -> float:
    intervals = sorted(
        (max(start, word["start"]), min(end, word["end"]))
        for word in words
        if min(end, word["end"]) > max(start, word["start"])
    )
    if not intervals:
        return 0.0
    merged = [list(intervals[0])]
    for interval_start, interval_end in intervals[1:]:
        if interval_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], interval_end)
        else:
            merged.append([interval_start, interval_end])
    return sum(interval_end - interval_start for interval_start, interval_end in merged)


def _artifact_source_ranges(
    timeline: Any, start: float, end: float, clock: str
) -> list[dict]:
    if clock == "source":
        clipped_start = max(0.0, start)
        clipped_end = min(timeline.source_duration, end)
        ranges = (
            timeline.source_ranges(clipped_start, clipped_end)
            if clipped_end > clipped_start
            else []
        )
    else:
        ranges = []
        for span in timeline.spans:
            output_start = max(start, span.output_start)
            output_end = min(end, span.output_end)
            if output_end <= output_start:
                continue
            ranges.append(
                (
                    span.source_start + output_start - span.output_start,
                    span.source_start + output_end - span.output_start,
                )
            )
    return [
        {
            "start_seconds": round(range_start, 6),
            "end_seconds": round(range_end, 6),
        }
        for range_start, range_end in ranges
    ]


def _episode_output_ranges(
    episode_timeline: Any, source_ranges: list[dict]
) -> list[dict]:
    projected = episode_timeline.project(
        [
            {"start": item["start_seconds"], "end": item["end_seconds"]}
            for item in source_ranges
        ],
        output_clock=True,
    )
    return [
        {
            "start_seconds": round(item["start"], 6),
            "end_seconds": round(item["end"], 6),
        }
        for item in projected
    ]


def analyze_windows(
    stats: WindowStats,
    *,
    transcript: dict | None = None,
    timeline: Any | None = None,
    source_fingerprint: str = "unknown",
    episode_id: str = "episode",
    config: AudioQAConfig | None = None,
) -> tuple[list[dict], dict, dict[str, dict]]:
    """Classify one-channel silence using context and transcript evidence."""
    settings = config or AudioQAConfig(frame_seconds=stats.frame_seconds)
    if stats.rms_dbfs.ndim != 2 or stats.rms_dbfs.shape[1] != 2:
        raise ValueError("Audio QA window statistics must contain two channels")
    if not (stats.rms_dbfs.shape == stats.peak.shape == stats.zero_fraction.shape):
        raise ValueError("Audio QA window statistics have inconsistent shapes")

    words = _transcript_words(transcript, settings.transcript_confidence)
    speaker_masks = _speaker_masks(words, len(stats.rms_dbfs), stats.frame_seconds)
    speaker_channels = _infer_speaker_channels(stats, speaker_masks, settings)
    findings: list[dict] = []
    suppressed_candidates: list[dict] = []

    for channel in (0, 1):
        other = 1 - channel
        exact = (stats.peak[:, channel] <= settings.exact_zero_peak) | (
            stats.zero_fraction[:, channel] >= 0.999
        )
        near = (stats.rms_dbfs[:, channel] <= settings.near_zero_dbfs) & ~exact
        survivor = stats.rms_dbfs[:, other] >= settings.survivor_floor_dbfs
        exact = exact & survivor
        near = near & survivor
        exact_or_near = exact | near
        collapse = (
            ~exact_or_near
            & (stats.rms_dbfs[:, channel] <= settings.collapse_ceiling_dbfs)
            & (stats.rms_dbfs[:, other] >= settings.survivor_floor_dbfs)
            & (
                stats.rms_dbfs[:, other] - stats.rms_dbfs[:, channel]
                >= settings.collapse_delta_db
            )
        )

        candidates = [
            ("digital_zero", start, end)
            for start, end in _mask_spans(exact, stats.frame_seconds, settings)
        ]
        candidates += [
            ("near_zero", start, end)
            for start, end in _mask_spans(near, stats.frame_seconds, settings)
        ]
        candidates += [
            ("sharp_level_collapse", start, end)
            for start, end in _mask_spans(collapse, stats.frame_seconds, settings)
        ]
        candidates = _remove_overlapping_collapses(candidates)

        for kind, start_index, end_index in candidates:
            decision = _classify_candidate(
                stats,
                start_index,
                end_index,
                channel,
                kind,
                words,
                speaker_masks,
                speaker_channels,
                settings,
            )
            if not decision["actionable"]:
                suppressed_candidates.append(
                    _build_suppressed_candidate(
                        stats,
                        start_index,
                        end_index,
                        channel,
                        kind,
                        decision,
                        timeline,
                        source_fingerprint,
                        episode_id,
                        settings,
                    )
                )
                continue
            finding = _build_finding(
                stats,
                start_index,
                end_index,
                channel,
                kind,
                decision,
                timeline,
                source_fingerprint,
                episode_id,
                settings,
            )
            findings.append(finding)

    findings.sort(
        key=lambda item: (item["source_time"]["start_seconds"], item["channel"])
    )
    exact_seconds = sum(
        item["source_time"]["duration_seconds"]
        for item in findings
        if item["kind"] in {"digital_zero", "near_zero"}
    )
    analysis = {
        "status": "complete",
        "clock": "source",
        "decoded_duration_seconds": round(stats.duration, 6),
        "window_count": len(stats.rms_dbfs),
        "finding_count": len(findings),
        "zero_or_near_zero_finding_seconds": round(exact_seconds, 3),
        "suppressed_candidate_count": len(suppressed_candidates),
        "suppressed_candidate_seconds": round(
            sum(
                candidate["source_time"]["duration_seconds"]
                for candidate in suppressed_candidates
            ),
            3,
        ),
        "suppressed_candidates": suppressed_candidates,
        "interpretation": (
            "Suppressed candidates were consistent with turn-taking or lacked "
            "evidence that the quiet channel should contain speech."
        ),
        "channel_relationship": _channel_relationship(stats, settings),
    }
    return findings, analysis, speaker_channels


def _channel_relationship(stats: WindowStats, settings: AudioQAConfig) -> dict:
    """Report where stereo samples offer a distinct alternate recovery channel."""
    delta = stats.channel_delta_peak
    if delta is None or len(delta) != len(stats.rms_dbfs):
        return {"status": "unknown", "window_seconds": 5.0, "ranges": []}

    frames_per_window = max(1, round(5 / stats.frame_seconds))
    minimum_active_frames = max(1, round(0.2 / stats.frame_seconds))
    windows = []
    for start in range(0, len(delta), frames_per_window):
        end = min(len(delta), start + frames_per_window)
        active = (
            np.max(stats.rms_dbfs[start:end], axis=1) >= settings.survivor_floor_dbfs
        )
        if np.count_nonzero(active) < minimum_active_frames:
            state = "insufficient_active_audio"
        elif np.mean(delta[start:end][active] <= settings.exact_zero_peak) >= 0.99:
            state = "duplicated_samples"
        else:
            state = "distinct_samples"
        left = round(start * stats.frame_seconds, 6)
        right = round(end * stats.frame_seconds, 6)
        if windows and windows[-1]["status"] == state:
            windows[-1]["end_seconds"] = right
            windows[-1]["duration_seconds"] = round(
                right - windows[-1]["start_seconds"], 6
            )
        else:
            windows.append(
                {
                    "status": state,
                    "start_seconds": left,
                    "end_seconds": right,
                    "duration_seconds": round(right - left, 6),
                }
            )
    active_states = {item["status"] for item in windows} - {"insufficient_active_audio"}
    return {
        "status": active_states.pop() if len(active_states) == 1 else "mixed",
        "window_seconds": round(frames_per_window * stats.frame_seconds, 6),
        "ranges": windows,
        "limitations": (
            "Duplicated-sample ranges have no distinct alternate source channel; "
            "distinct samples do not prove microphone or speaker identity."
        ),
    }


def release_gate(report: dict) -> dict:
    """Return the release decision represented by a quality report."""
    provenance = report.get("scope", {}).get("selected_mix_provenance", {})
    uses_checked_source = provenance.get("uses_checked_source_audio")
    analysis_status = report.get("analysis", {}).get("status")
    if uses_checked_source is False:
        return {
            "status": "not_applicable",
            "safe": None,
            "blocking_finding_ids": [],
            "review_finding_ids": [],
            "reason": "The selected mix does not use the checked camera-source audio.",
        }
    if analysis_status != "complete" or uses_checked_source is not True:
        return {
            "status": "unknown",
            "safe": False,
            "blocking_finding_ids": [],
            "review_finding_ids": [],
            "reason": (
                "Source-channel continuity was not fully analyzed for the selected mix."
            ),
        }

    proof_state, output_proof = _output_proof_state(report)
    unresolved = [
        finding
        for finding in report.get("findings", [])
        if finding.get("edited_time", {}).get("status") != "removed"
        and not _finding_resolution_is_current(finding, output_proof)
    ]
    blocking = [
        finding["id"]
        for finding in unresolved
        if finding.get("severity") in {"error", "critical"}
    ]
    review = [finding["id"] for finding in unresolved if finding["id"] not in blocking]
    if blocking:
        return {
            "status": "blocked",
            "safe": False,
            "blocking_finding_ids": blocking,
            "review_finding_ids": review,
            "reason": "Unresolved probable source-channel dropouts remain in the edited output.",
        }
    if review:
        return {
            "status": "review_required",
            "safe": False,
            "blocking_finding_ids": [],
            "review_finding_ids": review,
            "reason": "Source-channel continuity candidates still require review.",
        }
    if proof_state != "pass":
        messages = {
            "missing": (
                "output_unverified",
                "The currently selected audio output has not been checked.",
            ),
            "stale": (
                "output_stale",
                "Audio output evidence does not match the current source analysis, mix, or master.",
            ),
            "failed": (
                "output_failed",
                "The current audio output failed continuity verification.",
            ),
        }
        status, reason = messages[proof_state]
        return {
            "status": status,
            "safe": False,
            "blocking_finding_ids": [],
            "review_finding_ids": [],
            "reason": reason,
        }
    return {
        "status": "pass",
        "safe": True,
        "blocking_finding_ids": [],
        "review_finding_ids": [],
        "reason": "Source findings are resolved and the current audio output passed continuity verification.",
    }


def _output_proof_state(report: dict) -> tuple[str, dict | None]:
    """Classify evidence for the currently selected audio master."""
    scope = report.get("scope", {})
    provenance = scope.get("selected_mix_provenance", {})
    selected_output = provenance.get("selected_output")
    outputs = scope.get("outputs_checked") or []
    if not isinstance(selected_output, dict) or not outputs:
        return "missing", None

    expected_output = (selected_output.get("fingerprint") or {}).get("id")
    expected_mix = provenance.get("fingerprint")
    expected_report = report.get("fingerprint")
    for proof in outputs:
        if not isinstance(proof, dict) or proof.get("role") != "selected_audio_master":
            continue
        proof_output = (proof.get("fingerprint") or {}).get("id")
        if (
            proof.get("source_report_fingerprint") != expected_report
            or proof.get("selected_mix_fingerprint") != expected_mix
            or proof_output != expected_output
        ):
            continue
        verification = proof.get("verification") or {}
        checks = verification.get("checks") or []
        if (
            proof.get("status") == "failed"
            or verification.get("status") == "failed"
            or any(check.get("pass") is False for check in checks)
        ):
            return "failed", proof
        if (
            proof.get("status") == "pass"
            and verification.get("status") == "pass"
            and checks
            and all(check.get("pass") is True for check in checks)
        ):
            return "pass", proof
    return "stale", None


def _finding_resolution_is_current(finding: dict, output_proof: dict | None) -> bool:
    resolution = finding.get("resolution") or {}
    status = resolution.get("status", "unresolved")
    finding_fingerprint = finding.get("fingerprint") or finding.get("id")
    if status in {"accepted", "false_positive"}:
        return bool(
            resolution.get("reviewed_by")
            and resolution.get("reviewed_at")
            and resolution.get("evidence")
            and resolution.get("finding_fingerprint") == finding_fingerprint
        )
    if output_proof is None:
        return False
    verification = output_proof.get("verification") or {}
    if status == "repaired":
        return bool(
            resolution.get("finding_fingerprint") == finding_fingerprint
            and any(
                item.get("id") == finding.get("id")
                and item.get("fingerprint") == finding_fingerprint
                for item in verification.get("repaired_findings", [])
            )
        )
    if status == "not_in_selected_mix":
        return finding.get("id") in verification.get("excluded_finding_ids", [])
    return False


def render_finding_preview(
    source: str | Path,
    finding: dict,
    output_dir: str | Path,
    *,
    ffmpeg_bin: str | Path | None = None,
) -> dict:
    """Render the source mix and a surviving-channel recovery experiment.

    Recovery uses only samples already present on the other microphone. It does
    not synthesize, clone, or infer missing speech.
    """
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    decoder = resolve_ffmpeg(ffmpeg_bin)
    source_time = finding["source_time"]
    requested_start = max(
        0.0,
        float(source_time["start_seconds"])
        - float(finding["preview"]["padding_seconds"]),
    )
    requested_end = float(source_time["end_seconds"]) + float(
        finding["preview"]["padding_seconds"]
    )
    duration = requested_end - requested_start
    source_intervals = finding.get("repair_intervals") or [source_time]
    intervals = [
        (
            float(item["start_seconds"]) - requested_start,
            float(item["end_seconds"]) - requested_start,
        )
        for item in source_intervals
    ]
    channel = int(finding["channel"])
    survivor = 1 - channel
    gain_limit = min(
        18.0,
        max(0.0, float(finding.get("recovery", {}).get("maximum_gain_db", 12.0))),
    )
    gain_db = min(
        gain_limit,
        max(-12.0, float(finding["evidence"].get("estimated_recovery_gain_db", 0.0))),
    )
    stem = finding["id"].replace(":", "-")
    original = output_dir / f"{stem}-source.wav"
    fallback = output_dir / f"{stem}-grounded-fallback.wav"

    common = [
        str(decoder),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{requested_start:.6f}",
        "-t",
        f"{duration:.6f}",
        "-i",
        str(source),
        "-vn",
    ]
    original_filter = (
        f"{CAMERA_AUDIO_TIMELINE_FILTER},aformat=channel_layouts=stereo,"
        "pan=mono|c0=0.5*c0+0.5*c1,pan=stereo|c0=c0|c1=c0"
    )
    _checked_ffmpeg(
        [*common, "-af", original_filter, "-c:a", "pcm_s24le", str(original)]
    )

    fade_mode = finding.get("recovery", {}).get("fade_mode", "cross_boundary")
    repair_weights = [
        repair_envelope_weight(start, end, contained=fade_mode == "contained")
        for start, end in intervals
    ]
    fallback_weight = f"min(1,{'+'.join(f'({item})' for item in repair_weights)})"
    normal_weight = f"1-({fallback_weight})"
    filter_graph = (
        f"[0:a]{CAMERA_AUDIO_TIMELINE_FILTER},"
        "aformat=channel_layouts=stereo,asplit=2[m][s];"
        f"[m]pan=mono|c0=0.5*c0+0.5*c1,volume='{normal_weight}':eval=frame[n];"
        f"[s]pan=mono|c0=c{survivor},volume='({fallback_weight})*{10 ** (gain_db / 20):.8f}':"
        "eval=frame[r];[n][r]amix=inputs=2:duration=first:normalize=0,"
        "alimiter=limit=0.95:level=false:latency=true,"
        "pan=stereo|c0=c0|c1=c0[out]"
    )
    _checked_ffmpeg(
        [
            *common,
            "-filter_complex",
            filter_graph,
            "-map",
            "[out]",
            "-c:a",
            "pcm_s24le",
            str(fallback),
        ]
    )
    return {
        "finding_id": finding["id"],
        "original": str(original.resolve()),
        "grounded_fallback": str(fallback.resolve()),
        "fallback_source_channel": survivor,
        "fallback_gain_db": round(gain_db, 2),
        "synthetic_audio_used": False,
        "envelope": {
            "mode": fade_mode,
            "maximum_fade_seconds": REPAIR_FADE_SECONDS,
            "frame_guard_seconds": (
                REPAIR_FRAME_GUARD_SECONDS if fade_mode == "contained" else 0.0
            ),
        },
    }


def transcript_analysis_fingerprint(
    path: str | Path, transcript: dict, minimum_confidence: float
) -> dict:
    """Fingerprint only transcript fields used by continuity classification."""
    words = [
        {key: word[key] for key in ("start", "end", "speaker")}
        for word in _transcript_words(transcript, minimum_confidence)
    ]
    encoded = json.dumps(words, sort_keys=True, separators=(",", ":"))
    return {
        "id": "sha256:" + hashlib.sha256(encoded.encode()).hexdigest(),
        "method": TRANSCRIPT_ANALYSIS_FINGERPRINT_METHOD,
        "word_count": len(words),
        "content_fingerprint": file_fingerprint(path),
    }


def resolve_ffmpeg(explicit: str | Path | None = None) -> Path:
    """Resolve the full FFmpeg build when installed, with normal FFmpeg fallback."""
    if explicit:
        candidate = Path(explicit)
        resolved = shutil.which(str(explicit))
        if resolved:
            return Path(resolved)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise FileNotFoundError(f"FFmpeg executable not found: {explicit}")

    configured = os.getenv("CASCADE_FFMPEG")
    candidates = [
        Path(configured) if configured else None,
        Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
        Path(shutil.which("ffmpeg") or ""),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("FFmpeg is required for audio quality analysis")


def _classify_candidate(
    stats: WindowStats,
    start: int,
    end: int,
    channel: int,
    kind: str,
    words: list[dict],
    speaker_masks: dict[str, np.ndarray],
    speaker_channels: dict[str, dict],
    settings: AudioQAConfig,
) -> dict:
    interval_start = start * stats.frame_seconds
    interval_end = end * stats.frame_seconds
    duration = (end - start) * stats.frame_seconds
    context_frames = round(settings.context_seconds / stats.frame_seconds)
    before = stats.rms_dbfs[max(0, start - context_frames) : start, channel]
    after = stats.rms_dbfs[
        end : min(len(stats.rms_dbfs), end + context_frames), channel
    ]
    before_db = _upper_quartile(before)
    after_db = _upper_quartile(after)
    issue_db = float(np.median(stats.rms_dbfs[start:end, channel]))
    survivor_db = float(np.median(stats.rms_dbfs[start:end, 1 - channel]))
    context_peak = max(before_db, after_db)
    context_drop = context_peak - issue_db
    context_continuity = (
        before_db >= settings.context_active_dbfs
        and after_db >= settings.context_active_dbfs
        and context_drop >= settings.collapse_delta_db
    )

    expected_speakers = []
    surviving_speakers = []
    expected_frames = np.zeros(end - start, dtype=bool)
    surviving_frames = np.zeros(end - start, dtype=bool)
    for speaker, mask in speaker_masks.items():
        mapping = speaker_channels.get(speaker, {})
        mapped_channel = mapping.get("channel")
        if mapped_channel == channel:
            expected_speakers.append(speaker)
            expected_frames |= mask[start:end]
        elif mapped_channel == 1 - channel:
            surviving_speakers.append(speaker)
            surviving_frames |= mask[start:end]

    expected_ranges = _refine_activity_ranges(
        _activity_ranges(expected_frames, start, stats.frame_seconds),
        words,
        set(expected_speakers),
        interval_start,
        interval_end,
    )
    surviving_ranges = _refine_activity_ranges(
        _activity_ranges(surviving_frames, start, stats.frame_seconds),
        words,
        set(surviving_speakers),
        interval_start,
        interval_end,
    )
    expected_seconds = _ranges_duration(expected_ranges)
    surviving_seconds = _ranges_duration(surviving_ranges)
    temporal_overlap_seconds = _ranges_overlap_seconds(
        expected_ranges, surviving_ranges
    )
    expected_minimum = min(0.20, duration * 0.15)
    transcript_expected = expected_seconds >= expected_minimum
    transcript_contradicts = (
        surviving_seconds >= max(0.20, expected_seconds * 2) and not transcript_expected
    )
    if transcript_contradicts and not context_continuity:
        suppression_reason = "transcript_supports_surviving_channel"
    elif not transcript_expected and not context_continuity:
        suppression_reason = "no_expected_speech_or_two_sided_continuity"
    else:
        suppression_reason = None

    confidence = (
        0.48 if kind == "digital_zero" else 0.40 if kind == "near_zero" else 0.36
    )
    if (
        kind == "digital_zero"
        and float(np.median(stats.zero_fraction[start:end, channel])) >= 0.999
    ):
        confidence += 0.12
    if transcript_expected:
        confidence += 0.23 * min(1.0, expected_seconds / max(duration * 0.5, 0.1))
    if context_continuity:
        confidence += 0.15
    if survivor_db >= settings.survivor_floor_dbfs:
        confidence += 0.06
    if duration >= 1.0:
        confidence += 0.05
    confidence = round(min(0.99, confidence), 3)
    severity = "error" if transcript_expected and confidence >= 0.70 else "warning"
    overlap = [
        word
        for word in words
        if word["end"] > interval_start and word["start"] < interval_end
    ]
    excerpt = " ".join(word["word"] for word in overlap[:30])
    gain = max(0.0, min(12.0, context_peak - survivor_db))
    return {
        "actionable": suppression_reason is None,
        "suppression_reason": suppression_reason,
        "classification": (
            "probable_dropout" if transcript_expected else "candidate_discontinuity"
        ),
        "confidence": confidence,
        "severity": severity,
        "issue_dbfs": issue_db,
        "survivor_dbfs": survivor_db,
        "before_dbfs": before_db,
        "after_dbfs": after_db,
        "context_drop_db": context_drop,
        "context_continuity": context_continuity,
        "expected_speakers": expected_speakers,
        "surviving_speakers": surviving_speakers,
        "expected_speech_seconds": expected_seconds,
        "surviving_speech_seconds": surviving_seconds,
        "expected_speech_ranges": expected_ranges,
        "surviving_speech_ranges": surviving_ranges,
        "temporal_overlap_seconds": temporal_overlap_seconds,
        "transcript_excerpt": excerpt or None,
        "transcript_word_count": len(overlap),
        "estimated_recovery_gain_db": gain,
    }


def _build_finding(
    stats: WindowStats,
    start: int,
    end: int,
    channel: int,
    kind: str,
    decision: dict,
    timeline: Any | None,
    source_fingerprint: str,
    episode_id: str,
    settings: AudioQAConfig,
) -> dict:
    start_seconds = round(start * stats.frame_seconds, 6)
    end_seconds = round(end * stats.frame_seconds, 6)
    signature = json.dumps(
        [source_fingerprint, kind, channel, start_seconds, end_seconds],
        separators=(",", ":"),
    )
    finding_id = "aq_" + hashlib.sha256(signature.encode()).hexdigest()[:16]
    evidence = {
        "quiet_channel_median_dbfs": round(decision["issue_dbfs"], 2),
        "surviving_channel_median_dbfs": round(decision["survivor_dbfs"], 2),
        "level_before_dbfs": _round_finite(decision["before_dbfs"]),
        "level_after_dbfs": _round_finite(decision["after_dbfs"]),
        "context_drop_db": _round_finite(decision["context_drop_db"]),
        "context_continuity": decision["context_continuity"],
        "expected_transcript_speakers": decision["expected_speakers"],
        "surviving_transcript_speakers": decision["surviving_speakers"],
        "expected_speech_seconds": round(decision["expected_speech_seconds"], 3),
        "surviving_speech_seconds": round(decision["surviving_speech_seconds"], 3),
        "expected_speech_ranges": decision["expected_speech_ranges"],
        "surviving_speech_ranges": decision["surviving_speech_ranges"],
        "temporal_overlap_seconds": round(decision["temporal_overlap_seconds"], 3),
        "transcript_word_count": decision["transcript_word_count"],
        "transcript_excerpt": decision["transcript_excerpt"],
        "zero_sample_fraction": round(
            float(np.mean(stats.zero_fraction[start:end, channel])), 6
        ),
        "estimated_recovery_gain_db": round(decision["estimated_recovery_gain_db"], 2),
    }
    duration = round(end_seconds - start_seconds, 6)
    finding = {
        "id": finding_id,
        "kind": kind,
        "classification": decision["classification"],
        "severity": decision["severity"],
        "confidence": decision["confidence"],
        "channel": channel,
        "surviving_channel": 1 - channel,
        "source_time": {
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "duration_seconds": duration,
        },
        "edited_time": _edited_ranges(timeline, start_seconds, end_seconds),
        "evidence": evidence,
        "resolution": {"status": "unresolved"},
        "recovery": {
            "strategy": "surviving_channel_only",
            "grounded": True,
            "synthetic_audio": False,
            "status": "preview_available",
        },
        "preview": {
            "padding_seconds": settings.preview_padding_seconds,
            "source": (
                f"/api/episodes/{episode_id}/audio-qc/findings/{finding_id}/preview"
                "?variant=source"
            ),
            "grounded_fallback": (
                f"/api/episodes/{episode_id}/audio-qc/findings/{finding_id}/preview"
                "?variant=grounded-fallback"
            ),
        },
    }
    finding["fingerprint"] = _finding_fingerprint(finding)
    return finding


def _finding_fingerprint(finding: dict) -> str:
    """Bind repair proof to the semantic evidence behind a stable finding ID."""
    evidence = {
        key: value
        for key, value in (finding.get("evidence") or {}).items()
        if key != "transcript_excerpt"
    }
    return json_fingerprint(
        {
            key: finding.get(key)
            for key in (
                "id",
                "kind",
                "classification",
                "severity",
                "confidence",
                "channel",
                "surviving_channel",
                "source_time",
                "edited_time",
                "recovery",
            )
        }
        | {"evidence": evidence}
    )


def _build_suppressed_candidate(
    stats: WindowStats,
    start: int,
    end: int,
    channel: int,
    kind: str,
    decision: dict,
    timeline: Any | None,
    source_fingerprint: str,
    episode_id: str,
    settings: AudioQAConfig,
) -> dict:
    candidate = _build_finding(
        stats,
        start,
        end,
        channel,
        kind,
        decision,
        timeline,
        source_fingerprint,
        episode_id,
        settings,
    )
    return {
        key: candidate[key]
        for key in (
            "id",
            "kind",
            "channel",
            "surviving_channel",
            "source_time",
            "edited_time",
            "evidence",
            "preview",
        )
    } | {"suppression_reason": decision["suppression_reason"]}


def _edited_ranges(timeline: Any | None, start: float, end: float) -> dict:
    if timeline is None:
        return {"status": "unavailable", "ranges": []}
    if hasattr(timeline, "project"):
        projected = timeline.project([{"start": start, "end": end}], output_clock=True)
        ranges = [
            {
                "start_seconds": round(item["start"], 6),
                "end_seconds": round(item["end"], 6),
                "source_start_seconds": round(item["source_start"], 6),
                "source_end_seconds": round(item["source_end"], 6),
            }
            for item in projected
        ]
    else:
        output_start = timeline.source_to_output(start)
        output_end = timeline.source_to_output(end)
        ranges = (
            [
                {
                    "start_seconds": round(output_start, 6),
                    "end_seconds": round(output_end, 6),
                    "source_start_seconds": start,
                    "source_end_seconds": end,
                }
            ]
            if output_start is not None and output_end is not None
            else []
        )
    if not ranges:
        status = "removed"
    elif len(ranges) == 1 and math.isclose(
        ranges[0]["source_end_seconds"] - ranges[0]["source_start_seconds"],
        end - start,
        abs_tol=1e-4,
    ):
        status = "retained"
    else:
        status = "split"
    return {"status": status, "ranges": ranges}


def _mask_spans(
    mask: np.ndarray, frame_seconds: float, settings: AudioQAConfig
) -> list[tuple[int, int]]:
    indices = np.flatnonzero(mask)
    if not len(indices):
        return []
    bridge_frames = round(settings.bridge_seconds / frame_seconds)
    minimum_frames = max(1, math.ceil(settings.min_issue_seconds / frame_seconds))
    spans = []
    start = previous = int(indices[0])
    for value in indices[1:]:
        value = int(value)
        if value - previous > bridge_frames + 1:
            if previous + 1 - start >= minimum_frames:
                spans.append((start, previous + 1))
            start = value
        previous = value
    if previous + 1 - start >= minimum_frames:
        spans.append((start, previous + 1))
    return spans


def _remove_overlapping_collapses(
    candidates: list[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    exact = [
        (start, end)
        for kind, start, end in candidates
        if kind in {"digital_zero", "near_zero"}
    ]
    result = []
    for candidate in candidates:
        kind, start, end = candidate
        if kind == "sharp_level_collapse" and any(
            min(end, exact_end) > max(start, exact_start)
            for exact_start, exact_end in exact
        ):
            continue
        result.append(candidate)
    return result


def _transcript_words(transcript: dict | None, minimum_confidence: float) -> list[dict]:
    if not transcript:
        return []
    utterances = transcript.get("utterances")
    if utterances is None:
        utterances = transcript.get("results", {}).get("utterances", [])
    words = []
    for utterance in utterances or []:
        utterance_speaker = utterance.get("speaker")
        for word in utterance.get("words", []):
            try:
                start = float(word["start"])
                end = float(word["end"])
                confidence = float(
                    word.get("confidence", utterance.get("confidence", 1))
                )
            except (KeyError, TypeError, ValueError):
                continue
            speaker = word.get("speaker", utterance_speaker)
            if speaker is None or end <= start or confidence < minimum_confidence:
                continue
            words.append(
                {
                    "start": start,
                    "end": end,
                    "speaker": str(speaker),
                    "word": str(word.get("punctuated_word") or word.get("word") or ""),
                }
            )
    return words


def _activity_ranges(
    mask: np.ndarray, source_start_frame: int, frame_seconds: float
) -> list[dict]:
    """Describe transcript activity without merging separate speaker turns."""
    boundaries = np.flatnonzero(np.diff(np.pad(mask.astype(np.int8), (1, 1))))
    return [
        {
            "start_seconds": round((source_start_frame + start) * frame_seconds, 6),
            "end_seconds": round((source_start_frame + end) * frame_seconds, 6),
            "duration_seconds": round((end - start) * frame_seconds, 6),
        }
        for start, end in boundaries.reshape(-1, 2)
    ]


def _refine_activity_ranges(
    coarse_ranges: list[dict],
    words: list[dict],
    speakers: set[str],
    interval_start: float,
    interval_end: float,
) -> list[dict]:
    """Replace detector-frame edges with the transcript's exact word edges."""
    refined = []
    for coarse in coarse_ranges:
        matching = [
            word
            for word in words
            if word["speaker"] in speakers
            and word["end"] > coarse["start_seconds"]
            and word["start"] < coarse["end_seconds"]
        ]
        if not matching:
            continue
        start = max(interval_start, min(word["start"] for word in matching))
        end = min(interval_end, max(word["end"] for word in matching))
        if end > start:
            refined.append(
                {
                    "start_seconds": round(start, 6),
                    "end_seconds": round(end, 6),
                    "duration_seconds": round(end - start, 6),
                }
            )
    return refined


def _ranges_duration(ranges: list[dict]) -> float:
    return sum(
        float(item["end_seconds"]) - float(item["start_seconds"]) for item in ranges
    )


def _ranges_overlap_seconds(left: list[dict], right: list[dict]) -> float:
    return sum(
        max(
            0.0,
            min(float(a["end_seconds"]), float(b["end_seconds"]))
            - max(float(a["start_seconds"]), float(b["start_seconds"])),
        )
        for a in left
        for b in right
    )


def _speaker_masks(
    words: list[dict], frame_count: int, frame_seconds: float
) -> dict[str, np.ndarray]:
    masks: dict[str, np.ndarray] = {}
    for word in words:
        mask = masks.setdefault(word["speaker"], np.zeros(frame_count, dtype=bool))
        start = max(0, math.floor(word["start"] / frame_seconds))
        end = min(frame_count, math.ceil(word["end"] / frame_seconds))
        mask[start:end] = True
    return masks


def _infer_speaker_channels(
    stats: WindowStats,
    speaker_masks: dict[str, np.ndarray],
    settings: AudioQAConfig,
) -> dict[str, dict]:
    if not speaker_masks:
        return {}
    activity_count = np.sum(np.stack(list(speaker_masks.values())), axis=0)
    delta = stats.rms_dbfs[:, 0] - stats.rms_dbfs[:, 1]
    result = {}
    minimum_frames = max(5, round(0.5 / stats.frame_seconds))
    for speaker, mask in speaker_masks.items():
        isolated = mask & (activity_count == 1)
        levels = np.max(stats.rms_dbfs[isolated], axis=1)
        activity_floor = (
            max(
                -55.0,
                float(np.percentile(levels, settings.speaker_activity_percentile)),
            )
            if len(levels)
            else -55.0
        )
        usable = isolated & (np.max(stats.rms_dbfs, axis=1) >= activity_floor)
        values = delta[usable]
        if len(values) < minimum_frames:
            result[speaker] = {
                "channel": None,
                "median_left_minus_right_db": None,
                "evidence_seconds": round(len(values) * stats.frame_seconds, 3),
                "confidence": 0.0,
                "activity_floor_dbfs": round(activity_floor, 2),
            }
            continue
        median = float(np.median(values))
        channel = (
            0
            if median >= settings.speaker_affinity_db
            else 1
            if median <= -settings.speaker_affinity_db
            else None
        )
        confidence = min(0.99, abs(median) / 18) if channel is not None else 0.0
        result[speaker] = {
            "channel": channel,
            "median_left_minus_right_db": round(median, 2),
            "evidence_seconds": round(len(values) * stats.frame_seconds, 3),
            "confidence": round(confidence, 3),
            "activity_floor_dbfs": round(activity_floor, 2),
            "basis": "diarized speech energy affinity; not a persisted person identity",
        }
    return result


def _episode_timeline(duration: float, episode: dict) -> Any | None:
    try:
        from lib.timeline import Timeline
    except ImportError:
        return None
    return Timeline.from_edits(duration, episode.get("longform_edits", []))


def _mix_provenance(
    episode: dict,
    *,
    episode_dir: Path | None = None,
    source_fingerprint: dict | None = None,
) -> dict:
    tracks = episode.get("audio_tracks") or []
    by_number = {
        track.get("track_number"): track
        for track in tracks
        if track.get("track_number") is not None
    }
    by_stem = {
        Path(str(track.get("filename", ""))).stem: track
        for track in tracks
        if track.get("filename")
    }
    selected = []
    configured_mix = (episode.get("audio_mix") or {}).get("tracks") or []
    for entry in configured_mix:
        for stem in entry.get("stems") or [entry.get("stem")]:
            if stem in by_stem and float(entry.get("volume", 1)) > 0:
                selected.append(by_stem[stem])
    if not configured_mix:
        crop = episode.get("crop_config") or {}
        numbers = [
            speaker.get("track")
            for speaker in crop.get("speakers", [])
            if speaker.get("track")
        ]
        numbers += [
            ambient.get("track_number")
            for ambient in crop.get("ambient_tracks", []) or []
            if ambient.get("track_number")
        ]
        selected = [by_number[number] for number in numbers if number in by_number]

    selected_kinds = {track.get("track_type") for track in selected}
    uses_camera = "camera_channel" in selected_kinds
    uses_recorder = bool(selected_kinds - {"camera_channel"})
    if uses_recorder and not uses_camera:
        result = {
            "kind": "external_recorder",
            "uses_checked_source_audio": False,
            "basis": "The configured mix selects only external recorder tracks.",
            "input_paths": [
                str(track.get("dest_path") or track.get("filename"))
                for track in selected
            ],
        }
    elif uses_camera and uses_recorder:
        result = {
            "kind": "camera_and_external_recorder",
            "uses_checked_source_audio": True,
            "basis": "The configured mix includes camera-channel and external recorder tracks.",
            "input_paths": [
                str(track.get("dest_path") or track.get("filename"))
                for track in selected
            ],
        }
    elif uses_camera:
        result = {
            "kind": "camera_channel_extracts",
            "uses_checked_source_audio": True,
            "basis": "The configured mix selects channel extracts derived from source_merged.mp4.",
            "input_paths": [
                str(track.get("dest_path") or track.get("filename"))
                for track in selected
            ],
        }
    else:
        result = {
            "kind": "embedded_camera",
            "uses_checked_source_audio": True,
            "basis": "No usable track selection is configured; the mix falls back to embedded camera audio.",
            "input_paths": ["source_merged.mp4"],
        }

    audio_selection = audio_selection_settings(episode)
    input_fingerprints = []
    if episode_dir is not None:
        for value in result["input_paths"]:
            path = Path(value)
            if not path.is_absolute():
                path = episode_dir / path
            if not path.is_file():
                continue
            fingerprint = (
                source_fingerprint
                if source_fingerprint is not None
                and path.resolve() == (episode_dir / "source_merged.mp4").resolve()
                else media_fingerprint(path)
            )
            input_fingerprints.append(
                {"path": str(path.resolve()), "fingerprint": fingerprint["id"]}
            )
    repair_selection = (
        current_audio_selection(episode_dir, episode)
        if episode_dir is not None
        else None
    )
    if repair_selection is not None:
        result["kind"] = "selected_audio_repair"
        result["basis"] = (
            "A revision-bound grounded repair candidate is selected for rendering."
        )
        result["repair_selection"] = {
            key: repair_selection.get(key)
            for key in (
                "fingerprint",
                "status",
                "release_safe",
                "source_report_fingerprint",
                "repair_plan_fingerprint",
                "candidate_manifest_fingerprint",
                "repaired_findings",
                "unresolved_findings",
            )
        }
        result["selected_output"] = repair_selection["selected_output"]

    binding = {
        "kind": result["kind"],
        "uses_checked_source_audio": result["uses_checked_source_audio"],
        "inputs": input_fingerprints or result["input_paths"],
        "selection": audio_selection,
        "repair_selection": result.get("repair_selection"),
        "selected_output": (
            (result.get("selected_output") or {}).get("fingerprint", {}).get("id")
        ),
    }
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"), default=str)
    result["fingerprint"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    result["input_fingerprints"] = input_fingerprints

    if episode_dir is not None and repair_selection is None:
        selected_output = episode_dir / "work" / "audio_mix.wav"
        if selected_output.is_file():
            result["selected_output"] = {
                "path": str(selected_output.resolve()),
                "fingerprint": media_fingerprint(selected_output),
            }
    return result


def _report_fingerprint(
    source_fingerprint: str,
    transcript_fingerprint: str | None,
    edits: list[dict],
    mix_provenance: dict,
    settings: AudioQAConfig,
    findings: list[dict],
) -> str:
    semantic_findings = []
    for finding in findings:
        semantic = dict(finding)
        semantic["evidence"] = {
            key: value
            for key, value in (finding.get("evidence") or {}).items()
            if key != "transcript_excerpt"
        }
        semantic_findings.append(semantic)
    payload = json.dumps(
        {
            "schema": REPORT_SCHEMA,
            "detector_version": DETECTOR_VERSION,
            "source": source_fingerprint,
            "transcript": transcript_fingerprint,
            "edits": edits,
            "mix_provenance": {
                key: value
                for key, value in mix_provenance.items()
                if key != "selected_output"
            },
            "settings": asdict(settings),
            "findings": semantic_findings,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _load_episode_transcript(episode_dir: Path) -> tuple[Path | None, dict | None]:
    for name in ("diarized_transcript.json", "transcript.json"):
        path = episode_dir / name
        data = _read_json(path)
        if data:
            return path, data
    return None, None


def _read_json(path: Path) -> dict | None:
    try:
        with path.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _upper_quartile(values: np.ndarray) -> float:
    return float(np.percentile(values, 75)) if len(values) else -math.inf


def _round_finite(value: float) -> float | None:
    return round(value, 2) if math.isfinite(value) else None


def _switch_weight(start: float, end: float, fade: float, *, inverted: bool) -> str:
    down = f"({start:.6f}-t)/{fade:.6f}"
    up = f"(t-{end:.6f})/{fade:.6f}"
    normal = (
        f"if(lt(t,{start - fade:.6f}),1,"
        f"if(lt(t,{start:.6f}),{down},"
        f"if(lt(t,{end:.6f}),0,"
        f"if(lt(t,{end + fade:.6f}),{up},1))))"
    )
    return f"1-({normal})" if inverted else normal


def repair_envelope_weight(start: float, end: float, *, contained: bool) -> str:
    """Build a recovery envelope, optionally contained inside trusted speech."""
    duration = end - start
    if duration <= 0:
        raise ValueError("Audio repair interval must have positive duration")
    if not contained:
        fade = min(REPAIR_FADE_SECONDS, max(0.01, duration / 4))
        return _switch_weight(start, end, fade, inverted=True)

    remaining = duration - 2 * REPAIR_FRAME_GUARD_SECONDS
    if remaining < 0.02:
        raise ValueError("Transcript activity is too short for a contained repair")
    fade = min(REPAIR_FADE_SECONDS, remaining / 4)
    plateau_start = start + REPAIR_FRAME_GUARD_SECONDS + fade
    plateau_end = end - REPAIR_FRAME_GUARD_SECONDS - fade
    return _switch_weight(plateau_start, plateau_end, fade, inverted=True)


def _checked_ffmpeg(
    command: list[str], error: str = "Audio preview render failed"
) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{error}: {result.stderr[-1000:]}")
