"""Append-only Upload-Post scheduled-date amendment tests."""

import copy
import json
from datetime import datetime, timezone

import pytest

from agents.publish import (
    PublishAgent,
    _document_revision,
    cancellable_scheduled_receipt,
    complete_schedule_reschedule,
    effective_scheduled_date,
    schedule_cancellation_provider_safe,
    start_schedule_reschedule,
    validated_schedule_reschedule,
    validated_short_receipts,
)
from tests.test_schedule_cancellation import (
    _absent_evidence,
    _artifact_x_receipt_for,
    _queued_evidence_for,
    _remote_job,
    _remote_job_for,
    _setup,
)

pytest_plugins = ["tests.test_routes_episodes"]

TARGET = "2099-01-12T09:00:00-08:00"
TARGET_UTC = "2099-01-12T17:00:00"


def _intent(receipt=None, remote=None):
    receipt = receipt or _artifact_x_receipt_for(
        "job-reschedule", "47db1913-4d32-4acf-bcfe-31763c50e9c2"
    )
    remote = remote or _remote_job_for(receipt)
    remote.update(
        has_preview=True,
        fields={
            "external_id": receipt["external_id"],
            "youtube_description": "exact copy",
            "youtube_privacy_status": "public",
        },
    )
    wrapped = start_schedule_reschedule(
        receipt,
        remote,
        profile_username="up",
        to_scheduled_date=TARGET,
        timezone_name="America/Los_Angeles",
        operation_id="ce56df63-68f8-4f52-a693-5db69c862aca",
        actor="root-schedule-operator",
        reason="Move the approved paired pilot without changing its content",
        started_at="2098-12-01T12:00:00+00:00",
    )
    return receipt, remote, wrapped


def _complete():
    original, before, intent = _intent()
    after = {
        **before,
        "scheduled_date": TARGET_UTC,
        "original_scheduled_str": "2099-01-12T09:00:00",
    }
    result = complete_schedule_reschedule(
        intent,
        after,
        patch={
            "outcome": "response",
            "attempted_at": "2098-12-01T12:00:01+00:00",
            "http_status": 200,
            "response": {
                "success": True,
                "job_id": original["job_id"],
                "scheduled_date": TARGET_UTC,
            },
        },
        completed_at="2098-12-01T12:00:02+00:00",
    )
    return original, before, after, result


def test_intent_is_durable_and_blocks_effective_date_until_reconciled():
    original, _before, intent = _intent()

    assert intent["pre_reschedule_receipt"] == original
    assert validated_schedule_reschedule(intent)["state"] == "intent_recorded"
    assert validated_short_receipts({"shorts": [intent]}) == [intent]
    with pytest.raises(ValueError, match="must be reconciled"):
        effective_scheduled_date(intent)


def test_complete_preserves_original_and_resolves_provider_date():
    original, _before, _after, result = _complete()

    assert result["pre_reschedule_receipt"] == original
    assert result["scheduled_date"] == original["scheduled_date"]
    assert effective_scheduled_date(result) == TARGET
    assert validated_schedule_reschedule(result)["state"] == "complete"
    assert validated_short_receipts({"shorts": [result]}) == [result]


def test_nested_provider_fields_are_bound_and_tamper_rejected():
    _original, _before, _after, result = _complete()
    tampered = copy.deepcopy(result)
    tampered["schedule_reschedule"]["after"]["fields"]["youtube_description"] = (
        "changed copy"
    )

    assert validated_schedule_reschedule(tampered) is None
    with pytest.raises(ValueError, match="reschedule history"):
        validated_short_receipts({"shorts": [tampered]})


def test_provider_copy_or_settings_change_cannot_be_sealed_as_date_only():
    _original, before, intent = _intent()
    changed = copy.deepcopy(before)
    changed.update(
        scheduled_date=TARGET_UTC,
        original_scheduled_str="2099-01-12T09:00:00",
    )
    changed["fields"]["youtube_description"] = "provider-mutated copy"

    with pytest.raises(ValueError, match="does not match the intent"):
        complete_schedule_reschedule(
            intent,
            changed,
            patch={
                "outcome": "response",
                "attempted_at": "2098-12-01T12:00:01+00:00",
                "http_status": 200,
                "response": {
                    "success": True,
                    "job_id": intent["job_id"],
                    "scheduled_date": TARGET_UTC,
                },
            },
            completed_at="2098-12-01T12:00:02+00:00",
        )


def test_malformed_patch_response_date_fails_closed_without_exception():
    _original, _before, _after, result = _complete()
    malformed = copy.deepcopy(result)
    malformed["schedule_reschedule"]["patch"]["response"]["scheduled_date"] = (
        "not-an-iso-date"
    )
    malformed["schedule_reschedule"]["revision"] = _document_revision(
        {
            key: value
            for key, value in malformed["schedule_reschedule"].items()
            if key != "revision"
        }
    )

    assert validated_schedule_reschedule(malformed) is None


