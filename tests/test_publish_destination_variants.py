"""Focused safety checks for request-scoped short artifact publication."""

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agents.publish import (
    ARTIFACT_SHORT_DESTINATION_SCHEMA,
    EXPANDED_SHORT_DESTINATION_SCHEMA,
    PublishAgent,
    ShortDeliverySpec,
    _destination_external_id,
    _destination_receipt_valid,
    _document_revision,
    _valid_destination_copy_shape,
)
from agents.qa import SHORT_COPY_SCHEMA


def _facebook_receipt(*, schema: str, scheduled: bool, scheduled_date: str | None):
    version = {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "render_fingerprint": "sha256:render",
        "revision": "sha256:approval",
    }
    request_id = "b8c0b129-599c-48cb-b363-60a5fe4dc46c"
    target_revision = _document_revision(
        ShortDeliverySpec.target_fields("clip_04", version)
    )
    external_id = _destination_external_id(
        "ep_test", request_id, ["facebook"], target_revision, "clip_04", schema
    )
    copy = {"facebook": {"title": "A title", "description": "A description"}}
    receipt = {
        "clip_id": "clip_04",
        "status": "submitted",
        "platforms": ["facebook"],
        "request_id": external_id,
        "external_id": external_id,
        "idempotency_key": external_id,
        "scheduled": scheduled,
        "scheduled_date": scheduled_date,
        "timezone": "America/Los_Angeles",
        **{key: value for key, value in version.items() if key != "revision"},
        "approval_revision": version["revision"],
        "destination_schema": schema,
        "destination_episode_id": "ep_test",
        "destination_profile_username": "test-profile",
        "destination_request_id": request_id,
        "destination_actor": "release-operator",
        "destination_reason": "Publish the reviewed Facebook pilot",
        "deferred_platforms": [],
        "target_revision": target_revision,
        "destination_copy": copy,
        "copy_schema": SHORT_COPY_SCHEMA,
        "copy_revision": _document_revision(copy),
        "destination_bindings": {
            "facebook": {
                "account_id": "facebook-account",
                "target_kind": "page",
                "target_id": "facebook-page",
            }
        },
    }
    receipt["destination_request_revision"] = _document_revision(
        ShortDeliverySpec.request_fields(receipt)
    )
    return receipt


def test_only_v3_receipts_may_represent_an_immediate_destination_request():
    immediate = _facebook_receipt(
        schema=ARTIFACT_SHORT_DESTINATION_SCHEMA,
        scheduled=False,
        scheduled_date=None,
    )
    assert _destination_receipt_valid(immediate)

    legacy = _facebook_receipt(
        schema=EXPANDED_SHORT_DESTINATION_SCHEMA,
        scheduled=False,
        scheduled_date=None,
    )
    assert not _destination_receipt_valid(legacy)


def test_destination_copy_requires_provider_fields_as_strings():
    assert _valid_destination_copy_shape(
        {"facebook": {"title": "Title", "description": "Description"}},
        ["facebook"],
    )
    assert not _valid_destination_copy_shape({"facebook": {}}, ["facebook"])
    assert not _valid_destination_copy_shape(
        {"facebook": {"title": "Title", "description": 7}}, ["facebook"]
    )


def test_delivery_spec_freezes_nested_transport_inputs():
    clip = {"id": "clip_04", "title": "Reviewed title"}
    version = {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "path": "short_variants/speaker_panels_v1/clip_04.mp4",
        "render_fingerprint": "sha256:render",
        "revision": "sha256:approval",
    }
    target = _facebook_receipt(
        schema=ARTIFACT_SHORT_DESTINATION_SCHEMA,
        scheduled=False,
        scheduled_date=None,
    )
    copy = target["destination_copy"]
    spec = ShortDeliverySpec.create(
        clip,
        version,
        target,
    )
    frozen = spec.snapshot()
    receipt = spec.receipt(
        {"status": "submitted", "job_id": "provider-job"},
        None,
        "America/Los_Angeles",
    )

    clip["title"] = "mutated"
    version["path"] = "other.mp4"
    copy["facebook"]["title"] = "mutated"
    target["destination_bindings"]["facebook"]["target_id"] = "other-page"

    assert spec.snapshot() == frozen
    assert spec.snapshot()["clip"]["title"] == "Reviewed title"
    assert spec.snapshot()["version"]["path"].endswith("clip_04.mp4")
    assert (
        spec.snapshot()["target"]["destination_copy"]["facebook"]["title"] == "A title"
    )
    assert (
        spec.snapshot()["target"]["destination_bindings"]["facebook"]["target_id"]
        == "facebook-page"
    )
    assert (
        spec.receipt(
            {"status": "submitted", "job_id": "provider-job"},
            None,
            "America/Los_Angeles",
        )
        == receipt
    )


