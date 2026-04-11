"""FFprobe wrapper -- single source of truth for media file probing."""

import json
import subprocess
from pathlib import Path
from typing import Tuple


def probe(path: Path) -> dict:
    """Run ffprobe and return parsed JSON with format + streams info.

    Raises subprocess.CalledProcessError if ffprobe fails.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def get_duration(path: Path) -> float:
    """Get media file duration in seconds."""
    data = probe(path)
    return float(data.get("format", {}).get("duration", 0))


def get_dimensions(path: Path) -> Tuple[int, int]:
    """Get video dimensions (width, height) from first video stream.

    Raises StopIteration if no video stream found.
    """
    data = probe(path)
    video_stream = next(
        s for s in data["streams"] if s["codec_type"] == "video"
    )
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
