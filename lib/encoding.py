"""Shared video encoder infrastructure — VideoToolbox detection, LUT support, argument selection."""

import functools
import subprocess
import sys
from pathlib import Path


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

    On Apple Silicon with VideoToolbox available, uses hardware encoding by
    default (10-20x faster, dedicated Media Engine). Set use_hardware_accel=false
    in config to force software encoding.

    VideoToolbox path: ["-c:v", "h264_videotoolbox", "-q:v", "75"]
    Software fallback: ["-c:v", "libx264", "-crf", "<value>", "-preset", "medium"]
    """
    use_hw = config.get("processing", {}).get("use_hardware_accel", True)

    if use_hw and has_videotoolbox():
        # Apple Silicon Media Engine — fast and high quality for podcast content
        vt_quality = config.get("processing", {}).get("videotoolbox_quality", 75)
        return ["-c:v", "h264_videotoolbox", "-q:v", str(vt_quality)]

    crf = config.get("processing", {}).get(crf_key, 22)
    preset = config.get("processing", {}).get("encode_preset", "medium")
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]


def get_lut_filter(config: dict) -> str:
    """Return the ffmpeg lut3d filter string if a LUT is configured, else empty string.

    Resolves relative paths against the project root (config/ directory's parent).
    """
    processing = config.get("processing", {})
    lut_path = processing.get("lut_path", "")
    if not lut_path:
        return ""

    lut_file = Path(lut_path)
    if not lut_file.is_absolute():
        # Resolve relative to project root
        project_root = Path(__file__).resolve().parent.parent
        lut_file = project_root / lut_file

    if not lut_file.exists():
        return ""

    interp = processing.get("lut_interpolation", "tetrahedral")
    # Escape path for ffmpeg filter — use backslash escaping, no quotes
    # (single quotes conflict with subtitles filter quoting)
    escaped = str(lut_file).replace("\\", "\\\\\\\\").replace(":", "\\\\:").replace("'", "\\\\'").replace(" ", "\\\\ ")
    return f"lut3d={escaped}:interp={interp}"
