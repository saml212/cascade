"""API tests for video-feed dry-run and explicit publication dispatch."""

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.routes.video_feed as video_feed_routes


@pytest.fixture
def video_feed_api(tmp_path, monkeypatch):
    episodes = tmp_path / "episodes"
    episode_dir = episodes / "ep_test"
    episode_dir.mkdir(parents=True)
    episode = {
        "episode_id": "ep_test",
        "status": "ready_for_review",
        "source_path": "/source",
    }
    (episode_dir / "episode.json").write_text(json.dumps(episode))
    monkeypatch.setattr(video_feed_routes, "EPISODES_DIR", episodes)
    video_feed_routes._running.clear()
    app = FastAPI()
    app.include_router(video_feed_routes.router)
    yield TestClient(app), episode_dir
    video_feed_routes._running.clear()


def test_prepare_is_api_accessible_without_release_mutation(video_feed_api):
    client, episode_dir = video_feed_api
    before = (episode_dir / "episode.json").read_bytes()
    prepared = {
        "status": "prepared",
        "dry_run": True,
        "feed": {"key": "feed-video.xml"},
    }
    with patch.object(video_feed_routes, "VideoFeedAgent") as agent_class:
        agent_class.return_value.prepare.return_value = prepared
        response = client.post("/api/episodes/ep_test/delivery/video-feed/prepare")

    assert response.status_code == 200
    assert response.json() == prepared
    assert (episode_dir / "episode.json").read_bytes() == before


def test_publish_dispatches_only_video_feed_after_preflight(video_feed_api):
    client, _ = video_feed_api
    with (
        patch.object(video_feed_routes, "VideoFeedAgent") as agent_class,
        patch.object(video_feed_routes, "_start_pipeline_thread") as start_pipeline,
    ):
        agent_class.return_value._current_inputs.return_value = {"ready": True}
        response = client.post("/api/episodes/ep_test/delivery/video-feed/publish")

    assert response.status_code == 202
    assert response.json()["publication_agents"] == ["video_feed"]
    start_pipeline.assert_called_once_with("ep_test", "/source", ["video_feed"])


def test_publish_guard_failure_dispatches_nothing(video_feed_api):
    client, _ = video_feed_api
    with (
        patch.object(video_feed_routes, "VideoFeedAgent") as agent_class,
        patch.object(video_feed_routes, "_start_pipeline_thread") as start_pipeline,
    ):
        agent_class.return_value._current_inputs.side_effect = RuntimeError(
            "Current revision-bound publish approval is required"
        )
        response = client.post("/api/episodes/ep_test/delivery/video-feed/publish")

    assert response.status_code == 409
    assert "publish approval" in response.json()["detail"]
    start_pipeline.assert_not_called()
