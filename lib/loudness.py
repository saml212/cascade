"""Shared loudness policy, ffmpeg filters, and encoded-output verification."""

import json
import logging
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default recorded by the low-level measurement helper. Delivery verification
# replaces this with the target from the active profile policy.
_TARGET_LUFS = -16

# Regexes to extract the summary block values from ebur128 stderr output.
# We match the LAST occurrence of each because ffmpeg prints per-moment
# readings throughout the file followed by one final summary block.
_RE_INTEGRATED = re.compile(r"I:\s+(-?[\d.]+)\s+LUFS")
_RE_LRA = re.compile(r"LRA:\s+([\d.]+)\s+LU")
_RE_PEAK = re.compile(r"Peak:\s+(-?[\d.]+)\s+dBFS")
_LOUDNORM_JSON = re.compile(r'\{[^{}]*"input_i"[^{}]*\}', re.DOTALL)
LOUDNESS_POLICY_SCHEMA = "cascade.delivery-loudness/v1"


def delivery_loudness_policy(config: dict, profile: str) -> dict:
    """Return the measured loudness limits for an encoded delivery artifact."""
    if profile not in {"longform", "shorts"}:
        raise ValueError(f"Unknown delivery audio profile: {profile}")
    processing = config.get("processing", {})
    target_lufs = float(processing.get("audio_target_lufs", -16))
    target_true_peak = float(processing.get("audio_target_tp", -1.5))
    target_lra = float(processing.get("audio_target_lra", 11))
    tolerance = float(processing.get("audio_loudness_tolerance_lu", 1.0))
    max_true_peak = float(
        processing.get("audio_max_true_peak_dbfs", min(-0.5, target_true_peak + 0.5))
    )
    values = (target_lufs, target_true_peak, target_lra, tolerance, max_true_peak)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Delivery loudness policy must contain finite values")
    if not -70 <= target_lufs <= -5:
        raise ValueError("Audio loudness target must be between -70 and -5 LUFS")
    if not -9 <= target_true_peak <= 0:
        raise ValueError("Audio true-peak target must be between -9 and 0 dBFS")
    if not 1 <= target_lra <= 50:
        raise ValueError("Audio loudness-range target must be between 1 and 50 LU")
    if tolerance < 0:
        raise ValueError("Delivery loudness tolerance cannot be negative")
    if max_true_peak > 0:
        raise ValueError("Delivery true-peak limit cannot exceed 0 dBFS")
    if target_true_peak > max_true_peak:
        raise ValueError("Audio target true peak cannot exceed the delivery peak limit")
    return {
        "schema": LOUDNESS_POLICY_SCHEMA,
        "profile": profile,
        "target_lufs": target_lufs,
        "target_true_peak_dbfs": target_true_peak,
        "target_loudness_range_lu": target_lra,
        "integrated_tolerance_lu": tolerance,
        "max_true_peak_dbfs": max_true_peak,
    }


def parse_loudnorm_analysis(stderr: str) -> dict:
    """Parse strict pass-one measurements for ffmpeg's linear loudnorm mode."""
    matches = _LOUDNORM_JSON.findall(stderr or "")
    if not matches:
        raise RuntimeError("Loudness normalization analysis produced no measurements")
    try:
        data = json.loads(matches[-1])
        required = ("input_i", "input_lra", "input_tp", "input_thresh", "target_offset")
        parsed = {key: float(data[key]) for key in required}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Loudness normalization analysis is incomplete") from exc
    if not all(math.isfinite(value) for value in parsed.values()):
        raise RuntimeError("Loudness normalization analysis is non-finite")
    return parsed


