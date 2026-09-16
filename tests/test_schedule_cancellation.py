"""Receipt-bound Upload-Post schedule cancellation tests."""

import json

import pytest

from tests.test_routes_episodes import _create_episode

pytest_plugins = ["tests.test_routes_episodes"]

REQUEST_ID = "47db1913-4d32-4acf-bcfe-31763c50e9c2"
SCHEDULED_DATE = "2099-01-05T09:00:00-08:00"


def _receipt():
    return {
        "clip_id": "clip_01",
        "status": "submitted",
        "platforms": ["youtube", "tiktok"],
        "request_id": "cascade-short-old",
        "external_id": "cascade-short-old",
        "job_id": "job-old",
        "scheduled": True,
        "scheduled_date": SCHEDULED_DATE,
        "timezone": "America/Los_Angeles",
        "version": "base",
        "variant_id": None,
        "render_fingerprint": "sha256:base-render",
        "approval_revision": "sha256:base-approval",
    }


def _remote_job():
    return {
        "job_id": "job-old",
        "external_id": "cascade-short-old",
        "profile_username": "up",
        "scheduled_date": "2099-01-05T17:00:00",
        "original_scheduled_str": "2099-01-05T09:00:00",
        "original_timezone": "America/Los_Angeles",
        "platforms": ["youtube", "tiktok"],
        "source_filename": "clip_01.mp4",
        "fields": {"external_id": "cascade-short-old"},
    }


def _receipt_for(job_id, external_id, platform):
    return {
        **_receipt(),
        "platforms": [platform],
        "request_id": external_id,
        "external_id": external_id,
        "job_id": job_id,
    }


def _remote_job_for(receipt):
    return {
        **_remote_job(),
        "job_id": receipt["job_id"],
        "external_id": receipt["external_id"],
        "platforms": receipt["platforms"],
        "fields": {"external_id": receipt["external_id"]},
    }


def _queued_evidence_for(receipt):
    rows = [
        {
            "job_id": receipt["job_id"],
            "external_id": receipt["external_id"],
            "profile_username": "up",
            "platform": platform,
            "is_scheduled": True,
            "upload_status": "queued",
            "success": None,
            "post_url": None,
            "run_date": "2099-01-05T17:00:00",
        }
        for platform in receipt["platforms"]
    ]
    return {
        "status_not_found": False,
        "status": {
            "job_id": receipt["job_id"],
            "external_id": receipt["external_id"],
            "profile_username": "up",
            "status": "queued",
            "success": None,
        },
        "history": {"in_progress": rows, "history": []},
    }


def _queued_evidence(*, state="queued", success=None):
    rows = [
        {
            "job_id": "job-old",
            "external_id": "cascade-short-old",
            "profile_username": "up",
            "platform": platform,
            "is_scheduled": True,
            "upload_status": state,
            "success": success,
            "post_url": None,
            "run_date": "2099-01-05T17:00:00",
        }
        for platform in ("youtube", "tiktok")
    ]
    return {
        "status_not_found": False,
        "status": {
            "job_id": "job-old",
            "external_id": "cascade-short-old",
            "profile_username": "up",
            "status": state,
            "success": success,
        },
        "history": {"in_progress": rows, "history": []},
    }


def _scheduler_queued_evidence():
    """Anonymized shape returned for Ty's unattempted scheduled job."""
    evidence = _queued_evidence()
    evidence["status"] = {
        "job_id": "job-old",
        "request_id": "cascade-short-old",
        "external_id": "cascade-short-old",
        "status": "queued",
        "scheduler_status": "pending",
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "retryable": 0,
        "total": 2,
        "results": [
            {
                "platform": platform,
                "status": "queued",
                "attempts": 0,
                "success": False,
            }
            for platform in ("youtube", "tiktok")
        ],
    }
    return evidence


def _absent_evidence():
    return {
        "status_not_found": True,
        "status": None,
        "history": {"in_progress": [], "history": []},
    }


