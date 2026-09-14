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
    return TestClient(app), episode_file, config


def test_watch_links_and_html_are_current_and_read_only(watch_links_api):
    client, episode_file, _config = watch_links_api
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
    client, _episode_file, _config = watch_links_api

    response = client.get(f"/api/episodes/{episode_id}/watch-links")

    assert response.status_code == 404


def test_watch_links_reads_explicit_id_apple_catalog_from_trusted_config(
    watch_links_api, tmp_path
):
    client, episode_file, config = watch_links_api
    catalog = tmp_path / "apple-verified-catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "show_url": "https://podcasts.apple.com/show/local",
                "episodes": [
                    {
                        "episode_id": "different_episode",
                        "title": "Guest & Host",
                        "url": "https://podcasts.apple.com/episode/wrong",
                    },
                    {
                        "episode_id": "ep_test",
                        "feed_guid": "video:ep_test",
                        "url": "https://podcasts.apple.com/episode/exact",
                    },
                ],
            }
        )
    )
    config["podcast"]["links"]["apple_catalog_path"] = str(catalog)
    before = episode_file.read_bytes()

    response = client.get("/api/episodes/ep_test/watch-links")
    preview = client.get("/api/episodes/ep_test/watch-page")

    assert response.status_code == 200
    apple = next(
        item
        for item in response.json()["destinations"]
        if item["key"] == "apple_podcasts"
    )
    assert apple == {
        "key": "apple_podcasts",
        "label": "Watch on Apple Podcasts",
        "url": "https://podcasts.apple.com/episode/exact",
        "scope": "episode",
    }
    assert response.json()["exact_episode_destination_count"] == 3
    assert "https://podcasts.apple.com/episode/exact" in preview.text
    assert episode_file.read_bytes() == before


def test_watch_links_rejects_relative_apple_catalog_path(watch_links_api):
    client, _episode_file, config = watch_links_api
    config["podcast"]["links"]["apple_catalog_path"] = "apple-catalog.json"

    response = client.get("/api/episodes/ep_test/watch-links")

    assert response.status_code == 409
    assert "must be an absolute path" in response.json()["detail"]
