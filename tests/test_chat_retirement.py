"""The retired prose-chat surface must not re-enter the public API."""

import json

from fastapi.testclient import TestClient

from server.app import app
from tests.test_routes_episodes import _create_episode

pytest_plugins = ["tests.test_routes_episodes"]


RETIRED_CHAT_PATHS = {
    "/api/episodes/{episode_id}/chat/history",
    "/api/episodes/{episode_id}/chat",
    "/api/episodes/{episode_id}/complete-metadata",
}


def test_chat_paths_are_absent_from_routes_and_openapi():
    assert RETIRED_CHAT_PATHS.isdisjoint(app.openapi()["paths"])
    assert RETIRED_CHAT_PATHS.isdisjoint(
        {getattr(route, "path", None) for route in app.router.routes}
    )


def test_retired_history_read_uses_unknown_api_response():
    response = TestClient(app).get("/api/episodes/example/chat/history")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_reviewed_metadata_updates_use_canonical_routes(test_client):
    """External review writes metadata through the canonical APIs."""
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clips = [
        {
            "id": "clip_01",
            "start": 10.0,
            "end": 30.0,
            "start_seconds": 10.0,
            "end_seconds": 30.0,
            "duration": 20.0,
            "title": "Draft title",
            "status": "pending",
            "selection_status": "selected",
        }
    ]
    (episode_dir / "clips.json").write_text(json.dumps({"clips": clips}))

    reviewed = {
        "episode": {
            "title": "Reviewed episode title",
            "description": "Reviewed episode description",
            "tags": ["local", "podcast"],
        },
        "clips": [
            {
                "id": "clip_01",
                "title": "Reviewed clip title",
                "description": "Reviewed clip description",
                "hashtags": ["#local", "#podcast"],
                "metadata": {"youtube": {"title": "Reviewed YouTube title"}},
            }
        ],
    }

    episode_candidate = reviewed["episode"]
    clip_candidate = reviewed["clips"][0]
    episode_update = client.patch("/api/episodes/ep_001", json=episode_candidate)
    clip_update = client.patch(
        "/api/episodes/ep_001/clips/clip_01/metadata",
        json={key: value for key, value in clip_candidate.items() if key != "id"},
    )
    assert episode_update.status_code == 200
    assert clip_update.status_code == 200

    episode_readback = client.get("/api/episodes/ep_001")
    clip_readback = client.get("/api/episodes/ep_001/clips/clip_01")
    assert episode_readback.status_code == 200
    assert clip_readback.status_code == 200
    assert episode_readback.json()["title"] == "Reviewed episode title"
    assert episode_readback.json()["description"] == "Reviewed episode description"
    assert episode_readback.json()["tags"] == ["local", "podcast"]
    assert clip_readback.json()["title"] == "Reviewed clip title"
    assert clip_readback.json()["description"] == "Reviewed clip description"
    assert clip_readback.json()["hashtags"] == ["#local", "#podcast"]
    assert clip_readback.json()["metadata"] == {
        "youtube": {"title": "Reviewed YouTube title"}
    }
