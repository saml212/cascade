"""Camera-audio fallback — extract per-mic mono tracks from camera stereo.

When an episode is filmed with DJI Mic Mini wireless mics straight into the
Osmo Action 6 (no Zoom H6E), each speaker's mic lands on a separate channel
of the camera's stereo audio: speaker A on L, speaker B on R. To the rest of
cascade — speaker_cut, audio_mix, the crop-UI track mixer — those still need
to look like the per-input mono tracks an H6E would have produced, so the
same per-speaker volume/crop machinery just works.

This module demuxes the camera stereo into two mono WAVs named
camera_Tr1.WAV / camera_Tr2.WAV. The "_TrN" suffix is the same convention
H6E uses, so the frontend mixer regex (_Tr1\\., _Tr2\\.) and
audio_mix.py's track_number extraction both match unchanged.
"""

import logging
import subprocess
from pathlib import Path

from lib.ffprobe import probe as ffprobe

logger = logging.getLogger("cascade")


def extract_camera_channels(merged_path: Path, audio_dir: Path) -> list[dict]:
    """Demux a stereo camera file into two mono per-mic WAVs.

    Returns a list of track dicts shaped like ingest's H6E entries so the
    rest of the pipeline can treat them identically. Returns [] when the
    source isn't stereo (1 mic → nothing to split; 0 channels → no audio).
    """
    probe = ffprobe(merged_path)
    audio_stream = next(
        (s for s in probe.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )
    if not audio_stream:
        logger.info("camera_audio: no audio stream on %s, skipping", merged_path.name)
        return []

    channels = int(audio_stream.get("channels", 0))
    if channels < 2:
        logger.info(
            "camera_audio: %s is mono (1 channel) — single-mic recording, "
            "no L/R split needed",
            merged_path.name,
        )
        return []
    if channels > 2:
        # Future-proofing: surround would need a different mapping; the
        # consumer DJI cameras we use only ever produce stereo, so flag
        # and skip rather than guess.
        logger.warning(
            "camera_audio: %s has %d channels — only stereo is supported, "
            "skipping per-mic split",
            merged_path.name,
            channels,
        )
        return []

    audio_dir.mkdir(parents=True, exist_ok=True)
    sample_rate = int(audio_stream.get("sample_rate", 48000))
    duration = float(probe.get("format", {}).get("duration", 0))

    out_l = audio_dir / "camera_Tr1.WAV"
    out_r = audio_dir / "camera_Tr2.WAV"

    # channelsplit produces one mono output per input channel; map each to
    # its own pcm_s16le file. 48 kHz / 16-bit matches the H6E tracks we use
    # downstream so the mixer doesn't have to resample.
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(merged_path),
        "-filter_complex",
        "[0:a]channelsplit=channel_layout=stereo[L][R]",
        "-map",
        "[L]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(sample_rate),
        str(out_l),
        "-map",
        "[R]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(sample_rate),
        str(out_r),
    ]
    logger.info(
        "camera_audio: splitting %s stereo → camera_Tr1.WAV + camera_Tr2.WAV",
        merged_path.name,
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("camera_audio split failed: %s", result.stderr[-500:])
        return []

    tracks = []
    for idx, path in enumerate((out_l, out_r), start=1):
        tracks.append(
            {
                "source_path": str(merged_path),
                "dest_path": str(path),
                "filename": path.name,
                "track_type": "camera_channel",
                "channels": 1,
                "sample_rate": sample_rate,
                "bits": "16",
                "duration_seconds": round(duration, 3),
                "size_bytes": path.stat().st_size,
                "track_number": idx,
            }
        )

    logger.info(
        "camera_audio: extracted 2 mono tracks (L=camera_Tr1, R=camera_Tr2) from %s",
        merged_path.name,
    )
    return tracks