def _inert_tombstone_evidence():
    return {
        "status_not_found": False,
        "status": {
            "job_id": "job-old",
            "external_id": "cascade-short-old",
            "status": "failed",
            "message": "Upload appears to have failed (no activity for over 1 hour)",
            "completed": 0,
            "total": 2,
            "results": [],
        },
        "history": {"in_progress": [], "history": []},
    }


def _state(candidate):
    variant_id = candidate.get("distribution_variant_id")
    return {
        "version": variant_id or "base",
        "variant_id": variant_id,
        "label": "Motion background" if variant_id else "Base",
        "current": True,
        "approval_current": True,
        "revision": "sha256:motion-target",
        "render_fingerprint": "sha256:motion-render",
    }


def _setup(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    (episode_dir / "clips.json").write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "id": "clip_01",
                        "status": "approved",
                        "start_seconds": 1,
                        "end_seconds": 2,
                    }
                ]
            }
        )
    )
    original = _receipt()
    (episode_dir / "publish.json").write_text(
        json.dumps({"profile_username": "up", "shorts": [original]})
    )
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "secret")
    monkeypatch.setenv("UPLOAD_POST_USER", "up")

    from agents.publish import PublishAgent
    from server.routes import clips

    monkeypatch.setattr(clips, "_distribution_state", lambda *_args: _state(_args[1]))
    deleted = {"value": False, "calls": 0}
    monkeypatch.setattr(
        PublishAgent,
        "_remote_schedule",
        lambda *_args: [] if deleted["value"] else [_remote_job()],
    )
    monkeypatch.setattr(
        clips,
        "_upload_post_job_evidence",
        lambda *_args: _absent_evidence() if deleted["value"] else _queued_evidence(),
    )

    def delete(*_args):
        deleted["calls"] += 1
        deleted["value"] = True
        return {
            "http_status": 200,
            "response": {"success": True, "message": "Job job-old cancelled"},
        }

    monkeypatch.setattr(clips, "_delete_upload_post_schedule", delete)
    request = {
        "variant_id": "background_motion_v1",
        "expected_revision": "sha256:motion-target",
        "request_id": REQUEST_ID,
        "actor": "release-operator",
        "reason": "Replace the approved Base schedule with Motion",
    }
    return client, episode_dir, original, deleted, request


