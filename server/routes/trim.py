"""Trim endpoint — trim source and longform video files."""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes/{episode_id}", tags=["trim"])

EPISODES_DIR = get_episodes_dir()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TrimRequest(BaseModel):
    trim_start_seconds: float = 0.0
    trim_end_seconds: float = 0.0


class TrimResponse(BaseModel):
    new_duration: float
    backup_path: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _episode_dir(episode_id: str) -> Path:
    ep_dir = EPISODES_DIR / episode_id
    if not ep_dir.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return ep_dir


def _load_json_safe(path: Path) -> Optional[dict]:
    """Load a JSON file if it exists, otherwise return None."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _fix_track_durations(mp4_path: Path) -> Path:
    """Ensure audio and video tracks have matching durations.

    Stream-copy trims cut video at keyframes but audio precisely, leaving a
    duration mismatch that platforms like Spotify reject.  If the tracks
    differ by more than 50 ms, re-mux with -t set to the shorter duration.
    Returns the (possibly replaced) output path.
    """
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", str(mp4_path),
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        streams = json.loads(result.stdout).get("streams", [])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return mp4_path  # Can't probe — return as-is

    durations = {}
    for s in streams:
        if "duration" in s:
            durations[s["codec_type"]] = float(s["duration"])

    v_dur = durations.get("video")
    a_dur = durations.get("audio")
    if v_dur is None or a_dur is None:
        return mp4_path

    diff = abs(v_dur - a_dur)
    if diff <= 0.05:
        return mp4_path  # Close enough

    shorter = min(v_dur, a_dur)
    fixed_path = mp4_path.with_suffix(".fixed.mp4")
    fix_cmd = [
        "ffmpeg", "-y",
        "-i", str(mp4_path),
        "-t", str(shorter),
        "-c", "copy",
        "-use_editlist", "0",
        "-movflags", "+faststart",
        str(fixed_path),
    ]
    try:
        subprocess.run(fix_cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        return mp4_path  # Fix failed — return original

    os.remove(str(mp4_path))
    shutil.move(str(fixed_path), str(mp4_path))
    return mp4_path


# ---------------------------------------------------------------------------
# Trim endpoint
# ---------------------------------------------------------------------------

@router.post("/trim")
async def trim_episode(episode_id: str, req: TrimRequest) -> dict:
    """Trim the source_merged.mp4 by cutting off the beginning and/or end.

    Creates a backup of the original before replacing it.
    """
    logger.info("POST /api/episodes/%s/trim start=%.1f end=%.1f",
                episode_id, req.trim_start_seconds, req.trim_end_seconds)
    ep_dir = _episode_dir(episode_id)
    source_path = ep_dir / "source_merged.mp4"

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="source_merged.mp4 not found")

    # Probe current duration
    try:
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(source_path),
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(probe_result.stdout)
        current_duration = float(probe_data["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError, ValueError) as e:
        logger.error("Failed to probe source file for %s: %s", episode_id, e)
        raise HTTPException(status_code=500, detail=f"Could not probe source file: {e}")

    trim_start = req.trim_start_seconds
    trim_end = req.trim_end_seconds if req.trim_end_seconds > 0 else current_duration

    if trim_start < 0 or trim_end < 0:
        raise HTTPException(status_code=400, detail="Trim values must be non-negative")
    if trim_end > current_duration:
        trim_end = current_duration
    if trim_start >= trim_end:
        raise HTTPException(status_code=400, detail="Trim start must be before trim end")

    new_duration = trim_end - trim_start

    # Render trimmed version
    trimmed_path = ep_dir / "source_merged_trimmed.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(trim_start),
        "-to", str(trim_end),
        "-i", str(source_path),
        "-c", "copy",
        "-shortest",
        "-avoid_negative_ts", "make_zero",
        "-use_editlist", "0",
        "-movflags", "+faststart",
        str(trimmed_path),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg trim failed for %s: %s", episode_id, e.stderr[:500])
        raise HTTPException(status_code=500, detail=f"ffmpeg trim failed: {e.stderr[:500]}")

    # Backup original and replace
    backup_path = ep_dir / "source_merged_original.mp4"
    if not backup_path.exists():
        # Only backup if we haven't already (first trim)
        shutil.move(str(source_path), str(backup_path))
    else:
        # Subsequent trims — just remove the current version
        os.remove(str(source_path))

    shutil.move(str(trimmed_path), str(source_path))

    # Trim longform.mp4 the same way if it exists
    longform_path = ep_dir / "longform.mp4"
    if longform_path.exists():
        longform_backup = ep_dir / "longform_original.mp4"
        longform_trimmed = ep_dir / "longform_trimmed.mp4"
        lf_cmd = [
            "ffmpeg", "-y",
            "-ss", str(trim_start),
            "-to", str(trim_end),
            "-i", str(longform_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-use_editlist", "0",
            "-movflags", "+faststart",
            str(longform_trimmed),
        ]
        try:
            subprocess.run(lf_cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            logger.error("ffmpeg longform trim failed for %s: %s", episode_id, e.stderr[:500])
            raise HTTPException(status_code=500, detail=f"ffmpeg longform trim failed: {e.stderr[:500]}")

        # Verify audio/video track durations match; fix if they diverge
        longform_trimmed = _fix_track_durations(longform_trimmed)

        if not longform_backup.exists():
            shutil.move(str(longform_path), str(longform_backup))
        else:
            os.remove(str(longform_path))
        shutil.move(str(longform_trimmed), str(longform_path))

    # Update episode.json with new duration
    episode_file = ep_dir / "episode.json"
    if episode_file.exists():
        with open(episode_file) as f:
            episode = json.load(f)
        episode["duration_seconds"] = new_duration
        with open(episode_file, "w") as f:
            json.dump(episode, f, indent=2)

    logger.info("Trim complete for %s: new_duration=%.1f", episode_id, new_duration)
    return {
        "new_duration": new_duration,
        "backup_path": str(backup_path),
    }
