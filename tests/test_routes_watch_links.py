"""Read-only API access to current exact-episode watch links."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from links import episode_hub
from server.routes import watch_links


@pytest.fixture
def watch_links_api(tmp_path, monkeypatch):
    episodes = tmp_path / "episodes"
    episode_dir = episodes / "ep_test"
    episode_dir.mkdir(parents=True)
    episode_file = episode_dir / "episode.json"
    episode_file.write_text(
        json.dumps({"episode_id": "ep_test", "title": "Guest & Host"})
    )
    config = {
        "podcast": {
            "title": "Local",
            "r2": {"public_url": "https://public.example"},
            "links": {},
        }
    }
    monkeypatch.setattr(watch_links, "EPISODES_DIR", episodes)
    monkeypatch.setattr(watch_links, "load_config", lambda: config)
    monkeypatch.setattr(
        episode_hub,
        "current_funnel_urls_for_episode",
        lambda *_args: {
            "youtube": "https://youtube.example/watch?v=1&list=2",
            "spotify": "https://open.spotify.com/episode/one",
        },
    )
    app = FastAPI()
    app.include_router(watch_links.router)
    return TestClient(app), episode_file


def test_watch_links_and_html_are_current_and_read_only(watch_links_api):
    client, episode_file = watch_links_api
    before = episode_file.read_bytes()

    response = client.get("/api/episodes/ep_test/watch-links")
    preview = client.get("/api/episodes/ep_test/watch-page")

    assert response.status_code == 200
    assert response.json()["landing_page"]["url"] == (
        "https://public.example/links/episodes/ep_test.html"
    )
    assert response.json()["exact_episode_destination_count"] == 2
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("text/html")
    assert "Guest &amp; Host" in preview.text
    assert "v=1&amp;list=2" in preview.text
    assert episode_file.read_bytes() == before


@pytest.mark.parametrize("episode_id", ["missing", ".."])
def test_watch_links_rejects_missing_and_traversal(watch_links_api, episode_id):
    client, _ = watch_links_api

    response = client.get(f"/api/episodes/{episode_id}/watch-links")

    assert response.status_code == 404
