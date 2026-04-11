"""Shared video encoder infrastructure — VideoToolbox detection, LUT support, argument selection."""

import functools
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("cascade")


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

    On Apple Silicon with VideoToolbox available, uses hardware H.264 encoding
    (10-20x faster, dedicated Media Engine). Set use_hardware_accel=false
    in config to force software encoding.

    All output is H.264 for universal platform compatibility (YouTube, Spotify,
    Apple Podcasts, Instagram, TikTok, X, LinkedIn, Facebook).

    VideoToolbox path: ["-c:v", "h264_videotoolbox", "-q:v", "45", "-profile:v", "high"]
    Software fallback: ["-c:v", "libx264", "-crf", "<value>", "-preset", "medium"]
    """
    use_hw = config.get("processing", {}).get("use_hardware_accel", True)

    if use_hw and has_videotoolbox():
        vt_quality = config.get("processing", {}).get("videotoolbox_quality", 45)
        return ["-c:v", "h264_videotoolbox", "-q:v", str(vt_quality), "-profile:v", "high"]

    crf = config.get("processing", {}).get(crf_key, 22)
    preset = config.get("processing", {}).get("encode_preset", "medium")
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]


def get_color_metadata_args() -> list:
    """Return ffmpeg args for BT.709 color metadata.

    After the D-Log M → Rec.709 LUT is applied, the output IS BT.709.
    These flags tell players the correct color interpretation, preventing
    washed-out or oversaturated playback on YouTube/Spotify/etc.
    """
    return [
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
    ]


def get_scale_filter(width: int, height: int) -> str:
    """Build a high-quality scale filter with lanczos + error-diffusion dither.

    Lanczos provides ~11% better VMAF quality than default bicubic for
    downscaling. Error-diffusion (Floyd-Steinberg) dither breaks up banding
    that would otherwise form when converting 10-bit D-Log M to 8-bit yuv420p.

    The accurate_rnd and full_chroma_int flags improve chroma sampling at
    minor CPU cost.
    """
    return (
        f"scale={width}:{height}"
        ":flags=lanczos+accurate_rnd+full_chroma_int"
        ":sws_dither=ed"
        ":param0=5"  # 5-tap lanczos for sharper detail preservation
    )


def get_video_polish_filters(config: dict) -> str:
    """Build the post-scale video enhancement filter chain.

    Order: hqdn3d (denoise) → cas (sharpen) → eq (color polish).
    Returns a comma-separated filter string ready to inject into the chain
    AFTER scale+format=yuv420p but BEFORE subtitles.

    All settings configurable via config["processing"]:
    - video_denoise: false to disable hqdn3d
    - video_sharpen: false to disable cas
    - video_polish: false to disable eq
    """
    processing = config.get("processing", {})
    parts = []

    # hqdn3d — gentle spatiotemporal denoise. Removes HEVC mosquito noise
    # without softening pore detail. Temporal=6 is safe for tripod-mounted
    # talking head where motion between frames is minimal.
    if processing.get("video_denoise", True):
        parts.append("hqdn3d=luma_spatial=1.5:chroma_spatial=1.5:luma_tmp=6:chroma_tmp=6")

    # cas — Contrast Adaptive Sharpening (AMD FidelityFX algorithm).
    # Better than unsharp: contrast-adaptive so flat skin areas are sharpened
    # less than high-contrast edges. Restores detail lost in downscaling.
    cas_strength = processing.get("video_sharpen_strength", 0.3)
    if processing.get("video_sharpen", True) and cas_strength > 0:
        parts.append(f"cas=strength={cas_strength}")

    # eq — subtle tonal polish after the LUT. The LUT already handles
    # contrast conversion (D-Log M → Rec.709), so we keep contrast at 1.0 by
    # default to avoid crushing blacks / blowing highlights. Only gentle
    # saturation and a tiny gamma lift for warmth.
    if processing.get("video_polish", True):
        contrast = processing.get("video_contrast", 1.0)
        saturation = processing.get("video_saturation", 1.04)
        gamma = processing.get("video_gamma", 1.01)
        parts.append(f"eq=contrast={contrast}:saturation={saturation}:gamma={gamma}")

    return ",".join(parts)


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
        logger.warning("LUT file not found: %s — rendering without color grading", lut_file)
        return ""

    interp = processing.get("lut_interpolation", "tetrahedral")
    # Escape path for ffmpeg filter syntax (matches escape_srt_path in lib/srt.py)
    escaped = str(lut_file).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"lut3d={escaped}:interp={interp}"