def test_delivery_identity_preserves_lineage_omissions():
    version = {
        "version": "base",
        "variant_id": None,
        "path": "shorts/clip_04.mp4",
        "render_fingerprint": "sha256:render",
        "revision": "sha256:approval",
    }
    identity = "cascade-short-golden"
    request = {
        "request_id": "e3c0748e-6080-46cf-b64c-86d6b578ec04",
        "actor": "release-operator",
        "reason": "Reviewed re-release",
        "revision": "sha256:authorization",
        "receipt_history_revision": "sha256:history",
        "unresolved_history_acknowledgement": None,
    }
    rerelease = {**version, "re_release_request": request}
    fields = ShortDeliverySpec.receipt_identity(
        "clip_04", rerelease, identity, ["instagram"]
    )
    assert "unresolved_history_acknowledgement" not in fields
    acknowledgement = {"receipt_history_revision": "sha256:history"}
    request["unresolved_history_acknowledgement"] = acknowledgement
    assert (
        ShortDeliverySpec.receipt_identity(
            "clip_04", rerelease, identity, ["instagram"]
        )["unresolved_history_acknowledgement"]
        == acknowledgement
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("destination_request_id", []),
        ("platforms", ["facebook", "facebook"]),
        ("external_id", "cascade-short-tampered"),
        ("scheduled", "true"),
        ("target_revision", {}),
        ("destination_copy", {"facebook": {"title": [], "description": "Body"}}),
        (
            "destination_bindings",
            {
                "facebook": {
                    "account_id": "facebook-account",
                    "target_kind": "page",
                    "target_id": [],
                }
            },
        ),
    ),
)
def test_delivery_spec_rejects_malformed_reviewed_target(field, value):
    target = _facebook_receipt(
        schema=ARTIFACT_SHORT_DESTINATION_SCHEMA,
        scheduled=False,
        scheduled_date=None,
    )
    target[field] = deepcopy(value)
    version = {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "path": "short_variants/speaker_panels_v1/clip_04.mp4",
        "render_fingerprint": "sha256:render",
        "revision": "sha256:approval",
    }

    with pytest.raises(ValueError, match="target is invalid"):
        ShortDeliverySpec.create(
            {"id": "clip_04"},
            version,
            target,
        )


def test_delivery_spec_rejects_inputs_that_do_not_match_reviewed_target():
    target = _facebook_receipt(
        schema=ARTIFACT_SHORT_DESTINATION_SCHEMA,
        scheduled=False,
        scheduled_date=None,
    )
    version = {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "path": "short_variants/speaker_panels_v1/clip_04.mp4",
        "render_fingerprint": "sha256:render",
        "revision": "sha256:approval",
    }
    inputs = (
        ({"id": "clip_other"}, version),
        ({"id": "clip_04"}, {**version, "render_fingerprint": "sha256:other"}),
    )
    for clip, candidate in inputs:
        with pytest.raises(ValueError, match="target is invalid"):
            ShortDeliverySpec.create(clip, candidate, target)


@pytest.mark.parametrize(
    "variant_id",
    ("gameplay_surround_v1", "speaker_panels_v1"),
)
def test_active_variant_overrides_preserve_saved_historical_selection(
    tmp_path, variant_id
):
    agent = PublishAgent(tmp_path, {})
    approved = [{"id": "clip_04", "distribution_variant_id": "background_motion_v1"}]
    data = {"approved": approved}

    result = agent._validated_variant_overrides(
        data,
        {"variant_overrides": {"clip_04": variant_id}},
    )

    assert result == {"clip_04": variant_id}
    assert approved[0]["distribution_variant_id"] == "background_motion_v1"


@pytest.mark.parametrize(
    "variant_id",
    (
        "background_motion_v1",
        "satisfying_motion_v1",
        "minecraft_parkour_v1",
        "subway_surfers_v1",
        "gta_driving_v1",
    ),
)
def test_retired_variant_overrides_are_rejected_without_mutating_selection(
    tmp_path, variant_id
):
    agent = PublishAgent(tmp_path, {})
    approved = [{"id": "clip_04", "distribution_variant_id": "speaker_panels_v1"}]

    with pytest.raises(RuntimeError, match="existing media and history only"):
        agent._validated_variant_overrides(
            {"approved": approved},
            {"variant_overrides": {"clip_04": variant_id}},
        )

    assert approved[0]["distribution_variant_id"] == "speaker_panels_v1"


