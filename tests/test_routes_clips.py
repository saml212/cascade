"""Tests for clip API routes."""

# ruff: noqa: F811 - imported pytest fixtures are intentionally shadowed by arguments

import asyncio
import json
import threading
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from tests.test_routes_episodes import _create_episode, test_client  # noqa: F401


def _add_clips(episodes_dir, episode_id, clips):
    ep_dir = episodes_dir / episode_id
    with open(ep_dir / "clips.json", "w") as f:
        json.dump({"clips": clips}, f)


SAMPLE_CLIPS = [
    {
        "id": "clip_01",
        "start_seconds": 60,
        "end_seconds": 120,
        "start": 60,
        "end": 120,
        "duration": 60,
        "title": "Clip 1",
        "virality_score": 8,
        "status": "pending",
        "speaker": "L",
    },
    {
        "id": "clip_02",
        "start_seconds": 300,
        "end_seconds": 360,
        "start": 300,
        "end": 360,
        "duration": 60,
        "title": "Clip 2",
        "virality_score": 5,
        "status": "pending",
        "speaker": "R",
    },
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
    def test_approve_clip_binds_current_render(self, test_client, monkeypatch):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        import server.routes.clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_current_render",
            lambda _episode_dir, _clip: {"fingerprint": "sha256:current"},
        )
        resp = client.post("/api/episodes/ep_001/clips/clip_01/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ][0]
        assert stored["approved_render_fingerprint"] == "sha256:current"
        assert stored["approved_revision"].startswith("sha256:")

    def test_approve_clip_rejects_unrendered_candidate(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        resp = client.post("/api/episodes/ep_001/clips/clip_01/approve")

        assert resp.status_code == 409
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ][0]
        assert stored["status"] == "pending"

    def test_reject_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.post("/api/episodes/ep_001/clips/clip_01/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ][0]
        assert stored["selection_status"] == "rejected"

    def test_select_does_not_grant_final_approval(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        resp = client.post("/api/episodes/ep_001/clips/clip_01/select")

        assert resp.status_code == 200
        assert resp.json()["selection_status"] == "selected"
        assert resp.json()["status"] == "pending"

    def test_alternative_uses_configured_generation_path(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        generated = {
            "rejected_clip_id": "clip_01",
            "alternative": {"id": "clip_03", "title": "Fresh choice"},
            "generation": {"provider": "openai", "model": "test-model"},
        }

        from agents.clip_miner import ClipMinerAgent

        monkeypatch.setattr(
            ClipMinerAgent,
            "generate_alternative",
            lambda _self, _clip_id: generated,
        )
        response = client.post("/api/episodes/ep_001/clips/clip_01/alternative")

        assert response.status_code == 200
        assert response.json() == generated

    def test_bulk_approval_is_atomic_when_one_render_is_stale(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        import server.routes.clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_current_render",
            lambda _episode_dir, clip: (
                {"fingerprint": "sha256:current"} if clip["id"] == "clip_01" else None
            ),
        )
        resp = client.post(
            "/api/episodes/ep_001/clips/bulk/approve",
            json={"clip_ids": ["clip_01", "clip_02"]},
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["clip_ids"] == ["clip_02"]
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ]
        assert [clip["status"] for clip in stored] == ["pending", "pending"]

    def test_bulk_reject_preserves_final_approval(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        clips = [dict(SAMPLE_CLIPS[0], status="approved"), SAMPLE_CLIPS[1]]
        _add_clips(episodes_dir, "ep_001", clips)

        resp = client.post(
            "/api/episodes/ep_001/clips/bulk/reject", json={"max_score": 10}
        )

        assert resp.status_code == 200
        assert resp.json()["rejected"] == ["clip_02"]
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ]
        assert [clip["status"] for clip in stored] == ["approved", "rejected"]


class TestDistributionSelection:
    @staticmethod
    def _state(candidate, *, current=True, approval_current=True):
        variant_id = candidate.get("distribution_variant_id")
        return {
            "version": variant_id or "base",
            "variant_id": variant_id,
            "label": "Motion background" if variant_id else "Base",
            "current": current,
            "approval_current": approval_current,
            "revision": "sha256:selected-review",
        }

    def test_selects_exact_approved_variant_and_can_restore_base(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(SAMPLE_CLIPS[0], status="approved")
        _add_clips(episodes_dir, "ep_001", [clip])

        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "publication_change_lock",
            lambda *_: {"change_locked": False, "change_lock_reason": None},
        )
        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )

        selected = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": "background_motion_v1",
                "expected_revision": "sha256:selected-review",
            },
        )
        restored = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": None,
                "expected_revision": "sha256:selected-review",
            },
        )

        assert selected.status_code == 200
        assert selected.json()["distribution"] == {
            "version": "background_motion_v1",
            "variant_id": "background_motion_v1",
            "label": "Motion background",
            "current": True,
            "approval_current": True,
            "revision": "sha256:selected-review",
            "change_locked": False,
            "change_lock_reason": None,
        }
        assert restored.status_code == 200
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored

    @pytest.mark.parametrize(
        ("current", "approval_current", "revision", "detail"),
        (
            (True, True, "sha256:changed", "changed; refresh"),
            (False, True, "sha256:selected-review", "current render"),
            (True, False, "sha256:selected-review", "Approve this exact"),
        ),
    )
    def test_selection_fails_closed_for_stale_missing_or_unapproved_version(
        self,
        test_client,
        monkeypatch,
        current,
        approval_current,
        revision,
        detail,
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "publication_change_lock",
            lambda *_: {"change_locked": False, "change_lock_reason": None},
        )
        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: (
                self._state(
                    candidate,
                    current=current,
                    approval_current=approval_current,
                )
                | {"revision": revision}
            ),
        )

        response = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": "background_motion_v1",
                "expected_revision": "sha256:selected-review",
            },
        )

        assert response.status_code == 409
        assert detail in response.json()["detail"]
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored

    def test_unknown_variant_is_rejected_without_persisting(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        response = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={"variant_id": "unknown", "expected_revision": "sha256:any"},
        )

        assert response.status_code == 404
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored

    def test_any_recorded_receipt_locks_legacy_base_selection(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "release_revision": "sha256:older-release",
                    "shorts": [
                        {
                            "clip_id": "clip_01",
                            "status": "submitted",
                            "request_id": "legacy-job",
                        }
                    ],
                }
            )
        )

        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        response = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": "background_motion_v1",
                "expected_revision": "sha256:selected-review",
            },
        )

        assert response.status_code == 409
        assert "historical or current publication receipt" in response.json()["detail"]
        assert "explicit re-release identity" in response.json()["detail"]

    def test_pre_schema_receipt_without_release_or_version_still_locks_base(
        self, test_client
    ):
        _client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "shorts": [
                        {"clip_id": "clip_01", "status": "unknown"},
                    ]
                }
            )
        )

        from server.routes import clips as clips_mod

        state = clips_mod.publication_change_lock(episode_dir, "clip_01")
        assert state["change_locked"] is True
        assert "explicit re-release identity" in state["change_lock_reason"]

    def test_failed_receipt_does_not_lock_and_malformed_state_fails_closed(
        self, test_client
    ):
        _client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        receipt_path = episode_dir / "publish.json"
        receipt_path.write_text(
            json.dumps({"shorts": [{"clip_id": "clip_01", "status": "failed"}]})
        )

        from server.routes import clips as clips_mod

        assert clips_mod.publication_change_lock(episode_dir, "clip_01") == {
            "change_locked": False,
            "change_lock_reason": None,
        }
        receipt_path.write_text(json.dumps({"shorts": ["not-a-receipt"]}))
        malformed = clips_mod.publication_change_lock(episode_dir, "clip_01")
        assert malformed["change_locked"] is True
        assert "cannot be verified" in malformed["change_lock_reason"]
        receipt_path.write_text(json.dumps({"shorts": [{"status": "submitted"}]}))
        unattributed = clips_mod.publication_change_lock(episode_dir, "clip_01")
        assert unattributed["change_locked"] is True
        assert "cannot be verified" in unattributed["change_lock_reason"]

    def test_selection_waits_until_inflight_publish_receipt_is_saved(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        from agents import publish as publish_mod
        from server.routes import clips as clips_mod

        shared_lock = threading.Lock()

        @contextmanager
        def guarded_publication(_episodes_dir):
            with shared_lock:
                yield

        monkeypatch.setattr(publish_mod, "publication_lock", guarded_publication)
        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        remote_submission_finished = threading.Event()
        allow_receipt_write = threading.Event()
        agent = publish_mod.PublishAgent(episode_dir, {})

        def execute():
            remote_submission_finished.set()
            assert allow_receipt_write.wait(timeout=2)
            return {
                "release_revision": "sha256:old-release",
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": "submitted",
                        "external_id": "legacy-base-job",
                    }
                ],
            }

        monkeypatch.setattr(agent, "execute", execute)
        publish_thread = threading.Thread(target=agent.run)
        publish_thread.start()
        assert remote_submission_finished.wait(timeout=2)

        outcome = {}

        def select():
            try:
                outcome["result"] = clips_mod._select_clip_distribution_locked(
                    "ep_001",
                    "clip_01",
                    clips_mod.DistributionSelectionRequest(
                        variant_id="background_motion_v1",
                        expected_revision="sha256:selected-review",
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome["error"] = exc

        selection_thread = threading.Thread(target=select)
        selection_thread.start()
        selection_thread.join(timeout=0.05)
        assert selection_thread.is_alive()

        allow_receipt_write.set()
        publish_thread.join(timeout=2)
        selection_thread.join(timeout=2)

        assert not publish_thread.is_alive()
        assert not selection_thread.is_alive()
        assert isinstance(outcome.get("error"), HTTPException)
        assert outcome["error"].status_code == 409
        assert "explicit re-release identity" in outcome["error"].detail
        assert (episode_dir / "publish.json").is_file()
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored


class TestManualClip:
    def test_add_manual_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={
                "start_seconds": 500.0,
                "end_seconds": 560.0,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["manual"] is True

    def test_invalid_duration(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={
                "start_seconds": 100.0,
                "end_seconds": 50.0,  # end < start
            },
        )
        assert resp.status_code == 400

    def test_rejects_negative_start_without_creating_clip(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")

        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={"start_seconds": -1, "end_seconds": 30},
        )

        assert resp.status_code == 422
        assert not (episode_dir / "clips.json").exists()

    def test_rejects_end_beyond_probed_source_duration(self, test_client, monkeypatch):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "source_merged.mp4").write_bytes(b"probe fixture")

        import server.routes.clips as clips_mod

        monkeypatch.setattr(clips_mod, "get_duration", lambda _path: 100.0)
        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={"start_seconds": 50, "end_seconds": 101},
        )

        assert resp.status_code == 422
        assert resp.json()["detail"]["source_duration_seconds"] == 100.0
        assert not (episode_dir / "clips.json").exists()

    def test_rejects_nonfinite_json_without_server_error(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")

        for literal in ("NaN", "1e999", "-1e999"):
            resp = client.post(
                "/api/episodes/ep_001/clips/manual",
                content=f'{{"start_seconds":{literal},"end_seconds":30}}',
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422
        assert not (episode_dir / "clips.json").exists()


class TestClipMutation:
    def test_typed_patch_keeps_clocks_in_sync_and_invalidates_approval(
        self, test_client
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            approved_revision="sha256:old-copy",
            approved_render_fingerprint="sha256:old-render",
            metadata={"tiktok": {"caption": "keep", "hashtags": ["#old"]}},
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        resp = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            json={
                "title": "New title",
                "hook_text": "New hook",
                "compelling_reason": "It resolves the premise",
                "virality_score": 9.5,
                "speaker": "Speaker 1",
                "hashtags": ["#new"],
                "start_seconds": 65,
                "end_seconds": 125,
                "metadata": {"tiktok": {"caption": "updated"}},
            },
        )

        assert resp.status_code == 200
        updated = resp.json()
        assert updated["start"] == updated["start_seconds"] == 65
        assert updated["end"] == updated["end_seconds"] == 125
        assert updated["duration"] == 60
        assert updated["metadata"]["tiktok"] == {
            "caption": "updated",
            "hashtags": ["#old"],
        }
        assert updated["status"] == "pending"
        assert updated["selection_status"] == "selected"
        assert "approved_revision" not in updated
        assert "approved_render_fingerprint" not in updated

    def test_time_patch_rejects_nonfinite_and_out_of_source_bounds(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            start=10,
            start_seconds=10,
            end=50,
            end_seconds=50,
            duration=40,
        )
        _add_clips(episodes_dir, "ep_001", [clip])
        (episode_dir / "source_merged.mp4").write_bytes(b"probe fixture")

        import server.routes.clips as clips_mod

        monkeypatch.setattr(clips_mod, "get_duration", lambda _path: 100.0)
        nonfinite = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            content='{"start_seconds":NaN}',
            headers={"content-type": "application/json"},
        )
        past_end = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            json={"end_seconds": 101},
        )

        assert nonfinite.status_code == 422
        assert past_end.status_code == 422
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert stored["start_seconds"] == 10
        assert stored["end_seconds"] == 50

    def test_patch_rejects_nonfinite_score_without_persisting(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        resp = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            content='{"virality_score":1e999}',
            headers={"content-type": "application/json"},
        )

        assert resp.status_code == 422
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert stored["virality_score"] == 8

    def test_patch_rejects_nested_nonfinite_metadata(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        resp = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            content='{"metadata":{"tiktok":{"score":NaN}}}',
            headers={"content-type": "application/json"},
        )

        assert resp.status_code == 422
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "metadata" not in stored

    def test_delete_removes_only_requested_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        resp = client.delete("/api/episodes/ep_001/clips/clip_01")

        assert resp.status_code == 200
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ]
        assert [clip["id"] for clip in stored] == ["clip_02"]

    def test_render_demotes_approval_when_fingerprint_changes(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            approved_revision="sha256:review",
            approved_render_fingerprint="sha256:old",
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        import agents.shorts_render as render_mod

        monkeypatch.setattr(
            render_mod,
            "render_single_clip",
            lambda episode_dir, _config, clip_id: {
                "clip_id": clip_id,
                "output_path": str(episode_dir / "shorts" / f"{clip_id}.mp4"),
                "caption_path": str(episode_dir / "subtitles" / f"{clip_id}.ass"),
                "reused": False,
                "render": {"fingerprint": "sha256:new"},
            },
            raising=False,
        )
        resp = client.post("/api/episodes/ep_001/clips/clip_01/render")

        assert resp.status_code == 200
        assert resp.json()["render"]["fingerprint"] == "sha256:new"
        stored = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert stored["status"] == "pending"
        assert "approved_revision" not in stored
        job = json.loads((ep_dir / "work" / "clip_render_jobs.json").read_text())[
            "jobs"
        ]["clip_01"]
        assert job["status"] == "succeeded"
        assert job["render_fingerprint"] == "sha256:new"

    def test_render_rejects_duplicate_active_job(self, test_client):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        import server.routes.clips as clips_mod

        key = clips_mod._render_job_key(ep_dir, "clip_01")
        with clips_mod._render_jobs_lock:
            clips_mod._active_render_jobs.add(key)
        try:
            response = client.post("/api/episodes/ep_001/clips/clip_01/render")
        finally:
            with clips_mod._render_jobs_lock:
                clips_mod._active_render_jobs.discard(key)

        assert response.status_code == 409
        assert response.json()["detail"] == "Clip clip_01 is already rendering"

    def test_audio_repair_uses_public_adapter_and_demotes_prior_approval(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            approved_revision="sha256:review",
            approved_render_fingerprint="sha256:current",
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        import agents.shorts_render as render_mod

        monkeypatch.setattr(
            render_mod,
            "repair_single_clip_audio",
            lambda episode_dir, _config, clip_id: {
                "clip_id": clip_id,
                "output_path": str(episode_dir / "shorts" / f"{clip_id}.mp4"),
                "reused": True,
                "audio_repaired": True,
                "video_reencoded": False,
                "render": {"fingerprint": "sha256:current"},
            },
            raising=False,
        )

        response = client.post("/api/episodes/ep_001/clips/clip_01/repair-audio")

        assert response.status_code == 200
        assert response.json()["audio_repaired"] is True
        assert response.json()["video_reencoded"] is False
        stored = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert stored["status"] == "pending"
        assert stored["selection_status"] == "selected"
        assert "approved_revision" not in stored
        job = json.loads((ep_dir / "work" / "clip_render_jobs.json").read_text())[
            "jobs"
        ]["clip_01"]
        assert job["audio_repaired"] is True
        assert job["video_reencoded"] is False

    def test_variant_render_uses_qualified_job_and_preserves_base_approval(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            selection_status="selected",
            approved_revision="sha256:base-review",
            approved_render_fingerprint="sha256:base-render",
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        import agents.shorts_render as render_mod

        monkeypatch.setattr(
            render_mod,
            "render_single_clip_variant",
            lambda episode_dir, _config, clip_id, variant_id, asset_id: {
                "clip_id": clip_id,
                "variant_id": variant_id,
                "asset_id": asset_id,
                "output_path": str(
                    episode_dir / "short_variants" / variant_id / f"{clip_id}.mp4"
                ),
                "reused": False,
                "render": {"fingerprint": "sha256:variant"},
            },
        )

        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/variants/background_motion_v1/render",
            json={"asset_id": "motion_v1"},
        )

        assert response.status_code == 200
        assert response.json()["asset_id"] == "motion_v1"
        assert json.loads((ep_dir / "clips.json").read_text())["clips"][0] == clip
        job = json.loads((ep_dir / "work" / "clip_render_jobs.json").read_text())[
            "jobs"
        ]["clip_01@background_motion_v1"]
        assert job["status"] == "succeeded"

    def test_variant_routes_reject_unknown_id(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/variants/not_supported/render",
            json={},
        )

        assert response.status_code == 404
        assert "Unknown short variant" in response.json()["detail"]

    def test_variant_approval_binds_exact_revision_without_approving_base(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(SAMPLE_CLIPS[0])
        _add_clips(episodes_dir, "ep_001", [clip])
        record = {
            "fingerprint": "sha256:variant",
            "output": {
                "content_revision": "sha256:pixels",
                "scan_identity": {"inode": 1},
            },
        }

        from agents.qa import clip_review_revision
        from server.routes import clips as clips_mod

        revision = clip_review_revision(clip, record, None)
        monkeypatch.setattr(clips_mod, "_current_render", lambda *_args: {})
        monkeypatch.setattr(
            clips_mod,
            "background_variant_state",
            lambda *_args, **_kwargs: (
                record,
                {"current": True, "playable": True},
            ),
        )
        saved = []
        monkeypatch.setattr(
            clips_mod,
            "save_background_variant_approval",
            lambda *_args: saved.append(_args) or record,
        )

        stale = client.post(
            "/api/episodes/ep_001/clips/clip_01/variants/background_motion_v1/approve",
            json={"expected_revision": "sha256:stale"},
        )
        approved = client.post(
            "/api/episodes/ep_001/clips/clip_01/variants/background_motion_v1/approve",
            json={"expected_revision": revision},
        )

        assert stale.status_code == 409
        assert approved.status_code == 200
        assert approved.json()["approved_revision"] == revision
        assert len(saved) == 1
        assert json.loads((ep_dir / "clips.json").read_text())["clips"][0] == clip

    def test_current_render_requires_provenance_current_speaker_plan(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        audio = ep_dir / "work" / "audio_mix.wav"
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b"audio")

        from agents import speaker_cut
        from lib import audio_mix, delivery_video
        from server.routes import clips as clips_mod

        monkeypatch.setattr(speaker_cut, "current_speaker_segments", lambda *_: None)
        monkeypatch.setattr(audio_mix, "selected_audio_source", lambda *_: audio)
        called = []
        monkeypatch.setattr(
            delivery_video,
            "current_short_render",
            lambda *_: called.append(True) or {"fingerprint": "unexpected"},
        )

        assert clips_mod._current_render(ep_dir, SAMPLE_CLIPS[0]) is None
        assert called == []

    async def test_cancelled_request_keeps_clip_locked_until_worker_finishes(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])
        started = threading.Event()
        release = threading.Event()

        import agents.shorts_render as render_mod
        from server.routes import clips as clips_mod

        def render(*_args):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test worker timed out")
            return {
                "reused": False,
                "render": {"fingerprint": "sha256:variant"},
            }

        monkeypatch.setattr(render_mod, "render_single_clip_variant", render)
        request = asyncio.create_task(
            clips_mod._run_clip_render_operation(
                "ep_001",
                "clip_01",
                variant_id="background_motion_v1",
                asset_id="motion_v1",
            )
        )
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

            with pytest.raises(HTTPException) as blocked:
                await clips_mod._run_clip_render_operation(
                    "ep_001", "clip_01", variant_id="background_motion_v1"
                )
            assert getattr(blocked.value, "status_code", None) == 409
            assert (
                clips_mod.render_job_state(ep_dir, "clip_01@background_motion_v1")[
                    "status"
                ]
                == "rendering"
            )
        finally:
            release.set()

        for _ in range(100):
            if not clips_mod._active_render_jobs:
                break
            await asyncio.sleep(0.01)
        assert not clips_mod._active_render_jobs
        assert (
            clips_mod.render_job_state(ep_dir, "clip_01@background_motion_v1")["status"]
            == "succeeded"
        )

    async def test_render_job_setup_failure_releases_clip_lock(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        from server.routes import clips as clips_mod

        def fail_write(*_args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(clips_mod, "_write_render_job", fail_write)

        with pytest.raises(HTTPException, match="disk unavailable") as failed:
            await clips_mod._run_clip_render_operation("ep_001", "clip_01")
        assert failed.value.status_code == 500
        assert not clips_mod._active_render_jobs
