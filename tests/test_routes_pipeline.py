"""Tests for pipeline API routes."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from agents.publish import AggregatePublicationRetired
from tests.test_routes_episodes import _create_episode

pytest_plugins = ["tests.test_routes_episodes"]


def _release_snapshot(*, upload_post=True, podcast_rss=False, video_podcast_rss=False):
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
                "video_podcast_rss": {
                    "enabled": video_podcast_rss,
                    "format": "video",
                    "feed_key": "feed-video.xml",
                    "account_identity": "sha256:r2-account",
                    "destination_configured": True,
                    "channel_configured": True,
                    "episode_configured": True,
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


class TestShortDestinationPreview:
    def test_returns_exact_agent_preview_and_forbids_extra_fields(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        request = {
            "destinations": ["youtube", "tiktok"],
            "clip_ids": ["clip_01"],
            "request_id": "e5753781-47f9-455e-9ce5-0eead48a19cd",
            "actor": "operator",
            "reason": "Approved motion release",
            "expected_release_revision": "sha256:release",
            "copy_overrides": {
                "clip_01": {
                    "youtube": {"title": "Title", "description": "Line 1\nLine 2"},
                    "tiktok": {"text": "Approved copy"},
                }
            },
            "schedule_overrides": {"clip_01": "2026-09-20T09:30:00-07:00"},
        }
        with patch(
            "agents.publish.PublishAgent.preview_short_destinations",
            return_value={"preview_revision": "sha256:preview"},
        ) as preview:
            response = client.post(
                "/api/episodes/ep_001/publish-shorts/preview", json=request
            )
        assert response.status_code == 200
        assert response.json() == {"preview_revision": "sha256:preview"}
        assert preview.call_args.args[0]["request_id"] == request["request_id"]
        assert preview.call_args.args[0]["copy_overrides"] == request["copy_overrides"]
        assert (
            preview.call_args.args[0]["schedule_overrides"]
            == request["schedule_overrides"]
        )

        response = client.post(
            "/api/episodes/ep_001/publish-shorts/preview",
            json={**request, "unexpected": True},
        )
        assert response.status_code == 422

    def test_preserves_existing_request_normalization_and_schema(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        request_id = "E5753781-47F9-455E-9CE5-0EEAD48A19CD"
        request = {
            "destinations": ["youtube"],
            "request_id": request_id,
            "actor": " release operator ",
            "reason": " approved motion release ",
            "expected_release_revision": "sha256:release",
        }
        with patch(
            "agents.publish.PublishAgent.preview_short_destinations",
            return_value={"preview_revision": "sha256:preview"},
        ) as preview:
            response = client.post(
                "/api/episodes/ep_001/publish-shorts/preview", json=request
            )

        assert response.status_code == 200
        normalized = preview.call_args.args[0]
        assert normalized["request_id"] == request_id.lower()
        assert normalized["actor"] == request["actor"]
        assert normalized["reason"] == request["reason"]
        request_schema = client.get("/openapi.json").json()["components"]["schemas"][
            "ShortDestinationRequest"
        ]
        assert request_schema["properties"]["request_id"]["format"] == "uuid"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("request_id", 7),
            ("destinations", ["youtube", 7]),
            ("clip_ids", [7]),
            ("publish_now", 1),
            ("variant_overrides", {"clip_01": 7}),
            ("copy_overrides", {"clip_01": {"youtube": {"title": 7}}}),
            ("schedule_overrides", {"clip_01": 7}),
        ],
    )
    def test_rejects_coerced_wire_types(self, test_client, field, value):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        request = {
            "destinations": ["youtube"],
            "request_id": "e5753781-47f9-455e-9ce5-0eead48a19cd",
            "actor": "operator",
            "reason": "Approved motion release",
            "expected_release_revision": "sha256:release",
            field: value,
        }

        response = client.post(
            "/api/episodes/ep_001/publish-shorts/preview", json=request
        )

        assert response.status_code == 422


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

    @pytest.mark.parametrize(
        ("retired_agent", "path", "payload"),
        [
            ("metadata_gen", "run-agent/metadata_gen", {}),
            (
                "metadata_gen",
                "run-pipeline",
                {"agents": ["ingest", "metadata_gen"]},
            ),
            ("metadata_gen", "resume-pipeline", {"agents": ["metadata_gen"]}),
            ("podcast_feed", "run-agent/podcast_feed", {}),
            ("podcast_feed", "run-pipeline", {"agents": ["podcast_feed"]}),
            ("podcast_feed", "resume-pipeline", {"agents": ["podcast_feed"]}),
        ],
    )
    def test_retired_agent_rejects_before_worker_or_episode_mutation(
        self, test_client, retired_agent, path, payload
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        episode_file = episode_dir / "episode.json"
        before = episode_file.read_bytes()
        from server.routes import pipeline

        running_before = dict(pipeline._running)
        cancelled_before = set(pipeline._cancel_requested)
        with (
            patch("server.routes.pipeline.threading.Thread") as thread_class,
            patch("server.routes.pipeline._SingleAgentWorker") as single_worker,
        ):
            response = client.post(f"/api/episodes/ep_001/{path}", json=payload)

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "agent_retired",
            "agents": [retired_agent],
            "message": "One or more requested agents are retired.",
        }
        thread_class.assert_not_called()
        single_worker.assert_not_called()
        assert pipeline._running == running_before
        assert pipeline._cancel_requested == cancelled_before
        assert episode_file.read_bytes() == before

    @pytest.mark.parametrize("retired_agent", ("metadata_gen", "podcast_feed"))
    def test_thread_launcher_defensively_rejects_retired_agent(self, retired_agent):
        from server.routes import pipeline

        running_before = dict(pipeline._running)
        with (
            patch("server.routes.pipeline.threading.Thread") as thread_class,
            pytest.raises(HTTPException) as error,
        ):
            pipeline._start_pipeline_thread("ep_001", "/tmp/source", [retired_agent])

        assert error.value.status_code == 409
        assert error.value.detail["agents"] == [retired_agent]
        thread_class.assert_not_called()
        assert pipeline._running == running_before

    @pytest.mark.parametrize(
        ("path", "payload"),
        (
            ("run-pipeline", {"agents": ["ingest", "publish"]}),
            ("resume-pipeline", {"agents": ["publish"]}),
        ),
    )
    def test_generic_publish_rejects_before_worker_or_episode_mutation(
        self, test_client, path, payload
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        episode_file = episode_dir / "episode.json"
        before = episode_file.read_bytes()
        from server.routes import pipeline

        running_before = dict(pipeline._running)
        cancelled_before = set(pipeline._cancel_requested)
        with (
            patch("server.routes.pipeline._start_pipeline_thread") as start_thread,
            patch("server.routes.pipeline.threading.Thread") as thread_class,
        ):
            response = client.post(f"/api/episodes/ep_001/{path}", json=payload)

        assert response.status_code == 409
        assert response.json()["detail"] == AggregatePublicationRetired.detail()
        start_thread.assert_not_called()
        thread_class.assert_not_called()
        assert pipeline._running == running_before
        assert pipeline._cancel_requested == cancelled_before
        assert episode_file.read_bytes() == before

    def test_thread_launcher_defensively_rejects_generic_publish(self):
        from server.routes import pipeline

        with (
            patch("server.routes.pipeline.threading.Thread") as thread_class,
            pytest.raises(HTTPException) as error,
        ):
            pipeline._start_pipeline_thread("ep_001", "/tmp/source", ["publish"])

        assert error.value.status_code == 409
        assert error.value.detail == AggregatePublicationRetired.detail()
        thread_class.assert_not_called()


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
    def test_openapi_requires_the_approval_only_body(self, test_client):
        client, _ = test_client
        operation = client.get("/openapi.json").json()["paths"][
            "/api/episodes/{episode_id}/approve-publish"
        ]["post"]
        request_body = operation["requestBody"]
        schema = request_body["content"]["application/json"]["schema"]

        assert request_body["required"] is True
        assert schema["required"] == ["start_publication"]
        assert schema["additionalProperties"] is False
        start_publication = schema["properties"]["start_publication"]
        assert start_publication["type"] == "boolean"
        assert start_publication["const"] is False
        conflict = operation["responses"]["409"]["content"]["application/json"]
        response_ref = conflict["schema"]["$ref"].rsplit("/", 1)[-1]
        response_schema = client.get("/openapi.json").json()["components"]["schemas"][
            response_ref
        ]
        assert response_schema["properties"]["detail"] == {"title": "Detail"}
        retirement = conflict["examples"]["aggregate_publication_retired"]["value"]
        assert retirement["detail"]["code"] == "aggregate_publication_retired"

    @pytest.mark.parametrize(
        ("body_kind", "body"),
        (
            ("omitted", None),
            ("json", {}),
            ("null", None),
            ("json", {"start_publication": True}),
            ("json", {"start_publication": 0}),
            ("json", {"start_publication": "false"}),
        ),
    )
    def test_ambiguous_or_aggregate_request_is_retired_before_writes(
        self, test_client, body_kind, body
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        before = (episode_dir / "episode.json").read_bytes()

        with (
            patch("server.routes.pipeline.quality_snapshot") as snapshot,
            patch("server.routes.pipeline.atomic_write_json") as write_json,
            patch("server.routes.pipeline._start_pipeline_thread") as start_thread,
        ):
            if body_kind == "omitted":
                response = client.post("/api/episodes/ep_001/approve-publish")
            elif body_kind == "null":
                response = client.post(
                    "/api/episodes/ep_001/approve-publish",
                    content="null",
                    headers={"content-type": "application/json"},
                )
            else:
                response = client.post(
                    "/api/episodes/ep_001/approve-publish", json=body
                )

        assert response.status_code == 409
        assert response.json()["detail"] == AggregatePublicationRetired.detail()
        snapshot.assert_not_called()
        write_json.assert_not_called()
        start_thread.assert_not_called()
        assert (episode_dir / "episode.json").read_bytes() == before

    def test_records_video_plan_without_starting_any_publisher(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {"status": "ready_for_review"},
        )

        with (
            patch("server.routes.pipeline.quality_snapshot") as snapshot,
            patch("server.routes.pipeline._start_pipeline_thread") as start_pipeline,
        ):
            snapshot.return_value = _release_snapshot(
                upload_post=False,
                podcast_rss=False,
                video_podcast_rss=True,
            )
            response = client.post(
                "/api/episodes/ep_001/approve-publish",
                json={"start_publication": False},
            )

        assert response.status_code == 200
        assert response.json() == {
            "status": "approved",
            "episode_id": "ep_001",
            "publication_started": False,
            "publication_agents": [],
        }
        start_pipeline.assert_not_called()
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert episode["status"] == "ready_for_review"
        assert episode["publish_approval"]["revision"] == "sha256:approved-plan"
        assert episode["publish_approval"]["plan"]["video_podcast_rss"] == {
            "enabled": True,
            "format": "video",
            "feed_key": "feed-video.xml",
            "account_identity": "sha256:r2-account",
            "destination_configured": True,
            "channel_configured": True,
            "episode_configured": True,
        }

    def test_retired_rss_only_plan_dispatches_nothing(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        before = (episode_dir / "episode.json").read_bytes()

        with (
            patch("server.routes.pipeline.quality_snapshot") as snapshot,
            patch("server.routes.pipeline.threading.Thread") as thread_class,
        ):
            snapshot.return_value = _release_snapshot(
                upload_post=False, podcast_rss=True
            )
            response = client.post(
                "/api/episodes/ep_001/approve-publish",
                json={"start_publication": False},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "No publication destinations are enabled"
        thread_class.assert_not_called()
        assert (episode_dir / "episode.json").read_bytes() == before

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
            response = client.post(
                "/api/episodes/ep_001/approve-publish",
                json={"start_publication": False},
            )

        assert response.status_code == 409
        assert not thread_class.called
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert "publish_approval" not in episode

    def test_refuses_structured_release_gate_blocker(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        release = _release_snapshot()
        release["release_gate"].update(
            can_approve_publish=False,
            blockers=[{"code": "qa_stale", "message": "QA is stale"}],
        )

        with patch("server.routes.pipeline.quality_snapshot", return_value=release):
            response = client.post(
                "/api/episodes/ep_001/approve-publish",
                json={"start_publication": False},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "message": "Release prerequisites are not satisfied",
            "quality_url": "/api/episodes/ep_001/quality",
            "blockers": release["release_gate"]["blockers"],
        }
        assert "publish_approval" not in json.loads(
            (episode_dir / "episode.json").read_text()
        )

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
            response = client.post(
                "/api/episodes/ep_001/approve-publish",
                json={"start_publication": False},
            )

        assert response.status_code == 409
        assert "UPLOAD_POST_USER" in str(response.json()["detail"])
        assert not thread_class.called
        assert "publish_approval" not in json.loads(
            (episode_dir / "episode.json").read_text()
        )

    def test_retired_rss_account_binding_is_not_evaluated(self, test_client):
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
            response = client.post(
                "/api/episodes/ep_001/approve-publish",
                json={"start_publication": False},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "No publication destinations are enabled"
        assert not thread_class.called


class TestUploadPostReceipts:
    def test_malformed_short_receipts_fail_closed_before_provider_poll(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(json.dumps({"shorts": "invalid"}))
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")

        with patch("httpx.AsyncClient") as client_class:
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 409
        assert "cannot be verified" in result.json()["detail"]
        client_class.assert_not_called()

    def test_current_bound_supplied_url_skips_remote_poll(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        from agents.qa import editorial_revision

        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        episode.update(
            youtube_longform_url="https://youtu.be/current",
            youtube_longform_url_source="supplied",
            youtube_longform_url_editorial_revision=editorial_revision(
                episode_dir, episode
            ),
        )
        episode_path.write_text(json.dumps(episode))
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "longform": {
                        "status": "submitted",
                        "request_id": "request-1",
                    },
                    "shorts": [],
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")

        with patch("httpx.AsyncClient") as client_class:
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert result.json()["longform"] == {
            "status": "live",
            "url": "https://youtu.be/current",
        }
        client_class.assert_not_called()

    def test_stale_bound_supplied_url_polls_current_receipt_and_replaces_it(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        from agents.qa import editorial_revision, publication_identity

        current_editorial_revision = editorial_revision(episode_dir, episode)
        longform_identity = publication_identity(
            "ep_001", current_editorial_revision, "longform"
        )
        episode.update(
            youtube_longform_url="https://youtu.be/old",
            youtube_longform_url_source="supplied",
            youtube_longform_url_editorial_revision="sha256:old-longform",
            publish_approval={"revision": "sha256:current-release"},
        )
        episode_path.write_text(json.dumps(episode))
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "release_revision": "sha256:current-release",
                    "longform": {
                        "status": "submitted",
                        "request_id": "request-2",
                        "external_id": longform_identity,
                        "editorial_revision": current_editorial_revision,
                    },
                    "shorts": [],
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"youtube_url": "https://youtu.be/rebuilt"}
        http_client = AsyncMock()
        http_client.get.return_value = response

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert http_client.get.call_args.kwargs["params"] == {"request_id": "request-2"}
        updated = json.loads(episode_path.read_text())
        assert updated["youtube_longform_url"] == "https://youtu.be/rebuilt"
        assert updated["youtube_longform_url_source"] == "upload_post_receipt"
        assert (
            updated["youtube_longform_url_release_revision"] == "sha256:current-release"
        )

    def test_captured_youtube_url_records_receipt_provenance(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        from agents.qa import editorial_revision

        current_editorial_revision = editorial_revision(
            episode_dir, json.loads((episode_dir / "episode.json").read_text())
        )
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "release_revision": "sha256:release",
                    "longform": {
                        "status": "unknown",
                        "request_id": "request-1",
                        "external_id": "cascade-longform-current",
                        "editorial_revision": current_editorial_revision,
                    },
                    "shorts": [],
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "completed",
            "results": [
                {
                    "platform": "youtube",
                    "success": True,
                    "post_url": "https://youtu.be/receipt",
                }
            ],
        }
        http_client = AsyncMock()
        http_client.get.return_value = response

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert episode["youtube_longform_url"] == "https://youtu.be/receipt"
        assert episode["youtube_longform_url_source"] == "upload_post_receipt"
        assert episode["youtube_longform_url_external_id"] == "cascade-longform-current"
        assert episode["youtube_longform_url_release_revision"] == "sha256:release"
        assert http_client.get.call_args.kwargs["params"] == {"request_id": "request-1"}

    @pytest.mark.parametrize(
        ("provider_status", "unresolved_flag", "expected_status"),
        (
            ("queued", None, "pending"),
            ("processing", None, "pending"),
            ("inbox", None, "pending"),
            ("unknown", None, "pending"),
            ("failed", "fallback_to_inbox", "pending"),
            ("failed", None, "failed"),
        ),
    )
    def test_longform_result_uses_terminal_provider_state(
        self,
        test_client,
        monkeypatch,
        provider_status,
        unresolved_flag,
        expected_status,
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        from agents.qa import editorial_revision

        current_revision = editorial_revision(
            episode_dir, json.loads((episode_dir / "episode.json").read_text())
        )
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "longform": {
                        "status": "submitted",
                        "request_id": "request-1",
                        "editorial_revision": current_revision,
                    },
                    "shorts": [],
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        response = MagicMock()
        response.raise_for_status.return_value = None
        result_item = {
            "platform": "youtube",
            "success": False,
            "attempts": 0,
        }
        if provider_status != "processing":
            result_item["status"] = provider_status
        if unresolved_flag:
            result_item[unresolved_flag] = True
        response.json.return_value = {
            "status": provider_status,
            "results": [result_item],
        }
        http_client = AsyncMock()
        http_client.get.return_value = response

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        longform = result.json()["longform"]
        assert longform["status"] == expected_status
        assert longform["url"] is None
        if expected_status == "pending":
            assert longform["upload_post_state"] == provider_status
            assert "platform_failures" not in longform
        else:
            assert longform["platform_failures"]["youtube"]["status"] == "failed"

    @pytest.mark.parametrize("receipt_revision", [None, ["malformed"]])
    def test_unbound_or_malformed_longform_receipt_is_not_polled(
        self, test_client, monkeypatch, receipt_revision
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        receipt = {
            "status": "submitted",
            "request_id": "old-request",
            "external_id": "cascade-longform-old",
        }
        if receipt_revision is not None:
            receipt["editorial_revision"] = receipt_revision
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "release_revision": "sha256:old-release",
                    "longform": receipt,
                    "shorts": [],
                }
            )
        )
        before = (episode_dir / "episode.json").read_bytes()
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")

        with patch("httpx.AsyncClient") as client_class:
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert result.json()["longform"]["status"] == "stale"
        assert (episode_dir / "episode.json").read_bytes() == before
        client_class.assert_not_called()

    @pytest.mark.parametrize(
        ("receipt", "expected_params"),
        [
            (
                {
                    "status": "submitted",
                    "scheduled": True,
                    "job_id": "job-1",
                    "request_id": "request-1",
                },
                {"job_id": "job-1"},
            ),
            (
                {
                    "status": "unknown",
                    "scheduled": False,
                    "request_id": "request-2",
                },
                {"request_id": "request-2"},
            ),
        ],
    )
    def test_short_status_uses_receipt_kind_and_retries_unknown(
        self, test_client, monkeypatch, receipt, expected_params
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "longform": None,
                    "shorts": [{"clip_id": "clip-1", **receipt}],
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "processing"}
        http_client = AsyncMock()
        http_client.get.return_value = response

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert http_client.get.call_args_list[0].kwargs["params"] == expected_params
        assert http_client.get.await_count == 1
        assert result.json()["shorts"] == [
            {"clip_id": "clip-1", "status": "pending", "url": None}
        ]

    def test_short_status_surfaces_each_platform_failure(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "longform": None,
                    "shorts": [
                        {
                            "clip_id": "clip-1",
                            "status": "submitted",
                            "scheduled": True,
                            "job_id": "job-1",
                            "platforms": ["youtube", "x"],
                        }
                    ],
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "completed",
            "results": [
                {
                    "platform": "youtube",
                    "success": True,
                    "post_url": "https://youtu.be/clip",
                    "job_id": "job-1",
                    "profile_username": "up",
                },
                {
                    "platform": "x",
                    "success": False,
                    "status": "failed",
                    "error": "No access token",
                    "job_id": "job-1",
                    "profile_username": "up",
                },
            ],
        }
        http_client = AsyncMock()
        http_client.get.return_value = response

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        short = result.json()["shorts"][0]
        assert short["status"] == "partial_failure"
        assert short["url"] == "https://youtu.be/clip"
        stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
        assert stored["terminal_destinations"]["youtube"]["state"] == "published"
        assert stored["terminal_destinations"]["x"]["state"] == "failed"
        assert stored["status_history"][0]["previous_status"] == "submitted"

    def test_short_history_reconciliation_is_exact_and_persists_terminal_proof(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        receipt = {
            "clip_id": "clip-1",
            "status": "submitted",
            "request_id": "request-1",
            "platforms": ["youtube", "instagram"],
        }
        (episode_dir / "publish.json").write_text(
            json.dumps({"longform": None, "shorts": [receipt]})
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        status_response = MagicMock()
        status_response.raise_for_status.return_value = None
        status_response.json.return_value = {
            "status": "completed",
            "request_id": "request-1",
        }
        history_response = MagicMock()
        history_response.raise_for_status.return_value = None
        history_response.json.return_value = {
            "history": [
                {
                    "platform": "youtube",
                    "success": True,
                    "post_url": "https://youtu.be/clip",
                    "request_id": "request-1",
                    "profile_username": "up",
                },
                {
                    "platform": "instagram",
                    "success": True,
                    "post_url": "https://instagram.com/reel/clip",
                    "request_id": "request-1",
                    "profile_username": "up",
                },
                {
                    "platform": "x",
                    "success": True,
                    "post_url": "https://x.com/other/status/1",
                    "request_id": "other-request",
                    "profile_username": "up",
                },
            ],
            "in_progress": [],
        }
        http_client = AsyncMock()
        http_client.get.side_effect = [status_response, history_response]

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            first = client.post("/api/episodes/ep_001/check-upload-urls")

        assert first.status_code == 200
        assert first.json()["shorts"][0]["status"] == "published"
        stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
        assert stored["status_history"][-1]["evidence_source"] == "history"
        assert stored["status_history"][-1]["profile_username"] == "up"
        assert set(stored["terminal_destinations"]) == {"youtube", "instagram"}

        with patch("httpx.AsyncClient") as client_class:
            repeated = client.post("/api/episodes/ep_001/check-upload-urls")
        assert repeated.status_code == 200
        assert repeated.json()["shorts"][0]["status"] == "published"
        client_class.assert_not_called()

    @pytest.mark.parametrize(
        "status",
        [
            "processing",
            "queued",
            "pending",
            "retrying",
            "fallback_to_inbox",
            "scheduled",
            "submitted",
            "unknown",
            "waiting",
        ],
    )
    def test_exact_unresolved_status_does_not_accept_stale_terminal_history(
        self, test_client, monkeypatch, status
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        receipt = {
            "clip_id": "clip-1",
            "status": "submitted",
            "request_id": "request-1",
            "platforms": ["youtube"],
        }
        publish_path = episode_dir / "publish.json"
        publish_path.write_text(json.dumps({"shorts": [receipt]}))
        before = publish_path.read_bytes()
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        status_response = MagicMock()
        status_response.raise_for_status.return_value = None
        status_response.json.return_value = {
            "status": status,
            "request_id": "request-1",
        }
        history_response = MagicMock()
        history_response.raise_for_status.return_value = None
        history_response.json.return_value = {
            "history": [
                {
                    "platform": "youtube",
                    "success": True,
                    "post_url": "https://youtu.be/stale",
                    "request_id": "request-1",
                    "profile_username": "up",
                }
            ],
            "in_progress": [],
        }
        http_client = AsyncMock()
        http_client.get.side_effect = [status_response, history_response]

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert result.json()["shorts"] == [
            {"clip_id": "clip-1", "status": "pending", "url": None}
        ]
        assert http_client.get.await_count == 1
        assert publish_path.read_bytes() == before

    def test_retired_status_id_falls_back_to_exact_history(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "shorts": [
                        {
                            "clip_id": "clip-1",
                            "status": "submitted",
                            "request_id": "request-1",
                            "platforms": ["youtube"],
                        }
                    ]
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        status_response = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.test/status"),
        )
        history_response = httpx.Response(
            200,
            request=httpx.Request("GET", "https://example.test/history"),
            json={
                "history": [
                    {
                        "platform": "youtube",
                        "success": True,
                        "post_url": "https://youtu.be/clip",
                        "request_id": "request-1",
                        "profile_username": "up",
                    }
                ],
                "in_progress": [],
            },
        )
        http_client = AsyncMock()
        http_client.get.side_effect = [status_response, history_response]

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert result.json()["shorts"][0]["status"] == "published"
        assert http_client.get.await_count == 2

    def test_status_auth_error_does_not_fall_back_to_history(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "shorts": [
                        {
                            "clip_id": "clip-1",
                            "status": "submitted",
                            "request_id": "request-1",
                            "platforms": ["youtube"],
                        }
                    ]
                }
            )
        )
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        forbidden = httpx.Response(
            403,
            request=httpx.Request("GET", "https://example.test/status"),
        )
        http_client = AsyncMock()
        http_client.get.return_value = forbidden

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert result.json()["shorts"][0]["status"] == "pending"
        assert http_client.get.await_count == 1

    def test_conflicting_status_identity_does_not_fall_back_to_history(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        receipt = {
            "clip_id": "clip-1",
            "status": "submitted",
            "job_id": "expected-job",
            "request_id": "request-1",
            "platforms": ["youtube"],
        }
        (episode_dir / "publish.json").write_text(json.dumps({"shorts": [receipt]}))
        publish_before = (episode_dir / "publish.json").read_bytes()
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "completed",
            "job_id": "wrong-job",
            "request_id": "request-1",
            "results": [
                {
                    "platform": "youtube",
                    "success": True,
                    "post_url": "https://youtu.be/wrong",
                    "job_id": "wrong-job",
                    "request_id": "request-1",
                    "profile_username": "up",
                }
            ],
        }
        http_client = AsyncMock()
        http_client.get.return_value = response

        with patch("httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = http_client
            result = client.post("/api/episodes/ep_001/check-upload-urls")

        assert result.status_code == 200
        assert result.json()["shorts"][0]["status"] == "unresolved"
        assert "conflicting provider identifiers" in result.json()["shorts"][0]["error"]
        assert http_client.get.await_count == 1
        assert (episode_dir / "publish.json").read_bytes() == publish_before


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

    @pytest.mark.parametrize("payload", ({}, {"publish": None}))
    def test_empty_publish_rejects_before_construction_or_writes(
        self, test_client, payload
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        episode_before = (episode_dir / "episode.json").read_bytes()
        files_before = sorted(path.name for path in episode_dir.iterdir())
        from server.routes import pipeline

        running_before = dict(pipeline._running)
        with (
            patch("agents.pipeline.load_config") as load_config,
            patch("server.routes.pipeline._SingleAgentWorker") as worker,
        ):
            response = client.post(
                "/api/episodes/ep_001/run-agent/publish", json=payload
            )

        assert response.status_code == 409
        assert response.json()["detail"] == AggregatePublicationRetired.detail()
        load_config.assert_not_called()
        worker.assert_not_called()
        assert pipeline._running == running_before
        assert (episode_dir / "episode.json").read_bytes() == episode_before
        assert sorted(path.name for path in episode_dir.iterdir()) == files_before

    def test_publish_body_on_other_agent_is_rejected_before_construction(
        self, test_client
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        publish = {
            "destinations": ["x"],
            "request_id": "e5753781-47f9-455e-9ce5-0eead48a19cd",
            "actor": "operator",
            "reason": "Approved exact release",
            "expected_release_revision": "sha256:release",
            "preview_revision": "sha256:preview",
        }
        with patch("agents.pipeline.load_config") as load_config:
            response = client.post(
                "/api/episodes/ep_001/run-agent/qa", json={"publish": publish}
            )

        assert response.status_code == 400
        load_config.assert_not_called()

    def test_publish_execution_body_reaches_publish_agent(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        captured = {}

        class PublishAgent:
            def __init__(self, _episode_dir, _config):
                pass

            def run(self):
                captured.update(self.short_destination_request)
                return {"submitted": True}

        from agents import AGENT_REGISTRY

        monkeypatch.setitem(AGENT_REGISTRY, "publish", PublishAgent)
        publish = {
            "destinations": ["youtube", "tiktok"],
            "clip_ids": ["clip_01"],
            "request_id": "e5753781-47f9-455e-9ce5-0eead48a19cd",
            "actor": "operator",
            "reason": "Approved motion release",
            "expected_release_revision": "sha256:release",
            "preview_revision": "sha256:preview",
        }
        response = client.post(
            "/api/episodes/ep_001/run-agent/publish", json={"publish": publish}
        )
        assert response.status_code == 200
        assert captured == publish

    def test_publish_preflight_runtime_error_is_structured_conflict(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        from agents.publish import ShortDestinationConflict

        class PublishAgent:
            def __init__(self, _episode_dir, _config):
                pass

            def run(self):
                raise ShortDestinationConflict(
                    "Schedule collision for clip_02; no shorts were submitted"
                )

        from agents import AGENT_REGISTRY

        monkeypatch.setitem(AGENT_REGISTRY, "publish", PublishAgent)
        publish = {
            "destinations": ["x"],
            "clip_ids": ["clip_02"],
            "request_id": "e5753781-47f9-455e-9ce5-0eead48a19cd",
            "actor": "operator",
            "reason": "Approved motion release",
            "expected_release_revision": "sha256:release",
            "preview_revision": "sha256:preview",
        }
        response = client.post(
            "/api/episodes/ep_001/run-agent/publish", json={"publish": publish}
        )

        assert response.status_code == 409
        assert response.json() == {
            "detail": "Schedule collision for clip_02; no shorts were submitted"
        }

    def test_publish_execution_rejects_misspelled_top_level_input(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        called = False

        class PublishAgent:
            def __init__(self, _episode_dir, _config):
                nonlocal called
                called = True

        from agents import AGENT_REGISTRY

        monkeypatch.setitem(AGENT_REGISTRY, "publish", PublishAgent)
        response = client.post(
            "/api/episodes/ep_001/run-agent/publish",
            json={
                "publsih": {
                    "destinations": ["youtube"],
                    "request_id": "e5753781-47f9-455e-9ce5-0eead48a19cd",
                }
            },
        )
        assert response.status_code == 422
        assert called is False

    def test_worker_is_registered_for_source_recovery_guard(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        started = threading.Event()
        release = threading.Event()

        class BlockingAgent:
            def __init__(self, episode_dir, _config):
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


def _longform_gate(*, safe=False, can_approve=True):
    blockers = []
    if not safe:
        blockers.append(
            {
                "code": "longform_publish_approval_missing_or_stale",
                "message": "Scoped approval required",
            }
        )
    return {
        "schema": "cascade.longform-publish-approval/v1",
        "episode_id": "ep_001",
        "status": "ready" if safe else "awaiting_publish_approval",
        "safe": safe,
        "can_approve": can_approve,
        "revision": "sha256:longform-release",
        "source_release_revision": "sha256:aggregate-release",
        "editorial_revision": "sha256:editorial",
        "quality_revision": "sha256:quality",
        "publish_plan": {
            "schema": "cascade.longform-publish-plan/v1",
            "youtube": {"enabled": True, "destination_configured": True},
            "podcast_rss": {"enabled": False},
            "video_podcast_rss": {
                "enabled": True,
                "format": "video",
                "destination_configured": True,
                "channel_configured": True,
                "episode_configured": True,
            },
        },
        "enabled_destinations": ["youtube", "video_podcast_rss"],
        "blockers": blockers,
        "approval": {"current": safe, "revision": "sha256:longform-release"},
    }


class TestLongformPublicationRoutes:
    def test_get_exposes_exact_readiness_revision(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        gate = _longform_gate()
        with (
            patch(
                "server.routes.pipeline.longform_publication_snapshot",
                return_value=gate,
            ),
            patch("agents.pipeline.load_config", return_value={}),
        ):
            response = client.get("/api/episodes/ep_001/longform-publication")

        assert response.status_code == 200
        assert response.json()["revision"] == "sha256:longform-release"
        assert response.json()["can_approve"] is True
        assert response.json()["safe"] is False

    def test_approval_is_scoped_and_does_not_create_global_approval(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        with (
            patch(
                "server.routes.pipeline.longform_publication_snapshot",
                side_effect=[_longform_gate(), _longform_gate(safe=True)],
            ),
            patch("agents.pipeline.load_config", return_value={}),
        ):
            response = client.post(
                "/api/episodes/ep_001/longform-publication/approve",
                json={
                    "expected_revision": "sha256:longform-release",
                    "actor": "operator",
                    "reason": "Approved corrected full episode",
                },
            )

        assert response.status_code == 200
        stored = json.loads((episode_dir / "episode.json").read_text())
        assert "publish_approval" not in stored
        assert stored["longform_publish_approval"] == {
            "schema": "cascade.longform-publish-approval/v1",
            "revision": "sha256:longform-release",
            "approved_at": stored["longform_publish_approval"]["approved_at"],
            "actor": "operator",
            "reason": "Approved corrected full episode",
            "plan": _longform_gate()["publish_plan"],
        }

    def test_publish_dispatches_only_scoped_agents(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        with (
            patch(
                "server.routes.pipeline.longform_publication_snapshot",
                return_value=_longform_gate(safe=True),
            ),
            patch("agents.pipeline.load_config", return_value={}),
            patch("server.routes.pipeline._start_pipeline_thread") as start,
        ):
            response = client.post(
                "/api/episodes/ep_001/longform-publication/publish",
                json={
                    "expected_revision": "sha256:longform-release",
                    "destinations": ["youtube", "video_podcast_rss"],
                },
            )

        assert response.status_code == 202
        agents = start.call_args.args[2]
        assert agents == ["longform_publish", "longform_video_feed"]
        assert "publish" not in agents
        assert "video_feed" not in agents

    def test_global_approval_cannot_authorize_scoped_publish(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        episode = json.loads((episode_dir / "episode.json").read_text())
        episode["publish_approval"] = {"revision": "sha256:aggregate-release"}
        (episode_dir / "episode.json").write_text(json.dumps(episode))
        with (
            patch(
                "server.routes.pipeline.longform_publication_snapshot",
                return_value=_longform_gate(safe=False),
            ),
            patch("agents.pipeline.load_config", return_value={}),
            patch("server.routes.pipeline._start_pipeline_thread") as start,
        ):
            response = client.post(
                "/api/episodes/ep_001/longform-publication/publish",
                json={
                    "expected_revision": "sha256:longform-release",
                    "destinations": ["youtube"],
                },
            )

        assert response.status_code == 409
        start.assert_not_called()