def test_exact_preview_cancel_and_idempotent_rerelease(test_client, monkeypatch):
    client, episode_dir, original, deleted, request = _setup(test_client, monkeypatch)

    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["receipt"] == {
        "revision": body["receipt"]["revision"],
        "job_id": "job-old",
        "external_id": "cascade-short-old",
        "profile_username": "up",
        "scheduled_date": SCHEDULED_DATE,
        "platforms": ["youtube", "tiktok"],
    }
    assert body["remote_job"] == _remote_job()
    assert body["execute"]["expected_snapshot_revision"] == body["snapshot_revision"]

    cancelled = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=body["execute"],
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["replacement_safe"] is True
    assert deleted["calls"] == 1

    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert stored["pre_cancellation_receipt"] == original
    assert stored["status"] == "cancelled"
    assert stored["scheduled"] is False
    assert {value["state"] for value in stored["terminal_destinations"].values()} == {
        "cancelled"
    }
    from agents.publish import (
        receipt_terminal_destinations,
        validated_schedule_cancellation,
    )

    assert validated_schedule_cancellation(stored)["operation_id"] == REQUEST_ID
    assert receipt_terminal_destinations(stored, profile_username="up") is not None
    reconciled = client.post("/api/episodes/ep_001/check-upload-urls")
    assert reconciled.status_code == 200
    assert reconciled.json()["shorts"][0]["status"] == "cancelled"

    from agents.publish import PublishAgent

    assert PublishAgent(episode_dir, {})._occupied_schedule("secret", "up") == []

    # A completed retry returns its stored result before consulting live target/provider state.
    from server.routes import clips

    monkeypatch.setattr(
        clips,
        "_distribution_state",
        lambda *_args: (_ for _ in ()).throw(AssertionError),
    )
    repeated = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=body["execute"],
    )
    assert repeated.status_code == 200
    assert deleted["calls"] == 1

    wrong_clip = client.post(
        "/api/episodes/ep_001/clips/clip_02/schedule-cancellation",
        json=body["execute"],
    )
    assert wrong_clip.status_code == 409
    assert "different clip" in wrong_clip.json()["detail"]

    # A cancelled same-identity receipt is history, never a reusable submission.
    agent = PublishAgent(episode_dir, {})
    monkeypatch.setattr(agent, "_identity", lambda *_args: stored["external_id"])
    with pytest.raises(RuntimeError, match="explicit re-release"):
        agent._publish_shorts(
            [{"id": "clip_01"}],
            {
                "clip_01": {
                    "version": "base",
                    "variant_id": None,
                    "render_fingerprint": "sha256:base-render",
                    "revision": "sha256:base-approval",
                }
            },
            {},
            {},
            [stored],
            {},
            ["youtube", "tiktok"],
            "",
            "",
            "release-revision",
            "secret",
            "up",
        )

    monkeypatch.setattr(clips, "_distribution_state", lambda *_args: _state(_args[1]))
    rerelease = client.post(
        "/api/episodes/ep_001/clips/clip_01/re-release",
        json=cancelled.json()["next"]["body"],
    )
    assert rerelease.status_code == 200
    assert rerelease.json()["status"] == "prepared"

    publish = json.loads((episode_dir / "publish.json").read_text())
    replacement = {
        "clip_id": "clip_01",
        "status": "failed",
        "platforms": ["youtube"],
        "request_id": "replacement",
        "rerelease_request_id": REQUEST_ID,
        "terminal_destinations": {"youtube": {"state": "failed"}},
        "status_history": [
            {
                "profile_username": "up",
                "evidence_source": "status",
                "terminal_destinations": {"youtube": {"state": "failed"}},
            }
        ],
    }
    merged = PublishAgent._merge_short_receipts([replacement], publish["shorts"])
    from agents.publish import short_rerelease_state, validated_short_receipts

    validated_short_receipts({"shorts": merged})
    assert (
        short_rerelease_state({"shorts": merged}, "clip_01", profile_username="up")[
            "cancellation_request"
        ]
        is None
    )


