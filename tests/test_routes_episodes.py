"""Tests for episode API routes."""

import json
import os
import importlib
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    """Create a test client with a temp episodes directory."""
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(episodes_dir))

    # Force reimport of modules that read env at import time
    import lib.paths
    importlib.reload(lib.paths)

    import server.routes.episodes as ep_mod
    importlib.reload(ep_mod)

    import server.routes.clips as clips_mod
    importlib.reload(clips_mod)

    import server.routes.pipeline as pipe_mod
    importlib.reload(pipe_mod)

    import server.routes.chat as chat_mod
    importlib.reload(chat_mod)

    import server.routes.publish as pub_mod
    importlib.reload(pub_mod)

    import server.routes.analytics as analytics_mod
    importlib.reload(analytics_mod)

    import server.app as app_mod
    importlib.reload(app_mod)

    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)

    yield client, episodes_dir

    # Cleanup
    monkeypatch.delenv("CASCADE_OUTPUT_DIR", raising=False)
    importlib.reload(lib.paths)


def _create_episode(episodes_dir, episode_id, extra_data=None):
    """Create an episode directory with episode.json."""
    ep_dir = episodes_dir / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["shorts", "subtitles", "metadata", "qa"]:
        (ep_dir / sub).mkdir(exist_ok=True)
    data = {
        "episode_id": episode_id,
        "title": "Test {}".format(episode_id),
        "status": "ready_for_review",
        "source_path": "/tmp/source",
        "duration_seconds": 3600.0,
        "created_at": "2026-01-01T12:00:00+00:00",
        "clips": [],
        "pipeline": {"started_at": "2026-01-01T12:00:00+00:00", "completed_at": None, "agents_completed": []},
    }
    if extra_data:
        data.update(extra_data)
    with open(ep_dir / "episode.json", "w") as f:
        json.dump(data, f)
    return ep_dir


class TestListEpisodes:
    def test_empty_list(self, test_client):
        client, _ = test_client
        resp = client.get("/api/episodes/")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_episodes(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _create_episode(episodes_dir, "ep_002")
        resp = client.get("/api/episodes/")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestGetEpisode:
    def test_get_existing(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.get("/api/episodes/ep_001")
        assert resp.status_code == 200
        assert resp.json()["episode_id"] == "ep_001"

    def test_get_not_found(self, test_client):
        client, _ = test_client
        resp = client.get("/api/episodes/nonexistent")
        assert resp.status_code == 404


class TestCreateEpisode:
    def test_create(self, test_client):
        client, _ = test_client
        resp = client.post("/api/episodes/", json={"source_path": "/tmp/source"})
        assert resp.status_code == 200
        data = resp.json()
        assert "episode_id" in data
        assert data["status"] == "processing"


class TestUpdateEpisode:
    def test_update_title(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.patch("/api/episodes/ep_001", json={"title": "New Title"})
        assert resp.status_code == 200

        # Verify
        resp2 = client.get("/api/episodes/ep_001")
        assert resp2.json()["title"] == "New Title"


class TestDeleteEpisode:
    def test_delete_existing(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.delete("/api/episodes/ep_001")
        assert resp.status_code == 200
        assert not (episodes_dir / "ep_001").exists()

    def test_delete_not_found(self, test_client):
        client, _ = test_client
        resp = client.delete("/api/episodes/nonexistent")
        assert resp.status_code == 404


class TestCropConfig:
    def test_save_crop_config(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speaker_l_center_x": 480,
            "speaker_l_center_y": 540,
            "speaker_r_center_x": 1440,
            "speaker_r_center_y": 540,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "saved"

    def test_crop_frame_not_found(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.get("/api/episodes/ep_001/crop-frame")
        assert resp.status_code == 404
