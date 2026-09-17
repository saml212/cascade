"""Retired R2 watch-site commands fail before creating output."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ["links.generate", "links.episode_hub"])
def test_retired_generator_fails_with_actionable_migration(
    tmp_path: Path, module: str
) -> None:
    output = tmp_path / "old-links.html"
    result = subprocess.run(
        [sys.executable, "-m", module, "-o", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "R2 watch-site generator is retired" in result.stderr
    assert "thelocalpod.link" in result.stderr
    assert "GET /api/episodes/{episode_id}/watch-links" in result.stderr
    assert not output.exists()
