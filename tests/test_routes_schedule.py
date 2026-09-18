"""The schedule is a proposal based on current review and publication evidence."""

import asyncio
import json
import threading
from datetime import datetime

import pytest

from server.routes import schedule


def _episode(root, episode_id="example", **episode):
    path = root / episode_id
    path.mkdir()
    if episode.get("youtube_longform_url") and not episode.get(
        "youtube_longform_url_source"
    ):
        episode["youtube_longform_url_source"] = "supplied"
    (path / "episode.json").write_text(
        json.dumps({"episode_id": episode_id, "guest_name": "Guest", **episode})
    )
    return path


def _review(*, approved=True, distribution=None):
    state = {
        "enabled_destinations": [{"key": "youtube"}, {"key": "instagram"}],
        "longform": {
            "canonical_render": {"current": approved},
            "approval": {"current": approved},
        },
        "clips": [
            {
                "id": "clip_01",
                "title": "A current clip",
                "metadata": {"youtube": {"title": "Canonical clip title"}},
                "review": {
                    "selection": {"status": "selected"},
                    "render": {"current": approved},
                    "approval": {"current": approved},
                },
            }
        ],
    }
    if distribution is not None:
        state["clips"][0]["review"]["distribution"] = distribution

    def load(*_args, **_kwargs):
        return state

    return load


@pytest.fixture(autouse=True)
def _current_release_gate(monkeypatch):
    monkeypatch.setattr(
        schedule,
        "quality_snapshot",
        lambda *_args, **_kwargs: {
            "release_gate": {
                "can_approve_publish": True,
                "revision": "sha256:current",
                "blockers": [],
            }
        },
    )


def _fix_now(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat("2099-07-01T16:00:00+00:00")
            return instant.astimezone(tz)

    monkeypatch.setattr(schedule, "datetime", FixedDateTime)


def test_calendar_ignores_legacy_files_and_boolean_approval(tmp_path, monkeypatch):
    episode = _episode(tmp_path, status="ready_for_review")
    (episode / "longform.mp4").write_bytes(b"legacy")
    (episode / "clips.json").write_text(
        json.dumps([{"clip_id": "one", "approved": True}])
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review(approved=False))
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())

    assert result["mode"] == "proposal"
    assert result["total_items"] == 0


def test_calendar_proposes_only_current_revision_bound_approvals(tmp_path, monkeypatch):
    _episode(tmp_path, title="A current episode")
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    items = [item for day in result["schedule"] for item in day["items"]]

    assert result["total_items"] == 2
    assert items[0]["type"] == "longform"
    assert items[0]["destination"] == "youtube"
    assert items[1]["type"] == "short"
    assert items[1]["destinations"] == ["youtube", "instagram"]
    assert items[1]["version"] == "base"
    assert items[1]["variant_id"] is None


def test_calendar_proposal_exposes_selected_variant_identity(tmp_path, monkeypatch):
    _episode(tmp_path, title="A current episode")
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(
        schedule,
        "_schedule_review_state",
        _review(
            distribution={
                "version": "background_motion_v1",
                "variant_id": "background_motion_v1",
                "current": True,
                "approval_current": True,
            }
        ),
    )
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    short = next(
        item
        for day in result["schedule"]
        for item in day["items"]
        if item["type"] == "short"
    )

    assert short["version"] == "background_motion_v1"
    assert short["variant_id"] == "background_motion_v1"


