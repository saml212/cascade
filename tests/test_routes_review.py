"""Review API tests for current and previous media artifacts."""

# ruff: noqa: F811 - imported pytest fixture is intentionally shadowed

import hashlib
import json
import os
import threading

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


def _clock_repair_request(guards, *, scale=1.0, offset=0.0):
    return {
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


def _transcript_clock_repair_fixture(test_client, monkeypatch, raw=None):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001", {"duration_seconds": 5.0})
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
    resolve_episode = review._episode_dir

    def resolve(episode_id):
        threads["route"] = threading.get_ident()
        return resolve_episode(episode_id)

    def snapshot(episode_dir):
        threads["snapshot"] = threading.get_ident()
        return {"schema": "cascade.review/v1", "episode_id": episode_dir.name}

    monkeypatch.setattr(review, "_episode_dir", resolve)
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
    assert "/upload_video.mp4?v=" in longform["render"]["url"]
    assert longform["approval"]["current"] is False
    assert longform["source_preview_url"].endswith("/video-preview")


def test_review_media_urls_change_when_atomic_outputs_are_replaced(test_client):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    clip = {"id": "clip_01", "start_seconds": 10, "end_seconds": 30}
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
    client, episode_dir, _, _, raw_bytes, guards, _ = _transcript_clock_repair_fixture(
        test_client, monkeypatch, raw
    )
    assert guards["mapping"] is None
    assert guards["transcript_current"] is False

    response = client.post(
        "/api/episodes/ep_001/inspection/transcript/repair",
        json=_clock_repair_request(guards, scale=1.01, offset=0.1),
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

    current = client.get("/api/episodes/ep_001/inspection/transcript/repair")
    assert current.status_code == 200
    assert current.json()["mapping_current"] is True
    assert current.json()["transcript_current"] is True
    assert current.json()["mapping"] == payload["mapping"]


def test_transcript_clock_repair_rejects_stale_raw_and_source_guards(
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
    )
    for name in artifact_names:
        path = episode_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}")

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

    from lib.media_inspection import file_revision
    from server.routes import review

    expected_revision = file_revision(episode_dir / "diarized_transcript.json")
    monkeypatch.setattr(
        review,
        "current_diarized_transcript",
        lambda directory, *_args: json.loads(
            (directory / "diarized_transcript.json").read_text()
        ),
    )

    def rebuild(directory, _config):
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
    )
    for name in artifact_names:
        path = episode_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}")

    from lib.media_inspection import file_revision
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
    )
    for name in artifact_names:
        path = episode_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}")

    from lib.media_inspection import file_revision
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


def test_transcript_correction_api_requires_unique_operation_ids(
    test_client, monkeypatch
):
    client, episodes_dir = test_client
    episode_dir = _create_episode(episodes_dir, "ep_001")
    (episode_dir / "transcript.json").write_text("{}")
    (episode_dir / "diarized_transcript.json").write_text("{}")

    from lib.media_inspection import file_revision
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