def test_exact_job_cancellation_handles_three_jobs_without_replacement(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    receipts = [
        _receipt_for("job-youtube", "external-youtube", "youtube"),
        _receipt_for("job-instagram", "external-instagram", "instagram"),
        _receipt_for("job-x", "external-x", "x"),
    ]
    (episode_dir / "publish.json").write_text(
        json.dumps({"profile_username": "up", "shorts": receipts})
    )
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "secret")
    monkeypatch.setenv("UPLOAD_POST_USER", "up")

    from agents.publish import PublishAgent
    from server.routes import clips

    deleted = set()
    delete_calls = []
    by_job = {receipt["job_id"]: receipt for receipt in receipts}
    monkeypatch.setattr(
        PublishAgent,
        "_remote_schedule",
        lambda *_args: [
            _remote_job_for(receipt)
            for receipt in receipts
            if receipt["job_id"] not in deleted
        ],
    )
    monkeypatch.setattr(
        clips,
        "_upload_post_job_evidence",
        lambda _api_key, _profile, job_id: (
            _absent_evidence()
            if job_id in deleted
            else _queued_evidence_for(by_job[job_id])
        ),
    )

    def delete(_api_key, job_id):
        delete_calls.append(job_id)
        deleted.add(job_id)
        return {
            "http_status": 200,
            "response": {"success": True, "message": f"Job {job_id} cancelled"},
        }

    monkeypatch.setattr(clips, "_delete_upload_post_schedule", delete)

    last_execute = None
    for index, receipt in enumerate(receipts, start=1):
        path = (
            "/api/episodes/ep_001/clips/clip_01/scheduled-jobs/"
            f"{receipt['job_id']}/cancellation"
        )
        request = {
            "expected_external_id": receipt["external_id"],
            "request_id": f"00000000-0000-4000-8000-{index:012d}",
            "actor": "release-operator",
            "reason": "Remove the rejected queued creative",
        }
        preview = client.post(f"{path}/preview", json=request)
        assert preview.status_code == 200
        assert preview.json()["receipt"]["job_id"] == receipt["job_id"]
        last_execute = preview.json()["execute"]
        cancelled = client.post(path, json=last_execute)
        assert cancelled.status_code == 200
        assert cancelled.json()["prior_receipt_preserved"] is True
        assert "next" not in cancelled.json()

    assert delete_calls == [receipt["job_id"] for receipt in receipts]
    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"]
    from agents.publish import (
        EXACT_SCHEDULE_CANCELLATION_SCHEMA,
        short_rerelease_state,
        validated_schedule_cancellation,
    )

    assert all(receipt["status"] == "cancelled" for receipt in stored)
    assert all(
        receipt["pre_cancellation_receipt"]["scheduled"] is True
        and validated_schedule_cancellation(receipt)["schema"]
        == EXACT_SCHEDULE_CANCELLATION_SCHEMA
        for receipt in stored
    )
    eligibility = short_rerelease_state(
        {"shorts": stored}, "clip_01", profile_username="up"
    )
    assert eligibility["allowed"] is True
    assert eligibility["cancellation_request"] is None

    repeated = client.post(
        "/api/episodes/ep_001/clips/clip_01/scheduled-jobs/job-x/cancellation",
        json=last_execute,
    )
    assert repeated.status_code == 200
    assert delete_calls == [receipt["job_id"] for receipt in receipts]


def test_exact_job_preview_rejects_wrong_external_id(test_client, monkeypatch):
    client, _episode_dir, _original, deleted, _request = _setup(
        test_client, monkeypatch
    )
    response = client.post(
        "/api/episodes/ep_001/clips/clip_01/scheduled-jobs/job-old/cancellation/preview",
        json={
            "expected_external_id": "different-external-id",
            "request_id": REQUEST_ID,
            "actor": "release-operator",
            "reason": "Remove the rejected queued creative",
        },
    )

    assert response.status_code == 409
    assert "exact scheduled receipt" in response.json()["detail"]
    assert deleted["calls"] == 0


def test_exact_job_reconciles_ambiguous_delete_without_retrying_it(
    test_client, monkeypatch
):
    client, episode_dir, original, deleted, _request = _setup(test_client, monkeypatch)
    from server.routes import clips

    def ambiguous(_api_key, _job_id):
        deleted["calls"] += 1
        deleted["value"] = True
        return {"error": "connection reset after request"}

    monkeypatch.setattr(clips, "_delete_upload_post_schedule", ambiguous)
    path = "/api/episodes/ep_001/clips/clip_01/scheduled-jobs/job-old/cancellation"
    preview = client.post(
        f"{path}/preview",
        json={
            "expected_external_id": original["external_id"],
            "request_id": REQUEST_ID,
            "actor": "release-operator",
            "reason": "Remove the rejected queued creative",
        },
    ).json()
    cancelled = client.post(path, json=preview["execute"])

    assert cancelled.status_code == 200
    assert deleted["calls"] == 1
    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert stored["status"] == "cancelled"
    assert stored["schedule_cancellation"]["delete"]["error"] == (
        "connection reset after request"
    )
    repeated = client.post(path, json=preview["execute"])
    assert repeated.status_code == 200
    assert deleted["calls"] == 1


