"""Centralized path resolution for Cascade."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_project_root() -> Path:
    """Return the project root directory."""
    return PROJECT_ROOT


def get_episodes_dir() -> Path:
    """Return the episodes directory.

    Checks CASCADE_OUTPUT_DIR env var first, falls back to config.toml output_dir,
    then to PROJECT_ROOT / "output" / "episodes".
    """
    env_dir = os.getenv("CASCADE_OUTPUT_DIR", "")
    if env_dir:
        return Path(env_dir)
    return PROJECT_ROOT / "output" / "episodes"
