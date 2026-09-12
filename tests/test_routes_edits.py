"""Tests for source-clock edit API transitions."""

import importlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def edits_api(tmp_path, monkeypatch):
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(episodes_dir))

    import lib.paths
    import server.routes.edits as edits_mod

    importlib.reload(lib.paths)
    importlib.reload(edits_mod)
    app = FastAPI()
    app.include_router(edits_mod.router)
    yield TestClient(app), edits_mod, episodes_dir


def test_trim_end_prepares_verified_reuse_before_mutating_edits(edits_api, monkeypatch):
    client, edits_mod, episodes_dir = edits_api
    episode_dir = episodes_dir / "ep_test"
    episode_dir.mkdir()
    (episode_dir / "episode.json").write_text(
        json.dumps(
            {
                "episode_id": "ep_test",
                "longform_edits": [
                    {"type": "trim_start", "seconds": 1},
                    {"type": "trim_end", "seconds": 9},
                ],
            }
        )
    )
    observed = []

    def prepare(ep_dir):
        observed.append(json.loads((ep_dir / "episode.json").read_text()))
        return True

    monkeypatch.setattr(edits_mod, "_prepare_terminal_trim_reuse", prepare)

    response = client.post(
        "/api/episodes/ep_test/edits",
        json={"type": "trim_end", "seconds": 8, "reason": "remove post-roll"},
    )

    assert response.status_code == 200
    assert response.json()["longform_trim_reuse_prepared"] is True
    assert observed[0]["longform_edits"][-1]["seconds"] == 9
    assert response.json()["edits"][-1]["seconds"] == 8