def test_legacy_ack_keeps_exact_cancelled_schedule_request(test_client, monkeypatch):
    client, episode_dir, _original, _deleted, request = _setup(test_client, monkeypatch)
    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    ).json()
    cancelled = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )
    assert cancelled.status_code == 200

    publish_path = episode_dir / "publish.json"
    publish = json.loads(publish_path.read_text())
    publish["shorts"].append(
        {
            "clip_id": "clip_01",
            "status": "failed",
            "error": "Legacy response could not be parsed",
        }
    )
    publish_path.write_text(json.dumps(publish))

    from agents.publish import short_rerelease_state

    state = short_rerelease_state(publish, "clip_01", profile_username="up")
    assert state["unresolved_history_acknowledgement_allowed"] is True
    assert state["cancellation_request"]["request_id"] == REQUEST_ID
    acknowledgement = {
        "acknowledge_unresolved_history_revision": state["history_revision"]
    }
    wrong = client.post(
        "/api/episodes/ep_001/clips/clip_01/re-release",
        json={
            **cancelled.json()["next"]["body"],
            **acknowledgement,
            "request_id": "aaef05f4-5b12-4425-9e88-a8f160a52337",
        },
    )
    exact = client.post(
        "/api/episodes/ep_001/clips/clip_01/re-release",
        json={**cancelled.json()["next"]["body"], **acknowledgement},
    )

    assert wrong.status_code == 409
    assert (
        "exact request and target bound to the cancellation" in wrong.json()["detail"]
    )
    assert exact.status_code == 200
    assert exact.json()["status"] == "prepared"


def test_ambiguous_delete_is_durable_and_never_repeated(test_client, monkeypatch):
    client, episode_dir, original, deleted, request = _setup(test_client, monkeypatch)
    from server.routes import clips

    def ambiguous(*_args):
        deleted["calls"] += 1
        return {"error": "connection reset after request"}

    monkeypatch.setattr(clips, "_delete_upload_post_schedule", ambiguous)
    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    ).json()
    first = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )
    second = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )

    assert first.status_code == second.status_code == 409
    assert "do not send another DELETE" in second.json()["detail"]
    assert deleted["calls"] == 1
    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert stored["pre_cancellation_receipt"] == original
    assert stored["scheduled"] is True
    assert stored["schedule_cancellation"]["state"] == "outcome_uncertain"
    pending = client.post("/api/episodes/ep_001/check-upload-urls")
    assert pending.status_code == 200
    assert pending.json()["shorts"][0]["status"] == "cancellation_pending"


def test_crash_after_durable_marker_cannot_repeat_delete(test_client, monkeypatch):
    client, episode_dir, _original, deleted, request = _setup(test_client, monkeypatch)
    from server.routes import clips

    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    ).json()

    def crash(*_args):
        deleted["calls"] += 1
        raise RuntimeError("process interrupted after request began")

    monkeypatch.setattr(clips, "_delete_upload_post_schedule", crash)
    model = clips.ScheduleCancellationRequest(**preview["execute"])
    with pytest.raises(RuntimeError, match="interrupted"):
        clips._cancel_schedule_locked("ep_001", "clip_01", model)

    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert stored["schedule_cancellation"]["state"] == "delete_started"
    retry = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )
    assert retry.status_code == 409
    assert "do not send another DELETE" in retry.json()["detail"]
    assert deleted["calls"] == 1


