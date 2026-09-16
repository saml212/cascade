"""Review API tests for current and previous media artifacts."""

# ruff: noqa: F811 - imported pytest fixture is intentionally shadowed

import hashlib
import json
import os
import threading

from tests.test_routes_episodes import _create_episode, test_client  # noqa: F401


def _write_clips(episode_dir, clips):
    (episode_dir / "clips.json").write_text(json.dumps({"clips": clips}))


def _release_request():
    return {
        "request_id": "47db1913-4d32-4acf-bcfe-31763c50e9c2",
        "actor": "release-operator",
        "reason": "Rebuilt episode with current media",
        "variant_id": "background_motion_v1",
        "target_revision": "sha256:target",
        "render_fingerprint": "sha256:render",
        "receipt_history_revision": "sha256:history",
        "revision": "sha256:authorization",
        "created_at": "2026-09-13T20:00:00+00:00",
    }


def _record(path, fingerprint, mode):
    stat = path.stat()
    return {
        "path": str(path.name),
        "render_mode": mode,
        "fingerprint": fingerprint,
        "output": {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "completed_at": "2026-09-11T12:00:00+00:00",
    }


def _stub_transcript_media_probe(monkeypatch, review, source, duration=5.0):
    asr_input = source.parent / "work" / "audio.m4a"
    asr_input.parent.mkdir(exist_ok=True)
    asr_input.write_bytes(b"ASR input")
    monkeypatch.setattr(
        review,
        "probe",
        lambda path: {"format": {"duration": str(duration)}},
    )

    def fingerprint(path, _probe_data):
        stat = path.stat()
        return {
            "id": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            "method": "sha256-full/test",
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    monkeypatch.setattr(review, "media_fingerprint", fingerprint)


def _clock_repair_request(guards, *, scale=1.0, offset=0.0, speaker_bindings=None):
    request = {
        "expected_binding_revision": guards["binding_revision"],
        "source_seconds_per_asr_second": scale,
        "source_offset_seconds": offset,
        "evidence": {
            "method": "grounded correlation anchors",
            "anchor_count": 3,
            "summary": "Three retained source/ASR matches",
            "fit_r_squared": 0.999,
        },
    }
    if speaker_bindings is not None:
        request["speaker_bindings"] = speaker_bindings
    return request


def test_enabled_destination_contract_ignores_disabled_expansion_copy():
    from server.routes.review import _enabled_destinations

    destinations = _enabled_destinations(
        {
            "platforms": {
                "youtube": {"enabled": True},
                "facebook": {"enabled": False},
                "threads": {"enabled": True},
            }
        }
    )

    assert [item["key"] for item in destinations] == ["youtube", "threads"]
    assert destinations[1] == {
        "key": "threads",
        "label": "Threads",
        "required_fields": ["text"],
    }


def _transcript_clock_repair_fixture(
    test_client, monkeypatch, raw=None, episode_data=None
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(
        episodes_dir,
        "ep_001",
        {"duration_seconds": 5.0, **(episode_data or {})},
    )
    source = episode_dir / "source_merged.mp4"
    source.write_bytes(b"source media")
    raw_path = episode_dir / "transcript.json"
    raw_bytes = json.dumps(
        raw or {"results": {"utterances": []}}, separators=(",", ":")
    ).encode()
    raw_path.write_bytes(raw_bytes)

    from server.routes import review

    _stub_transcript_media_probe(monkeypatch, review, source)
    inspected = client.get("/api/episodes/ep_001/inspection/transcript/repair")
    assert inspected.status_code == 200
    return client, episode_dir, source, raw_path, raw_bytes, inspected.json(), review


def test_review_builds_snapshot_off_the_event_loop(test_client, monkeypatch):
    client, episodes_dir = test_client
    _create_episode(episodes_dir, "ep_001")

    from server.routes import review

    threads = {}
    resolve_episode = review.require_episode_dir

    def resolve(root, episode_id):
        threads["route"] = threading.get_ident()
        return resolve_episode(root, episode_id)

    def snapshot(episode_dir):
        threads["snapshot"] = threading.get_ident()
        return {"schema": "cascade.review/v1", "episode_id": episode_dir.name}

    monkeypatch.setattr(review, "require_episode_dir", resolve)
    monkeypatch.setattr(review, "episode_review_state", snapshot)

    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    assert response.json()["episode_id"] == "ep_001"
    assert threads["snapshot"] != threads["route"]


def test_review_keeps_stale_short_playable(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clip = {
        "id": "clip_01",
        "start_seconds": 10,
        "end_seconds": 30,
        "status": "pending",
        "metadata": {"youtube": {"title": "Title", "description": "Copy"}},
    }
    _write_clips(episode_dir, [clip])
    output = episode_dir / "shorts" / "clip_01.mp4"
    output.write_bytes(b"previous pixels")
    (episode_dir / "render_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "shorts": {
                    "clip_01": _record(output, "sha256:previous", "speaker_cut_short")
                },
            }
        )
    )

    from server.routes import review

    monkeypatch.setattr(
        review,
        "_expected_fingerprints",
        lambda *_args: (None, {"clip_01": "sha256:current"}),
    )
    monkeypatch.setattr(
        review,
        "load_config",
        lambda: {
            "platforms": {
                "youtube": {"enabled": True},
                "tiktok": {"enabled": False},
                "x": {"enabled": True},
            }
        },
    )

    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    payload = response.json()
    render = payload["clips"][0]["review"]["render"]
    assert render["status"] == "stale"
    assert render["reason_code"] == "render_inputs_changed"
    assert render["playable"] is True
    assert render["current"] is False
    assert "/shorts/clip_01.mp4?v=" in render["url"]
    approval = payload["clips"][0]["review"]["approval"]
    assert approval["current"] is False
    assert approval["revision"].startswith("sha256:")
    assert [item["key"] for item in payload["enabled_destinations"]] == [
        "youtube",
        "x",
    ]
    assert payload["clip_summary"] == {
        "candidate_count": 1,
        "selected_count": 0,
        "unselected_count": 1,
        "rejected_count": 0,
    }
    assert payload["clips"][0]["review"]["selection"]["status"] == "unselected"
    metadata = payload["clips"][0]["review"]["metadata"]
    assert metadata["enabled_destination_count"] == 2
    assert metadata["complete_destination_count"] == 1


def test_review_keeps_new_clip_approval_current_until_copy_changes(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clip = {
        "id": "clip_01",
        "start_seconds": 10,
        "end_seconds": 30,
        "status": "pending",
        "metadata": {"youtube": {"title": "Title", "description": "Copy"}},
    }
    _write_clips(episode_dir, [clip])
    output = episode_dir / "shorts" / "clip_01.mp4"
    output.write_bytes(b"current pixels")
    record = _record(output, "sha256:current", "speaker_cut_short")
    (episode_dir / "render_manifest.json").write_text(
        json.dumps({"version": 1, "shorts": {"clip_01": record}})
    )

    from agents import qa
    from server.routes import clips as clips_route
    from server.routes import review

    monkeypatch.setattr(clips_route, "_current_render", lambda *_args: record)
    monkeypatch.setattr(
        qa, "_render_status", lambda *_args: (None, {"clip_01": record})
    )
    monkeypatch.setattr(
        review,
        "_expected_fingerprints",
        lambda *_args: (None, {"clip_01": "sha256:current"}),
    )

    approved = client.post("/api/episodes/ep_001/clips/clip_01/approve")
    assert approved.status_code == 200
    current = client.get("/api/episodes/ep_001/review").json()["clips"][0]["review"][
        "approval"
    ]
    assert current == {
        "status": "current",
        "current": True,
        "revision": approved.json()["approved_revision"],
    }
    quality = qa.quality_snapshot(episode_dir, config={})
    blockers = quality["release_gate"]["blockers"]
    assert "clip_approval_missing_or_stale" not in {
        blocker["code"] for blocker in blockers
    }

    stored = json.loads((episode_dir / "clips.json").read_text())
    stored["clips"][0]["metadata"]["youtube"]["title"] = "Changed title"
    _write_clips(episode_dir, stored["clips"])

    stale = client.get("/api/episodes/ep_001/review").json()["clips"][0]["review"][
        "approval"
    ]
    assert stale["status"] == "stale"
    assert stale["current"] is False
    assert stale["revision"] != current["revision"]
    changed_quality = qa.quality_snapshot(episode_dir, config={})
    changed_blockers = changed_quality["release_gate"]["blockers"]
    assert "clip_approval_missing_or_stale" in {
        blocker["code"] for blocker in changed_blockers
    }


def test_review_exposes_untracked_longform_despite_episode_status(test_client):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001", {"status": "ready_to_render"})
    (episode_dir / "upload_video.mp4").write_bytes(b"previous longform")

    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    longform = response.json()["longform"]
    assert longform["render"]["status"] == "untracked"
    assert longform["render"]["playable"] is True
    assert longform["render"]["current"] is False
    assert "/upload_video.mp4?v=" in longform["render"]["url"]
    assert longform["approval"]["current"] is False
    assert longform["source_preview_url"].endswith("/video-preview")


def test_review_media_urls_change_when_atomic_outputs_are_replaced(test_client):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clip = {
        "id": "clip_01",
        "start_seconds": 10,
        "end_seconds": 30,
    }
    _write_clips(episode_dir, [clip])
    longform = episode_dir / "upload_video.mp4"
    short = episode_dir / "shorts" / "clip_01.mp4"
    longform.write_bytes(b"old full")
    short.write_bytes(b"old short")

    first = client.get("/api/episodes/ep_001/review").json()
    first_longform = first["longform"]["render"]
    first_short = first["clips"][0]["review"]["render"]

    longform.write_bytes(b"new full replacement")
    short.write_bytes(b"new short replacement")
    second = client.get("/api/episodes/ep_001/review").json()
    second_longform = second["longform"]["render"]
    second_short = second["clips"][0]["review"]["render"]

    assert second_longform["url"] != first_longform["url"]
    assert second_short["url"] != first_short["url"]
    assert second_longform["media_revision"] != first_longform["media_revision"]
    assert second_short["media_revision"] != first_short["media_revision"]
    assert second_longform["download_url"] == second_longform["url"]
    assert second_short["download_url"] == second_short["url"]


def test_review_distinguishes_missing_short_from_stale(test_client):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    _write_clips(
        episode_dir,
        [{"id": "clip_01", "start_seconds": 10, "end_seconds": 30}],
    )

    response = client.get("/api/episodes/ep_001/review")

    render = response.json()["clips"][0]["review"]["render"]
    assert render == {
        "status": "missing",
        "current": False,
        "playable": False,
        "path": "shorts/clip_01.mp4",
        "reason_code": "artifact_missing",
        "detail": "No rendered file exists yet.",
        "url": None,
        "download_url": None,
    }


def test_review_advertises_gameplay_variants_before_render(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    _write_clips(
        episode_dir,
        [{"id": "clip_01", "start_seconds": 10, "end_seconds": 30}],
    )

    from lib.short_variants import (
        GAMEPLAY_SURROUND_ASSET_SET_ID,
        GAMEPLAY_SURROUND_ASSETS,
        GAMEPLAY_SURROUND_VARIANT_ID,
        GTA_DRIVING_ASSET_ID,
        GTA_DRIVING_VARIANT_ID,
        MINECRAFT_PARKOUR_ASSET_ID,
        MINECRAFT_PARKOUR_VARIANT_ID,
        SPEAKER_PANELS_VARIANT_ID,
        SUBWAY_SURFERS_ASSET_ID,
        SUBWAY_SURFERS_VARIANT_ID,
    )
    from server.routes import review

    def missing_variant(_episode_dir, clip_id, *, variant_id, **_kwargs):
        return {}, {
            "status": "missing",
            "current": False,
            "playable": False,
            "path": f"short_variants/{variant_id}/{clip_id}.mp4",
            "reason_code": "artifact_missing",
            "detail": "No rendered file exists yet.",
        }

    monkeypatch.setattr(review, "background_variant_state", missing_variant)

    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    variants = response.json()["clips"][0]["review"]["variants"]
    expected = {
        MINECRAFT_PARKOUR_VARIANT_ID: (
            "Minecraft parkour",
            MINECRAFT_PARKOUR_ASSET_ID,
        ),
        SUBWAY_SURFERS_VARIANT_ID: (
            "Subway Surfers",
            SUBWAY_SURFERS_ASSET_ID,
        ),
        GTA_DRIVING_VARIANT_ID: ("GTA driving", GTA_DRIVING_ASSET_ID),
        GAMEPLAY_SURROUND_VARIANT_ID: (
            "Gameplay surround",
            GAMEPLAY_SURROUND_ASSET_SET_ID,
        ),
        SPEAKER_PANELS_VARIANT_ID: ("Clean speaker panels", None),
    }
    for variant_id, (label, asset_id) in expected.items():
        assert variants[variant_id]["label"] == label
        assert variants[variant_id]["asset_id"] == asset_id
        assert variants[variant_id]["render"]["status"] == "missing"
    assert variants[GAMEPLAY_SURROUND_VARIANT_ID]["asset_ids"] == [
        asset_id for _, asset_id in GAMEPLAY_SURROUND_ASSETS
    ]
    assert variants[SPEAKER_PANELS_VARIANT_ID]["asset_ids"] == []
    assert variants[SPEAKER_PANELS_VARIANT_ID]["asset_free"] is True
    assert "satisfying_motion_v1" not in variants


def test_review_exposes_background_variant_as_separate_media(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clip = {
        "id": "clip_01",
        "start_seconds": 10,
        "end_seconds": 30,
        "distribution_variant_id": "background_motion_v1",
    }
    _write_clips(episode_dir, [clip])
    variant_path = (
        episode_dir / "short_variants" / "background_motion_v1" / "clip_01.mp4"
    )
    variant_path.parent.mkdir(parents=True)
    variant_path.write_bytes(b"background pixels")
    stat = variant_path.stat()
    variant_record = {
        "fingerprint": "sha256:variant",
        "asset": {"asset_id": "original_block_parkour_v1"},
        "output": {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    }

    from server.routes import review

    monkeypatch.setattr(
        review,
        "background_variant_state",
        lambda *_args, **_kwargs: (
            variant_record,
            {
                "status": "current",
                "current": True,
                "playable": True,
                "path": "short_variants/background_motion_v1/clip_01.mp4",
                "reason_code": None,
                "detail": "current",
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            },
        ),
    )

    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    variant = response.json()["clips"][0]["review"]["variants"]["background_motion_v1"]
    assert variant["asset_id"] == "original_block_parkour_v1"
    assert variant["render"]["current"] is True
    assert (
        "/short_variants/background_motion_v1/clip_01.mp4?v="
        in variant["render"]["url"]
    )
    assert variant["approval"]["current"] is False
    assert variant["approval"]["revision"].startswith("sha256:")
    distribution = response.json()["clips"][0]["review"]["distribution"]
    assert distribution == {
        "version": "background_motion_v1",
        "variant_id": "background_motion_v1",
        "label": "Motion background",
        "current": True,
        "approval_current": False,
        "revision": variant["approval"]["revision"],
        "re_release_request": None,
        "change_locked": False,
        "change_lock_reason": None,
        "re_release_allowed": False,
        "re_release_reason": "No prior remote submission requires a re-release.",
        "re_release_request_consumed": None,
        "re_release_history_revision": None,
        "unresolved_receipt_obligations": [],
        "unresolved_history_acknowledgement_allowed": False,
    }

    (episode_dir / "publish.json").write_text(
        json.dumps({"shorts": [{"clip_id": "clip_01", "status": "submitted"}]})
    )
    locked = client.get("/api/episodes/ep_001/review").json()["clips"][0]["review"][
        "distribution"
    ]
    assert locked["change_locked"] is True
    assert "explicit re-release identity" in locked["change_lock_reason"]

    clip["distribution_release"] = None
    _write_clips(episode_dir, [clip])
    malformed = client.get("/api/episodes/ep_001/review").json()["clips"][0]["review"][
        "distribution"
    ]
    assert malformed["re_release_request"] is None
    assert malformed["re_release_allowed"] is False
    assert "cannot be verified" in malformed["change_lock_reason"]

    (episode_dir / "publish.json").unlink()
    clip["distribution_release"] = _release_request()
    _write_clips(episode_dir, [clip])
    missing_history = client.get("/api/episodes/ep_001/review").json()["clips"][0][
        "review"
    ]["distribution"]
    assert missing_history["re_release_request"] == clip["distribution_release"]
    assert missing_history["change_locked"] is True
    assert missing_history["re_release_allowed"] is False
    assert "cannot be verified" in missing_history["change_lock_reason"]

    variant_record["asset"]["asset_id"] = []
    fallback = client.get("/api/episodes/ep_001/review").json()["clips"][0]["review"]
    assert (
        fallback["variants"]["background_motion_v1"]["asset_id"]
        == "original_block_parkour_v1"
    )


def test_inspection_reports_revision_bound_clip_boundary_evidence(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clip = {
        "id": "clip_01",
        "start_seconds": 10.0,
        "end_seconds": 20.0,
        "selection_status": "selected",
    }
    _write_clips(episode_dir, [clip])
    transcript = {
        "clock": "source",
        "utterances": [
            {
                "speaker": 2,
                "words": [
                    {
                        "word": "where",
                        "start": 9.8,
                        "end": 10.1,
                        "confidence": 0.97,
                    }
                ],
            }
        ],
    }
    (episode_dir / "diarized_transcript.json").write_text(json.dumps(transcript))

    from agents import qa

    monkeypatch.setattr(qa, "current_diarized_transcript", lambda *_args: transcript)
    inspection_response = client.get("/api/episodes/ep_001/inspection/clip-boundaries")

    assert inspection_response.status_code == 200
    evidence = inspection_response.json()
    assert evidence["current"] is True
    assert evidence["status"] == "review_required"
    assert evidence["transcript_revision"].startswith("sha256:")
    finding = evidence["clips"][0]["findings"][0]
    assert finding["word"]["word"] == "where"
    assert finding["word"]["confidence"] == 0.97
    assert finding["word"]["speaker"] == 2
    assert finding["inspection_request"] == {
        "method": "GET",
        "endpoint": "/api/episodes/ep_001/inspection/preview",
        "query": {
            "target": "source",
            "clock": "source",
            "seconds": 8.0,
            "duration_seconds": 4.0,
        },
    }

    first_clip_revision = evidence["clips"][0]["clip_revision"]
    _write_clips(
        episode_dir,
        [
            {
                "id": "clip_01",
                "start_seconds": 10.2,
                "end_seconds": 20.0,
                "selection_status": "selected",
            }
        ],
    )
    changed = client.get("/api/episodes/ep_001/inspection/clip-boundaries").json()
    assert changed["transcript_revision"] == evidence["transcript_revision"]
    assert changed["clips"][0]["clip_revision"] != first_clip_revision

    monkeypatch.setattr(qa, "current_diarized_transcript", lambda *_args: None)
    unavailable = client.get("/api/episodes/ep_001/inspection/clip-boundaries").json()
    assert unavailable["current"] is False
    assert unavailable["status"] == "unavailable"


def test_review_survives_invalid_selected_audio(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    _write_clips(
        episode_dir,
        [{"id": "clip_01", "start_seconds": 10.0, "end_seconds": 20.0}],
    )

    from server.routes import review

    def stale_selection(*_args):
        raise ValueError("stale selected audio")

    monkeypatch.setattr(review, "selected_audio_source", stale_selection)
    response = client.get("/api/episodes/ep_001/review")

    assert response.status_code == 200
    assert response.json()["clips"][0]["review"]["render"]["current"] is False


def test_inspection_preview_maps_output_across_source_cut(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    media = episode_dir / "upload_video.mp4"
    media.write_bytes(b"render")

    import server.media_inspection as inspection
    from lib.timeline import Timeline
    from server.media_inspection import InspectionTarget
    from server.routes import review

    target = InspectionTarget(
        path=media,
        timeline=Timeline.from_edits(
            100, [{"type": "cut", "start_seconds": 10, "end_seconds": 20}]
        ),
        source_duration=100,
        media_duration=90,
        fingerprint="sha256:current",
        preserve_audio_timestamps=False,
    )
    monkeypatch.setattr(review, "resolve_target", lambda *_args, **_kwargs: target)

    def render(_target, cache_dir, **_kwargs):
        asset = cache_dir / ("a" * 64 + ".mp4")
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"preview")
        return asset, False

    monkeypatch.setattr(inspection, "render_cached_inspection", render)
    response = client.get(
        "/api/episodes/ep_001/inspection/preview",
        params={
            "target": "longform",
            "clock": "output",
            "seconds": 5,
            "duration_seconds": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mapping"] == {
        "source_seconds": 5.0,
        "output_seconds": 5.0,
        "source_ranges": [
            {"start_seconds": 5.0, "end_seconds": 10.0},
            {"start_seconds": 20.0, "end_seconds": 25.0},
        ],
    }
    assert payload["artifact"]["fingerprint"] == "sha256:current"
    assert client.get(payload["asset"]["url"]).content == b"preview"


def test_inspection_rejects_nonfinite_and_unbounded_windows(test_client, monkeypatch):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    media = episode_dir / "source_merged.mp4"
    media.write_bytes(b"source")

    from lib.timeline import Timeline
    from server.media_inspection import InspectionTarget
    from server.routes import review

    target = InspectionTarget(
        path=media,
        timeline=Timeline.from_edits(100),
        source_duration=100,
        media_duration=100,
        fingerprint="source",
        preserve_audio_timestamps=True,
    )
    monkeypatch.setattr(review, "resolve_target", lambda *_args, **_kwargs: target)

    for seconds in ("nan", "inf", "-inf", "-1"):
        response = client.get(
            "/api/episodes/ep_001/inspection/frame", params={"seconds": seconds}
        )
        assert response.status_code == 422
    response = client.get(
        "/api/episodes/ep_001/inspection/preview",
        params={"seconds": 1, "duration_seconds": 30.01},
    )
    assert response.status_code == 422


def test_inspection_rejects_a_stale_render_input(test_client, monkeypatch):
    client, episodes_dir = test_client
    _create_episode(episodes_dir, "ep_001")

    from server.routes import review

    def stale(*_args, **_kwargs):
        raise ValueError("Selected audio is stale")

    monkeypatch.setattr(review, "resolve_target", stale)
    response = client.get(
        "/api/episodes/ep_001/inspection/frame",
        params={"target": "longform", "seconds": 2},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Selected audio is stale"


def test_inspection_rejects_unknown_short_variant(test_client):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    _write_clips(
        episode_dir,
        [{"id": "clip_01", "start_seconds": 1.0, "end_seconds": 3.0}],
    )

    response = client.get(
        "/api/episodes/ep_001/inspection/frame",
        params={
            "target": "short_variant",
            "clip_id": "clip_01",
            "variant_id": "unknown",
        },
    )

    assert response.status_code == 404
    assert "Unknown short variant" in response.json()["detail"]


def test_audio_inspection_names_camera_channel_and_uncertainty(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    _create_episode(episodes_dir, "ep_001")

    import server.media_inspection as inspection

    def export(_dir, _episode, _track, start, end, output, **_selector):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"channel")
        return {
            "source": {"kind": "camera", "channel": "right"},
            "source_window": {
                "start": start,
                "end": end,
                "duration_seconds": end - start,
            },
            "fingerprint": "window-fingerprint",
            "audio": {"sha256": "b" * 64, "size_bytes": 7},
        }

    monkeypatch.setattr(inspection, "export_logical_track_window", export)
    monkeypatch.setattr(
        inspection,
        "camera_channel_evidence",
        lambda *_args: {
            "analysis_current": False,
            "relationship": "unknown",
            "usable_for_speaker_separation": None,
        },
    )
    response = client.get(
        "/api/episodes/ep_001/inspection/audio-preview",
        params={
            "source_kind": "camera",
            "channel": "right",
            "start_seconds": 5,
            "duration_seconds": 7,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == {"kind": "camera", "channel": "right"}
    assert payload["channel_evidence"]["relationship"] == "unknown"
    assert client.get(payload["asset"]["url"]).content == b"channel"


def test_inspection_exposes_only_current_transcript_and_shot_plan(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    transcript = {"clock": "source", "utterances": [{"text": "Hello"}]}
    shot_plan = {"clock": "source", "segments": [{"speaker": "speaker_0"}]}
    (episode_dir / "diarized_transcript.json").write_text(json.dumps(transcript))
    (episode_dir / "segments.json").write_text(json.dumps(shot_plan))

    from server.routes import review

    monkeypatch.setattr(
        review, "current_diarized_transcript", lambda *_args: transcript
    )
    monkeypatch.setattr(review, "current_speaker_segments", lambda *_args: shot_plan)

    transcript_response = client.get("/api/episodes/ep_001/inspection/transcript")
    shot_response = client.get("/api/episodes/ep_001/inspection/shot-plan")

    assert transcript_response.status_code == 200
    assert transcript_response.json()["transcript"] == transcript
    assert transcript_response.json()["revision"].startswith("sha256:")
    assert shot_response.status_code == 200
    assert shot_response.json()["shot_plan"] == shot_plan
    assert shot_response.json()["revision"].startswith("sha256:")

    monkeypatch.setattr(review, "current_diarized_transcript", lambda *_args: None)
    assert client.get("/api/episodes/ep_001/inspection/transcript").status_code == 409


def test_transcript_clock_repair_api_applies_affine_mapping_and_exposes_guards(
    test_client, monkeypatch
):
    raw = {
        "metadata": {"duration": 2.0},
        "results": {
            "utterances": [
                {
                    "speaker": 0,
                    "start": 0.5,
                    "end": 1.0,
                    "transcript": "Hello",
                    "confidence": 0.99,
                    "words": [
                        {
                            "word": "hello",
                            "start": 0.5,
                            "end": 1.0,
                            "confidence": 0.99,
                            "speaker": 0,
                        }
                    ],
                }
            ]
        },
    }
    client, episode_dir, _, _, raw_bytes, guards, review = (
        _transcript_clock_repair_fixture(
            test_client,
            monkeypatch,
            raw,
            {"crop_config": {"speakers": [{"label": "Host"}]}},
        )
    )
    assert guards["mapping"] is None
    assert guards["transcript_current"] is False
    (episode_dir / "segments.json").write_text(
        json.dumps(
            {
                "clock": "source",
                "fingerprint": "camera-analysis",
                "track_mapping": [{"speaker": "speaker_0", "person": "Host"}],
                "segments": [{"speaker": "speaker_0", "start": 0.0, "end": 2.0}],
            }
        )
    )
    monkeypatch.setattr(
        review,
        "current_speaker_segments",
        lambda directory, *_args: json.loads((directory / "segments.json").read_text()),
    )
    guards = client.get("/api/episodes/ep_001/inspection/transcript/repair").json()

    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair",
        json=_clock_repair_request(
            guards,
            scale=1.01,
            offset=0.1,
            speaker_bindings=[
                {
                    "asr_speaker": 0,
                    "crop_speaker_index": 0,
                    "evidence": "reviewed self-introduction",
                }
            ],
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["raw_transcript_unchanged"] is True
    assert payload["source_media_unchanged"] is True
    assert payload["mapping"]["formula"].startswith("source_seconds =")
    assert (episode_dir / "transcript.json").read_bytes() == raw_bytes
    diarized = json.loads((episode_dir / "diarized_transcript.json").read_text())
    assert diarized["utterances"][0]["start"] == 0.605
    assert diarized["utterances"][0]["end"] == 1.11
    assert diarized["provenance"]["clock_mapping"] == payload["mapping"]
    assert diarized["speaker_map"][0]["target_speaker"] == "speaker_0"
    assert payload["speaker_map"] == diarized["speaker_map"]

    current = client.get("/api/episodes/ep_001/inspection/transcript/repair")
    assert current.status_code == 200
    assert current.json()["mapping_current"] is True
    assert current.json()["transcript_current"] is True
    assert current.json()["mapping"] == payload["mapping"]
    assert current.json()["speaker_bindings"] == diarized["speaker_map"]


def test_transcript_clock_repair_rolls_back_binding_without_current_speaker_plan(
    test_client, monkeypatch
):
    raw = {
        "results": {
            "utterances": [
                {
                    "speaker": 0,
                    "start": 0.5,
                    "end": 1.5,
                    "words": [
                        {
                            "word": "hello",
                            "start": 0.5,
                            "end": 1.5,
                            "confidence": 0.99,
                            "speaker": 0,
                        }
                    ],
                }
            ]
        }
    }
    client, episode_dir, _, raw_path, raw_bytes, guards, _ = (
        _transcript_clock_repair_fixture(
            test_client,
            monkeypatch,
            raw,
            {"crop_config": {"speakers": [{"label": "Host"}]}},
        )
    )

    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair",
        json=_clock_repair_request(
            guards,
            speaker_bindings=[
                {
                    "asr_speaker": 0,
                    "crop_speaker_index": 0,
                    "evidence": "reviewed self-introduction",
                }
            ],
        ),
    )

    assert response.status_code == 409
    assert "current aligned speaker plan" in response.json()["detail"]
    assert raw_path.read_bytes() == raw_bytes
    for relative in (
        "diarized_transcript.json",
        "transcript_provenance.json",
        "subtitles/transcript.srt",
        "segments.json",
    ):
        assert not (episode_dir / relative).exists()


def test_transcript_clock_repair_rejects_stale_raw_source_and_episode_guards(
    test_client, monkeypatch
):
    client, episode_dir, source, raw_path, _, original, _ = (
        _transcript_clock_repair_fixture(test_client, monkeypatch)
    )
    request = _clock_repair_request(original)

    raw_path.write_text(raw_path.read_text() + "\n")
    stale_raw = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair", json=request
    )
    assert stale_raw.status_code == 409
    assert "repair inputs changed" in stale_raw.json()["detail"].lower()
    assert not (episode_dir / "transcript_provenance.json").exists()

    refreshed = client.get("/api/episodes/ep_001/inspection/transcript/repair").json()
    request = _clock_repair_request(refreshed)
    source_stat = source.stat()
    source.write_bytes(b"source mediX")
    os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    stale_source = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair", json=request
    )
    assert stale_source.status_code == 409
    assert "repair inputs changed" in stale_source.json()["detail"].lower()
    assert not (episode_dir / "transcript_provenance.json").exists()

    refreshed = client.get("/api/episodes/ep_001/inspection/transcript/repair").json()
    episode_path = episode_dir / "episode.json"
    episode = json.loads(episode_path.read_text())
    episode["title"] = "Changed after inspection"
    episode_path.write_text(json.dumps(episode))
    stale_episode = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair",
        json=_clock_repair_request(refreshed),
    )
    assert stale_episode.status_code == 409
    assert "repair inputs changed" in stale_episode.json()["detail"].lower()
    assert not (episode_dir / "transcript_provenance.json").exists()


def test_transcript_clock_repair_rechecks_revision_after_render_inspection(
    test_client, monkeypatch
):
    client, episode_dir, _, _, _, guards, review = _transcript_clock_repair_fixture(
        test_client, monkeypatch
    )
    repaired = []

    def change_episode_during_capture(*_args):
        path = episode_dir / "episode.json"
        episode = json.loads(path.read_text())
        episode["title"] = "Concurrent change"
        path.write_text(json.dumps(episode))

    monkeypatch.setattr(
        review, "_capture_transcript_render_reuse", change_episode_during_capture
    )
    monkeypatch.setattr(
        review,
        "repair_existing_transcript",
        lambda *_args, **_kwargs: repaired.append(True),
    )

    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair",
        json=_clock_repair_request(guards),
    )

    assert response.status_code == 409
    assert "during render inspection" in response.json()["detail"]
    assert repaired == []


def test_transcript_clock_repair_restores_derived_artifacts_on_failure(
    test_client, monkeypatch
):
    client, episode_dir, _, raw_path, raw_bytes, guards, review = (
        _transcript_clock_repair_fixture(test_client, monkeypatch)
    )
    artifact_names = (
        "diarized_transcript.json",
        "transcript_provenance.json",
        "subtitles/transcript.srt",
        "segments.json",
        "render_manifest.json",
    )
    for name in artifact_names:
        path = episode_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}")
    guards = client.get("/api/episodes/ep_001/inspection/transcript/repair").json()

    def fail_after_partial_writes(directory, _config, **_kwargs):
        for name in artifact_names:
            (directory / name).write_text(f"changed {name}")
        raise ValueError("invalid mapped transcript")

    monkeypatch.setattr(review, "repair_existing_transcript", fail_after_partial_writes)
    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair",
        json=_clock_repair_request(guards),
    )

    assert response.status_code == 422
    assert raw_path.read_bytes() == raw_bytes
    for name in artifact_names:
        assert (episode_dir / name).read_text() == f"original {name}"


def test_transcript_correction_api_upserts_without_losing_existing_operations(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    raw = b'{"immutable":"raw ASR"}'
    (episode_dir / "transcript.json").write_bytes(raw)
    (episode_dir / "diarized_transcript.json").write_text(
        json.dumps({"clock": "source", "generation": 1})
    )
    existing = {
        "version": 1,
        "clock": "source",
        "raw_transcript_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "review_note": "preserve me",
        "operations": [
            {
                "id": "existing",
                "op": "replace_word",
                "word_id": "word_1",
                "speaker": 0,
            }
        ],
    }
    (episode_dir / "transcript_corrections.json").write_text(json.dumps(existing))

    from server.media_inspection import file_revision
    from server.routes import review

    expected_revision = file_revision(episode_dir / "diarized_transcript.json")
    monkeypatch.setattr(
        review,
        "current_diarized_transcript",
        lambda directory, *_args: json.loads(
            (directory / "diarized_transcript.json").read_text()
        ),
    )
    canonical_audio = episode_dir / "work" / "audio_mix.wav"
    canonical_audio.parent.mkdir(exist_ok=True)
    canonical_audio.write_bytes(b"canonical audio")
    current_plan = {"segments": [{"speaker": "speaker_0", "start": 0, "end": 5}]}
    monkeypatch.setattr(review, "current_speaker_segments", lambda *_args: current_plan)
    events = []

    def capture(*args):
        events.append(("capture", args[0]))
        return {"version": "verified-transcript-rebind/v1", "renders": [{}]}

    def migrate(*args):
        events.append(("migrate", args[0]))
        return {
            "version": "verified-transcript-rebind/v1",
            "manifest_updated": True,
            "longform_migrated": True,
            "shorts_migrated": [],
        }

    monkeypatch.setattr(review, "capture_transcript_render_reuse_proof", capture)
    monkeypatch.setattr(
        review, "migrate_unchanged_transcript_render_fingerprints", migrate
    )

    def rebuild(directory, _config):
        events.append(("rebuild", directory))
        corrections = json.loads(
            (directory / "transcript_corrections.json").read_text()
        )
        (directory / "diarized_transcript.json").write_text(
            json.dumps(
                {
                    "clock": "source",
                    "applied": [
                        operation["id"] for operation in corrections["operations"]
                    ],
                }
            )
        )
        (directory / "transcript_provenance.json").write_text("{}")
        (directory / "subtitles" / "transcript.srt").write_text("corrected")
        (directory / "segments.json").write_text("{}")
        return {"speaker_alignment": {"status": "aligned"}}

    monkeypatch.setattr(review, "repair_existing_transcript", rebuild)
    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/corrections",
        json={
            "expected_revision": expected_revision,
            "operations": [
                {
                    "id": "existing",
                    "op": "replace_word",
                    "word_id": "word_1",
                    "speaker": 1,
                },
                {
                    "id": "new",
                    "op": "replace_word",
                    "word_id": "word_2",
                    "speaker": 0,
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["added_ids"] == ["new"]
    assert response.json()["updated_ids"] == ["existing"]
    assert response.json()["correction_count"] == 2
    assert response.json()["revision"] != expected_revision
    assert response.json()["render_reuse"]["longform_migrated"] is True
    assert [event[0] for event in events] == ["capture", "rebuild", "migrate"]
    stored = json.loads((episode_dir / "transcript_corrections.json").read_text())
    assert stored["review_note"] == "preserve me"
    assert [operation["id"] for operation in stored["operations"]] == [
        "existing",
        "new",
    ]
    assert stored["operations"][0]["speaker"] == 1
    assert (episode_dir / "transcript.json").read_bytes() == raw

    inspected = client.get("/api/episodes/ep_001/inspection/transcript/corrections")
    assert inspected.status_code == 200
    assert inspected.json()["transcript_revision"] == response.json()["revision"]
    assert inspected.json()["correction_count"] == 2


def test_transcript_correction_api_rejects_stale_revision_before_writing(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    (episode_dir / "transcript.json").write_text("{}")
    (episode_dir / "diarized_transcript.json").write_text(
        json.dumps({"clock": "source"})
    )

    from server.routes import review

    monkeypatch.setattr(review, "current_diarized_transcript", lambda *_args: {})
    rebuilt = []
    monkeypatch.setattr(
        review, "repair_existing_transcript", lambda *_args: rebuilt.append(True)
    )

    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/corrections",
        json={
            "expected_revision": "sha256:stale",
            "operations": [{"id": "new", "op": "replace_word"}],
        },
    )

    assert response.status_code == 409
    assert "revision changed" in response.json()["detail"].lower()
    assert rebuilt == []
    assert not (episode_dir / "transcript_corrections.json").exists()


def test_transcript_correction_api_preserves_a_concurrent_transcript_generation(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    raw_path = episode_dir / "transcript.json"
    raw_path.write_text("original raw generation")
    artifact_names = (
        "diarized_transcript.json",
        "transcript_provenance.json",
        "subtitles/transcript.srt",
        "segments.json",
        "render_manifest.json",
    )
    for name in artifact_names:
        path = episode_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}")

    from server.media_inspection import file_revision
    from server.routes import review

    expected_revision = file_revision(episode_dir / "diarized_transcript.json")
    monkeypatch.setattr(review, "current_diarized_transcript", lambda *_args: {})

    def fail_after_partial_writes(directory, _config):
        (directory / "transcript.json").write_text("concurrent new raw generation")
        for name in artifact_names:
            (directory / name).write_text(f"changed {name}")
        raise ValueError("invalid correction")

    monkeypatch.setattr(review, "repair_existing_transcript", fail_after_partial_writes)
    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/corrections",
        json={
            "expected_revision": expected_revision,
            "operations": [{"id": "new", "op": "replace_word"}],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid correction"
    assert not (episode_dir / "transcript_corrections.json").exists()
    assert raw_path.read_text() == "concurrent new raw generation"
    for name in artifact_names:
        assert (episode_dir / name).read_text() == f"changed {name}"


def test_transcript_correction_api_restores_derived_artifacts_on_local_failure(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    (episode_dir / "transcript.json").write_text("unchanged raw")
    artifact_names = (
        "diarized_transcript.json",
        "transcript_provenance.json",
        "subtitles/transcript.srt",
        "segments.json",
        "render_manifest.json",
    )
    for name in artifact_names:
        path = episode_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}")

    from server.media_inspection import file_revision
    from server.routes import review

    expected_revision = file_revision(episode_dir / "diarized_transcript.json")
    monkeypatch.setattr(review, "current_diarized_transcript", lambda *_args: {})

    def fail_after_partial_writes(directory, _config):
        for name in artifact_names:
            (directory / name).write_text(f"changed {name}")
        raise ValueError("invalid correction")

    monkeypatch.setattr(review, "repair_existing_transcript", fail_after_partial_writes)
    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/corrections",
        json={
            "expected_revision": expected_revision,
            "operations": [{"id": "new", "op": "replace_word"}],
        },
    )

    assert response.status_code == 422
    assert not (episode_dir / "transcript_corrections.json").exists()
    for name in artifact_names:
        assert (episode_dir / name).read_text() == f"original {name}"


def test_transcript_correction_api_rolls_back_render_reuse_failure(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    (episode_dir / "transcript.json").write_text("unchanged raw")
    artifact_names = (
        "diarized_transcript.json",
        "transcript_provenance.json",
        "subtitles/transcript.srt",
        "segments.json",
        "render_manifest.json",
    )
    for name in artifact_names:
        path = episode_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}")

    from server.media_inspection import file_revision
    from server.routes import review

    monkeypatch.setattr(review, "current_diarized_transcript", lambda *_args: {})

    def rebuild(directory, _config):
        for name in artifact_names[:-1]:
            (directory / name).write_text(f"changed {name}")
        return {"speaker_alignment": None}

    def fail_migration(directory, *_args):
        (directory / "render_manifest.json").write_text("changed manifest")
        raise OSError("manifest update failed")

    monkeypatch.setattr(review, "repair_existing_transcript", rebuild)
    monkeypatch.setattr(
        review, "_capture_transcript_render_reuse", lambda *_args: {"renders": [{}]}
    )
    monkeypatch.setattr(review, "_migrate_transcript_render_reuse", fail_migration)
    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/corrections",
        json={
            "expected_revision": file_revision(
                episode_dir / "diarized_transcript.json"
            ),
            "operations": [{"id": "new", "op": "replace_word"}],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "manifest update failed"
    assert not (episode_dir / "transcript_corrections.json").exists()
    for name in artifact_names:
        assert (episode_dir / name).read_text() == f"original {name}"


def test_transcript_correction_api_requires_unique_operation_ids(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    (episode_dir / "transcript.json").write_text("{}")
    (episode_dir / "diarized_transcript.json").write_text("{}")

    from server.media_inspection import file_revision
    from server.routes import review

    monkeypatch.setattr(review, "current_diarized_transcript", lambda *_args: {})
    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/corrections",
        json={
            "expected_revision": file_revision(
                episode_dir / "diarized_transcript.json"
            ),
            "operations": [
                {"id": "same", "op": "replace_word"},
                {"id": "same", "op": "replace_range"},
            ],
        },
    )

    assert response.status_code == 422
    assert "duplicate requested" in response.json()["detail"].lower()
    assert not (episode_dir / "transcript_corrections.json").exists()
