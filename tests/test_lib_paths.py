"""Tests for lib.paths module — resolve_path() and get_episodes_dir()."""

import os
import importlib
import pytest
from pathlib import Path


def test_get_project_root():
    from lib.paths import get_project_root
    root = get_project_root()
    assert root.is_dir()
    assert (root / "agents").is_dir()


def test_get_project_root_contains_config():
    from lib.paths import get_project_root
    root = get_project_root()
    assert (root / "config" / "config.toml").exists()


def test_get_episodes_dir_returns_path():
    from lib.paths import get_episodes_dir
    result = get_episodes_dir()
    assert isinstance(result, Path)


def test_get_episodes_dir_default():
    from lib.paths import get_episodes_dir
    # Clear env var if set
    old = os.environ.pop("CASCADE_OUTPUT_DIR", None)
    try:
        import lib.paths
        importlib.reload(lib.paths)
        from lib.paths import get_episodes_dir as ged
        ep_dir = ged()
        assert str(ep_dir).endswith("episodes")
    finally:
        if old:
            os.environ["CASCADE_OUTPUT_DIR"] = old
            import lib.paths
            importlib.reload(lib.paths)


def test_get_episodes_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(tmp_path))
    import lib.paths
    importlib.reload(lib.paths)
    from lib.paths import get_episodes_dir
    result = get_episodes_dir()
    assert result == tmp_path
    # Clean up
    monkeypatch.delenv("CASCADE_OUTPUT_DIR", raising=False)
    importlib.reload(lib.paths)


class TestResolvePath:
    """Test resolve_path() with various path configurations."""

    def test_relative_path_resolves_under_project_root(self):
        from lib.paths import resolve_path, PROJECT_ROOT
        result = resolve_path("relative/dir", "fallback")
        assert result == PROJECT_ROOT / "fallback"

    def test_absolute_path_with_existing_parent(self, tmp_path):
        """Absolute path with existing parent should be returned directly."""
        from lib.paths import resolve_path
        configured = str(tmp_path / "episodes")
        result = resolve_path(configured, "fallback")
        assert result == Path(configured)

    def test_volume_path_falls_back_when_volume_missing(self):
        """A /Volumes/... path should fall back if the volume doesn't exist."""
        from lib.paths import resolve_path, PROJECT_ROOT
        result = resolve_path("/Volumes/FakeVolume123/cascade/episodes", "episodes")
        assert result == PROJECT_ROOT / "episodes"

    def test_env_var_override_for_episodes(self, tmp_path, monkeypatch):
        """CASCADE_OUTPUT_DIR env var should override configured path."""
        monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(tmp_path))
        from lib.paths import resolve_path
        result = resolve_path("/some/configured/path", "episodes")
        assert result == tmp_path
        monkeypatch.delenv("CASCADE_OUTPUT_DIR", raising=False)

    def test_env_var_override_for_work(self, tmp_path, monkeypatch):
        """CASCADE_WORK_DIR should override for work fallback."""
        monkeypatch.setenv("CASCADE_WORK_DIR", str(tmp_path / "work"))
        from lib.paths import resolve_path
        result = resolve_path("/some/path", "work")
        assert result == tmp_path / "work"
        monkeypatch.delenv("CASCADE_WORK_DIR", raising=False)

    def test_env_var_override_for_backup(self, tmp_path, monkeypatch):
        """CASCADE_BACKUP_DIR should override for backup fallback."""
        monkeypatch.setenv("CASCADE_BACKUP_DIR", str(tmp_path / "backup"))
        from lib.paths import resolve_path
        result = resolve_path("/some/path", "backup")
        assert result == tmp_path / "backup"
        monkeypatch.delenv("CASCADE_BACKUP_DIR", raising=False)

    def test_no_env_var_and_existing_absolute_path(self, tmp_path, monkeypatch):
        """Absolute path with existing parent and no env var override should be returned."""
        monkeypatch.delenv("CASCADE_OUTPUT_DIR", raising=False)
        monkeypatch.delenv("CASCADE_WORK_DIR", raising=False)
        configured = str(tmp_path / "my_output")
        from lib.paths import resolve_path
        result = resolve_path(configured, "something_else")
        assert result == Path(configured)

    def test_volume_path_used_when_volume_exists(self):
        """A /Volumes/ path should fall back if volume is not mounted."""
        from lib.paths import resolve_path, PROJECT_ROOT
        result = resolve_path("/Volumes/DEFINITELY_NOT_MOUNTED_XYZ/cascade", "episodes")
        assert result == PROJECT_ROOT / "episodes"

    def test_non_volume_absolute_path_with_missing_parent(self):
        """Non-volume absolute path with non-existing parent should fall back."""
        from lib.paths import resolve_path, PROJECT_ROOT
        result = resolve_path("/definitely/not/a/real/path", "fallback_dir")
        assert result == PROJECT_ROOT / "fallback_dir"