def test_prepared_rerelease_is_suggested_despite_historical_receipt(
    tmp_path, monkeypatch
):
    episode = _episode(tmp_path, title="A current episode")
    (episode / "publish.json").write_text(
        json.dumps(
            {
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": "failed",
                        "platforms": ["youtube", "instagram"],
                        "request_id": "old-request",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(
        schedule,
        "_schedule_review_state",
        _review(
            distribution={
                "version": "background_motion_v1",
                "variant_id": "background_motion_v1",
                "current": True,
                "approval_current": True,
                "re_release_request": {"request_id": "new-request"},
                "re_release_request_consumed": False,
            }
        ),
    )
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    shorts = [
        item
        for day in result["schedule"]
        for item in day["items"]
        if item["type"] == "short"
    ]

    assert len(shorts) == 1
    assert shorts[0]["state"] == "suggested"
    assert shorts[0]["variant_id"] == "background_motion_v1"


def test_calendar_surfaces_rss_without_claiming_youtube_publication(
    tmp_path, monkeypatch
):
    episode = _episode(tmp_path)
    (episode / "podcast_feed.json").write_text(
        json.dumps(
            {
                "audio_url": "https://media.example/audio.mp3",
                "feed_url": "https://media.example/feed.xml",
            }
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review(approved=False))
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())

    assert result["total_items"] == 0
    assert result["publication_evidence"] == [
        {
            "episode_id": "example",
            "name": "Guest",
            "content_type": "podcast_audio",
            "destination": "podcast_rss",
            "status": "published",
            "url": "https://media.example/audio.mp3",
            "feed_url": "https://media.example/feed.xml",
            "evidence_source": "podcast_feed.json",
        }
    ]


def test_stale_bound_youtube_url_is_not_current_publication_evidence(
    tmp_path, monkeypatch
):
    _episode(
        tmp_path,
        youtube_longform_url="https://youtube.example/old",
        youtube_longform_url_source="supplied",
        youtube_longform_url_editorial_revision="sha256:old-longform",
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())

    assert all(
        item.get("content_type") != "longform"
        for item in result["publication_evidence"]
    )
    assert any(
        item["type"] == "longform"
        for day in result["schedule"]
        for item in day["items"]
    )


def test_recorded_submissions_block_ambiguous_duplicate_proposals(
    tmp_path, monkeypatch
):
    episode = _episode(tmp_path)
    (episode / "publish.json").write_text(
        json.dumps(
            {
                "longform": {"status": "submitted", "request_id": "long-1"},
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": "submitted",
                        "platforms": ["youtube", "instagram"],
                        "request_id": "short-1",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())

    assert result["total_items"] == 0
    assert [item["status"] for item in result["publication_evidence"]] == [
        "submitted",
        "submitted",
    ]
    assert result["publication_evidence"][0]["destination"] == "unknown"
    assert result["publication_evidence"][1]["version"] == "base"
    assert result["publication_evidence"][1]["variant_id"] is None


def test_calendar_day_uses_configured_timezone(tmp_path, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat("2026-09-11T00:30:00+00:00")
            return instant.astimezone(tz)

    monkeypatch.setattr(schedule, "datetime", FixedDateTime)
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(
        schedule,
        "_load_config",
        lambda: {"schedule": {"timezone": "America/Los_Angeles"}},
    )
    result = asyncio.run(schedule.get_schedule())
    assert result["schedule"][0]["date"] == "2026-09-10"


def test_exact_episode_schedule_replaces_generated_slot(tmp_path, monkeypatch):
    _fix_now(monkeypatch)
    _episode(
        tmp_path,
        youtube_longform_url="https://youtube.example/video",
        publish_schedule=[
            {
                "clip_id": "clip_01",
                "scheduled_date": "2099-07-04T18:00:00-07:00",
            }
        ],
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    items = [item for day in result["schedule"] for item in day["items"]]

    assert [(item["state"], item["scheduled_date"]) for item in items] == [
        ("planned", "2099-07-04T18:00:00-07:00")
    ]
    assert items[0]["title"] == "Canonical clip title"


def test_published_receipt_is_not_labeled_scheduled():
    assert schedule._receipt_state({"status": "published"}) is None


def test_receipt_artifact_current_uses_stable_media_and_rerelease_identity():
    version = {
        "version": "background_motion_v1",
        "variant_id": "background_motion_v1",
        "render_fingerprint": "sha256:current-media",
        "re_release_request": {
            "request_id": "replacement-01",
            "revision": "sha256:replacement",
        },
    }
    receipt = {
        "version": "background_motion_v1",
        "variant_id": "background_motion_v1",
        "render_fingerprint": "sha256:current-media",
        "rerelease_request_id": "replacement-01",
        "rerelease_authorization_revision": "sha256:replacement",
        "approval_revision": "sha256:older-copy-approval",
    }

    assert schedule._receipt_artifact_current(receipt, version) is True
    assert (
        schedule._receipt_artifact_current(
            receipt | {"render_fingerprint": "sha256:prior-media"}, version
        )
        is False
    )
    assert (
        schedule._receipt_artifact_current(
            {
                key: value
                for key, value in receipt.items()
                if key != "render_fingerprint"
            },
            version,
        )
        is None
    )


def _paired_release_request():
    return {
        "schema": "cascade.destination-release/v1",
        "request_id": "replacement-pair",
        "actor": "Release operator",
        "reason": "Release the reviewed destination pair.",
        "targets": [
            {
                "variant_id": "gameplay_surround_v1",
                "target_revision": f"sha256:{'1' * 64}",
                "render_fingerprint": f"sha256:{'2' * 64}",
                "destinations": ["facebook", "instagram", "tiktok", "youtube"],
            },
            {
                "variant_id": "speaker_panels_v1",
                "target_revision": f"sha256:{'3' * 64}",
                "render_fingerprint": f"sha256:{'4' * 64}",
                "destinations": ["x"],
            },
        ],
        "receipt_history_revision": f"sha256:{'5' * 64}",
        "revision": f"sha256:{'6' * 64}",
        "created_at": "2099-07-01T00:00:00+00:00",
    }


def _paired_variant_clip(*, clean_current=True, clean_approval_current=True):
    request = _paired_release_request()
    return {
        "id": "clip_01",
        "review": {
            "distribution": {"re_release_request": request},
            "variants": {
                "speaker_panels_v1": {
                    "render": {
                        "current": clean_current,
                        "fingerprint": f"sha256:{'4' * 64}",
                    },
                    "approval": {"current": clean_approval_current},
                }
            },
        },
    }


def _clean_receipt():
    request = _paired_release_request()
    return {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "render_fingerprint": f"sha256:{'4' * 64}",
        "approval_revision": f"sha256:{'3' * 64}",
        "rerelease_request_id": request["request_id"],
        "rerelease_authorization_revision": request["revision"],
    }


def test_receipt_artifact_version_resolves_disjoint_destination_variant():
    receipt = _clean_receipt()
    selected = {
        "version": "gameplay_surround_v1",
        "variant_id": "gameplay_surround_v1",
        "render_fingerprint": f"sha256:{'2' * 64}",
        "current": True,
        "re_release_request": _paired_release_request(),
    }

    version = schedule._receipt_artifact_version(
        receipt, _paired_variant_clip(), selected
    )

    assert version == {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "render_fingerprint": f"sha256:{'4' * 64}",
        "current": True,
        "re_release_request": _paired_release_request(),
    }
    assert schedule._receipt_artifact_current(receipt, version) is True


def test_receipt_artifact_current_rejects_stale_render_with_retained_fingerprint():
    receipt = _clean_receipt()
    version = schedule._receipt_artifact_version(
        receipt, _paired_variant_clip(clean_current=False), None
    )

    assert version["render_fingerprint"] == receipt["render_fingerprint"]
    assert schedule._receipt_artifact_current(receipt, version) is False


def test_receipt_artifact_current_does_not_conflate_copy_approval_staleness():
    receipt = _clean_receipt()
    version = schedule._receipt_artifact_version(
        receipt,
        _paired_variant_clip(clean_approval_current=False),
        None,
    )

    assert schedule._receipt_artifact_current(receipt, version) is True


def test_receipt_artifact_version_fails_closed_for_wrong_lineage_or_variant():
    receipt = _clean_receipt()
    clip = _paired_variant_clip()
    version = schedule._receipt_artifact_version(receipt, clip, None)

    assert (
        schedule._receipt_artifact_current(
            receipt | {"rerelease_request_id": "different-request"}, version
        )
        is False
    )
    assert (
        schedule._receipt_artifact_version(
            receipt
            | {
                "version": "unknown_v1",
                "variant_id": "unknown_v1",
            },
            clip,
            None,
        )
        is None
    )
    clip["review"]["distribution"]["re_release_request"]["targets"][1][
        "render_fingerprint"
    ] = f"sha256:{'7' * 64}"
    wrong_target = schedule._receipt_artifact_version(receipt, clip, None)
    assert schedule._receipt_artifact_current(receipt, wrong_target) is False


def test_same_clip_destination_receipts_keep_separate_current_rows(
    tmp_path, monkeypatch
):
    _fix_now(monkeypatch)
    episode = _episode(
        tmp_path,
        youtube_longform_url="https://youtube.example/video",
        publish_schedule=[
            {
                "clip_id": "clip_01",
                "scheduled_date": "2099-07-05T09:00:00-07:00",
            }
        ],
    )
    request = _paired_release_request()
    gameplay_identity = {
        "version": "gameplay_surround_v1",
        "variant_id": "gameplay_surround_v1",
        "render_fingerprint": f"sha256:{'2' * 64}",
        "rerelease_request_id": request["request_id"],
        "rerelease_authorization_revision": request["revision"],
    }
    clean_identity = {
        "version": "speaker_panels_v1",
        "variant_id": "speaker_panels_v1",
        "render_fingerprint": f"sha256:{'4' * 64}",
        "rerelease_request_id": request["request_id"],
        "rerelease_authorization_revision": request["revision"],
    }
    (episode / "publish.json").write_text(
        json.dumps(
            {
                "release_revision": "sha256:prior-aggregate-release",
                "shorts": [
                    {
                        **gameplay_identity,
                        "clip_id": "clip_01",
                        "status": "submitted",
                        "scheduled": True,
                        "scheduled_date": "2099-07-05T09:00:00-07:00",
                        "platforms": ["youtube", "tiktok"],
                        "job_id": "job-video",
                    },
                    {
                        **clean_identity,
                        "clip_id": "clip_01",
                        "status": "submitted",
                        "scheduled": True,
                        "scheduled_date": "2099-07-05T09:00:00-07:00",
                        "platforms": ["x"],
                        "job_id": "job-x",
                    },
                ],
            }
        )
    )

    clip = _paired_variant_clip()
    clip.update(
        title="A current clip",
        metadata={"youtube": {"title": "Canonical clip title"}},
    )

    def paired_review(*_args, **_kwargs):
        return {
            "enabled_destinations": [
                {"key": "youtube"},
                {"key": "instagram"},
                {"key": "x"},
            ],
            "longform": {
                "canonical_render": {"current": True},
                "approval": {"current": True},
            },
            "clips": [clip],
        }

    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", paired_review)
    monkeypatch.setattr(schedule, "_load_config", dict)
    monkeypatch.setattr(
        schedule,
        "quality_snapshot",
        lambda *_args, **_kwargs: {
            "release_gate": {
                "can_approve_publish": True,
                "revision": "sha256:current-aggregate-release",
                "blockers": [],
                "short_versions": {
                    "clip_01": {
                        "version": "gameplay_surround_v1",
                        "variant_id": "gameplay_surround_v1",
                        "render_fingerprint": f"sha256:{'2' * 64}",
                        "current": True,
                        "re_release_request": request,
                    }
                },
            }
        },
    )

    result = asyncio.run(schedule.get_schedule())
    items = [
        item
        for day in result["schedule"]
        for item in day["items"]
        if item["type"] == "short"
    ]

    assert [item["job_id"] for item in items] == ["job-video", "job-x"]
    assert [item["destinations"] for item in items] == [
        ["youtube", "tiktok"],
        ["x"],
    ]
    assert [item["variant_id"] for item in items] == [
        "gameplay_surround_v1",
        "speaker_panels_v1",
    ]
    assert [item["artifact_current"] for item in items] == [True, True]


def test_delete_confirmed_receipt_is_pending_cancellation_not_scheduled(
    tmp_path, monkeypatch
):
    _fix_now(monkeypatch)
    episode = _episode(tmp_path, youtube_longform_url="https://youtube.example/video")
    (episode / "publish.json").write_text(
        json.dumps(
            {
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": "submitted",
                        "scheduled": True,
                        "scheduled_date": "2099-07-05T09:00:00-07:00",
                        "platforms": ["youtube", "instagram"],
                        "job_id": "job-01",
                        "request_id": "request-01",
                        "schedule_cancellation": {"state": "delete_confirmed"},
                    }
                ]
            }
        )
    )
    from agents import publish as publish_agent

    monkeypatch.setattr(
        publish_agent,
        "validated_schedule_cancellation",
        lambda receipt: receipt.get("schedule_cancellation"),
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review(approved=False))
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    items = [item for day in result["schedule"] for item in day["items"]]
    evidence = next(
        record
        for record in result["publication_evidence"]
        if record.get("clip_id") == "clip_01"
    )

    assert len(items) == 1
    assert items[0]["state"] == "cancellation_pending"
    assert items[0]["scheduled_date"] == "2099-07-05T09:00:00-07:00"
    assert items[0]["job_id"] == "job-01"
    assert evidence["status"] == "cancellation_pending"
    assert evidence["scheduled"] is False
    assert evidence["schedule_cancellation_state"] == "delete_confirmed"


@pytest.mark.parametrize(
    ("receipt_status", "expected_state"),
    [("submitted", "scheduled"), ("failed", "failed"), ("unknown", "unknown")],
)
def test_receipt_date_and_state_override_plan(
    tmp_path, monkeypatch, receipt_status, expected_state
):
    _fix_now(monkeypatch)
    episode = _episode(
        tmp_path,
        youtube_longform_url="https://youtube.example/video",
        publish_schedule=[
            {
                "clip_id": "clip_01",
                "scheduled_date": "2099-07-04T18:00:00-07:00",
            }
        ],
    )
    (episode / "publish.json").write_text(
        json.dumps(
            {
                "release_revision": "sha256:old",
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": receipt_status,
                        "scheduled": True,
                        "scheduled_date": "2099-07-05T09:00:00-07:00",
                        "platforms": ["youtube", "instagram"],
                        "job_id": "job-01",
                        "request_id": "request-01",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review(approved=False))
    monkeypatch.setattr(schedule, "_load_config", dict)

    result = asyncio.run(schedule.get_schedule())
    items = [item for day in result["schedule"] for item in day["items"]]

    assert len(items) == 1
    assert items[0]["state"] == expected_state
    assert items[0]["scheduled_date"] == "2099-07-05T09:00:00-07:00"
    assert items[0]["planned_date"] == "2099-07-04T18:00:00-07:00"
    assert items[0]["job_id"] == "job-01"
    assert items[0]["artifact_current"] is None


def test_qa_blocked_clip_is_held_out_of_suggestions(tmp_path, monkeypatch):
    _episode(tmp_path, youtube_longform_url="https://youtube.example/video")
    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_schedule_review_state", _review())
    monkeypatch.setattr(schedule, "_load_config", dict)
    monkeypatch.setattr(
        schedule,
        "quality_snapshot",
        lambda *_args, **_kwargs: {
            "release_gate": {
                "can_approve_publish": False,
                "blockers": [{"message": "Quality review is stale."}],
            }
        },
    )

    result = asyncio.run(schedule.get_schedule())

    assert result["total_items"] == 0
    assert all(not day["items"] for day in result["schedule"])
    assert result["held_items"][0]["episode_id"] == "example"
    assert result["held_items"][0]["blockers"] == ["Quality review is stale."]


def test_schedule_review_checks_only_scheduled_receipt_variants(monkeypatch, tmp_path):
    captured = {}

    def review(_ep_dir, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(schedule, "episode_review_state", review)
    receipts = [
        {
            "clip_id": "clip_01",
            "status": "submitted",
            "scheduled": True,
            "scheduled_date": "2099-07-05T09:00:00-07:00",
            "variant_id": "speaker_panels_v1",
        },
        {
            "clip_id": "clip_01",
            "status": "cancelled",
            "scheduled": False,
            "scheduled_date": "2099-07-05T09:00:00-07:00",
            "variant_id": "gameplay_surround_v1",
        },
    ]
    variant_state = object()

    schedule._schedule_review_state(tmp_path, {}, receipts, variant_state)

    assert captured["variant_ids_by_clip"] == {
        "clip_01": ("speaker_panels_v1",)
    }
    assert captured["variant_state"] is variant_state


def test_request_variant_state_reuses_only_identical_inputs(monkeypatch, tmp_path):
    calls = []

    def variant_state(*args, **kwargs):
        calls.append((args, kwargs))
        return {"call": len(calls)}, {"current": True}

    monkeypatch.setattr(schedule, "background_variant_state", variant_state)
    current = schedule._request_variant_state()
    inputs = {
        "base_record": {"fingerprint": "sha256:base"},
        "encoding": {"codec": "h264"},
        "variant_id": "speaker_panels_v1",
    }

    first = current(tmp_path, "clip_01", **inputs)
    second = current(tmp_path, "clip_01", **inputs)
    inputs["base_record"]["fingerprint"] = "sha256:changed"
    changed_base = current(tmp_path, "clip_01", **inputs)
    changed_encoding = current(
        tmp_path,
        "clip_01",
        **(inputs | {"encoding": {"codec": "hevc"}}),
    )
    next_request = schedule._request_variant_state()
    uncached_next_request = next_request(tmp_path, "clip_01", **inputs)

    assert first is second
    assert changed_base is not first
    assert changed_encoding is not changed_base
    assert uncached_next_request is not changed_base
    assert len(calls) == 4


def test_schedule_build_does_not_block_the_event_loop(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_build(_episodes_dir, _config):
        started.set()
        assert release.wait(timeout=0.5), "event loop could not release schedule build"
        return [], [], []

    monkeypatch.setattr(schedule, "get_episodes_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_load_config", dict)
    monkeypatch.setattr(schedule, "_get_approved_items", blocked_build)

    async def run():
        task = asyncio.create_task(schedule.get_schedule())
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        release.set()
        return await task

    result = asyncio.run(run())

    assert result["total_items"] == 0
