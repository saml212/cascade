"""FFprobe and content identity helpers for local media artifacts."""

import hashlib
import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path


def probe(path: Path) -> dict:
    """Run ffprobe and return parsed JSON with format + streams info.

    Raises subprocess.CalledProcessError if ffprobe fails.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def get_duration(path: Path) -> float:
    """Get media file duration in seconds."""
    data = probe(path)
    return float(data.get("format", {}).get("duration", 0))


def get_audio_stream(data: dict) -> dict:
    """Return the first audio stream from already-probed media."""
    stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    if stream is None:
        raise ValueError("Media has no audio stream")
    return stream


def get_dimensions(path: Path) -> tuple[int, int]:
    """Get video dimensions (width, height) from first video stream.

    Raises StopIteration if no video stream found.
    """
    data = probe(path)
    video_stream = next(s for s in data["streams"] if s["codec_type"] == "video")
    return int(video_stream["width"]), int(video_stream["height"])


def get_video_properties(path: Path) -> dict:
    """Get video stream properties: codec, fps, pixel format, dimensions, color space.

    Parses r_frame_rate (e.g. "30000/1001" for 29.97) into a float.
    Used by ingest to capture source properties for downstream agents.
    """
    data = probe(path)
    vs = next(s for s in data["streams"] if s["codec_type"] == "video")

    # Parse fractional frame rate
    r_rate = vs.get("r_frame_rate", "30/1")
    try:
        num, den = r_rate.split("/")
        fps = round(int(num) / int(den), 3)
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    return {
        "width": int(vs["width"]),
        "height": int(vs["height"]),
        "codec": vs.get("codec_name", ""),
        "pix_fmt": vs.get("pix_fmt", ""),
        "fps": fps,
        "color_space": vs.get("color_space", ""),
        "color_primaries": vs.get("color_primaries", ""),
        "color_transfer": vs.get("color_transfer", ""),
    }


def media_fingerprint(path: str | Path, probe_data: dict | None = None) -> dict:
    """Fingerprint large media using stream metadata and sparse byte samples."""
    path = Path(path)
    stat = path.stat()
    probe_data = probe_data or probe(path)
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


def _file_identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def scan_identity(path: str | Path) -> dict | None:
    """Return stable path and stat identity, or None if the file changes."""
    requested = Path(path)
    try:
        resolved = requested.resolve(strict=True)
        before = resolved.stat()
        if requested.resolve(strict=True) != resolved:
            return None
        after = resolved.stat()
    except (OSError, RuntimeError):
        return None
    if _file_identity(before) != _file_identity(after):
        return None
    return {
        "resolved_path": str(resolved),
        "size_bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "ctime_ns": after.st_ctime_ns,
        "device": after.st_dev,
        "inode": after.st_ino,
    }


def _hash_open_file(handle) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=256)
def _file_digest(resolved_path: str, identity: tuple[int, int, int, int, int]) -> str:
    path = Path(resolved_path)
    try:
        with path.open("rb") as handle:
            if _file_identity(os.fstat(handle.fileno())) != identity:
                raise OSError
            digest = _hash_open_file(handle)
            if _file_identity(os.fstat(handle.fileno())) != identity:
                raise OSError
        if _file_identity(path.stat()) != identity:
            raise OSError
    except OSError as exc:
        raise OSError(f"{path} changed while its fingerprint was read") from exc
    return digest


def file_fingerprint(path: str | Path) -> dict:
    """Return a cached full SHA-256 only when the file identity stays stable."""
    requested = Path(path)
    resolved = requested.resolve(strict=True)
    initial = resolved.stat()
    identity = _file_identity(initial)
    digest = _file_digest(str(resolved), identity)
    try:
        final_resolved = requested.resolve(strict=True)
        final = _file_identity(final_resolved.stat())
    except OSError as exc:
        raise OSError(f"{requested} changed while its fingerprint was read") from exc
    if final_resolved != resolved or final != identity:
        raise OSError(f"{requested} changed while its fingerprint was read")

    return {
        "id": f"sha256:{digest}",
        "method": "sha256-full/v1",
        "size_bytes": initial.st_size,
        "mtime_ns": initial.st_mtime_ns,
    }
