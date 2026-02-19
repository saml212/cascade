"""Tests for pipeline API routes."""

import json
import pytest
from tests.test_routes_episodes import test_client, _create_episode


class TestPipelineStatus:
    def test_pipeline_status(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.get("/api/episodes/ep_001/pipeline-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["episode_id"] == "ep_001"
        assert "is_running" in data
        assert data["is_running"] is False

    def test_pipeline_status_not_found(self, test_client):
        client, _ = test_client
        resp = client.get("/api/episodes/nonexistent/pipeline-status")
        assert resp.status_code == 404


class TestRunPipeline:
    def test_run_without_source_path(self, test_client):
        client, episodes_dir = test_client
        # Create episode without source_path
        _create_episode(episodes_dir, "ep_001", {"source_path": None})
        # Override episode.json to not have source_path
        ep_file = episodes_dir / "ep_001" / "episode.json"
        with open(ep_file) as f:
            data = json.load(f)
        data["source_path"] = ""
        with open(ep_file, "w") as f:
            json.dump(data, f)

        resp = client.post("/api/episodes/ep_001/run-pipeline", json={})
        assert resp.status_code == 400

    def test_run_with_source_path(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")

        # Mock the pipeline run to avoid actual execution
        from unittest.mock import patch
        with patch("server.routes.pipeline.threading.Thread") as mock_thread:
            mock_instance = mock_thread.return_value
            mock_instance.is_alive.return_value = False
            resp = client.post("/api/episodes/ep_001/run-pipeline", json={
                "source_path": "/tmp/test_source"
            })
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"


class TestCancelPipeline:
    def test_cancel_not_running(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post("/api/episodes/ep_001/cancel-pipeline")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_running"


class TestAutoApprove:
    def test_auto_approve(self, test_client):
        client, episodes_dir = test_client
        clips = [
            {"id": "clip_01", "status": "pending", "virality_score": 8},
            {"id": "clip_02", "status": "pending", "virality_score": 5},
        ]
        ep_dir = _create_episode(episodes_dir, "ep_001", {"clips": clips})
        with open(ep_dir / "clips.json", "w") as f:
            json.dump({"clips": clips}, f)

        resp = client.post("/api/episodes/ep_001/auto-approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"


class TestResumeAfterComplete:
    def test_resume_already_complete(self, test_client):
        client, episodes_dir = test_client
        from agents import PIPELINE_ORDER
        _create_episode(episodes_dir, "ep_001", {
            "pipeline": {
                "started_at": "2026-01-01T12:00:00+00:00",
                "completed_at": "2026-01-01T13:00:00+00:00",
                "agents_completed": list(PIPELINE_ORDER),
            }
        })
        resp = client.post("/api/episodes/ep_001/resume-pipeline")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_complete"


class TestRunSingleAgent:
    def test_unknown_agent(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post("/api/episodes/ep_001/run-agent/nonexistent", json={})
        assert resp.status_code == 404

    def test_episode_not_found(self, test_client):
        client, _ = test_client
        resp = client.post("/api/episodes/nonexistent/run-agent/ingest", json={})
        assert resp.status_code == 404
