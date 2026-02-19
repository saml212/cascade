"""Shared video encoder infrastructure — VideoToolbox detection and argument selection."""

import functools
import subprocess
import sys


@functools.lru_cache(maxsize=1)
def has_videotoolbox() -> bool:
    """Check if h264_videotoolbox encoder is available. Result is cached."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "h264_videotoolbox" in result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def get_video_encoder_args(config: dict, crf_key: str = "video_crf") -> list:
    """Return ffmpeg encoder arguments based on config and platform capabilities.

    VideoToolbox path: ["-c:v", "h264_videotoolbox", "-q:v", "65"]
    Software fallback: ["-c:v", "libx264", "-crf", "<value>", "-preset", "fast"]
    """
    use_hw = config.get("processing", {}).get("use_hardware_accel", True)

    if use_hw and has_videotoolbox():
        return ["-c:v", "h264_videotoolbox", "-q:v", "65"]

    crf = config.get("processing", {}).get(crf_key, 22)
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", "fast"]