def loudnorm_filter(policy: dict, measurements: dict | None = None) -> str:
    """Build one loudnorm pass from a shared delivery policy."""
    value = (
        f"loudnorm=I={policy['target_lufs']}:TP={policy['target_true_peak_dbfs']}:"
        f"LRA={policy['target_loudness_range_lu']}"
    )
    if measurements is None:
        return value + ":print_format=json"
    return (
        value
        + f":measured_I={measurements['input_i']}"
        + f":measured_LRA={measurements['input_lra']}"
        + f":measured_TP={measurements['input_tp']}"
        + f":measured_thresh={measurements['input_thresh']}"
        + f":offset={measurements['target_offset']}"
        + ":linear=true:print_format=summary"
    )


def loudness_status(measurement: dict | None, policy: dict) -> dict:
    """Classify a final encoded artifact without confusing missing proof with pass."""
    if not isinstance(measurement, dict):
        return {
            "status": "missing",
            "safe": False,
            "policy": policy,
            "error": "Encoded output loudness was not measured",
        }
    try:
        integrated = float(measurement["integrated_lufs"])
        true_peak = float(measurement["true_peak_dbfs"])
    except (KeyError, TypeError, ValueError):
        return {
            "status": "error",
            "safe": False,
            "policy": policy,
            "measurement": measurement,
            "error": "Encoded output loudness measurement is incomplete",
        }
    if not math.isfinite(integrated) or not math.isfinite(true_peak):
        return {
            "status": "error",
            "safe": False,
            "policy": policy,
            "measurement": measurement,
            "error": "Encoded output loudness measurement is non-finite",
        }
    problems = []
    delta = abs(integrated - policy["target_lufs"])
    if delta > policy["integrated_tolerance_lu"]:
        problems.append(
            f"integrated loudness {integrated:.1f} LUFS is outside "
            f"{policy['target_lufs']:.1f} ±{policy['integrated_tolerance_lu']:.1f} LU"
        )
    if true_peak > policy["max_true_peak_dbfs"]:
        problems.append(
            f"true peak {true_peak:.1f} dBFS exceeds "
            f"{policy['max_true_peak_dbfs']:.1f} dBFS"
        )
    return {
        "status": "failed" if problems else "passed",
        "safe": not problems,
        "policy": policy,
        "measurement": measurement,
        "integrated_delta_lu": round(delta, 3),
        "errors": problems,
    }


def require_delivery_loudness(measurement: dict | None, policy: dict) -> dict:
    """Return a verified measurement or raise before an artifact is installed."""
    normalized = (
        {
            **measurement,
            "target_lufs": policy["target_lufs"],
            "target_true_peak_dbfs": policy["target_true_peak_dbfs"],
        }
        if isinstance(measurement, dict)
        else measurement
    )
    status = loudness_status(normalized, policy)
    if not status["safe"]:
        detail = "; ".join(status.get("errors", [])) or status.get(
            "error", "Encoded output loudness failed verification"
        )
        raise RuntimeError(f"Encoded output loudness failed verification: {detail}")
    return {
        **normalized,
        "verification": status,
    }


def measure_loudness(input_path: Path, *, ffmpeg_bin: str = "ffmpeg") -> dict | None:
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
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
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
            timeout=600,
            check=False,
        )
    except FileNotFoundError:
        logger.error("ffmpeg not found — cannot measure loudness")
        return None
    except subprocess.TimeoutExpired:
        logger.error("Loudness measurement timed out for %s", input_path)
        return None

    # ebur128 writes everything (including the summary) to stderr
    stderr = result.stderr

    if result.returncode != 0:
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

    if not all(
        math.isfinite(value)
        for value in (
            integrated_lufs,
            loudness_range_lu,
            true_peak_dbfs,
        )
    ):
        logger.warning("Non-finite ebur128 values for %s", input_path)
        return None

    measured_at = datetime.now(timezone.utc).isoformat()
    return {
        "integrated_lufs": integrated_lufs,
        "true_peak_dbfs": true_peak_dbfs,
        "loudness_range_lu": loudness_range_lu,
        "target_lufs": _TARGET_LUFS,
        "measured_at": measured_at,
    }