def test_publishing_race_after_delete_stays_reconcilable_without_second_delete(
    test_client, monkeypatch
):
    client, episode_dir, _original, deleted, request = _setup(test_client, monkeypatch)
    from server.routes import clips

    post_evidence = {
        "status_not_found": False,
        "status": {
            "job_id": "job-old",
            "external_id": "cascade-short-old",
            "profile_username": "up",
            "status": "publishing",
            "success": False,
        },
        "history": {"in_progress": [], "history": []},
    }
    monkeypatch.setattr(
        clips,
        "_upload_post_job_evidence",
        lambda *_args: post_evidence if deleted["value"] else _queued_evidence(),
    )
    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    ).json()
    first = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )
    assert first.status_code == 409
    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert stored["schedule_cancellation"]["state"] == "delete_confirmed"

    monkeypatch.setenv("UPLOAD_POST_USER", "different-profile")
    wrong_profile = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )
    assert wrong_profile.status_code == 409
    assert "exact Upload-Post account" in wrong_profile.json()["detail"]
    assert deleted["calls"] == 1
    monkeypatch.setenv("UPLOAD_POST_USER", "up")

    monkeypatch.setattr(
        clips, "_upload_post_job_evidence", lambda *_args: _inert_tombstone_evidence()
    )
    retry = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )
    assert retry.status_code == 200
    assert deleted["calls"] == 1
    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert stored["status"] == "cancelled"
    assert stored["schedule_cancellation"]["state"] == "cancelled"


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (_queued_evidence(state="processing", success=False), "queued work"),
        (_queued_evidence(state="queued", success=False), "queued work"),
        (
            {
                **_queued_evidence(),
                "status": {
                    **_queued_evidence()["status"],
                    "upload_status": "queued",
                    "upload_overall_status": "processing",
                },
            },
            "queued work",
        ),
        (
            {
                **_queued_evidence(),
                "status": {
                    **_queued_evidence()["status"],
                    "results": [
                        {
                            "platform": "youtube",
                            "status": "publishing",
                            "success": None,
                        }
                    ],
                },
            },
            "Provider status",
        ),
    ],
)
def test_preview_rejects_unsafe_provider_states(
    test_client, monkeypatch, evidence, message
):
    client, _episode_dir, _original, _deleted, request = _setup(
        test_client, monkeypatch
    )
    from server.routes import clips

    monkeypatch.setattr(clips, "_upload_post_job_evidence", lambda *_args: evidence)
    response = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    )
    assert response.status_code == 409
    assert message in response.json()["detail"]


def test_preview_accepts_exact_unattempted_scheduler_status(test_client, monkeypatch):
    client, _episode_dir, _original, _deleted, request = _setup(
        test_client, monkeypatch
    )
    from server.routes import clips

    monkeypatch.setattr(
        clips, "_upload_post_job_evidence", lambda *_args: _scheduler_queued_evidence()
    )

    response = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancellable"


@pytest.mark.parametrize(
    "mutation",
    [
        "failed_counter",
        "attempted_child",
        "published_child",
        "duplicate_platform",
        "wrong_child_identity",
        "wrong_child_profile",
    ],
)
def test_unattempted_scheduler_status_still_fails_closed(mutation):
    from agents.publish import schedule_cancellation_provider_safe

    evidence = _scheduler_queued_evidence()
    status = evidence["status"]
    if mutation == "failed_counter":
        status["failed"] = 1
    elif mutation == "attempted_child":
        status["results"][0]["attempts"] = 1
    elif mutation == "published_child":
        status["results"][0]["post_url"] = "https://example.com/published"
    elif mutation == "duplicate_platform":
        status["results"][0]["platform"] = "tiktok"
    elif mutation == "wrong_child_identity":
        status["results"][0]["job_id"] = "other-job"
    else:
        status["results"][0]["profile_username"] = "other-profile"

    safe, _ = schedule_cancellation_provider_safe(
        _receipt(), evidence, profile_username="up", after_delete=False
    )

    assert safe is False


def test_changed_snapshot_and_traversal_fail_before_delete(test_client, monkeypatch):
    client, _episode_dir, _original, deleted, request = _setup(test_client, monkeypatch)
    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    ).json()
    changed = {
        **preview["execute"],
        "expected_snapshot_revision": "sha256:changed",
    }
    response = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation", json=changed
    )
    traversal = client.post(
        "/api/episodes/../ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )
    assert response.status_code == 409
    assert traversal.status_code in {404, 405}
    assert deleted["calls"] == 0


def test_tampered_cancellation_proof_fails_closed(test_client, monkeypatch):
    client, episode_dir, _original, _deleted, request = _setup(test_client, monkeypatch)
    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    ).json()
    assert (
        client.post(
            "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
            json=preview["execute"],
        ).status_code
        == 200
    )
    publish = json.loads((episode_dir / "publish.json").read_text())
    publish["shorts"][0]["schedule_cancellation"]["post_delete"]["calendar_absent"] = (
        False
    )
    (episode_dir / "publish.json").write_text(json.dumps(publish))

    response = client.post(
        "/api/episodes/ep_001/clips/clip_01/re-release",
        json={
            key: value
            for key, value in preview["execute"].items()
            if key != "expected_snapshot_revision"
        },
    )
    assert response.status_code == 409
    assert "cannot be verified" in response.json()["detail"]


