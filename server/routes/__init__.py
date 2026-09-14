"""Shared HTTP helpers for Cascade route modules."""

from pathlib import Path

from fastapi import HTTPException


def require_episode_dir(root: Path, episode_id: str) -> Path:
    root = root.resolve()
    episode_dir = (root / episode_id).resolve()
    if episode_dir.parent != root or not (episode_dir / "episode.json").is_file():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return episode_dir