def test_variant_currentness_uses_the_frozen_approval_metadata(tmp_path, monkeypatch):
    agent = PublishAgent(tmp_path, {})
    frozen = {"id": "clip_04", "facebook": {"title": "reviewed"}}
    gameplay_variant = "gameplay_surround_v1"
    data = {
        "episode": {},
        "approved": [{"id": "clip_04", "distribution_variant_id": "old"}],
        "short_versions": {"clip_04": {"variant_id": "background_motion_v1"}},
        "approval_short_metadata": {"clip_04": frozen},
    }
    monkeypatch.setattr(
        "agents.publish.read_render_manifest", lambda *_args: {"shorts": {}}
    )

    def state(_episode_dir, _episode, _config, clip, _record, metadata):
        assert clip["distribution_variant_id"] == gameplay_variant
        assert metadata is frozen
        return {
            "current": True,
            "approval_current": True,
            "active_for_new_writes": True,
        }

    monkeypatch.setattr("agents.publish.short_distribution_state", state)
    versions = agent._destination_versions(data, {"clip_04": gameplay_variant})
    assert versions["clip_04"]["approval_current"] is True
    assert data["approved"][0]["distribution_variant_id"] == "old"
    assert data["short_versions"]["clip_04"]["variant_id"] == "background_motion_v1"


def test_required_variant_does_not_affect_other_destinations_or_absent_policy(tmp_path):
    versions = {"clip_04": {"variant_id": "gameplay_surround_v1"}}
    configured = PublishAgent(
        tmp_path,
        {
            "platforms": {
                "x": {"required_short_variant_id": "speaker_panels_v1"},
            }
        },
    )
    configured._enforce_required_short_variants(
        ["clip_04"], versions, ["youtube", "tiktok"]
    )
    PublishAgent(tmp_path, {})._enforce_required_short_variants(
        ["clip_04"], versions, ["x"]
    )


def test_immediate_destination_uses_override_media_without_schedule_fields(
    tmp_path, monkeypatch
):
    episode_dir = tmp_path / "ep_test"
    media = episode_dir / "short_variants" / "speaker_panels_v1" / "clip_04.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"reviewed video")
    config = {
        "schedule": {
            "timezone": "America/Los_Angeles",
            "shorts_per_day_weekday": 1,
            "shorts_per_day_weekend": 2,
        }
    }
    agent = PublishAgent(episode_dir, config)
    monkeypatch.setattr(agent, "_occupied_schedule", lambda *_args: [])
    monkeypatch.setattr(agent, "_verify_destination_bindings", lambda *_args: None)
    monkeypatch.setattr(
        agent,
        "_schedule_reference",
        lambda *_args, **_kwargs: datetime(2026, 9, 15, tzinfo=ZoneInfo("UTC")),
    )
    commands = []
    monkeypatch.setattr(
        agent,
        "_submit",
        lambda command, *_args: (
            commands.append(command)
            or {"status": "submitted", "response": {"request_id": "provider-job"}}
        ),
    )

    version = {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "path": str(media.relative_to(episode_dir)),
        "render_fingerprint": "sha256:render",
        "revision": "sha256:approval",
        "active_for_new_writes": True,
    }
    target = _facebook_receipt(
        schema=ARTIFACT_SHORT_DESTINATION_SCHEMA,
        scheduled=False,
        scheduled_date=None,
    )
    target["status"] = "intent_recorded"
    spec = ShortDeliverySpec.create(
        {"id": "clip_04", "title": "Title"},
        version,
        target,
    )
    prior_background = {
        "clip_id": "clip_04",
        "status": "submitted",
        "external_id": "cascade:existing-background-wave",
        "scheduled": True,
        "scheduled_date": "2026-09-21T09:00:00-07:00",
        "platforms": ["instagram"],
        "version": "background_motion_v1",
        "variant_id": "background_motion_v1",
        "render_fingerprint": "sha256:existing-render",
        "approval_revision": "sha256:existing-approval",
    }
    result = agent._publish_short_deliveries(
        [(spec, target)],
        {},
        "test-key",
        "test-profile",
        co_schedule_ids={prior_background["external_id"]},
    )

    assert result[0]["scheduled"] is False
    assert commands and f"video=@{media}" in commands[0]
    assert not any("scheduled_date=" in value for value in commands[0])
    assert not any("timezone=" in value for value in commands[0])

    retried = agent._publish_short_deliveries(
        [(spec, result[0])],
        {},
        "test-key",
        "test-profile",
    )
    assert len(commands) == 1
    assert retried[0]["external_id"] == target["external_id"]
    assert retried[0]["reused_receipt"] is True
    assert prior_background["external_id"] == "cascade:existing-background-wave"
