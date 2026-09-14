"""Security contract for shared route path validation."""

import pytest
from fastapi import HTTPException

from server.routes import require_episode_dir


def test_require_episode_dir_uses_root_and_rejects_escape(tmp_path):
    root = tmp_path / "episodes"
    episode = root / "ep_test"
    episode.mkdir(parents=True)
    (episode / "episode.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "episode.json").write_text("{}")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    assert require_episode_dir(root, "ep_test") == episode.resolve()

    for episode_id in ("missing", "../outside", "linked"):
        with pytest.raises(HTTPException) as caught:
            require_episode_dir(root, episode_id)
        assert caught.value.status_code == 404
        assert caught.value.detail == f"Episode {episode_id} not found"
