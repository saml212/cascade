"""SRT subtitle utilities."""

from pathlib import Path


def fmt_timecode(seconds: float) -> str:
    """Format seconds as SRT timecode: HH:MM:SS,mmm"""
    seconds = max(0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def escape_srt_path(path: Path) -> str:
    """Escape a file path for use in ffmpeg subtitle filters.

    Handles backslashes, colons, and single quotes that would break
    ffmpeg's subtitle filter path parsing.
    """
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
