"""Loudness measurement via ffmpeg ebur128.

Measurement is a separate concern from the audio_enhance filter chain —
this module only measures, it never modifies audio.
"""

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Cascade loudness target (EBU R128 broadcast standard, widely used by podcast
# platforms). Hardcoded for now; plumb from config if needed in the future.
_TARGET_LUFS = -14

# Regexes to extract the summary block values from ebur128 stderr output.
# We match the LAST occurrence of each because ffmpeg prints per-moment
# readings throughout the file followed by one final summary block.
_RE_INTEGRATED = re.compile(r"I:\s+(-?[\d.]+)\s+LUFS")
_RE_LRA = re.compile(r"LRA:\s+([\d.]+)\s+LU")
_RE_PEAK = re.compile(r"Peak:\s+(-?[\d.]+)\s+dBFS")


def measure_loudness(input_path: Path) -> dict | None:
    """Run ffmpeg ebur128 and parse the integrated summary block.

    Returns:
        {
            "integrated_lufs": float,
            "true_peak_dbfs": float,
            "loudness_range_lu": float,
            "target_lufs": -14,
            "measured_at": "<iso utc now>",
        }
        or None if measurement failed (no audio stream, ffmpeg error, parse miss).

    Note: ebur128 on a 90-minute file takes 30-60 seconds on Apple Silicon.
    This is expected and acceptable at end-of-render.
    """
    input_path = Path(input_path)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(input_path),
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.error("ffmpeg not found — cannot measure loudness")
        return None

    # ebur128 writes everything (including the summary) to stderr
    stderr = result.stderr

    # Non-zero returncode is expected: ffmpeg exits 1 when output is /dev/null
    # equivalent ("-f null -"). Only treat it as a hard failure when stderr
    # contains no ebur128 output at all (e.g. no audio stream in the file).
    if "ebur128" not in stderr and result.returncode != 0:
        logger.warning(
            "ffmpeg ebur128 failed for %s (rc=%d): %s",
            input_path,
            result.returncode,
            stderr[:300],
        )
        return None

    # Extract LAST occurrence of each metric (the summary block comes last)
    integrated_matches = _RE_INTEGRATED.findall(stderr)
    lra_matches = _RE_LRA.findall(stderr)
    peak_matches = _RE_PEAK.findall(stderr)

    if not integrated_matches or not lra_matches or not peak_matches:
        logger.warning("ebur128 summary not found in ffmpeg output for %s", input_path)
        return None

    try:
        integrated_lufs = float(integrated_matches[-1])
        loudness_range_lu = float(lra_matches[-1])
        true_peak_dbfs = float(peak_matches[-1])
    except (ValueError, IndexError) as exc:
        logger.warning("Failed to parse ebur128 values for %s: %s", input_path, exc)
        return None

    measured_at = datetime.now(timezone.utc).isoformat()
    return {
        "integrated_lufs": integrated_lufs,
        "true_peak_dbfs": true_peak_dbfs,
        "loudness_range_lu": loudness_range_lu,
        "target_lufs": _TARGET_LUFS,
        "measured_at": measured_at,
    }
