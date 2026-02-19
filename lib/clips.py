"""Clip normalization and I/O utilities."""

import json
from pathlib import Path
from typing import List


def normalize_clip(clip: dict) -> dict:
    """Ensure clips have both start/end and start_seconds/end_seconds.

    Bi-directional: fills in whichever pair is missing.
    """
    if "start_seconds" in clip and "start" not in clip:
        clip["start"] = clip["start_seconds"]
    if "end_seconds" in clip and "end" not in clip:
        clip["end"] = clip["end_seconds"]
    if "start" in clip and "start_seconds" not in clip:
        clip["start_seconds"] = clip["start"]
    if "end" in clip and "end_seconds" not in clip:
        clip["end_seconds"] = clip["end"]
    return clip


def load_clips(episode_dir: Path) -> List[dict]:
    """Load clips from clips.json in episode directory.

    Returns empty list if file doesn't exist.
    """
    clips_file = episode_dir / "clips.json"
    if not clips_file.exists():
        return []
    with open(clips_file) as f:
        data = json.load(f)
    clips = data.get("clips", data) if isinstance(data, dict) else data
    return [normalize_clip(c) for c in clips]


def save_clips(episode_dir: Path, clips: list):
    """Save clips list to clips.json in episode directory."""
    clips_file = episode_dir / "clips.json"
    clips_file.parent.mkdir(parents=True, exist_ok=True)
    with open(clips_file, "w") as f:
        json.dump({"clips": clips}, f, indent=2)
