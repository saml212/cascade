"""Tests for pipeline API routes."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_routes_episodes import _create_episode

pytest_plugins = ["tests.test_routes_episodes"]


def _release_snapshot(*, upload_post=True, podcast_rss=False):
    return {
        "release_gate": {
            "can_approve_publish": True,
            "revision": "sha256:approved-plan",
            "blockers": [],
            "publish_plan": {
                "upload_post": {
                    "enabled": upload_post,
                    "account_identity": "sha256:upload-account",
                },
                "podcast_rss": {
                    "enabled": podcast_rss,
                    "account_identity": "sha256:r2-account",
                    "destination_configured": True,
                    "channel_configured": True,
                },
            },
        }
    }


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

        with (
            patch("server.routes.pipeline.threading.Thread") as mock_thread,
            patch("agents.pipeline.run_pipeline") as run_pipeline,
        ):
            mock_instance = mock_thread.return_value
            mock_instance.is_alive.return_value = False
            resp = client.post(
                "/api/episodes/ep_001/run-pipeline",
                json={
                    "source_path": "/tmp/test_source",
                    "audio_path": "/tmp/test_audio",
                    "agents": ["ingest"],
                },
            )
            mock_thread.call_args.kwargs["target"]()
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        assert run_pipeline.call_args.kwargs == {
            "source_path": "/tmp/test_source",
            "audio_path": "/tmp/test_audio",
            "episode_id": "ep_001",
            "agents": ["ingest"],
        }


class TestCancelPipeline:
    def test_cancel_not_running(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post("/api/episodes/ep_001/cancel-pipeline")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_running"


class TestAutoApprove:
    def test_auto_approve_refuses_unrendered_clips(self, test_client):
        client, episodes_dir = test_client
        clips = [
            {"id": "clip_01", "status": "pending", "virality_score": 8},
            {"id": "clip_02", "status": "pending", "virality_score": 5},
        ]
        ep_dir = _create_episode(episodes_dir, "ep_001", {"clips": clips})
        with open(ep_dir / "clips.json", "w") as f:
            json.dump({"clips": clips}, f)

        resp = client.post("/api/episodes/ep_001/auto-approve")
        assert resp.status_code == 409
        assert resp.json()["detail"]["clip_ids"] == ["clip_01", "clip_02"]


class TestEditorialApproval:
    def test_approving_longform_does_not_publish(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {
                "publish_approved": True,
                "publish_approval": {"revision": "old"},
            },
        )

        with (
            patch("server.routes.pipeline.threading.Thread") as thread_class,
            patch(
                "server.routes.pipeline._current_longform_for_approval",
                return_value={"fingerprint": "sha256:current"},
            ),
            patch("agents.pipeline.run_pipeline") as run_pipeline,
        ):
            response = client.post("/api/episodes/ep_001/approve-longform")
            thread_class.call_args.kwargs["target"]()

        assert response.status_code == 200
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert episode["editorial_approval"]["revision"].startswith("sha256:")
        assert episode["status"] == "processing"
        assert "publish_approved" not in episode
        assert "publish_approval" not in episode
        requested = run_pipeline.call_args.kwargs["agents"]
        assert requested == [
            "clip_miner",
            "shorts_render",
            "metadata_gen",
            "thumbnail_gen",
            "qa",
        ]
        assert response.json()["production_started"] is True

    def test_approval_without_production_preserves_existing_package(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {
                "status": "ready_for_review",
                "publish_approved": True,
                "publish_approved_at": "legacy",
                "publish_approval": {"revision": "old"},
            },
        )
        clips_path = episode_dir / "clips.json"
        metadata_path = episode_dir / "metadata" / "metadata.json"
        clips_path.write_text(
            json.dumps(
                {
                    "clips": [
                        {
                            "id": "clip_01",
                            "status": "approved",
                            "approved_revision": "sha256:reviewed",
                        }
                    ]
                }
            )
        )
        metadata_path.write_text(
            json.dumps({"clips": [{"id": "clip_01", "title": "Reviewed"}]})
        )
        protected_files = {
            clips_path: clips_path.read_bytes(),
            metadata_path: metadata_path.read_bytes(),
        }

        with (
            patch(
                "server.routes.pipeline._current_longform_for_approval",
                return_value={"fingerprint": "sha256:current"},
            ),
            patch(
                "server.routes.pipeline.editorial_revision",
                return_value="sha256:current-editorial",
            ),
            patch("server.routes.pipeline._start_pipeline_thread") as start_pipeline,
        ):
            response = client.post(
                "/api/episodes/ep_001/approve-longform",
                json={"continue_production": False},
            )

        assert response.status_code == 200
        assert response.json() == {
            "status": "approved",
            "episode_id": "ep_001",
            "production_started": False,
        }
        assert not start_pipeline.called
        assert all(
            path.read_bytes() == contents for path, contents in protected_files.items()
        )
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert episode["status"] == "ready_for_review"
        assert episode["editorial_approval"]["revision"] == "sha256:current-editorial"
        assert episode["longform_approved"] is True
        assert "publish_approved" not in episode
        assert "publish_approved_at" not in episode
        assert "publish_approval" not in episode

    def test_approval_rejects_previous_or_missing_render(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "upload_video.mp4").write_bytes(b"previous pixels")

        with patch("server.routes.pipeline.threading.Thread") as thread_class:
            response = client.post(
                "/api/episodes/ep_001/approve-longform",
                json={"continue_production": False},
            )

        assert response.status_code == 409
        assert "current speaker-cut" in response.json()["detail"]
        assert not thread_class.called
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert "editorial_approval" not in episode


class TestResumeAfterComplete:
    def test_resume_already_complete(self, test_client):
        client, episodes_dir = test_client
        from agents import PIPELINE_ORDER

        _create_episode(
            episodes_dir,
            "ep_001",
            {
                "pipeline": {
                    "started_at": "2026-01-01T12:00:00+00:00",
                    "completed_at": "2026-01-01T13:00:00+00:00",
                    "agents_completed": list(PIPELINE_ORDER),
                }
            },
        )
        resp = client.post("/api/episodes/ep_001/resume-pipeline")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_complete"

    def test_implicit_resume_excludes_publication_agents(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")

        with patch("server.routes.pipeline.threading.Thread") as thread_class:
            response = client.post("/api/episodes/ep_001/resume-pipeline", json={})

        assert response.status_code == 200
        remaining = response.json()["remaining_agents"]
        assert "publish" not in remaining
        assert "podcast_feed" not in remaining
        assert thread_class.called


class TestPublishApproval:
    def test_dispatches_only_enabled_publication_agents(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {"publish_approved": True, "publish_approved_at": "legacy"},
        )

        with (
            patch("server.routes.pipeline.quality_snapshot") as snapshot,
            patch("server.routes.pipeline.threading.Thread") as thread_class,
            patch("agents.pipeline.run_pipeline") as run_pipeline,
        ):
            snapshot.return_value = _release_snapshot(
                upload_post=True, podcast_rss=True
            )
            response = client.post("/api/episodes/ep_001/approve-publish")
            thread_class.call_args.kwargs["target"]()

        assert response.status_code == 200
        assert run_pipeline.call_args.kwargs["agents"] == ["publish", "podcast_feed"]
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert episode["publish_approval"]["revision"] == "sha256:approved-plan"
        assert (
            episode["publish_approval"]["plan"]
            == snapshot.return_value["release_gate"]["publish_plan"]
        )
        assert "publish_approved" not in episode
        assert "publish_approved_at" not in episode

    def test_rss_only_plan_dispatches_only_podcast_feed(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")

        with (
            patch("server.routes.pipeline.quality_snapshot") as snapshot,
            patch("server.routes.pipeline.threading.Thread") as thread_class,
            patch("agents.pipeline.run_pipeline") as run_pipeline,
        ):
            snapshot.return_value = _release_snapshot(
                upload_post=False, podcast_rss=True
            )
            response = client.post("/api/episodes/ep_001/approve-publish")
            thread_class.call_args.kwargs["target"]()

        assert response.status_code == 200
        assert run_pipeline.call_args.kwargs["agents"] == ["podcast_feed"]

    def test_refuses_plan_without_destinations(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")

        with (
            patch("server.routes.pipeline.quality_snapshot") as snapshot,
            patch("server.routes.pipeline.threading.Thread") as thread_class,
        ):
            snapshot.return_value = _release_snapshot(
                upload_post=False, podcast_rss=False
            )
            response = client.post("/api/episodes/ep_001/approve-publish")

        assert response.status_code == 409
        assert not thread_class.called
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert "publish_approval" not in episode

    def test_refuses_enabled_upload_post_without_bound_account(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        release = _release_snapshot(upload_post=True)
        release["release_gate"]["publish_plan"]["upload_post"]["account_identity"] = (
            None
        )

        with (
            patch("server.routes.pipeline.quality_snapshot", return_value=release),
            patch("server.routes.pipeline.threading.Thread") as thread_class,
        ):
            response = client.post("/api/episodes/ep_001/approve-publish")

        assert response.status_code == 409
        assert "UPLOAD_POST_USER" in str(response.json()["detail"])
        assert not thread_class.called
        assert "publish_approval" not in json.loads(
            (episode_dir / "episode.json").read_text()
        )

    def test_refuses_enabled_rss_without_bound_account(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        release = _release_snapshot(upload_post=False, podcast_rss=True)
        release["release_gate"]["publish_plan"]["podcast_rss"]["account_identity"] = (
            None
        )

        with (
            patch("server.routes.pipeline.quality_snapshot", return_value=release),
            patch("server.routes.pipeline.threading.Thread") as thread_class,
        ):
            response = client.post("/api/episodes/ep_001/approve-publish")

        assert response.status_code == 409
        assert "CLOUDFLARE_ACCOUNT_ID" in str(response.json()["detail"])
        assert not thread_class.called


class TestUploadPostReceipts:
    def test_captured_youtube_url_records_receipt_provenance(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "longform": {"status": "submitted", "request_id": "request-1"},
                    "shorts": [],
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"youtube_url": "https://youtu.be/receipt"}
        http_client = AsyncMock()
        http_client.get.return_value = response

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert episode["youtube_longform_url"] == "https://youtu.be/receipt"
        assert episode["youtube_longform_url_source"] == "upload_post_receipt"


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

    def test_worker_is_registered_for_source_recovery_guard(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        started = threading.Event()
        release = threading.Event()

        class BlockingAgent:
            def __init__(self, episode_dir, config):
                pass

            def run(self):
                started.set()
                assert release.wait(timeout=5)
                return {"finished": True}

        from agents import AGENT_REGISTRY
        from server.routes import pipeline, source_recovery

        monkeypatch.setitem(AGENT_REGISTRY, "blocking_test", BlockingAgent)
        response = {}

        def request() -> None:
            response["value"] = client.post(
                "/api/episodes/ep_001/run-agent/blocking_test", json={}
            )

        request_thread = threading.Thread(target=request)
        request_thread.start()
        try:
            assert started.wait(timeout=5)
            assert pipeline._running["ep_001"].is_alive()
            assert (
                source_recovery._registered_job_reason(
                    "ep_001", episodes_dir / "ep_001"
                )
                == "pipeline"
            )
            cancel = client.post("/api/episodes/ep_001/cancel-pipeline")
            assert cancel.status_code == 409
            assert "ep_001" not in pipeline._cancel_requested
        finally:
            release.set()
            request_thread.join(timeout=5)

        assert not request_thread.is_alive()
        assert response["value"].status_code == 200
        assert response["value"].json()["result"] == {"finished": True}
        assert "ep_001" not in pipeline._running
        assert "ep_001" not in pipeline._cancel_requested

    def test_cancelled_request_keeps_live_worker_registered(self):
        from server.routes import pipeline

        worker = MagicMock(spec=pipeline._SingleAgentWorker)
        worker.is_alive.return_value = True
        pipeline._running["ep_001"] = worker
        pipeline._cancel_requested.add("ep_001")

        async def exercise() -> None:
            await pipeline._unregister_single_agent("ep_001", worker)
            assert pipeline._running["ep_001"] is worker
            assert "ep_001" in pipeline._cancel_requested

            await pipeline._unregister_single_agent(
                "ep_001", worker, work_complete=True
            )

        asyncio.run(exercise())
        assert "ep_001" not in pipeline._running
        assert "ep_001" not in pipeline._cancel_requested
