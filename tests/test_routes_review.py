"""Review API tests for current and previous media artifacts."""

# ruff: noqa: F811 - imported pytest fixture is intentionally shadowed

import json

from tests.test_routes_episodes import _create_episode, test_client  # noqa: F401


def _write_clips(episode_dir, clips):
    (episode_dir / "clips.json").write_text(json.dumps({"clips": clips}))


def _record(path, fingerprint, mode):
    stat = path.stat()
    return {
        "path": str(path.name),
        "render_mode": mode,
        "fingerprint": fingerprint,
        "output": {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "completed_at": "2026-09-11T12:00:00+00:00",
    }


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
    assert render["url"].endswith("/shorts/clip_01.mp4")
    assert payload["clips"][0]["review"]["approval"]["current"] is False
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
    assert longform["render"]["url"].endswith("/upload_video.mp4")
    assert longform["approval"]["current"] is False
    assert longform["source_preview_url"].endswith("/video-preview")


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

    import lib.media_inspection as inspection
    from lib.media_inspection import InspectionTarget
    from lib.timeline import Timeline
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

    from lib.media_inspection import InspectionTarget
    from lib.timeline import Timeline
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


def test_audio_inspection_names_camera_channel_and_uncertainty(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    _create_episode(episodes_dir, "ep_001")

    import lib.media_inspection as inspection

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
