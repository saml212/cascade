"""Shared video encoder infrastructure — VideoToolbox detection, LUT support, argument selection."""

import functools
import logging
import math
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("cascade")

_ENCODING_DEFAULTS = {
    "longform": {
        "video_bitrate": "12M",
        "video_max_bitrate": "16M",
        "video_buffer_size": "24M",
        "audio_bitrate": "192k",
    },
    "shorts": {
        "video_bitrate": "10M",
        "video_max_bitrate": "14M",
        "video_buffer_size": "20M",
        "audio_bitrate": "192k",
    },
}


def parse_bitrate(value: str | float, name: str) -> int:
    """Parse an ffmpeg-style bitrate into positive bits per second."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a positive bitrate")
    if isinstance(value, (int, float)):
        bitrate = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*", value)
        if not match:
            raise ValueError(f"Invalid {name}: {value!r}")
        multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[
            match.group(2).lower()
        ]
        bitrate = float(match.group(1)) * multiplier
    else:
        raise TypeError(f"Invalid {name}: {value!r}")
    if not math.isfinite(bitrate) or bitrate <= 0:
        raise ValueError(f"{name} must be a positive finite bitrate")
    return round(bitrate)


def _format_bitrate(bits_per_second: int) -> str:
    if bits_per_second % 1_000_000 == 0:
        return f"{bits_per_second // 1_000_000}M"
    if bits_per_second % 1_000 == 0:
        return f"{bits_per_second // 1_000}k"
    return str(bits_per_second)


def get_video_encoding_policy(config: dict, profile: str = "longform") -> dict:
    """Resolve the bounded bitrate policy shared by encoding and preflight."""
    if profile not in _ENCODING_DEFAULTS:
        raise ValueError(f"Unknown video encoding profile: {profile}")
    defaults = _ENCODING_DEFAULTS[profile]
    processing = config.get("processing", {})
    prefix = "shorts_" if profile == "shorts" else ""
    values = {
        "video_bitrate": processing.get(
            f"{prefix}video_bitrate", defaults["video_bitrate"]
        ),
        "video_max_bitrate": processing.get(
            f"{prefix}video_max_bitrate", defaults["video_max_bitrate"]
        ),
        "video_buffer_size": processing.get(
            f"{prefix}video_buffer_size", defaults["video_buffer_size"]
        ),
        "audio_bitrate": processing.get(
            f"{prefix}audio_bitrate", defaults["audio_bitrate"]
        ),
    }
    parsed = {key: parse_bitrate(value, key) for key, value in values.items()}
    if parsed["video_max_bitrate"] < parsed["video_bitrate"]:
        raise ValueError("video_max_bitrate must be at least video_bitrate")
    if parsed["video_buffer_size"] < parsed["video_max_bitrate"]:
        raise ValueError("video_buffer_size must be at least video_max_bitrate")
    return {
        "profile": profile,
        **{key: _format_bitrate(value) for key, value in parsed.items()},
        **{f"{key}_bps": value for key, value in parsed.items()},
    }


@functools.lru_cache(maxsize=1)
def has_videotoolbox() -> bool:
    """Check if h264_videotoolbox encoder is available. Result is cached."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return "h264_videotoolbox" in result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def get_video_encoder_args(config: dict, profile: str = "longform") -> list[str]:
    """Return H.264 arguments constrained by the shared bitrate policy."""
    use_hw = config.get("processing", {}).get("use_hardware_accel", True)
    policy = get_video_encoding_policy(config, profile)

    if use_hw and has_videotoolbox():
        encoder = ["-c:v", "h264_videotoolbox", "-profile:v", "high"]
    else:
        preset = config.get("processing", {}).get("encode_preset", "medium")
        encoder = ["-c:v", "libx264", "-preset", str(preset), "-profile:v", "high"]
    return [
        *encoder,
        "-b:v",
        policy["video_bitrate"],
        "-maxrate",
        policy["video_max_bitrate"],
        "-bufsize",
        policy["video_buffer_size"],
    ]


def get_color_metadata_args() -> list:
    """Return ffmpeg args for BT.709 color metadata.

    After the D-Log M → Rec.709 LUT is applied, the output IS BT.709.
    These flags tell players the correct color interpretation, preventing
    washed-out or oversaturated playback on YouTube/Spotify/etc.
    """
    return [
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
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
        parts.append(
            "hqdn3d=luma_spatial=1.5:chroma_spatial=1.5:luma_tmp=6:chroma_tmp=6"
        )

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


def resolve_lut_path(config: dict) -> Path | None:
    """Resolve the configured LUT path, returning None when it is unavailable."""
    lut_path = config.get("processing", {}).get("lut_path", "")
    if not lut_path:
        return None

    lut_file = Path(lut_path).expanduser()
    if not lut_file.is_absolute():
        project_root = Path(__file__).resolve().parent.parent
        lut_file = project_root / lut_file
    if not lut_file.exists():
        logger.warning(
            "LUT file not found: %s — rendering without color grading", lut_file
        )
        return None
    return lut_file.resolve()


def get_lut_filter(config: dict) -> str:
    """Return the ffmpeg lut3d filter string if a LUT is configured."""
    lut_file = resolve_lut_path(config)
    if lut_file is None:
        return ""

    interp = config.get("processing", {}).get("lut_interpolation", "tetrahedral")
    # Escape path for ffmpeg filter syntax (matches escape_srt_path in lib/srt.py)
    escaped = (
        str(lut_file).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    )
    return f"lut3d={escaped}:interp={interp}"
