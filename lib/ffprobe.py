"""FFprobe wrapper -- single source of truth for media file probing."""

import json
import subprocess
from pathlib import Path
from typing import Optional, Tuple


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
