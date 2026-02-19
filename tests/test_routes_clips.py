"""Tests for clip API routes."""

import json
import pytest
from tests.test_routes_episodes import test_client, _create_episode


def _add_clips(episodes_dir, episode_id, clips):
    ep_dir = episodes_dir / episode_id
    with open(ep_dir / "clips.json", "w") as f:
        json.dump({"clips": clips}, f)


SAMPLE_CLIPS = [
    {"id": "clip_01", "start_seconds": 60, "end_seconds": 120, "start": 60, "end": 120, "duration": 60, "title": "Clip 1", "virality_score": 8, "status": "pending", "speaker": "L"},
    {"id": "clip_02", "start_seconds": 300, "end_seconds": 360, "start": 300, "end": 360, "duration": 60, "title": "Clip 2", "virality_score": 5, "status": "pending", "speaker": "R"},
]


class TestListClips:
    def test_list_clips(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.get("/api/episodes/ep_001/clips")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_empty(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.get("/api/episodes/ep_001/clips")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetClip:
    def test_get_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.get("/api/episodes/ep_001/clips/clip_01")
        assert resp.status_code == 200
        assert resp.json()["id"] == "clip_01"

    def test_get_clip_not_found(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.get("/api/episodes/ep_001/clips/clip_99")
        assert resp.status_code == 404


class TestApproveReject:
    def test_approve_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.post("/api/episodes/ep_001/clips/clip_01/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_reject_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.post("/api/episodes/ep_001/clips/clip_01/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"


class TestManualClip:
    def test_add_manual_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.post("/api/episodes/ep_001/clips/manual", json={
            "start_seconds": 500.0,
            "end_seconds": 560.0,
        })
        assert resp.status_code == 200
        assert resp.json()["manual"] is True

    def test_invalid_duration(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post("/api/episodes/ep_001/clips/manual", json={
            "start_seconds": 100.0,
            "end_seconds": 50.0,  # end < start
        })
        assert resp.status_code == 400