def test_rescheduled_receipt_remains_exactly_cancellable():
    _original, _before, after, result = _complete()
    receipt, remote, _revision = cancellable_scheduled_receipt(
        {"shorts": [result]},
        result["clip_id"],
        [after],
        profile_username="up",
        job_id=result["job_id"],
        external_id=result["external_id"],
        now=datetime(2098, 12, 2, tzinfo=timezone.utc),
    )

    assert receipt == result
    assert remote == after
    evidence = _queued_evidence_for(result)
    for row in evidence["history"]["in_progress"]:
        row["run_date"] = TARGET_UTC
    assert schedule_cancellation_provider_safe(
        result, evidence, profile_username="up", after_delete=False
    ) == (True, None)


def test_occupied_schedule_uses_effective_date(tmp_path, monkeypatch):
    _original, _before, after, result = _complete()
    episode = tmp_path / "ep_test"
    episode.mkdir()
    (episode / "publish.json").write_text(
        __import__("json").dumps({"profile_username": "up", "shorts": [result]})
    )
    agent = PublishAgent(episode, {"schedule": {}})
    monkeypatch.setattr(agent, "_remote_schedule", lambda *_args: [after])

    records = agent._occupied_schedule("key", "up")

    assert len(records) == 1
    assert records[0]["scheduled_at"] == datetime(2099, 1, 12, 17, tzinfo=timezone.utc)


def test_completed_amendment_can_be_chained_without_losing_original():
    first_original, _before, after, first = _complete()
    second = start_schedule_reschedule(
        first,
        after,
        profile_username="up",
        to_scheduled_date="2099-01-19T09:00:00-08:00",
        timezone_name="America/Los_Angeles",
        operation_id="f587e0c4-5696-4cfb-ad37-48b341846735",
        actor="root-schedule-operator",
        reason="Move the same exact scheduled job again with durable evidence",
        started_at="2098-12-02T12:00:00+00:00",
    )

    assert second["pre_reschedule_receipt"] == first
    assert first["pre_reschedule_receipt"] == first_original
    assert validated_schedule_reschedule(second)["state"] == "intent_recorded"


def test_status_update_then_exact_cancellation_preserves_reschedule_history(
    test_client, monkeypatch
):
    client, episode_dir, original, deleted, request = _setup(test_client, monkeypatch)
    before = {**_remote_job(), "has_preview": True}
    intent = start_schedule_reschedule(
        original,
        before,
        profile_username="up",
        to_scheduled_date=TARGET,
        timezone_name="America/Los_Angeles",
        operation_id="ce56df63-68f8-4f52-a693-5db69c862aca",
        actor="root-schedule-operator",
        reason="Move the approved schedule without changing its content",
        started_at="2098-12-01T12:00:00+00:00",
    )
    after = {
        **before,
        "scheduled_date": TARGET_UTC,
        "original_scheduled_str": "2099-01-12T09:00:00",
    }
    amended = complete_schedule_reschedule(
        intent,
        after,
        patch={
            "outcome": "response",
            "attempted_at": "2098-12-01T12:00:01+00:00",
            "http_status": 200,
            "response": {
                "success": True,
                "job_id": original["job_id"],
                "scheduled_date": TARGET_UTC,
            },
        },
        completed_at="2098-12-01T12:00:02+00:00",
    )
    amended.update(
        status="unknown",
        status_history=[{"evidence_source": "status-poll", "status": "unknown"}],
        terminal_destinations=None,
    )
    assert validated_schedule_reschedule(amended) is not None
    validated_short_receipts({"shorts": [amended]})
    (episode_dir / "publish.json").write_text(
        json.dumps({"profile_username": "up", "shorts": [amended]})
    )

    from agents.publish import PublishAgent, validated_schedule_cancellation
    from server.routes import clips

    monkeypatch.setattr(
        PublishAgent,
        "_remote_schedule",
        lambda *_args: [] if deleted["value"] else [after],
    )
    evidence = _queued_evidence_for(amended)
    for row in evidence["history"]["in_progress"]:
        row["run_date"] = TARGET_UTC
    monkeypatch.setattr(
        clips,
        "_upload_post_job_evidence",
        lambda *_args: _absent_evidence() if deleted["value"] else evidence,
    )
    path = "/api/episodes/ep_001/clips/clip_01/scheduled-jobs/job-old/cancellation"
    preview = client.post(
        f"{path}/preview",
        json={
            "expected_external_id": original["external_id"],
            "request_id": request["request_id"],
            "actor": request["actor"],
            "reason": request["reason"],
        },
    )
    assert preview.status_code == 200
    assert preview.json()["receipt"]["scheduled_date"] == TARGET
    cancelled = client.post(path, json=preview.json()["execute"])
    assert cancelled.status_code == 200

    stored = json.loads((episode_dir / "publish.json").read_text())["shorts"][0]
    assert stored["pre_cancellation_receipt"] == amended
    assert validated_schedule_cancellation(stored) is not None
    assert validated_schedule_reschedule(stored) is not None
    validated_short_receipts({"shorts": [stored]})
