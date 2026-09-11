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
from lib.audio_mix import CAMERA_AUDIO_TIMELINE_FILTER
from lib.ffprobe import probe as ffprobe

REPORT_SCHEMA = "cascade.audio-quality/v1"
DETECTOR_VERSION = "1.0"


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

    @property
    def duration(self) -> float:
        return len(self.rms_dbfs) * self.frame_seconds


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
    audio_stream = next(
        (
            stream
            for stream in media_probe.get("streams", [])
            if stream.get("codec_type") == "audio"
        ),
        None,
    )
    if audio_stream is None:
        raise ValueError(f"Source media has no audio stream: {source}")

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
        file_fingerprint(transcript_path) if transcript_path is not None else None
    )
    fingerprint = media_fingerprint(source, media_probe)
    decoder = resolve_ffmpeg(ffmpeg_bin)
    mix_provenance = _mix_provenance(episode)

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
    report["release_gate"] = release_gate(report)

    if report_path is not None:
        destination = Path(report_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(destination, report)
    return report


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
    )


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
    }
    return findings, analysis, speaker_channels


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

    unresolved = [
        finding
        for finding in report.get("findings", [])
        if finding.get("edited_time", {}).get("status") != "removed"
        and finding.get("resolution", {}).get("status", "unresolved")
        not in {"repaired", "accepted", "false_positive", "not_in_selected_mix"}
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
    return {
        "status": "pass",
        "safe": True,
        "blocking_finding_ids": [],
        "review_finding_ids": [],
        "reason": "No unresolved channel-continuity findings remain in the edited output.",
    }


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
    issue_start = float(source_time["start_seconds"]) - requested_start
    issue_end = float(source_time["end_seconds"]) - requested_start
    channel = int(finding["channel"])
    survivor = 1 - channel
    gain_db = min(
        12.0,
        max(0.0, float(finding["evidence"].get("estimated_recovery_gain_db", 0.0))),
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

    fade = min(0.08, max(0.01, (issue_end - issue_start) / 4))
    normal_weight = _switch_weight(issue_start, issue_end, fade, inverted=False)
    fallback_weight = _switch_weight(issue_start, issue_end, fade, inverted=True)
    filter_graph = (
        f"[0:a]{CAMERA_AUDIO_TIMELINE_FILTER},"
        "aformat=channel_layouts=stereo,asplit=2[m][s];"
        f"[m]pan=mono|c0=0.5*c0+0.5*c1,volume='{normal_weight}':eval=frame[n];"
        f"[s]pan=mono|c0=c{survivor},volume='{fallback_weight}*{10 ** (gain_db / 20):.8f}':"
        "eval=frame[r];[n][r]amix=inputs=2:duration=first:normalize=0,"
        "alimiter=limit=0.95,pan=stereo|c0=c0|c1=c0[out]"
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
    }


def media_fingerprint(path: str | Path, probe_data: dict | None = None) -> dict:
    """Fingerprint media with sparse content samples and stream metadata."""
    path = Path(path)
    stat = path.stat()
    probe_data = probe_data or ffprobe(path)
    audio_streams = [
        {
            key: stream.get(key)
            for key in (
                "index",
                "codec_name",
                "sample_rate",
                "channels",
                "channel_layout",
                "start_time",
                "duration",
                "time_base",
            )
        }
        for stream in probe_data.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    metadata = json.dumps(
        {
            "size_bytes": stat.st_size,
            "format_duration": probe_data.get("format", {}).get("duration"),
            "audio_streams": audio_streams,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(metadata)
    sample_size = 256 * 1024
    offsets = sorted(
        {
            0,
            max(0, stat.st_size // 2 - sample_size // 2),
            max(0, stat.st_size - sample_size),
        }
    )
    with path.open("rb") as media:
        for offset in offsets:
            media.seek(offset)
            digest.update(offset.to_bytes(8, "big"))
            digest.update(media.read(sample_size))
    return {
        "id": f"sha256:{digest.hexdigest()}",
        "method": "sha256-stream-metadata-plus-3x256KiB/v1",
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def file_fingerprint(path: str | Path) -> dict:
    """Return a complete content fingerprint for a bounded metadata file."""
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    stat = path.stat()
    return {
        "id": f"sha256:{digest.hexdigest()}",
        "method": "sha256-full/v1",
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
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

    expected_seconds = float(np.count_nonzero(expected_frames) * stats.frame_seconds)
    surviving_seconds = float(np.count_nonzero(surviving_frames) * stats.frame_seconds)
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
    interval_start = start * stats.frame_seconds
    interval_end = end * stats.frame_seconds
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
        "transcript_word_count": decision["transcript_word_count"],
        "transcript_excerpt": decision["transcript_excerpt"],
        "zero_sample_fraction": round(
            float(np.mean(stats.zero_fraction[start:end, channel])), 6
        ),
        "estimated_recovery_gain_db": round(decision["estimated_recovery_gain_db"], 2),
    }
    duration = round(end_seconds - start_seconds, 6)
    return {
        "id": finding_id,
        "fingerprint": finding_id,
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


def _mix_provenance(episode: dict) -> dict:
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
        return {
            "kind": "external_recorder",
            "uses_checked_source_audio": False,
            "basis": "The configured mix selects only external recorder tracks.",
            "input_paths": [
                str(track.get("dest_path") or track.get("filename"))
                for track in selected
            ],
        }
    if uses_camera and uses_recorder:
        return {
            "kind": "camera_and_external_recorder",
            "uses_checked_source_audio": True,
            "basis": "The configured mix includes camera-channel and external recorder tracks.",
            "input_paths": [
                str(track.get("dest_path") or track.get("filename"))
                for track in selected
            ],
        }
    if uses_camera:
        return {
            "kind": "camera_channel_extracts",
            "uses_checked_source_audio": True,
            "basis": "The configured mix selects channel extracts derived from source_merged.mp4.",
            "input_paths": [
                str(track.get("dest_path") or track.get("filename"))
                for track in selected
            ],
        }
    return {
        "kind": "embedded_camera",
        "uses_checked_source_audio": True,
        "basis": "No usable track selection is configured; the mix falls back to embedded camera audio.",
        "input_paths": ["source_merged.mp4"],
    }


def _report_fingerprint(
    source_fingerprint: str,
    transcript_fingerprint: str | None,
    edits: list[dict],
    mix_provenance: dict,
    settings: AudioQAConfig,
    findings: list[dict],
) -> str:
    payload = json.dumps(
        {
            "schema": REPORT_SCHEMA,
            "detector_version": DETECTOR_VERSION,
            "source": source_fingerprint,
            "transcript": transcript_fingerprint,
            "edits": edits,
            "mix_provenance": mix_provenance,
            "settings": asdict(settings),
            "findings": findings,
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


def _checked_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Audio preview render failed: {result.stderr[-1000:]}")
