"""Build source-clock transcripts from Deepgram audio responses.

Recorder episodes use one ASR channel per logical microphone track. Camera-only
recordings use diarization. Raw API responses remain immutable evidence while a
canonical transcript removes cross-microphone bleed for captions and agents.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np

from agents.base import BaseAgent
from lib.atomic_write import atomic_write_json
from lib.audio_mix import CAMERA_AUDIO_TIMELINE_FILTER, logical_track_groups
from lib.srt import fmt_timecode

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_AUDIO_CONTENT_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
}
CAMERA_AUDIO_CACHE_VERSION = "source-clock-v3"
TRANSCRIPT_CANONICAL_VERSION = "source-clock-v3"
_TRANSCRIPT_AUDIO_VERSION = "logical-tracks-v3"
_TRACK_WINDOW_VERSION = "source-track-window-v2"
_TRANSCRIPT_COVERAGE_VERSION = "source-clock-v1"
_SOURCE_ACTIVITY_FINGERPRINT_VERSION = "source-activity-v2"
_WORD_TIME_TOLERANCE = 0.12
MAX_TRANSCRIPT_REVIEW_SECONDS = 120.0


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _file_identity(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path.resolve()), "missing": True}
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _audio_content_type(path: Path) -> str:
    """Return the upload MIME from the actual container, not ASR options."""
    return _AUDIO_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _normalized_word(value: object) -> str:
    return re.sub(r"[^\w']+", "", str(value or "").casefold(), flags=re.UNICODE)


def remap_transcript_timestamps(data: dict, gaps: list[tuple[float, float]]) -> dict:
    """Map decoded-sample ASR times onto the source media timeline.

    Each gap is ``(collapsed_time, cumulative_source_offset)``. Deep copies
    are intentional so callers can back up and compare the original payload.
    """
    import copy

    result = copy.deepcopy(data)

    def mapped(value: float) -> float:
        offset = 0.0
        for threshold, cumulative_offset in gaps:
            if value >= threshold:
                offset = cumulative_offset
            else:
                break
        return value + offset

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"start", "end"} and isinstance(child, (int, float)):
                    value[key] = mapped(float(child))
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(result)
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and isinstance(
        metadata.get("duration"), (int, float)
    ):
        metadata["duration"] = mapped(float(metadata["duration"]))
        metadata["timeline_remap"] = [
            {"collapsed_time": threshold, "cumulative_offset": offset}
            for threshold, offset in gaps
        ]
    return result


@dataclass
class _SourceActivity:
    arrays: dict[int, np.ndarray]
    frame_seconds: float
    fingerprint: str
    preferred_channels: np.ndarray | None = None

    def level(self, channel: int, start: float, end: float) -> float | None:
        values = self.arrays.get(channel)
        if values is None or not len(values):
            return None
        first = max(0, int(start / self.frame_seconds))
        last = min(len(values), max(first + 1, int(np.ceil(end / self.frame_seconds))))
        if first >= len(values):
            return None
        finite = values[first:last]
        finite = finite[np.isfinite(finite)]
        return float(np.median(finite)) if len(finite) else None

    def preferred_channel(self, start: float, end: float) -> int | None:
        if self.preferred_channels is None or not len(self.preferred_channels):
            return None
        midpoint = max(0, int(((start + end) / 2) / self.frame_seconds))
        if midpoint >= len(self.preferred_channels):
            return None
        channel = int(self.preferred_channels[midpoint])
        return channel if channel >= 0 else None


class _DisjointSet:
    def __init__(self, size: int):
        self.parents = list(range(size))

    def find(self, item: int) -> int:
        while self.parents[item] != item:
            self.parents[item] = self.parents[self.parents[item]]
            item = self.parents[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


def _channel_map_from_episode(
    episode_dir: Path, episode: dict, historic_map: list[dict] | None = None
) -> list[dict]:
    """Resolve ASR channel order without sorting repeated logical track numbers."""
    speakers = episode.get("crop_config", {}).get("speakers", [])[:4]
    speakers_by_track = {
        speaker.get("track"): speaker
        for speaker in speakers
        if isinstance(speaker.get("track"), int)
        and not isinstance(speaker.get("track"), bool)
    }
    groups = logical_track_groups(
        episode_dir, episode, recorder_only=True, existing_only=False
    )

    ordered_tracks: list[int] = []
    if isinstance(historic_map, list):
        for entry in sorted(
            (entry for entry in historic_map if isinstance(entry, dict)),
            key=lambda entry: entry.get("index", 0),
        ):
            track = entry.get("logical_track", entry.get("track"))
            if track in speakers_by_track and track not in ordered_tracks:
                ordered_tracks.append(track)
    for speaker in speakers:
        track = speaker.get("track")
        if track in groups and track not in ordered_tracks:
            ordered_tracks.append(track)

    result = []
    for channel, track in enumerate(ordered_tracks):
        speaker = speakers_by_track[track]
        sources = groups.get(track, [])
        person = speaker.get("label") or f"Speaker {channel}"
        result.append(
            {
                "index": channel,
                "label": person,
                "person": person,
                "track": track,
                "logical_track": track,
                "source_files": [
                    source.get("filename") or Path(source.get("dest_path", "")).name
                    for source in sources
                ],
                "clock": "source",
            }
        )
    return result


def _transcription_audio_fingerprint(
    episode_dir: Path, episode: dict, channel_map: list[dict] | None
) -> str:
    sync = episode.get("audio_sync", {})
    duration = sync.get("video_duration") or episode.get("duration_seconds")
    if channel_map:
        groups = logical_track_groups(
            episode_dir, episode, recorder_only=True, existing_only=False
        )
        sources = [
            _file_identity(Path(track.get("dest_path", "")))
            for entry in channel_map
            for track in groups.get(entry["logical_track"], [])
        ]
        payload = {
            "version": _TRANSCRIPT_AUDIO_VERSION,
            "mode": "multichannel",
            "clock": "source",
            "logical_track_order": [entry["logical_track"] for entry in channel_map],
            "audio_sync": sync,
            "duration": duration,
            "sources": sources,
        }
    else:
        source = episode_dir / "source_merged.mp4"
        payload = {
            "version": CAMERA_AUDIO_CACHE_VERSION,
            "mode": "camera",
            "clock": "source",
            "timeline_filter": CAMERA_AUDIO_TIMELINE_FILTER,
            "audio_sync": sync,
            "duration": duration,
            "source": _file_identity(source),
        }
    return _stable_hash(payload)


def _asr_config_fingerprint(config: dict, multichannel: bool) -> str:
    transcription = config.get("transcription", {})
    return _stable_hash(
        {
            "provider": "deepgram",
            "model": transcription.get("model", "nova-3"),
            "language": transcription.get("language", "en"),
            "smart_format": transcription.get("smart_format", True),
            "keyterms": transcription.get("keyterms", []),
            "multichannel": multichannel,
            "utterances": True,
            "punctuate": True,
        }
    )


def _raw_is_multichannel(raw: dict) -> bool:
    channels = raw.get("metadata", {}).get("channels")
    if isinstance(channels, int):
        return channels > 1
    utterance_channels = {
        utterance.get("channel")
        for utterance in raw.get("results", {}).get("utterances", [])
        if isinstance(utterance.get("channel"), int)
        and not isinstance(utterance.get("channel"), bool)
    }
    return len(utterance_channels) > 1


def _load_corrections(episode_dir: Path) -> dict | None:
    try:
        corrections = json.loads(
            (episode_dir / "transcript_corrections.json").read_text()
        )
    except FileNotFoundError:
        return None
    if corrections.get("version") != 1 or corrections.get("clock") != "source":
        raise ValueError(
            "transcript_corrections.json must use version 1 and source clock"
        )
    if not isinstance(corrections.get("operations", []), list):
        raise TypeError("transcript correction operations must be a list")
    return corrections


def _validated_source_window(
    episode: dict, start: float, end: float
) -> tuple[float, float]:
    start = float(start)
    end = float(end)
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("Source window bounds must be finite")
    if start < 0 or end <= start:
        raise ValueError(
            "Source window must have non-negative start and positive duration"
        )
    if end - start > MAX_TRANSCRIPT_REVIEW_SECONDS:
        raise ValueError(
            f"Source window cannot exceed {MAX_TRANSCRIPT_REVIEW_SECONDS:g} seconds"
        )
    duration = episode.get("audio_sync", {}).get("video_duration") or episode.get(
        "duration_seconds"
    )
    if duration is not None and end > float(duration) + 1e-3:
        raise ValueError("Source window ends after the episode")
    return start, end


def export_logical_track_window(
    episode_dir: Path,
    episode: dict,
    logical_track: int | None,
    start: float,
    end: float,
    output_path: Path,
    *,
    source_kind: Literal["recorder", "camera"] = "recorder",
    channel: Literal["left", "right"] | None = None,
) -> dict:
    """Export one bounded recorder track or camera channel on the source clock.

    Repeated recorder sessions are concatenated before the episode sync trim,
    delay, and drift correction. Camera channels decode source_merged.mp4 with
    AAC gap correction before channel selection.
    """
    episode_dir = Path(episode_dir)
    output_path = Path(output_path)
    if output_path.suffix.casefold() != ".flac":
        raise ValueError("Track review audio must use a .flac destination")
    start, end = _validated_source_window(episode, start, end)
    if source_kind == "recorder":
        if not isinstance(logical_track, int) or isinstance(logical_track, bool):
            raise TypeError("logical_track must be an integer")
        if channel is not None:
            raise ValueError("channel is only valid for camera audio")
        groups = logical_track_groups(
            episode_dir, episode, recorder_only=True, existing_only=True
        )
        tracks = groups.get(logical_track, [])
        if not tracks:
            raise FileNotFoundError(f"Logical track {logical_track} is unavailable")
        paths = [Path(track["dest_path"]) for track in tracks]
        sync = episode.get("audio_sync", {})
        offset = float(sync.get("offset_seconds", 0))
        tempo = (
            float(sync.get("tempo_factor", 1.0))
            if float(sync.get("r_squared", 0)) > 0.5
            else 1.0
        )
        if not math.isfinite(offset) or not math.isfinite(tempo) or tempo <= 0:
            raise ValueError("Episode audio sync values are invalid")
        fingerprint_state = {
            "version": _TRACK_WINDOW_VERSION,
            "clock": "source",
            "logical_track": logical_track,
            "source_window": {"start": start, "end": end},
            "audio_sync": sync,
            "sources": [_file_identity(path) for path in paths],
            "codec": {"name": "flac", "sample_rate": 16000, "channels": 1},
        }
    elif source_kind == "camera":
        if logical_track is not None:
            raise ValueError("logical_track is only valid for recorder audio")
        if channel not in {"left", "right"}:
            raise ValueError("camera channel must be left or right")
        paths = [episode_dir / "source_merged.mp4"]
        if not paths[0].is_file():
            raise FileNotFoundError("Camera source is unavailable")
        sync = {}
        fingerprint_state = {
            "version": _TRACK_WINDOW_VERSION,
            "clock": "source",
            "source_kind": "camera",
            "channel": channel,
            "source_window": {"start": start, "end": end},
            "timeline_filter": CAMERA_AUDIO_TIMELINE_FILTER,
            "sources": [_file_identity(paths[0])],
            "codec": {"name": "flac", "sample_rate": 16000, "channels": 1},
        }
    else:
        raise ValueError(f"Unsupported audio source kind: {source_kind}")
    fingerprint = _stable_hash(fingerprint_state)
    manifest_path = output_path.with_suffix(f"{output_path.suffix}.json")
    try:
        cached = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        cached = {}
    if (
        cached.get("fingerprint") == fingerprint
        and output_path.is_file()
        and output_path.stat().st_size > 0
        and cached.get("audio", {}).get("sha256") == _file_sha256(output_path)
    ):
        return cached

    output_path.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    filters: list[str] = []
    if source_kind == "camera":
        inputs.extend(["-i", str(paths[0])])
        channel_index = 0 if channel == "left" else 1
        filters.append(
            f"[0:a]{CAMERA_AUDIO_TIMELINE_FILTER},pan=mono|c0=c{channel_index},"
            f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
            "aresample=16000[out]"
        )
    else:
        labels: list[str] = []
        for index, path in enumerate(paths):
            inputs.extend(["-i", str(path)])
            label = f"part{index}"
            filters.append(f"[{index}:a]aformat=channel_layouts=mono[{label}]")
            labels.append(f"[{label}]")
        if len(labels) > 1:
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[joined]")
        else:
            filters.append(f"{labels[0]}anull[joined]")
        chain = "[joined]asetpts=PTS-STARTPTS"
        if offset > 0:
            chain += f",atrim=start={offset:.8f},asetpts=PTS-STARTPTS"
        elif offset < 0:
            chain += f",adelay={round(abs(offset) * 1000)}"
        if abs(tempo - 1.0) > 1e-7:
            chain += f",atempo={tempo:.8f}"
        chain += (
            f",atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS"
            ",aresample=16000[out]"
        )
        filters.append(chain)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output_path.stem}.",
        suffix=".tmp.flac",
        dir=output_path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-c:a",
        "flac",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(temporary),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"Logical track export failed: {completed.stderr[-500:]}"
            )
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    stat = output_path.stat()
    manifest = {
        "version": _TRACK_WINDOW_VERSION,
        "clock": "source",
        "fingerprint": fingerprint,
        "source": {
            "kind": source_kind,
            **(
                {"logical_track": logical_track}
                if source_kind == "recorder"
                else {"channel": channel}
            ),
        },
        **({"logical_track": logical_track} if source_kind == "recorder" else {}),
        "source_window": {
            "start": start,
            "end": end,
            "duration_seconds": end - start,
        },
        "audio_sync": sync,
        "source_files": [_file_identity(path) for path in paths],
        "audio": {
            "path": str(output_path.resolve()),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": _file_sha256(output_path),
            "sample_rate": 16000,
            "channels": 1,
            "codec": "flac",
        },
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def _shift_transcript_result(
    raw: dict, source_start: float, logical_track: int
) -> dict:
    shifted = deepcopy(raw)

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"start", "end"} and isinstance(child, (int, float)):
                    value[key] = float(child) + source_start
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(shifted.get("results", {}))
    metadata = shifted.setdefault("metadata", {})
    metadata["clock"] = "source"
    metadata["source_window_start"] = source_start
    metadata["logical_track"] = logical_track
    return shifted


def transcribe_logical_track_window(
    episode_dir: Path,
    config: dict,
    logical_track: int,
    start: float,
    end: float,
    output_dir: Path,
) -> dict:
    """Run cached review-only ASR for one bounded logical-track window."""
    episode_dir = Path(episode_dir)
    output_dir = Path(output_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    start, end = _validated_source_window(episode, start, end)
    stem = f"track_{logical_track}_{start:.3f}_{end:.3f}"
    audio_path = output_dir / f"{stem}.flac"
    audio = export_logical_track_window(
        episode_dir, episode, logical_track, start, end, audio_path
    )
    fingerprint = _stable_hash(
        {
            "version": _TRACK_WINDOW_VERSION,
            "audio_fingerprint": audio["fingerprint"],
            "asr_config_fingerprint": _asr_config_fingerprint(config, False),
        }
    )
    raw_path = output_dir / f"{stem}.deepgram.json"
    source_path = output_dir / f"{stem}.source.json"
    manifest_path = output_dir / f"{stem}.evidence.json"
    try:
        cached = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        cached = {}
    if (
        cached.get("fingerprint") == fingerprint
        and raw_path.is_file()
        and source_path.is_file()
        and cached.get("raw_response", {}).get("sha256") == _file_sha256(raw_path)
        and cached.get("source_response", {}).get("sha256") == _file_sha256(source_path)
    ):
        return cached

    agent = TranscribeAgent(episode_dir, config)
    raw = agent._request_deepgram(audio_path, multichannel=False)
    shifted = _shift_transcript_result(raw, start, logical_track)
    atomic_write_json(raw_path, raw)
    atomic_write_json(source_path, shifted)
    manifest = {
        "version": _TRACK_WINDOW_VERSION,
        "clock": "source",
        "fingerprint": fingerprint,
        "logical_track": logical_track,
        "source_window": {
            "start": start,
            "end": end,
            "duration_seconds": end - start,
        },
        "audio": audio,
        "provider": "deepgram",
        "model": config.get("transcription", {}).get("model", "nova-3"),
        "review_only": True,
        "applied_to_canonical": False,
        "raw_response": {
            "path": str(raw_path.resolve()),
            "sha256": _file_sha256(raw_path),
        },
        "source_response": {
            "path": str(source_path.resolve()),
            "sha256": _file_sha256(source_path),
        },
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def _speaker_logical_tracks(transcript: dict) -> dict[int, int]:
    result = {}
    for mapping in transcript.get("speaker_map", []):
        speaker = mapping.get("index")
        track = mapping.get("logical_track")
        if (
            isinstance(speaker, int)
            and not isinstance(speaker, bool)
            and isinstance(track, int)
            and not isinstance(track, bool)
        ):
            result[speaker] = track
    return result


def _speaker_map_identity(speaker_map: list[dict] | None) -> list[dict]:
    """Compare who a diarization ID names without unstable overlap scores."""
    return [
        {
            key: mapping.get(key)
            for key in (
                "index",
                "person",
                "logical_track",
                "camera_channel",
            )
        }
        for mapping in (speaker_map or [])
    ]


def analyze_transcript_coverage(
    episode_dir: Path,
    episode: dict,
    config: dict,
    output_path: Path | None = None,
) -> dict:
    """Find logical-mic speech activity that has no canonical transcript words."""
    episode_dir = Path(episode_dir)
    transcript = json.loads((episode_dir / "diarized_transcript.json").read_text())
    provenance = json.loads((episode_dir / "transcript_provenance.json").read_text())
    segments = json.loads((episode_dir / "segments.json").read_text())
    rms_metadata = json.loads((episode_dir / "work/rms_meta.json").read_text())
    if (
        transcript.get("clock") != "source"
        or provenance.get("clock") != "source"
        or segments.get("clock") != "source"
        or rms_metadata.get("clock") != "source"
        or rms_metadata.get("fingerprint") != segments.get("fingerprint")
    ):
        raise RuntimeError("Transcript or microphone activity is stale")
    frame_seconds = float(rms_metadata.get("frame_seconds", 0))
    if not math.isfinite(frame_seconds) or frame_seconds <= 0:
        raise RuntimeError("Microphone activity frame size is invalid")
    speaker_tracks = _speaker_logical_tracks(transcript)
    people_by_track = {}
    for mapping in transcript.get("speaker_map", []):
        track = mapping.get("logical_track")
        if isinstance(track, int) and mapping.get("person"):
            people_by_track.setdefault(track, set()).add(mapping["person"])
    speaker_index_by_track = {}
    for mapping in segments.get("track_mapping", []):
        speaker = str(mapping.get("speaker", ""))
        track = mapping.get("logical_track")
        if speaker.startswith("speaker_") and isinstance(track, int):
            speaker_index_by_track[track] = int(speaker.rsplit("_", 1)[1])
    words_by_track: dict[int, list[dict]] = {}
    for utterance in transcript.get("utterances", []):
        for word in utterance.get("words", []):
            track = speaker_tracks.get(word.get("speaker", utterance.get("speaker")))
            if track is not None:
                words_by_track.setdefault(track, []).append(word)

    processing = config.get("processing", {})
    margin = float(
        episode.get("speaker_cut_config", {}).get(
            "speech_db_margin", processing.get("speech_db_margin", 6)
        )
    )
    padding_seconds = 0.4
    bridge_seconds = 0.8
    minimum_active_seconds = 1.2
    minimum_gap_seconds = 3.0
    findings = []
    track_summaries = []
    array_identities = []
    for track, speaker_index in sorted(speaker_index_by_track.items()):
        rms_path = episode_dir / "work" / f"speaker_{speaker_index}_rms_db.npy"
        try:
            values = np.load(rms_path, mmap_mode="r")
        except (OSError, ValueError):
            continue
        array_identities.append(_file_identity(rms_path))
        finite_values = np.asarray(values[np.isfinite(values)])
        if not len(finite_values):
            continue
        threshold = float(np.percentile(finite_values, 10) + margin)
        owned = np.zeros(len(values), dtype=bool)
        for segment in segments.get("segments", []):
            if segment.get("speaker") != f"speaker_{speaker_index}":
                continue
            first = max(0, int(float(segment.get("start", 0)) / frame_seconds))
            last = min(
                len(owned),
                math.ceil(float(segment.get("end", 0)) / frame_seconds),
            )
            owned[first:last] = True
        active = np.asarray((values > threshold) & owned, dtype=bool)
        covered = np.zeros(len(active), dtype=bool)
        track_words = sorted(
            words_by_track.get(track, []), key=lambda word: float(word.get("start", 0))
        )
        for word in track_words:
            start = max(0.0, float(word.get("start", 0)) - padding_seconds)
            end = max(start, float(word.get("end", start)) + padding_seconds)
            first = max(0, int(start / frame_seconds))
            last = min(len(covered), math.ceil(end / frame_seconds))
            covered[first:last] = True
        missing_indices = np.flatnonzero(active & ~covered)
        groups: list[list[int]] = []
        bridge_frames = max(1, round(bridge_seconds / frame_seconds))
        for frame in missing_indices:
            if groups and frame - groups[-1][-1] <= bridge_frames:
                groups[-1].append(int(frame))
            else:
                groups.append([int(frame)])
        track_count = 0
        for group in groups:
            active_seconds = len(group) * frame_seconds
            span_seconds = (group[-1] - group[0] + 1) * frame_seconds
            if (
                active_seconds < minimum_active_seconds
                or span_seconds < minimum_gap_seconds
            ):
                continue
            first = group[0]
            last = group[-1]
            start = first * frame_seconds
            end = (last + 1) * frame_seconds
            levels = np.asarray(values[group], dtype=float)
            finding_id = f"track_{track}_{start:.3f}_{end:.3f}"
            findings.append(
                {
                    "id": finding_id,
                    "kind": "untranscribed_mic_activity",
                    "clock": "source",
                    "logical_track": track,
                    "people": sorted(people_by_track.get(track, [])),
                    "source_window": {
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "duration_seconds": round(end - start, 3),
                    },
                    "review_window": {
                        "start": round(max(0.0, start - 2.0), 3),
                        "end": round(min(len(values) * frame_seconds, end + 2.0), 3),
                    },
                    "evidence": {
                        "active_seconds": round(active_seconds, 3),
                        "activity_fraction": round(
                            active_seconds / max(end - start, frame_seconds), 4
                        ),
                        "threshold_db": round(threshold, 2),
                        "median_db": round(float(np.median(levels)), 2),
                        "peak_db": round(float(np.max(levels)), 2),
                        "speaker_owned_frames_only": True,
                    },
                    "status": "review_required",
                }
            )
            track_count += 1
        track_summaries.append(
            {
                "logical_track": track,
                "people": sorted(people_by_track.get(track, [])),
                "word_count": len(track_words),
                "finding_count": track_count,
                "activity_threshold_db": round(threshold, 2),
            }
        )
    report = {
        "version": _TRANSCRIPT_COVERAGE_VERSION,
        "clock": "source",
        "status": "review_required" if findings else "pass",
        "fingerprint": _stable_hash(
            {
                "version": _TRANSCRIPT_COVERAGE_VERSION,
                "transcript_sha256": _file_sha256(
                    episode_dir / "diarized_transcript.json"
                ),
                "transcript_provenance": provenance,
                "speaker_cut_fingerprint": segments.get("fingerprint"),
                "rms_metadata": rms_metadata,
                "rms_arrays": array_identities,
                "settings": {
                    "speech_db_margin": margin,
                    "word_padding_seconds": padding_seconds,
                    "bridge_seconds": bridge_seconds,
                    "minimum_active_seconds": minimum_active_seconds,
                    "minimum_gap_seconds": minimum_gap_seconds,
                    "speaker_owned_frames_only": True,
                },
            }
        ),
        "summary": {
            "finding_count": len(findings),
            "tracks": track_summaries,
        },
        "findings": findings,
    }
    destination = output_path or episode_dir / "qa/transcript-coverage.json"
    atomic_write_json(Path(destination), report)
    return report


def repair_existing_transcript(episode_dir: Path, config: dict) -> dict:
    """Canonicalize a stored ASR response without uploading audio again.

    This is the API-friendly repair path for historical episodes. It preserves
    ``transcript.json`` byte-for-byte and atomically refreshes only derived
    transcript and caption artifacts.
    """
    agent = TranscribeAgent(Path(episode_dir), config)
    episode = agent.load_json("episode.json")
    raw = agent.load_json("transcript.json")
    multichannel = _raw_is_multichannel(raw)
    channel_map = agent._resolve_channel_map(episode) if multichannel else None
    input_fingerprint = _transcription_audio_fingerprint(
        agent.episode_dir, episode, channel_map
    )
    return agent._save_canonical_outputs(
        raw,
        multichannel,
        channel_map,
        input_fingerprint,
        _asr_config_fingerprint(config, multichannel),
        reused_raw=True,
    )


def current_diarized_transcript(
    episode_dir: Path, episode: dict, config: dict
) -> dict | None:
    """Return the canonical transcript only when all source provenance is current."""
    agent = TranscribeAgent(Path(episode_dir), config)
    try:
        raw = agent.load_json("transcript.json")
        diarized = agent.load_json("diarized_transcript.json")
        provenance = agent.load_json("transcript_provenance.json")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    multichannel = _raw_is_multichannel(raw)
    channel_map = agent._resolve_channel_map(episode) if multichannel else None
    activity = agent._load_source_activity(channel_map)
    if multichannel and activity is None:
        return None
    expected_input = _transcription_audio_fingerprint(
        agent.episode_dir, episode, channel_map
    )
    expected_config = _asr_config_fingerprint(config, multichannel)
    try:
        raw_hash = _file_sha256(agent.episode_dir / "transcript.json")
    except OSError:
        return None
    corrections = _load_corrections(agent.episode_dir)
    corrections_fingerprint = _stable_hash(corrections) if corrections else None
    speaker_map = (
        channel_map if multichannel else agent._infer_diarized_speaker_map(raw)
    )
    if (
        diarized.get("clock") != "source"
        or diarized.get("algorithm_version") != TRANSCRIPT_CANONICAL_VERSION
        or provenance.get("version") != TRANSCRIPT_CANONICAL_VERSION
        or provenance.get("clock") != "source"
        or provenance.get("asr_input_fingerprint") != expected_input
        or provenance.get("asr_config_fingerprint") != expected_config
        or provenance.get("raw_transcript_sha256") != raw_hash
        or provenance.get("channel_map") != (channel_map or [])
        or _speaker_map_identity(provenance.get("speaker_map"))
        != _speaker_map_identity(speaker_map)
        or provenance.get("corrections_fingerprint") != corrections_fingerprint
    ):
        return None
    expected_activity = activity.fingerprint if activity else None
    if provenance.get(
        "canonical_activity_fingerprint"
    ) != expected_activity and not _canonical_content_matches_activity(
        agent,
        raw,
        diarized,
        multichannel=multichannel,
        speaker_map=speaker_map,
        activity=activity,
        corrections=corrections,
    ):
        return None
    return diarized


def _canonical_content_matches_activity(
    agent: TranscribeAgent,
    raw: dict,
    stored: dict,
    *,
    multichannel: bool,
    speaker_map: list[dict] | None,
    activity: _SourceActivity | None,
    corrections: dict | None,
) -> bool:
    """Verify a legacy activity fingerprint by rebuilding canonical content.

    Older provenance included speaker-cut wrapper fingerprints. A picture-only
    crop edit could therefore make a byte-identical activity cache look stale.
    Rebuilding is a bounded local compatibility check: every public word,
    speaker decision, and correction must still match before the stored
    transcript is accepted.
    """
    try:
        rebuilt = agent._build_diarized_transcript(
            raw,
            multichannel=multichannel,
            channel_map=speaker_map,
            activity=activity,
        )
        if corrections:
            rebuilt, _ = agent._apply_transcript_corrections(rebuilt, corrections)
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False
    existing = deepcopy(stored)
    existing.pop("provenance", None)
    for transcript in (rebuilt, existing):
        canonicalization = transcript.get("canonicalization")
        if isinstance(canonicalization, dict):
            canonicalization["activity_fingerprint"] = None
    return rebuilt == existing


class TranscribeAgent(BaseAgent):
    name = "transcribe"

    def execute(self) -> dict:
        work_dir = self.episode_dir / "work"
        work_dir.mkdir(exist_ok=True)
        episode = self.load_json_safe("episode.json")
        channel_map = self._resolve_channel_map(episode)
        multichannel = self._can_prepare_multichannel(episode, channel_map)
        if not multichannel:
            channel_map = None

        input_fingerprint = _transcription_audio_fingerprint(
            self.episode_dir, episode, channel_map
        )
        config_fingerprint = _asr_config_fingerprint(self.config, multichannel)
        raw_path = self.episode_dir / "transcript.json"
        provenance = self.load_json_safe("transcript_provenance.json")
        force = bool(self.config.get("transcription", {}).get("force", False))
        reuse_raw = (
            not force
            and raw_path.is_file()
            and provenance.get("asr_input_fingerprint") == input_fingerprint
            and provenance.get("asr_config_fingerprint") == config_fingerprint
            and provenance.get("clock") == "source"
        )

        audio_size_mb = 0.0
        if reuse_raw:
            raw = self.load_json("transcript.json")
            self.logger.info("Reusing source-current Deepgram response")
        else:
            if multichannel:
                audio_path, channel_map = self._prepare_multichannel_audio(
                    episode, channel_map=channel_map
                )
                if audio_path is None:
                    self.logger.warning(
                        "Multichannel prep failed, falling back to camera audio"
                    )
                    multichannel = False
                    channel_map = None
                    input_fingerprint = _transcription_audio_fingerprint(
                        self.episode_dir, episode, None
                    )
                    config_fingerprint = _asr_config_fingerprint(self.config, False)
                    audio_path = self._prepare_camera_audio(episode, input_fingerprint)
            else:
                audio_path = self._prepare_camera_audio(episode, input_fingerprint)
            audio_size_mb = audio_path.stat().st_size / 1e6
            raw = self._request_deepgram(audio_path, multichannel)
            self.save_json("transcript.json", raw)

        result = self._save_canonical_outputs(
            raw,
            multichannel,
            channel_map,
            input_fingerprint,
            config_fingerprint,
            reused_raw=reuse_raw,
        )
        result["audio_size_mb"] = round(audio_size_mb, 1)
        result["mode"] = "multichannel" if multichannel else "diarized"
        result["reused_raw"] = reuse_raw
        return result

    @staticmethod
    def _apply_transcript_corrections(
        diarized: dict, corrections: dict
    ) -> tuple[dict, dict]:
        words = []
        for utterance_index, utterance in enumerate(diarized.get("utterances", [])):
            source = utterance.get("source_utterance", utterance_index)
            for word in utterance.get("words", []):
                copied = dict(word)
                copied["_group"] = f"source_{source}"
                words.append(copied)

        applied = []
        for operation_index, operation in enumerate(corrections.get("operations", [])):
            correction_id = str(
                operation.get("id") or f"correction_{operation_index:03d}"
            )
            kind = operation.get("op")
            if kind == "replace_word":
                word_id = operation.get("word_id")
                matching = [word for word in words if word.get("id") == word_id]
                if len(matching) != 1:
                    raise ValueError(
                        f"Correction {correction_id} expected one word {word_id}, "
                        f"found {len(matching)}"
                    )
                word = matching[0]
                original = {
                    key: word.get(key)
                    for key in (
                        "word",
                        "punctuated_word",
                        "speaker",
                        "confidence",
                    )
                }
                for key in ("word", "punctuated_word", "speaker", "confidence"):
                    if key in operation:
                        word[key] = operation[key]
                word.setdefault("alternatives", []).append(
                    {**original, "reason": "pre_correction"}
                )
                word["corrected"] = True
                word["correction_id"] = correction_id
                word["correction_reason"] = operation.get("reason")
                word["suspect"] = bool(operation.get("suspect", False))
                word["suspect_reasons"] = operation.get("suspect_reasons", [])
                applied.append(correction_id)
                continue
            if kind != "replace_range":
                raise ValueError(f"Unsupported transcript correction op: {kind}")
            start = float(operation.get("start", -1))
            end = float(operation.get("end", -1))
            replacement = operation.get("words")
            if start < 0 or end <= start or not isinstance(replacement, list):
                raise ValueError(f"Invalid range correction {correction_id}")
            replace_speakers = operation.get("replace_speakers")
            if replace_speakers is not None and (
                not isinstance(replace_speakers, list)
                or any(
                    not isinstance(speaker, int) or isinstance(speaker, bool)
                    for speaker in replace_speakers
                )
            ):
                raise ValueError(
                    f"Correction {correction_id} has invalid replace_speakers"
                )
            replace_speakers = (
                set(replace_speakers) if replace_speakers is not None else None
            )
            words = [
                word
                for word in words
                if not (
                    start <= (word["start"] + word["end"]) / 2 < end
                    and (
                        replace_speakers is None
                        or word.get("speaker") in replace_speakers
                    )
                )
            ]
            for replacement_index, item in enumerate(replacement):
                word_start = float(item.get("start", -1))
                word_end = float(item.get("end", -1))
                speaker = item.get("speaker")
                if (
                    word_start < start
                    or word_end > end
                    or word_end < word_start
                    or not isinstance(speaker, int)
                ):
                    raise ValueError(
                        f"Correction {correction_id} has an invalid replacement word"
                    )
                value = str(item.get("word", "")).strip()
                if not value:
                    raise ValueError(
                        f"Correction {correction_id} has an empty replacement word"
                    )
                words.append(
                    {
                        "id": f"{correction_id}_{replacement_index:03d}",
                        "word": value,
                        "punctuated_word": item.get("punctuated_word", value),
                        "start": word_start,
                        "end": word_end,
                        "confidence": float(item.get("confidence", 0)),
                        "speaker": speaker,
                        "asr_channel": item.get("asr_channel"),
                        "alternatives": item.get("alternatives", []),
                        "suspect": bool(item.get("suspect", False)),
                        "suspect_reasons": item.get("suspect_reasons", []),
                        "corrected": True,
                        "correction_id": correction_id,
                        "correction_reason": operation.get("reason"),
                        "_group": correction_id,
                    }
                )
            applied.append(correction_id)

        words.sort(
            key=lambda word: (
                float(word.get("start", 0)),
                float(word.get("end", 0)),
                int(word.get("speaker", 0)),
            )
        )
        groups: list[list[dict]] = []
        for word in words:
            if (
                groups
                and groups[-1][-1]["_group"] == word["_group"]
                and groups[-1][-1]["speaker"] == word["speaker"]
            ):
                groups[-1].append(word)
            else:
                groups.append([word])
        utterances = []
        for group in groups:
            public_words = []
            for word in group:
                public_word = dict(word)
                public_word.pop("_group", None)
                public_words.append(public_word)
            utterances.append(
                {
                    "speaker": public_words[0]["speaker"],
                    "start": public_words[0]["start"],
                    "end": public_words[-1]["end"],
                    "text": " ".join(word["punctuated_word"] for word in public_words),
                    "confidence": sum(
                        word.get("confidence", 0) for word in public_words
                    )
                    / len(public_words),
                    "words": public_words,
                    "source_utterance": group[0]["_group"],
                }
            )
        diarized = dict(diarized)
        diarized["utterances"] = utterances
        canonicalization = dict(diarized.get("canonicalization", {}))
        canonicalization.update(
            {
                "output_words": len(words),
                "output_utterances": len(utterances),
                "suspect_words": sum(word.get("suspect", False) for word in words),
                "corrections_applied": applied,
                "corrected_words": sum(word.get("corrected", False) for word in words),
            }
        )
        diarized["canonicalization"] = canonicalization
        return diarized, {"applied": applied, "word_count": len(words)}

    def _resolve_channel_map(self, episode: dict) -> list[dict]:
        historic = self.load_json_safe("diarized_transcript.json").get("speaker_map")
        return _channel_map_from_episode(self.episode_dir, episode, historic)

    def _infer_diarized_speaker_map(self, raw: dict) -> list[dict]:
        """Match mono diarization IDs to source-clock microphone decisions."""
        try:
            segments = self.load_json("segments.json")
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        if segments.get("clock") != "source":
            return []
        sources = [
            mapping
            for mapping in segments.get("track_mapping", [])
            if str(mapping.get("speaker", "")).startswith("speaker_")
        ]
        diarized_ids = sorted(
            {
                utterance.get("speaker")
                for utterance in raw.get("results", {}).get("utterances", [])
                if isinstance(utterance.get("speaker"), int)
                and not isinstance(utterance.get("speaker"), bool)
            }
        )
        if not diarized_ids or not sources:
            return []
        scores = {diarized_id: [0.0] * len(sources) for diarized_id in diarized_ids}
        source_index = {
            mapping["speaker"]: index for index, mapping in enumerate(sources)
        }
        source_segments = [
            segment
            for segment in segments.get("segments", [])
            if segment.get("speaker") in source_index
        ]
        for utterance in raw.get("results", {}).get("utterances", []):
            diarized_id = utterance.get("speaker")
            if diarized_id not in scores:
                continue
            start = float(utterance.get("start", 0))
            end = float(utterance.get("end", start))
            for segment in source_segments:
                overlap = min(end, float(segment["end"])) - max(
                    start, float(segment["start"])
                )
                if overlap > 0:
                    scores[diarized_id][source_index[segment["speaker"]]] += overlap
        if len(diarized_ids) <= len(sources):
            best_assignment = max(
                itertools.permutations(range(len(sources)), len(diarized_ids)),
                key=lambda assignment: sum(
                    scores[diarized_id][source]
                    for diarized_id, source in zip(diarized_ids, assignment)
                ),
            )
            mapping_method = "diarization_segment_overlap"
        else:
            best_assignment = tuple(
                max(range(len(sources)), key=lambda source: scores[diarized_id][source])
                for diarized_id in diarized_ids
            )
            mapping_method = "diarization_segment_overlap_many_to_one"
        collision_counts = {
            source: best_assignment.count(source) for source in set(best_assignment)
        }
        result = []
        for diarized_id, assigned_source in zip(diarized_ids, best_assignment):
            mapping = sources[assigned_source]
            row = scores[diarized_id]
            total = sum(row)
            result.append(
                {
                    "index": diarized_id,
                    "label": mapping.get("person") or f"Speaker {diarized_id}",
                    "person": mapping.get("person"),
                    "logical_track": mapping.get("logical_track"),
                    "camera_channel": mapping.get("camera_channel"),
                    "clock": "source",
                    "mapping_method": mapping_method,
                    "mapping_collision": collision_counts[assigned_source] > 1,
                    "mapping_confidence": (
                        round(row[assigned_source] / total, 4) if total else 0.0
                    ),
                }
            )
        return result

    def _can_prepare_multichannel(self, episode: dict, channel_map: list[dict]) -> bool:
        if not channel_map:
            return False
        groups = logical_track_groups(
            self.episode_dir, episode, recorder_only=True, existing_only=True
        )
        return all(entry["logical_track"] in groups for entry in channel_map)

    def _prepare_camera_audio(self, episode: dict, fingerprint: str) -> Path:
        work_dir = self.episode_dir / "work"
        output = work_dir / "audio.m4a"
        metadata_path = work_dir / "audio.m4a.fingerprint.json"
        metadata = {}
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        if (
            output.is_file()
            and output.stat().st_size > 0
            and metadata.get("fingerprint") == fingerprint
        ):
            return output

        source = self.episode_dir / "source_merged.mp4"
        temporary = work_dir / "audio.tmp.m4a"
        self.logger.info("Extracting source-clock camera audio...")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-af",
                    CAMERA_AUDIO_TIMELINE_FILTER,
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        atomic_write_json(
            metadata_path,
            {
                "version": CAMERA_AUDIO_CACHE_VERSION,
                "clock": "source",
                "fingerprint": fingerprint,
            },
        )
        return output

    def _prepare_multichannel_audio(
        self, episode: dict, channel_map: list[dict] | None = None
    ) -> tuple[Path | None, list[dict] | None]:
        """Join every recorder session, align it to source time, then amerge."""
        channel_map = channel_map or self._resolve_channel_map(episode)
        if not self._can_prepare_multichannel(episode, channel_map):
            return None, None
        groups = logical_track_groups(
            self.episode_dir, episode, recorder_only=True, existing_only=True
        )
        fingerprint = _transcription_audio_fingerprint(
            self.episode_dir, episode, channel_map
        )
        work_dir = self.episode_dir / "work"
        output = work_dir / "transcript_audio.flac"
        metadata_path = work_dir / "transcript_audio.fingerprint.json"
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}
        if (
            output.is_file()
            and output.stat().st_size > 0
            and metadata.get("fingerprint") == fingerprint
        ):
            return output, channel_map

        sync = episode.get("audio_sync", {})
        offset = float(sync.get("offset_seconds", 0))
        tempo = (
            float(sync.get("tempo_factor", 1.0))
            if float(sync.get("r_squared", 0)) > 0.5
            else 1.0
        )
        duration = sync.get("video_duration") or episode.get("duration_seconds")
        inputs: list[str] = []
        filters: list[str] = []
        aligned_labels: list[str] = []
        input_index = 0
        for channel, entry in enumerate(channel_map):
            session_labels = []
            for session, track in enumerate(groups[entry["logical_track"]]):
                inputs.extend(["-i", str(Path(track["dest_path"]))])
                label = f"c{channel}_{session}"
                filters.append(
                    f"[{input_index}:a]aformat=channel_layouts=mono[{label}]"
                )
                session_labels.append(f"[{label}]")
                input_index += 1
            joined = f"joined{channel}"
            if len(session_labels) > 1:
                filters.append(
                    f"{''.join(session_labels)}concat=n={len(session_labels)}:v=0:a=1[{joined}]"
                )
            else:
                filters.append(f"{session_labels[0]}anull[{joined}]")
            chain = f"[{joined}]asetpts=PTS-STARTPTS"
            if offset > 0:
                chain += f",atrim=start={offset},asetpts=PTS-STARTPTS"
            elif offset < 0:
                chain += f",adelay={round(abs(offset) * 1000)}"
            if abs(tempo - 1.0) > 1e-7:
                chain += f",atempo={tempo:.8f}"
            if duration:
                chain += f",apad,atrim=duration={float(duration):.6f}"
            aligned = f"aligned{channel}"
            filters.append(f"{chain}[{aligned}]")
            aligned_labels.append(f"[{aligned}]")

        filters.append(
            f"{''.join(aligned_labels)}amerge=inputs={len(channel_map)}[out]"
        )
        temporary = work_dir / "transcript_audio.tmp.flac"
        cmd = [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-c:a",
            "flac",
            "-ar",
            "16000",
            str(temporary),
        ]
        self.logger.info(
            "Building %d source-clock ASR channels from logical tracks",
            len(channel_map),
        )
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                self.logger.error(
                    "Multichannel merge failed: %s", completed.stderr[-500:]
                )
                return None, None
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        atomic_write_json(
            metadata_path,
            {
                "version": _TRANSCRIPT_AUDIO_VERSION,
                "clock": "source",
                "fingerprint": fingerprint,
                "channel_map": channel_map,
            },
        )
        return output, channel_map

    def _request_deepgram(self, audio_path: Path, multichannel: bool) -> dict:
        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set in environment")
        transcription = self.config.get("transcription", {})
        params = {
            "model": transcription.get("model", "nova-3"),
            "language": transcription.get("language", "en"),
            "utterances": "true",
            "smart_format": str(transcription.get("smart_format", True)).lower(),
            "punctuate": "true",
        }
        params["multichannel" if multichannel else "diarize"] = "true"
        url = DEEPGRAM_URL
        keyterms = transcription.get("keyterms", [])
        if keyterms:
            from urllib.parse import urlencode

            url = f"{DEEPGRAM_URL}?{urlencode(params)}&{urlencode([('keyterm', term) for term in keyterms])}"
            params = None
        self.logger.info("Sending source-clock audio to Deepgram Nova-3...")
        with audio_path.open("rb") as audio:
            response = httpx.post(
                url,
                params=params,
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": _audio_content_type(audio_path),
                },
                content=audio,
                timeout=600.0,
            )
        response.raise_for_status()
        return response.json()

    def _load_source_activity(
        self, channel_map: list[dict] | None
    ) -> _SourceActivity | None:
        if not channel_map:
            return None
        try:
            segments = self.load_json("segments.json")
            metadata = self.load_json("work/rms_meta.json")
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if (
            segments.get("clock") != "source"
            or metadata.get("clock") != "source"
            or metadata.get("fingerprint") != segments.get("fingerprint")
            or not isinstance(metadata.get("frame_seconds"), (int, float))
            or metadata["frame_seconds"] <= 0
        ):
            return None
        speaker_index_by_track = {}
        for mapping in segments.get("track_mapping", []):
            speaker = str(mapping.get("speaker", ""))
            track = mapping.get("logical_track")
            if speaker.startswith("speaker_") and isinstance(track, int):
                try:
                    speaker_index_by_track[track] = int(speaker.rsplit("_", 1)[1])
                except ValueError:
                    continue
        arrays = {}
        identities = []
        channel_by_speaker_index = {}
        for entry in channel_map:
            channel = entry["index"]
            speaker_index = speaker_index_by_track.get(entry["logical_track"])
            if speaker_index is None:
                continue
            path = self.episode_dir / "work" / f"speaker_{speaker_index}_rms_db.npy"
            try:
                arrays[channel] = np.load(path, mmap_mode="r")
            except (OSError, ValueError):
                continue
            identities.append(
                {
                    "channel": channel,
                    "logical_track": entry["logical_track"],
                    "size": path.stat().st_size,
                    "sha256": _file_sha256(path),
                }
            )
            channel_by_speaker_index[speaker_index] = channel
        if not arrays:
            return None
        frame_seconds = float(metadata["frame_seconds"])
        preferred_channels = np.full(
            max(len(array) for array in arrays.values()), -1, dtype=np.int16
        )
        alignment = segments.get("transcript_alignment")
        base_segments = (
            alignment.get("base_segments") if isinstance(alignment, dict) else None
        )
        activity_segments = (
            base_segments
            if isinstance(base_segments, list)
            else segments.get("segments", [])
        )
        for segment in activity_segments:
            if not isinstance(segment, dict):
                continue
            speaker = str(segment.get("speaker", ""))
            if not speaker.startswith("speaker_"):
                continue
            try:
                speaker_index = int(speaker.rsplit("_", 1)[1])
            except ValueError:
                continue
            channel = channel_by_speaker_index.get(speaker_index)
            if channel is None:
                continue
            first = max(0, int(float(segment.get("start", 0)) / frame_seconds))
            last = min(
                len(preferred_channels),
                int(np.ceil(float(segment.get("end", 0)) / frame_seconds)),
            )
            preferred_channels[first:last] = channel
        fingerprint = _stable_hash(
            {
                "version": _SOURCE_ACTIVITY_FINGERPRINT_VERSION,
                "clock": "source",
                "frame_seconds": frame_seconds,
                "channel_tracks": [
                    {
                        "index": entry["index"],
                        "logical_track": entry["logical_track"],
                    }
                    for entry in channel_map
                ],
                "arrays": sorted(identities, key=lambda item: item["channel"]),
                "preferred_channels": {
                    "count": len(preferred_channels),
                    "dtype": preferred_channels.dtype.str,
                    "sha256": hashlib.sha256(
                        np.ascontiguousarray(preferred_channels).tobytes()
                    ).hexdigest(),
                },
            }
        )
        return _SourceActivity(
            arrays, frame_seconds, fingerprint, preferred_channels=preferred_channels
        )

    @staticmethod
    def _timed_token_similarity(left: dict, right: dict) -> float:
        left_words = left["words"]
        right_words = right["words"]
        if not left_words or not right_words:
            return 0.0
        if len(left_words) > len(right_words):
            left_words, right_words = right_words, left_words
        used = set()
        matches = 0
        for word in left_words:
            candidates = []
            center = (word["start"] + word["end"]) / 2
            for index, other in enumerate(right_words):
                if index in used or word["normalized"] != other["normalized"]:
                    continue
                other_center = (other["start"] + other["end"]) / 2
                gap = max(word["start"], other["start"]) - min(
                    word["end"], other["end"]
                )
                if abs(center - other_center) <= 0.35 or gap <= _WORD_TIME_TOLERANCE:
                    candidates.append((abs(center - other_center), index))
            if candidates:
                _, chosen = min(candidates)
                used.add(chosen)
                matches += 1
        return matches / min(len(left_words), len(right_words))

    @staticmethod
    def _word_alignment(left: dict, right: dict) -> list[tuple[int, int]]:
        candidates = []
        for left_index, left_word in enumerate(left["words"]):
            left_center = (left_word["start"] + left_word["end"]) / 2
            left_duration = max(0.05, left_word["end"] - left_word["start"])
            for right_index, right_word in enumerate(right["words"]):
                right_center = (right_word["start"] + right_word["end"]) / 2
                right_duration = max(0.05, right_word["end"] - right_word["start"])
                center_distance = abs(left_center - right_center)
                gap = max(left_word["start"], right_word["start"]) - min(
                    left_word["end"], right_word["end"]
                )
                same = left_word["normalized"] == right_word["normalized"]
                if same and center_distance <= 0.35:
                    candidates.append((0, center_distance, left_index, right_index))
                elif gap <= _WORD_TIME_TOLERANCE and center_distance <= max(
                    0.18, 0.65 * max(left_duration, right_duration)
                ):
                    candidates.append((1, center_distance, left_index, right_index))
        used_left = set()
        used_right = set()
        result = []
        for _, _, left_index, right_index in sorted(candidates):
            if left_index in used_left or right_index in used_right:
                continue
            used_left.add(left_index)
            used_right.add(right_index)
            result.append((left_index, right_index))
        return result

    def _canonicalize_multichannel_words(
        self, utterances: list[dict], activity: _SourceActivity | None
    ) -> tuple[list[dict], dict]:
        words = [word for utterance in utterances for word in utterance["words"]]
        word_index = {id(word): index for index, word in enumerate(words)}
        sets = _DisjointSet(len(words))
        duplicate_pairs = []
        duplicate_utterances = set()
        active: list[int] = []
        order = sorted(
            range(len(utterances)), key=lambda index: utterances[index]["start"]
        )
        for index in order:
            utterance = utterances[index]
            active = [
                other
                for other in active
                if utterances[other]["end"] >= utterance["start"] - _WORD_TIME_TOLERANCE
            ]
            for other_index in active:
                other = utterances[other_index]
                if other["speaker"] == utterance["speaker"]:
                    continue
                overlap = min(other["end"], utterance["end"]) - max(
                    other["start"], utterance["start"]
                )
                shorter = max(
                    0.01,
                    min(
                        other["end"] - other["start"],
                        utterance["end"] - utterance["start"],
                    ),
                )
                if overlap < -_WORD_TIME_TOLERANCE:
                    continue
                similarity = self._timed_token_similarity(other, utterance)
                if similarity < 0.6 or (
                    overlap / shorter < 0.35
                    and abs(other["start"] - utterance["start"]) > 0.25
                ):
                    continue
                duplicate_pairs.append((other_index, index))
                duplicate_utterances.update((other["id"], utterance["id"]))
                for left_index, right_index in self._word_alignment(other, utterance):
                    sets.union(
                        word_index[id(other["words"][left_index])],
                        word_index[id(utterance["words"][right_index])],
                    )
            active.append(index)

        # A short utterance can miss the pair-level threshold while still carrying
        # an exact bleed word. Exact timed tokens are always one acoustic event.
        token_windows: dict[str, list[int]] = {}
        for index in sorted(range(len(words)), key=lambda item: words[item]["start"]):
            word = words[index]
            candidates = token_windows.setdefault(word["normalized"], [])
            candidates[:] = [
                other
                for other in candidates
                if words[other]["end"] >= word["start"] - _WORD_TIME_TOLERANCE
            ]
            center = (word["start"] + word["end"]) / 2
            for other in candidates:
                other_word = words[other]
                other_center = (other_word["start"] + other_word["end"]) / 2
                if (
                    other_word["speaker"] != word["speaker"]
                    and abs(center - other_center) <= 0.3
                ):
                    sets.union(index, other)
            candidates.append(index)

        events: dict[int, list[dict]] = {}
        for index, word in enumerate(words):
            events.setdefault(sets.find(index), []).append(word)
        selected = []
        removed = 0
        ambiguous = 0
        for candidates in events.values():
            if len(candidates) == 1:
                chosen = candidates[0]
                chosen["_canonical_id"] = f"word_{chosen['_id']:06d}"
                chosen["_alternatives"] = []
                chosen["_suspect_reasons"] = (
                    ["low_confidence"] if chosen["confidence"] < 0.6 else []
                )
                selected.append(chosen)
                continue
            scored = []
            levels = {}
            preferred_channel = (
                activity.preferred_channel(
                    min(word["start"] for word in candidates),
                    max(word["end"] for word in candidates),
                )
                if activity
                else None
            )
            for word in candidates:
                level = (
                    activity.level(word["speaker"], word["start"], word["end"])
                    if activity
                    else None
                )
                levels[word["_id"]] = level
                scored.append(
                    (
                        int(word["speaker"] == preferred_channel),
                        float("-inf") if level is None else level,
                        word["confidence"],
                        -word["speaker"],
                        word,
                    )
                )
            scored.sort(key=lambda item: item[:3], reverse=True)
            chosen = scored[0][4]
            chosen["_canonical_id"] = (
                f"word_{min(word['_id'] for word in candidates):06d}"
            )
            chosen["_alternatives"] = [
                {
                    "word": word["word"],
                    "punctuated_word": word["punctuated_word"],
                    "start": word["start"],
                    "end": word["end"],
                    "confidence": word["confidence"],
                    "asr_channel": word["speaker"],
                    "activity_db": levels[word["_id"]],
                }
                for word in candidates
                if word is not chosen
            ]
            suspect_reasons = []
            if chosen["confidence"] < 0.6:
                suspect_reasons.append("low_confidence")
            removed += len(candidates) - 1
            variants = {word["normalized"] for word in candidates}
            if len(variants) > 1:
                suspect_reasons.append("channel_variant")
                top_level, next_level = scored[0][1], scored[1][1]
                if (
                    not np.isfinite(top_level)
                    or not np.isfinite(next_level)
                    or top_level - next_level < 3.0
                ):
                    ambiguous += 1
                    suspect_reasons.append("channel_variant_ambiguous")
            chosen["_suspect_reasons"] = suspect_reasons
            selected.append(chosen)

        selected.sort(key=lambda word: (word["start"], word["end"], word["speaker"]))
        if activity:
            for word in selected:
                preferred = activity.preferred_channel(word["start"], word["end"])
                if (
                    preferred is not None
                    and word["_utterance"] in duplicate_utterances
                    and preferred != word["speaker"]
                ):
                    word["speaker"] = preferred
                    if not word["_alternatives"]:
                        word["_suspect_reasons"].append("unmatched_channel_variant")
        stats = {
            "input_words": len(words),
            "output_words": len(selected),
            "removed_duplicate_words": removed,
            "duplicate_utterance_pairs": len(duplicate_pairs),
            "ambiguous_variant_events": ambiguous,
            "activity_status": "source_rms" if activity else "unavailable",
            "activity_fingerprint": activity.fingerprint if activity else None,
        }
        return selected, stats

    def _build_diarized_transcript(
        self,
        raw: dict,
        multichannel: bool = False,
        channel_map: list[dict] | None = None,
        activity: _SourceActivity | None = None,
    ) -> dict:
        """Build one source-clock word stream and retain real overlapping speech."""
        speaker_key = "channel" if multichannel else "speaker"
        prepared = []
        word_id = 0
        for utterance_id, raw_utterance in enumerate(
            raw.get("results", {}).get("utterances", [])
        ):
            speaker = raw_utterance.get(speaker_key, 0)
            if not isinstance(speaker, int) or isinstance(speaker, bool):
                speaker = 0
            prepared_words = []
            raw_words = raw_utterance.get("words", [])
            transcript_tokens = str(raw_utterance.get("transcript", "")).split()
            for raw_word_index, raw_word in enumerate(raw_words):
                value = raw_word.get("word", raw_word.get("punctuated_word", ""))
                normalized = _normalized_word(value)
                if not normalized:
                    continue
                start = float(raw_word.get("start", raw_utterance.get("start", 0)))
                end = float(raw_word.get("end", start))
                if end < start:
                    continue
                prepared_words.append(
                    {
                        "_id": word_id,
                        "_utterance": utterance_id,
                        "normalized": normalized,
                        "word": value,
                        "punctuated_word": raw_word.get(
                            "punctuated_word",
                            (
                                transcript_tokens[raw_word_index]
                                if len(transcript_tokens) == len(raw_words)
                                else value
                            ),
                        ),
                        "start": start,
                        "end": end,
                        "confidence": float(raw_word.get("confidence", 0) or 0),
                        "speaker": speaker,
                        "asr_channel": speaker if multichannel else None,
                    }
                )
                word_id += 1
            prepared.append(
                {
                    "id": utterance_id,
                    "speaker": speaker,
                    "start": float(raw_utterance.get("start", 0)),
                    "end": float(raw_utterance.get("end", 0)),
                    "confidence": float(raw_utterance.get("confidence", 0) or 0),
                    "words": prepared_words,
                }
            )

        if multichannel:
            selected, canonicalization = self._canonicalize_multichannel_words(
                prepared, activity
            )
        else:
            selected = [word for utterance in prepared for word in utterance["words"]]
            for word in selected:
                word["_canonical_id"] = f"word_{word['_id']:06d}"
                word["_alternatives"] = []
                word["_suspect_reasons"] = (
                    ["low_confidence"] if word["confidence"] < 0.6 else []
                )
            selected.sort(
                key=lambda word: (word["start"], word["end"], word["speaker"])
            )
            canonicalization = {
                "input_words": len(selected),
                "output_words": len(selected),
                "removed_duplicate_words": 0,
                "duplicate_utterance_pairs": 0,
                "ambiguous_variant_events": 0,
                "activity_status": "not_applicable",
                "activity_fingerprint": None,
            }

        # Split an original utterance when a real overlapping interjection lands
        # inside it. Flattening the public utterances then remains source-time
        # ordered for API consumers that do not perform their own final sort.
        word_groups: list[list[dict]] = []
        for word in selected:
            if (
                word_groups
                and word_groups[-1][-1]["_utterance"] == word["_utterance"]
                and word_groups[-1][-1]["speaker"] == word["speaker"]
            ):
                word_groups[-1].append(word)
            else:
                word_groups.append([word])
        utterances = []
        for chosen_words in word_groups:
            public_words = []
            for word in chosen_words:
                public_word = {
                    "id": word["_canonical_id"],
                    "word": word["word"],
                    "punctuated_word": word["punctuated_word"],
                    "start": word["start"],
                    "end": word["end"],
                    "confidence": word["confidence"],
                    "speaker": word["speaker"],
                    "suspect": bool(word["_suspect_reasons"]),
                    "suspect_reasons": word["_suspect_reasons"],
                    "alternatives": word["_alternatives"],
                }
                if multichannel:
                    public_word["asr_channel"] = word["asr_channel"]
                public_words.append(public_word)
            source = prepared[chosen_words[0]["_utterance"]]
            speaker = public_words[0]["speaker"]
            utterances.append(
                {
                    "speaker": speaker,
                    "start": public_words[0]["start"],
                    "end": public_words[-1]["end"],
                    "text": " ".join(word["punctuated_word"] for word in public_words),
                    "confidence": (
                        sum(word["confidence"] for word in public_words)
                        / len(public_words)
                    ),
                    "words": public_words,
                    "source_utterance": source["id"],
                }
            )
        utterances.sort(
            key=lambda utterance: (
                utterance["start"],
                utterance["end"],
                utterance["speaker"],
            )
        )
        canonicalization["input_utterances"] = len(prepared)
        canonicalization["output_utterances"] = len(utterances)
        canonicalization["suspect_words"] = sum(
            bool(word["_suspect_reasons"]) for word in selected
        )
        result = {
            "mode": "multichannel" if multichannel else "diarized",
            "clock": "source",
            "algorithm_version": TRANSCRIPT_CANONICAL_VERSION,
            "utterances": utterances,
            "canonicalization": canonicalization,
        }
        if channel_map:
            result["speaker_map"] = channel_map
        return result

    def _generate_srt(self, transcript: dict, multichannel: bool = False) -> Path:
        """Write every canonical word; overlapping speakers are never discarded."""
        if "results" in transcript:
            transcript = self._build_diarized_transcript(
                transcript, multichannel=multichannel
            )
        words = [
            word
            for utterance in transcript.get("utterances", [])
            for word in utterance.get("words", [])
        ]
        words.sort(
            key=lambda word: (
                float(word.get("start", 0)),
                float(word.get("end", 0)),
                int(word.get("speaker", 0)),
            )
        )
        lines = []
        for cue, first in enumerate(range(0, len(words), 5), 1):
            chunk = words[first : first + 5]
            start = min(float(word.get("start", 0)) for word in chunk)
            end = max(float(word.get("end", start)) for word in chunk)
            text = " ".join(
                str(word.get("punctuated_word", word.get("word", ""))) for word in chunk
            )
            lines.append(
                f"{cue}\n{fmt_timecode(start)} --> {fmt_timecode(end)}\n{text}\n"
            )
        path = self.episode_dir / "subtitles" / "transcript.srt"
        _atomic_write_text(path, "\n".join(lines))
        self.logger.info("SRT: %d blocks", len(lines))
        return path

    def _save_canonical_outputs(
        self,
        raw: dict,
        multichannel: bool,
        channel_map: list[dict] | None,
        input_fingerprint: str,
        config_fingerprint: str,
        *,
        reused_raw: bool,
    ) -> dict:
        activity = self._load_source_activity(channel_map)
        speaker_map = (
            channel_map if multichannel else self._infer_diarized_speaker_map(raw)
        )
        diarized = self._build_diarized_transcript(
            raw,
            multichannel=multichannel,
            channel_map=speaker_map,
            activity=activity,
        )
        raw_path = self.episode_dir / "transcript.json"
        raw_hash = _file_sha256(raw_path) if raw_path.is_file() else _stable_hash(raw)
        corrections = _load_corrections(self.episode_dir)
        corrections_fingerprint = None
        if corrections:
            expected_raw = corrections.get("raw_transcript_sha256")
            if expected_raw and expected_raw != raw_hash:
                raise RuntimeError(
                    "Transcript corrections target a different raw transcript"
                )
            diarized, _ = self._apply_transcript_corrections(diarized, corrections)
            corrections_fingerprint = _stable_hash(corrections)
        provenance = {
            "version": TRANSCRIPT_CANONICAL_VERSION,
            "clock": "source",
            "mode": "multichannel" if multichannel else "diarized",
            "asr_input_fingerprint": input_fingerprint,
            "asr_config_fingerprint": config_fingerprint,
            "raw_transcript_sha256": raw_hash,
            "canonical_activity_fingerprint": (
                activity.fingerprint if activity else None
            ),
            "corrections_fingerprint": corrections_fingerprint,
            "channel_map": channel_map or [],
            "speaker_map": speaker_map or [],
            "raw_reused_without_api": reused_raw,
        }
        diarized["provenance"] = provenance
        self.save_json("diarized_transcript.json", diarized)
        atomic_write_json(self.episode_dir / "transcript_provenance.json", provenance)
        srt_path = self._generate_srt(diarized)
        from agents.speaker_cut import align_speaker_segments_to_transcript

        aligned_segments = align_speaker_segments_to_transcript(self.episode_dir)
        utterances = diarized["utterances"]
        return {
            "transcript_path": str(raw_path),
            "diarized_path": str(self.episode_dir / "diarized_transcript.json"),
            "srt_path": str(srt_path),
            "utterance_count": len(utterances),
            "word_count": sum(
                len(utterance.get("words", [])) for utterance in utterances
            ),
            "canonicalization": diarized["canonicalization"],
            "provenance_path": str(self.episode_dir / "transcript_provenance.json"),
            "speaker_alignment": (
                aligned_segments.get("transcript_alignment")
                if aligned_segments is not None
                else None
            ),
        }