def test_target_is_rechecked_after_provider_calls_before_delete(
    test_client, monkeypatch
):
    client, episode_dir, _original, deleted, request = _setup(test_client, monkeypatch)
    preview = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation/preview",
        json=request,
    ).json()
    from server.routes import clips

    calls = {"value": 0}

    def changing_target(*args):
        calls["value"] += 1
        state = _state(args[1])
        if calls["value"] > 1:
            state["render_fingerprint"] = "sha256:changed-during-provider-read"
        return state

    monkeypatch.setattr(clips, "_distribution_state", changing_target)
    response = client.post(
        "/api/episodes/ep_001/clips/clip_01/schedule-cancellation",
        json=preview["execute"],
    )

    assert response.status_code == 409
    assert "changed during provider verification" in response.json()["detail"]
    assert deleted["calls"] == 0
    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert "schedule_cancellation" not in stored


def test_provider_evidence_requires_all_exact_queued_platforms():
    from agents.publish import schedule_cancellation_provider_safe

    receipt = _receipt()
    evidence = _queued_evidence()
    evidence["history"]["in_progress"].pop()
    safe, reason = schedule_cancellation_provider_safe(
        receipt, evidence, profile_username="up", after_delete=False
    )
    assert safe is False
    assert "uniformly queued" in reason


@pytest.mark.parametrize(
    "evidence",
    [
        {
            **_absent_evidence(),
            "status": {
                "job_id": "job-old",
                "external_id": "cascade-short-old",
                "profile_username": "up",
                "status": "processing",
                "success": False,
            },
        },
        {
            "status_not_found": False,
            "status": {
                "job_id": "job-old",
                "external_id": "cascade-short-old",
                "profile_username": "up",
                "status": "cancelled",
                "success": False,
                "fallback_to_inbox": True,
            },
            "history": {"in_progress": [], "history": []},
        },
    ],
)
def test_post_delete_evidence_rejects_contradictory_or_unresolved_status(evidence):
    from agents.publish import schedule_cancellation_provider_safe

    safe, _ = schedule_cancellation_provider_safe(
        _receipt(), evidence, profile_username="up", after_delete=True
    )

    assert safe is False


def test_post_delete_accepts_exact_inert_tombstone_only_after_delete():
    from agents.publish import schedule_cancellation_provider_safe

    evidence = _inert_tombstone_evidence()
    assert schedule_cancellation_provider_safe(
        _receipt(), evidence, profile_username="up", after_delete=True
    ) == (True, None)
    assert (
        schedule_cancellation_provider_safe(
            _receipt(), evidence, profile_username="up", after_delete=False
        )[0]
        is False
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message", "Upload failed"),
        ("completed", 1),
        ("total", 1),
        ("results", [{"platform": "youtube", "status": "failed"}]),
        ("retryable", 1),
        ("post_url", "https://example.com/post"),
        ("job_id", "other-job"),
    ],
)
def test_post_delete_rejects_mutated_inert_tombstone(field, value):
    from agents.publish import schedule_cancellation_provider_safe

    evidence = _inert_tombstone_evidence()
    evidence["status"][field] = value
    assert (
        schedule_cancellation_provider_safe(
            _receipt(), evidence, profile_username="up", after_delete=True
        )[0]
        is False
    )
