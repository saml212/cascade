"""Centralized path resolution for Cascade."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_project_root() -> Path:
    """Return the project root directory."""
    return PROJECT_ROOT


def resolve_path(configured_path: str, local_fallback: str) -> Path:
    """Resolve a configured path with automatic local fallback.

    If the configured path is absolute and its parent volume exists, use it.
    Otherwise fall back to a local directory under PROJECT_ROOT.

    This lets users with an external SSD use it, while everyone else
    gets a working local setup out of the box.

    Args:
        configured_path: Path from config.toml (may be absolute or relative).
        local_fallback: Relative path under PROJECT_ROOT to use as fallback.
    """
    # Environment variable overrides always win
    env_map = {
        "episodes": "CASCADE_OUTPUT_DIR",
        "work": "CASCADE_WORK_DIR",
        "backup": "CASCADE_BACKUP_DIR",
    }
    for key, env_var in env_map.items():
        if key in local_fallback:
            env_val = os.getenv(env_var, "")
            if env_val:
                return Path(env_val)

    p = Path(configured_path)

    # Absolute path — check if the volume/parent exists
    if p.is_absolute():
        # For paths like /Volumes/SSD/cascade/episodes, check the volume
        parts = p.parts
        if len(parts) >= 3 and parts[1] == "Volumes":
            volume = Path(parts[0]) / parts[1] / parts[2]
            if volume.exists():
                return p
            # Volume not mounted — fall back to local
        elif p.parent.exists():
            return p

    # Relative path or missing volume — resolve under project root
    return PROJECT_ROOT / local_fallback


def get_episodes_dir() -> Path:
    """Return the episodes directory.

    Checks CASCADE_OUTPUT_DIR env var first, falls back to config.toml output_dir,
    then to PROJECT_ROOT / "episodes".
    """
    env_dir = os.getenv("CASCADE_OUTPUT_DIR", "")
    if env_dir:
        return Path(env_dir)
    return PROJECT_ROOT / "episodes"
