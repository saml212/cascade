"""The retired alternate link-page command points to the canonical hub."""

import subprocess
import sys
from pathlib import Path


def test_retired_generator_fails_with_actionable_migration(tmp_path: Path) -> None:
    output = tmp_path / "old-links.html"
    result = subprocess.run(
        [sys.executable, "-m", "links.generate", "-o", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "standalone show-level links page was retired" in result.stderr
    assert "links.episode_hub prepare" in result.stderr
    assert "links.episode_hub upload" in result.stderr
    assert not output.exists()
