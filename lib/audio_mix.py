"""Audio mix — generate pre-mixed audio from H6E multi-track recordings.

Creates work/audio_mix.wav by mixing individual H6E speaker and ambient
tracks with configurable per-track volumes, time-aligned to video via
the sync offset from ingest.  Render agents use this instead of camera
audio when available.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("cascade")

# Materialize timestamp discontinuities in embedded camera audio. DJI files can
# end an AAC stream before its matching video segment; ordinary decode collapses
# those gaps and shifts every later utterance earlier on the video timeline.
CAMERA_AUDIO_TIMELINE_FILTER = "aresample=async=1000:min_hard_comp=0.001:first_pts=0"
AUDIO_SELECTION_SCHEMA = "cascade.audio-selection/v1"
AUDIO_SELECTION_PATH = Path("work/audio_selection.json")
SELECTED_REPAIR_AUDIO_PATH = Path("work/audio_repair_selected.wav")

# Serialize audio mix generation across threads — both render agents call
# generate_audio_mix() and would otherwise race on the same output files.
_audio_mix_lock = threading.Lock()


def audio_selection_settings(episode_data: dict) -> dict:
    """Return only episode fields that can change the selected audio mix."""
    crop = episode_data.get("crop_config") or {}
    return {
        "audio_sync": episode_data.get("audio_sync") or {},
        "audio_mix": episode_data.get("audio_mix") or {},
        "speaker_tracks": [
            {
                "track": speaker.get("track"),
                "volume": speaker.get("volume", 1.0),
            }
            for speaker in crop.get("speakers", [])
        ],
        "ambient_tracks": crop.get("ambient_tracks", []),
    }


def audio_processing_settings(config: dict | None) -> dict:
    """Return processing settings that can change mastered audio bytes."""
    processing = (config or {}).get("processing", {})
    return {key: value for key, value in processing.items() if key.startswith("audio_")}


def _mix_fingerprint(
    episode_data: dict, config: dict | None, source_paths: list[Path]
) -> str:
    """Return a stable fingerprint of every input that affects the rendered mix."""
    sources = []
    for path in source_paths:
        stat = path.stat()
        sources.append(
            {
                "path": str(path.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    payload = {
        "selection": audio_selection_settings(episode_data),
        "processing": audio_processing_settings(config),
        "sources": sources,
        "version": 4,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _cache_matches(output_path: Path, fingerprint: str) -> bool:
    """Only reuse a complete mix made from the exact same inputs and settings."""
    fingerprint_path = output_path.with_suffix(".fingerprint")
    try:
        return (
            output_path.stat().st_size > 44
            and fingerprint_path.read_text().strip() == fingerprint
        )
    except OSError:
        return False


def _publish_mix(temp_path: Path, output_path: Path, fingerprint: str) -> None:
    """Atomically publish a completed WAV and its cache fingerprint."""
    os.replace(temp_path, output_path)
    fingerprint_path = output_path.with_suffix(".fingerprint")
    temp_fingerprint = fingerprint_path.with_suffix(".fingerprint.tmp")
    temp_fingerprint.write_text(fingerprint)
    os.replace(temp_fingerprint, fingerprint_path)


def current_audio_selection(
    episode_dir: str | Path,
    episode_data: dict | None = None,
    config: dict | None = None,
) -> dict | None:
    """Return a current fixed-path repair selection, or raise when it is stale."""
    episode_dir = Path(episode_dir)
    record_path = episode_dir / AUDIO_SELECTION_PATH
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Selected audio record is unreadable") from exc
    if not isinstance(record, dict) or record.get("schema") != AUDIO_SELECTION_SCHEMA:
        raise ValueError("Selected audio record is invalid")

    selected = (episode_dir / SELECTED_REPAIR_AUDIO_PATH).resolve()
    output = record.get("selected_output") or {}
    try:
        recorded_path = Path(output["path"]).resolve()
        fingerprint = output["fingerprint"]
        stat = selected.stat()
    except (KeyError, TypeError, OSError) as exc:
        raise ValueError("Selected repair audio is unavailable") from exc
    if recorded_path != selected:
        raise ValueError("Selected audio record points outside its canonical path")
    if (
        not fingerprint.get("id")
        or fingerprint.get("size_bytes") != stat.st_size
        or fingerprint.get("mtime_ns") != stat.st_mtime_ns
    ):
        raise ValueError("Selected repair audio has changed since review")

    source = episode_dir / "source_merged.mp4"
    recorded_source = record.get("source") or {}
    try:
        source_stat = source.stat()
    except OSError as exc:
        raise ValueError("Selected repair source media is unavailable") from exc
    source_fingerprint = recorded_source.get("fingerprint") or {}
    if (
        Path(recorded_source.get("path", "")).resolve() != source.resolve()
        or source_fingerprint.get("size_bytes") != source_stat.st_size
        or source_fingerprint.get("mtime_ns") != source_stat.st_mtime_ns
    ):
        raise ValueError("Selected repair audio is stale for the source media")

    if episode_data is not None and record.get("audio_selection_settings") != (
        audio_selection_settings(episode_data)
    ):
        raise ValueError("Selected repair audio is stale for the audio mix settings")
    if config is not None and record.get("audio_processing_settings") != (
        audio_processing_settings(config)
    ):
        raise ValueError("Selected repair audio is stale for audio processing settings")
    return record


def selected_audio_source(
    episode_dir: str | Path,
    episode_data: dict | None = None,
    config: dict | None = None,
) -> Path | None:
    """Resolve the reviewed repair selection used by renderers, when active."""
    record = current_audio_selection(episode_dir, episode_data, config)
    if record is None:
        return None
    return Path(record["selected_output"]["path"])


def publish_audio_selection(
    episode_dir: str | Path, staged_audio: str | Path, record: dict
) -> dict:
    """Atomically make a validated staged repair the renderer audio source."""
    episode_dir = Path(episode_dir)
    work_dir = episode_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    selected = (episode_dir / SELECTED_REPAIR_AUDIO_PATH).resolve()
    record_path = episode_dir / AUDIO_SELECTION_PATH
    staged_audio = Path(staged_audio).resolve()
    if staged_audio.parent != work_dir.resolve() or staged_audio == selected:
        raise ValueError("Selected audio must be staged in the episode work directory")
    if (
        record.get("schema") != AUDIO_SELECTION_SCHEMA
        or Path(record.get("selected_output", {}).get("path", "")).resolve() != selected
    ):
        raise ValueError("Selected audio record is invalid")

    fd, staged_record_name = tempfile.mkstemp(
        prefix=".audio-selection-", suffix=".json", dir=work_dir
    )
    staged_record = Path(staged_record_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        with _audio_mix_lock:
            os.replace(staged_audio, selected)
            os.replace(staged_record, record_path)
    finally:
        staged_audio.unlink(missing_ok=True)
        staged_record.unlink(missing_ok=True)
    return record


def clear_audio_selection(episode_dir: str | Path) -> bool:
    """Remove a repair selection while preserving the generated base master."""
    episode_dir = Path(episode_dir)
    record_path = episode_dir / AUDIO_SELECTION_PATH
    selected = episode_dir / SELECTED_REPAIR_AUDIO_PATH
    with _audio_mix_lock:
        existed = record_path.exists() or selected.exists()
        record_path.unlink(missing_ok=True)
        selected.unlink(missing_ok=True)
    return existed


def generate_audio_mix(
    episode_dir: Path, episode_data: dict, config: dict | None = None
) -> Path | None:
    """Return selected repair audio or generate the base mix used by renders.

    Two modes:
    1. **H6E multi-track mode**: When `episode_data["audio_tracks"]` contains
       Zoom H6E tracks, mixes them according to `audio_mix.tracks` (or
       `crop_config` speaker/ambient volumes), applies sync offset + tempo
       correction, and saves to work/audio_mix.wav.
    2. **Camera-audio mode** (no H6E): Extracts the embedded audio from
       source_merged.mp4 directly. Used for episodes recorded with wireless
       DJI mics where audio is baked into the camera file.

    In both cases, if config is provided and audio_enhance is enabled, the
    resulting WAV is run through the audio enhancement pipeline (DeepFilterNet
    denoise + ffmpeg EQ/compression/loudness normalization).

    Returns:
        Path to generated WAV, or None if no audio source available.
    """
    work_dir = episode_dir / "work"
    work_dir.mkdir(exist_ok=True)
    output_path = work_dir / "audio_mix.wav"

    # Acquire lock to prevent concurrent generation by parallel render agents.
    # The second caller will block here, then check if the file is recent enough
    # to skip regeneration entirely.
    _audio_mix_lock.acquire()
    try:
        selected = selected_audio_source(episode_dir, episode_data, config)
        if selected is not None:
            logger.info("Using revision-bound selected repair audio")
            return selected
        return _generate_audio_mix_locked(
            episode_dir, episode_data, config, work_dir, output_path
        )
    finally:
        _audio_mix_lock.release()


def _generate_audio_mix_locked(
    episode_dir: Path,
    episode_data: dict,
    config: dict,
    work_dir: Path,
    output_path: Path,
) -> Path | None:
    """Internal implementation, called under _audio_mix_lock."""
    audio_sync = episode_data.get("audio_sync", {})
    offset = audio_sync.get("offset_seconds", 0)
    # Only apply tempo correction if the drift regression was reliable
    r_sq = audio_sync.get("r_squared", 0)
    tempo_factor = audio_sync.get("tempo_factor", 1.0) if r_sq > 0.5 else 1.0

    mix_cfg = episode_data.get("audio_mix", {})
    mix_tracks = mix_cfg.get("tracks", [])
    master_vol = mix_cfg.get("master_volume", 1.0)

    available_tracks = episode_audio_tracks(episode_dir, episode_data)
    camera_channel_only = bool(available_tracks) and all(
        track.get("track_type") == "camera_channel" for track in available_tracks
    )
    if camera_channel_only:
        # The materialized camera_Tr WAVs from older runs may have collapsed
        # AAC timestamp gaps. Decode the merged source with its PTS intact.
        mix_tracks = []
    elif not mix_tracks:
        mix_tracks = _build_from_crop_config(episode_dir, episode_data)

    # Camera-audio mode: no H6E tracks available, extract from source_merged.mp4
    if not mix_tracks:
        merged_path = episode_dir / "source_merged.mp4"
        if not merged_path.exists():
            logger.warning(
                "No H6E tracks and no source_merged.mp4 — cannot generate audio mix"
            )
            return None
        fingerprint = _mix_fingerprint(episode_data, config, [merged_path])
        if _cache_matches(output_path, fingerprint):
            logger.info("Reusing unchanged audio_mix.wav")
            return output_path
        return _generate_camera_audio_mix(
            merged_path, output_path, config, fingerprint=fingerprint
        )

    stem_to_path = _map_track_stems(episode_dir, episode_data)
    entries = []
    for track in mix_tracks:
        volume = track.get("volume", 1.0) * master_vol
        stems = track.get("stems") or [track.get("stem")]
        paths = [stem_to_path[stem] for stem in stems if stem in stem_to_path]
        if volume > 0 and paths:
            entries.append(
                {
                    "paths": paths,
                    "volume": volume,
                    # Legacy audio_mix entries were speaker tracks in practice.
                    "role": track.get("role", "speaker"),
                }
            )
    if not entries:
        logger.warning("No valid audio tracks for mixing")
        return None

    fingerprint = _mix_fingerprint(
        episode_data,
        config,
        [path for entry in entries for path in entry["paths"]],
    )
    if _cache_matches(output_path, fingerprint):
        logger.info("Reusing unchanged audio_mix.wav")
        return output_path

    # Video duration for trimming — sync data > stitch.json > episode duration
    video_duration = audio_sync.get("video_duration") or episode_data.get(
        "duration_seconds"
    )

    if tempo_factor != 1.0:
        logger.info(
            f"Tempo correction: {tempo_factor:.8f} ({audio_sync.get('drift_rate_ppm', 0):.1f} ppm)"
        )

    # Per-mic normalization is available for unusual recordings, but remains
    # off by default because independently raising inactive open mics also
    # raises their bleed and room noise. The master stage handles loudness.
    processing = (config or {}).get("processing", {})
    per_speaker_target = processing.get("audio_per_speaker_lufs", -19)
    # Master enhancement owns loudness by default. Per-mic loudnorm is opt-in:
    # independently processing open mics can exaggerate bleed and room tone.
    enable_per_speaker = processing.get("audio_per_speaker_leveling", False)

    # Build ffmpeg filter graph
    inputs = []
    filters = []
    labels = []

    input_index = 0
    for i, entry in enumerate(entries):
        vol = entry["volume"]
        role = entry["role"]

        segment_labels = []
        for path in entry["paths"]:
            inputs += ["-i", str(path)]
            segment_label = f"s{i}_{len(segment_labels)}"
            filters.append(
                f"[{input_index}:a]aformat=channel_layouts=mono[{segment_label}]"
            )
            segment_labels.append(f"[{segment_label}]")
            input_index += 1

        if len(segment_labels) > 1:
            filters.append(
                f"{''.join(segment_labels)}concat=n={len(segment_labels)}:v=0:a=1[c{i}]"
            )
            f = f"[c{i}]anull"
        else:
            f = f"{segment_labels[0]}anull"
        if offset >= 0 and offset:
            f += f",atrim=start={offset},asetpts=PTS-STARTPTS"
        if offset < 0:
            delay_ms = int(abs(offset) * 1000)
            f += f",adelay={delay_ms}"
        if abs(tempo_factor - 1.0) > 1e-7:
            f += f",atempo={tempo_factor:.8f}"
        if enable_per_speaker and role == "speaker":
            # Optional continuous gain riding is separate from the per-file
            # loudness target and must be requested explicitly.
            if processing.get("audio_per_speaker_dynaudnorm", False):
                f += ",dynaudnorm=f=300:g=11:p=0.9:m=20:r=0.0:s=12"
            f += f",loudnorm=I={per_speaker_target}:TP=-1.5:LRA=7"
        f += f",volume={vol:.3f}[t{i}]"
        filters.append(f)
        labels.append(f"[t{i}]")

    n = len(entries)
    fc = "; ".join(filters)
    if n > 1:
        fc += f"; {''.join(labels)}amix=inputs={n}:duration=longest:normalize=0[mix]"
        fc += "; [mix]alimiter=limit=0.95:attack=5:release=50"
        fc += ",pan=stereo|c0=c0|c1=c0[out]"
    else:
        fc += "; " + labels[0] + "anull[mono]"
        fc += "; [mono]pan=stereo|c0=c0|c1=c0[out]"

    temp_path = output_path.with_suffix(".tmp.wav")
    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        fc,
        "-map",
        "[out]",
        "-c:a",
        "pcm_f32le",
        "-ar",
        "48000",
    ]

    # Trim output to video duration so H6E doesn't extend past the video
    if video_duration:
        cmd += ["-t", str(video_duration)]

    cmd.append(str(temp_path))

    logger.info(f"Generating audio mix from {n} tracks (offset={offset:.4f}s)...")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Audio mix failed: {result.stderr[-500:]}")

    # Apply audio enhancement (EQ, compression, loudness normalization)
    if config:
        from lib.audio_enhance import enhance_audio

        enhanced_path = work_dir / "audio_mix_enhanced.wav"
        result_path = enhance_audio(temp_path, enhanced_path, config)
        if result_path != temp_path and result_path.exists():
            os.replace(result_path, temp_path)

    _publish_mix(temp_path, output_path, fingerprint)
    size_mb = output_path.stat().st_size / 1e6
    logger.info(f"Audio mix: {output_path.name} ({size_mb:.1f} MB)")

    return output_path


def _generate_camera_audio_mix(
    merged_path: Path,
    output_path: Path,
    config: dict,
    *,
    fingerprint: str,
) -> Path | None:
    """Extract camera audio from source_merged.mp4 to PCM WAV, then enhance.

    Used for episodes recorded with wireless mics where audio is embedded in
    the camera file (no separate H6E recording). The L/R channels typically
    correspond to two DJI mics, so average them and publish dual-mono stereo.
    This keeps both speakers centered and avoids one-sided podcast audio.
    """
    logger.info("Extracting camera audio to %s...", output_path.name)
    temp_path = output_path.with_suffix(".tmp.wav")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(merged_path),
        "-vn",  # video not needed
        "-af",
        (
            # Camera concat files can contain valid timestamp gaps when an AAC
            # stream ends before its corresponding video segment. Preserve the
            # source timeline by materializing those gaps as silence before
            # filters otherwise collapse decoded samples into a shorter stream.
            f"{CAMERA_AUDIO_TIMELINE_FILTER},"
            "aformat=channel_layouts=stereo,"
            "pan=mono|c0=0.5*c0+0.5*c1,"
            "pan=stereo|c0=c0|c1=c0"
        ),
        "-c:a",
        "pcm_f32le",
        "-ar",
        "48000",
        str(temp_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("Camera audio extraction failed: %s", result.stderr[-500:])
        return None

    # Apply enhancement (DeepFilterNet + ffmpeg chain) — same as H6E path
    if config:
        from lib.audio_enhance import enhance_audio

        enhanced_path = output_path.parent / "audio_mix_enhanced.wav"
        result_path = enhance_audio(temp_path, enhanced_path, config)
        if result_path != temp_path and result_path.exists():
            os.replace(result_path, temp_path)

    _publish_mix(temp_path, output_path, fingerprint)
    size_mb = output_path.stat().st_size / 1e6
    logger.info("Camera audio extracted: %s (%.1f MB)", output_path.name, size_mb)

    return output_path


def _build_from_crop_config(episode_dir: Path, episode_data: dict) -> list[dict]:
    """Build track list from crop_config speaker/ambient track assignments.

    Tags each entry with role="speaker" or role="ambient" so the mix builder
    knows whether to run per-speaker leveling (voices) or pass through at
    fader level (room-mic / built-in ambient).
    """
    crop = episode_data.get("crop_config", {})
    audio_tracks = episode_audio_tracks(episode_dir, episode_data)

    num_to_stems = {}
    for t in audio_tracks:
        tn = t.get("track_number")
        if tn is not None:
            num_to_stems.setdefault(tn, []).append(Path(t["filename"]).stem)

    result = []
    for spk in crop.get("speakers", []):
        tn = spk.get("track")
        if tn and tn in num_to_stems:
            result.append(
                {
                    "stem": num_to_stems[tn][0],
                    "stems": num_to_stems[tn],
                    "volume": spk.get("volume", 1.0),
                    "role": "speaker",
                }
            )

    for amb in crop.get("ambient_tracks", []):
        tn = amb.get("track_number")
        stem = amb.get("stem")
        if tn and tn in num_to_stems:
            result.append(
                {
                    "stem": num_to_stems[tn][0],
                    "stems": num_to_stems[tn],
                    "volume": amb.get("volume", 0.2),
                    "role": "ambient",
                }
            )
        elif stem:
            result.append(
                {
                    "stem": stem,
                    "volume": amb.get("volume", 0.2),
                    "role": "ambient",
                }
            )

    # Legacy 2-speaker crop_config (pre-N-speaker migration): if no speakers
    # array but legacy speaker_l_/speaker_r_ fields exist, synthesize a
    # 2-entry speakers list. Speaker L maps to track 1, Speaker R to track 2 —
    # matching the convention used by camera-stereo episodes (camera_Tr1/Tr2)
    # and old 2-speaker H6E episodes (Tr1/Tr2 were always the two mics).
    if not crop.get("speakers") and not result and "speaker_l_center_x" in crop:
        for track_number in (1, 2):
            stems = num_to_stems.get(track_number, [])
            stem = stems[0] if stems else None
            result.append(
                {
                    "stem": stem,
                    "stems": stems,
                    "volume": 1.0,
                    "role": "speaker",
                }
            )

    return result


def _map_track_stems(episode_dir: Path, episode_data: dict) -> dict[str, Path]:
    """Map track filename stems to their disk paths."""
    tracks = episode_audio_tracks(episode_dir, episode_data)
    result = {}
    for t in tracks:
        stem = Path(t["filename"]).stem
        path = Path(t["dest_path"])
        if path.exists():
            result[stem] = path
    return result


def episode_audio_tracks(episode_dir: Path, episode_data: dict) -> list[dict]:
    """Get audio tracks, merging from ingest.json if needed."""
    tracks = episode_data.get("audio_tracks", [])
    if tracks:
        return tracks

    ingest_file = episode_dir / "ingest.json"
    if ingest_file.exists():
        try:
            with open(ingest_file) as f:
                return json.load(f).get("audio", {}).get("tracks", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def logical_track_groups(
    episode_dir: Path,
    episode_data: dict,
    *,
    recorder_only: bool = False,
    existing_only: bool = True,
) -> dict[int, list[dict]]:
    """Group consecutive recorder sessions by logical input number.

    Zoom recorders restart names such as ``Tr1`` for each session. Manifest
    order is the recording order, so callers concatenate each returned list
    instead of letting a later session overwrite the earlier one.
    """
    groups: dict[int, list[dict]] = {}
    for track in episode_audio_tracks(episode_dir, episode_data):
        track_number = track.get("track_number")
        if not isinstance(track_number, int) or isinstance(track_number, bool):
            continue
        if recorder_only and track.get("track_type") == "camera_channel":
            continue
        path = Path(track.get("dest_path", ""))
        if existing_only and not path.is_file():
            continue
        groups.setdefault(track_number, []).append(track)
    return groups
