"""Tests for clip API routes."""

# ruff: noqa: F811 - imported pytest fixtures are intentionally shadowed by arguments

import json

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
