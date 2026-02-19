"""Tests for lib.paths module."""

import os
import pytest
from pathlib import Path


def test_get_project_root():
    from lib.paths import get_project_root
    root = get_project_root()
    assert root.is_dir()
    assert (root / "agents").is_dir()


def test_get_episodes_dir_default():
    from lib.paths import get_episodes_dir
    # Clear env var if set
    old = os.environ.pop("CASCADE_OUTPUT_DIR", None)
    try:
        import importlib
        import lib.paths
        importlib.reload(lib.paths)
        from lib.paths import get_episodes_dir as ged
        ep_dir = ged()
        assert str(ep_dir).endswith("episodes")
    finally:
        if old:
            os.environ["CASCADE_OUTPUT_DIR"] = old
            import importlib
            import lib.paths
            importlib.reload(lib.paths)


def test_get_episodes_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(tmp_path))
    import importlib
    import lib.paths
    importlib.reload(lib.paths)
    from lib.paths import get_episodes_dir
    result = get_episodes_dir()
    assert result == tmp_path
    # Clean up
    monkeypatch.delenv("CASCADE_OUTPUT_DIR", raising=False)
    importlib.reload(lib.paths)


def test_get_project_root_contains_config():
    from lib.paths import get_project_root
    root = get_project_root()
    assert (root / "config" / "config.toml").exists()


def test_get_episodes_dir_returns_path():
    from lib.paths import get_episodes_dir
    result = get_episodes_dir()
    assert isinstance(result, Path)
