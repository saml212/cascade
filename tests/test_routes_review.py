"""Review API tests for current and previous media artifacts."""

# ruff: noqa: F811 - imported pytest fixture is intentionally shadowed

import json

from tests.test_routes_episodes import _create_episode, test_client  # noqa: F401


def _write_clips(episode_dir, clips):
    (episode_dir / "clips.json").write_text(json.dumps({"clips": clips}))


def _record(path, fingerprint, mode):
    stat = path.stat()
    return {
        "path": str(path.name),
        "render_mode": mode,
        "fingerprint": fingerprint,
        "output": {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "completed_at": "2026-09-11T12:00:00+00:00",
    }


def test_review_keeps_stale_short_playable(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clip = {
        "id": "clip_01",
        "start_seconds": 10,
        "end_seconds": 30,
        "status": "pending",
        "metadata": {"youtube": {"title": "Title", "description": "Copy"}},
    }
    _write_clips(episode_dir, [clip])
    output = episode_dir / "shorts" / "clip_01.mp4"
    output.write_bytes(b"previous pixels")
    (episode_dir / "render_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "shorts": {
                    "clip_01": _record(output, "sha256:previous", "speaker_cut_short")
                },
            }
        )
    )

    from server.routes import review

    monkeypatch.setattr(
        review,
        "_expected_fingerprints",
        lambda *_args: (None, {"clip_01": "sha256:current"}),
    )
    monkeypatch.setattr(
        review,
        "load_config",
        lambda: {
            "platforms": {
                "youtube": {"enabled": True},
                "tiktok": {"enabled": False},
                "x": {"enabled": True},
            }
        },
    )

    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    payload = response.json()
    render = payload["clips"][0]["review"]["render"]
    assert render["status"] == "stale"
    assert render["reason_code"] == "render_inputs_changed"
    assert render["playable"] is True
    assert render["current"] is False
    assert render["url"].endswith("/shorts/clip_01.mp4")
    assert payload["clips"][0]["review"]["approval"]["current"] is False
    assert [item["key"] for item in payload["enabled_destinations"]] == [
        "youtube",
        "x",
    ]
    assert payload["clip_summary"] == {
        "candidate_count": 1,
        "selected_count": 0,
        "unselected_count": 1,
        "rejected_count": 0,
    }
    assert payload["clips"][0]["review"]["selection"]["status"] == "unselected"
    metadata = payload["clips"][0]["review"]["metadata"]
    assert metadata["enabled_destination_count"] == 2
    assert metadata["complete_destination_count"] == 1


def test_review_exposes_untracked_longform_despite_episode_status(test_client):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001", {"status": "ready_to_render"})
    (episode_dir / "upload_video.mp4").write_bytes(b"previous longform")

    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    longform = response.json()["longform"]
    assert longform["render"]["status"] == "untracked"
    assert longform["render"]["playable"] is True
    assert longform["render"]["current"] is False
    assert longform["render"]["url"].endswith("/upload_video.mp4")
    assert longform["approval"]["current"] is False
    assert longform["source_preview_url"].endswith("/video-preview")


def test_review_distinguishes_missing_short_from_stale(test_client):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    _write_clips(
        episode_dir,
        [{"id": "clip_01", "start_seconds": 10, "end_seconds": 30}],
    )

    response = client.get("/api/episodes/ep_001/review")

    render = response.json()["clips"][0]["review"]["render"]
    assert render == {
        "status": "missing",
        "current": False,
        "playable": False,
        "path": "shorts/clip_01.mp4",
        "reason_code": "artifact_missing",
        "detail": "No rendered file exists yet.",
        "url": None,
        "download_url": None,
    }
