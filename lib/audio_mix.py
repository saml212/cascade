"""Audio mix — generate pre-mixed audio from H6E multi-track recordings.

Creates work/audio_mix.wav by mixing individual H6E speaker and ambient
tracks with configurable per-track volumes, time-aligned to video via
the sync offset from ingest.  Render agents use this instead of camera
audio when available.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("cascade")


def generate_audio_mix(episode_dir: Path, episode_data: dict) -> Path | None:
    """Generate work/audio_mix.wav from H6E tracks with per-track volumes.

    Reads mix settings from episode_data["audio_mix"]["tracks"] — a list of
    {stem, volume} entries.  Falls back to crop_config speaker/ambient volumes
    if no explicit audio_mix config exists.

    Audio sync offset is applied so the output aligns with video timeline.

    Returns:
        Path to generated WAV, or None if no tracks available.
    """
    work_dir = episode_dir / "work"
    work_dir.mkdir(exist_ok=True)
    output_path = work_dir / "audio_mix.wav"

    offset = episode_data.get("audio_sync", {}).get("offset_seconds", 0)
    mix_cfg = episode_data.get("audio_mix", {})
    mix_tracks = mix_cfg.get("tracks", [])
    master_vol = mix_cfg.get("master_volume", 1.0)

    if not mix_tracks:
        mix_tracks = _build_from_crop_config(episode_dir, episode_data)

    if not mix_tracks:
        return None

    # Resolve stems to disk paths
    stem_to_path = _map_track_stems(episode_dir, episode_data)

    # Filter to valid, non-muted tracks
    entries = []
    for t in mix_tracks:
        stem = t["stem"]
        vol = t.get("volume", 1.0) * master_vol
        if vol <= 0 or stem not in stem_to_path:
            continue
        entries.append((stem_to_path[stem], vol))

    if not entries:
        logger.warning("No valid audio tracks for mixing")
        return None

    # Build ffmpeg filter graph
    inputs = []
    filters = []
    labels = []

    for i, (path, vol) in enumerate(entries):
        if offset >= 0:
            inputs += ["-ss", str(offset), "-i", str(path)]
        else:
            inputs += ["-i", str(path)]

        f = f"[{i}:a]aformat=channel_layouts=mono"
        if offset < 0:
            delay_ms = int(abs(offset) * 1000)
            f += f",adelay={delay_ms}|{delay_ms}"
        f += f",volume={vol:.3f}[t{i}]"
        filters.append(f)
        labels.append(f"[t{i}]")

    n = len(entries)
    fc = "; ".join(filters)
    if n > 1:
        fc += f"; {''.join(labels)}amix=inputs={n}:duration=longest:normalize=0[mix]"
        fc += "; [mix]pan=stereo|c0=c0|c1=c0[out]"
    else:
        fc = filters[0].replace("[t0]", "[mono]")
        fc += "; [mono]pan=stereo|c0=c0|c1=c0[out]"

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", fc,
        "-map", "[out]",
        "-c:a", "pcm_s16le", "-ar", "48000",
        str(output_path),
    ]

    logger.info(f"Generating audio mix from {n} tracks...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Audio mix failed: {result.stderr[-500:]}")

    size_mb = output_path.stat().st_size / 1e6
    logger.info(f"Audio mix: {output_path.name} ({size_mb:.1f} MB)")
    return output_path


def _build_from_crop_config(episode_dir: Path, episode_data: dict) -> list[dict]:
    """Build track list from crop_config speaker/ambient track assignments."""
    crop = episode_data.get("crop_config", {})
    audio_tracks = _get_audio_tracks(episode_dir, episode_data)

    num_to_stem = {}
    for t in audio_tracks:
        tn = t.get("track_number")
        if tn is not None:
            num_to_stem[tn] = Path(t["filename"]).stem

    result = []
    for spk in crop.get("speakers", []):
        tn = spk.get("track")
        if tn and tn in num_to_stem:
            result.append({"stem": num_to_stem[tn], "volume": spk.get("volume", 1.0)})

    for amb in crop.get("ambient_tracks", []):
        tn = amb.get("track_number")
        if tn and tn in num_to_stem:
            result.append({"stem": num_to_stem[tn], "volume": amb.get("volume", 0.2)})

    return result


def _map_track_stems(episode_dir: Path, episode_data: dict) -> dict[str, Path]:
    """Map track filename stems to their disk paths."""
    tracks = _get_audio_tracks(episode_dir, episode_data)
    result = {}
    for t in tracks:
        stem = Path(t["filename"]).stem
        path = Path(t["dest_path"])
        if path.exists():
            result[stem] = path
    return result


def _get_audio_tracks(episode_dir: Path, episode_data: dict) -> list[dict]:
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
