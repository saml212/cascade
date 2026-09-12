"""Tests for local delivery preparation routes."""

import asyncio
import importlib
import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(episodes_dir))

    import lib.paths
    import server.routes.delivery as delivery_mod
    import server.routes.trim as trim_mod

    importlib.reload(lib.paths)
    importlib.reload(delivery_mod)
    importlib.reload(trim_mod)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(delivery_mod.router)
    app.include_router(trim_mod.router)
    yield TestClient(app), delivery_mod, episodes_dir


def make_episode(episodes_dir, episode_id="ep_test", *, with_source=True):
    episode_dir = episodes_dir / episode_id
    episode_dir.mkdir()
    (episode_dir / "episode.json").write_text(
        json.dumps(
            {
                "episode_id": episode_id,
                "audio_tracks": [],
                "duration_seconds": 3600.25,
            }
        )
    )
    if with_source:
        (episode_dir / "source_merged.mp4").write_bytes(b"video")
    return episode_dir


def request_with_concurrent_health(
    app, path: str, started: threading.Event, release: threading.Event, **kwargs
):
    """Issue a slow request beside a cheap request on the same ASGI event loop."""

    @app.get("/health")
    async def health():
        while not started.is_set():
            await asyncio.sleep(0)
        release.set()
        return {"status": "ok"}

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            slow_request = asyncio.create_task(client.get(path, **kwargs))
            health_request = asyncio.create_task(client.get("/health"))
            health_response = await asyncio.wait_for(health_request, timeout=2.0)
            response = await asyncio.wait_for(slow_request, timeout=2.0)
            return health_response, response

    return asyncio.run(exercise())


def verified_video_status():
    return {
        "video_status": "ready",
        "video_progress": 100.0,
        "video_source_fingerprint": "current-video",
        "video_output_stat": {"size": 12, "mtime_ns": 34},
        "video_download_url": "/api/episodes/ep_test/delivery/video?v=c-22",
        "video": {
            "filename": "upload_video.mp4",
            "render_mode": "speaker_cut",
            "render_fingerprint": "current-video",
        },
    }


def test_status_defaults_to_not_prepared(delivery):
    client, _, episodes_dir = delivery
    make_episode(episodes_dir)
    response = client.get("/api/episodes/ep_test/delivery")
    assert response.status_code == 200
    assert response.json()["status"] == "not_prepared"


def test_status_recovers_missing_video_fields_from_current_manifest(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"current render")
    record = {
        "path": video.name,
        "render_mode": "speaker_cut",
        "fingerprint": "current-video",
        "completed_at": "2026-09-11T00:00:00+00:00",
        "output": {
            "duration_seconds": 3590.0,
            "width": 3840,
            "height": 2160,
            "size_bytes": video.stat().st_size,
            "mtime_ns": video.stat().st_mtime_ns,
        },
    }

    with (
        patch.object(mod, "_current_video_record", return_value=record),
        patch.object(
            mod,
            "_video_audio_status",
            return_value={"status": "passed", "safe": True},
        ),
        patch.object(mod, "_write_status") as write_status,
    ):
        status = mod._refresh_status(episode_dir)

    assert status["video_status"] == "ready"
    assert status["video_source_fingerprint"] == "current-video"
    assert status["video_output_stat"] == mod._file_stat(video)
    assert status["video"]["render_mode"] == "speaker_cut"
    assert status["video"]["duration_seconds"] == 3590.0
    assert "/delivery/video?v=" in status["video_download_url"]
    write_status.assert_not_called()
    assert not (episode_dir / "delivery.json").exists()


def test_status_does_not_recover_unproven_video_file(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"unproven render")

    with patch.object(mod, "_current_video_record", return_value=None):
        status = mod._refresh_status(episode_dir)

    assert status["video_status"] == "not_prepared"
    assert "video_source_fingerprint" not in status
    assert "video_output_stat" not in status
    assert video.read_bytes() == b"unproven render"


def test_status_does_not_mix_currentness_with_replaced_video(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"manifest-backed render")
    original = video.stat()
    record = {
        "path": video.name,
        "render_mode": "speaker_cut",
        "fingerprint": "current-video",
        "output": {
            "size_bytes": original.st_size,
            "mtime_ns": original.st_mtime_ns,
            "audio_loudness": {
                "integrated_lufs": -16.0,
                "true_peak_dbfs": -1.2,
            },
        },
    }

    def replace_after_currentness(*_args):
        video.write_bytes(b"replacement bytes from a later render")
        return record

    with (
        patch.object(
            mod, "_current_video_record", side_effect=replace_after_currentness
        ),
        patch.object(mod, "_video_audio_status") as audio_status,
    ):
        status = mod._refresh_status(episode_dir)

    assert status["video_status"] == "not_prepared"
    assert "video_source_fingerprint" not in status
    audio_status.assert_not_called()


def test_audio_source_fingerprint_ignores_picture_crop(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    episode["crop_config"] = {
        "speakers": [{"track": 1, "volume": 0.8, "center_x": 400, "center_y": 500}]
    }
    config = {"processing": {}}
    fingerprint = mod._source_fingerprint(episode_dir, episode, config)

    episode["crop_config"]["speakers"][0]["longform_center_y"] = 650
    assert mod._source_fingerprint(episode_dir, episode, config) == fingerprint

    episode["crop_config"]["speakers"][0]["volume"] = 1.0
    assert mod._source_fingerprint(episode_dir, episode, config) != fingerprint


def test_audio_source_fingerprint_tracks_selected_repair(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    selected = episode_dir / "work" / "audio_repair_selected.wav"
    selected.parent.mkdir()
    selected.write_bytes(b"reviewed repair")

    with patch("agents.podcast_feed.selected_audio_source", return_value=None):
        base_fingerprint = mod._source_fingerprint(episode_dir, episode, {})
    with patch("agents.podcast_feed.selected_audio_source", return_value=selected):
        selected_fingerprint = mod._source_fingerprint(episode_dir, episode, {})

    assert selected_fingerprint != base_fingerprint


def test_audio_fingerprint_migration_does_not_bless_unrendered_selection(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    selected = episode_dir / "work" / "audio_repair_selected.wav"
    selected.parent.mkdir()
    selected.write_bytes(b"selected audio")
    legacy = mod._legacy_source_fingerprint(episode_dir, episode, {})
    mod._write_status(
        episode_dir,
        {"status": "ready", "source_fingerprint": legacy},
    )

    with patch.object(mod, "selected_audio_source", return_value=selected):
        migrated = mod.migrate_unchanged_delivery_audio_fingerprint(
            episode_dir, episode, episode, {}
        )

    assert migrated is False
    assert mod._read_status(episode_dir)["source_fingerprint"] == legacy


def test_prepare_rejects_missing_input(delivery):
    client, _, episodes_dir = delivery
    make_episode(episodes_dir, with_source=False)
    response = client.post("/api/episodes/ep_test/delivery/prepare")
    assert response.status_code == 422


def test_prepare_rejects_double_click(delivery):
    client, mod, episodes_dir = delivery
    make_episode(episodes_dir)
    mod._running.add("ep_test")
    response = client.post("/api/episodes/ep_test/delivery/prepare")
    assert response.status_code == 409
    mod._running.clear()


def test_prepare_keeps_verified_video_state_before_worker_starts(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    current = {"status": "not_prepared", **verified_video_status()}

    with (
        patch.object(mod, "_refresh_status", return_value=current),
        patch.object(mod.threading.Thread, "start"),
    ):
        response = asyncio.run(mod.prepare_delivery("ep_test"))

    assert response == current
    persisted = json.loads((episode_dir / "delivery.json").read_text())
    assert persisted["status"] == "preparing"
    for key, value in verified_video_status().items():
        assert persisted[key] == value
    mod._running.clear()


def test_video_prepare_requires_ready_audio(delivery):
    client, _, episodes_dir = delivery
    make_episode(episodes_dir)
    response = client.post("/api/episodes/ep_test/delivery/video/prepare")
    assert response.status_code == 409


def test_video_prepare_explains_missing_speaker_prerequisites(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    audio = episode_dir / "work" / "audio_mix.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    with patch.object(mod, "_refresh_status", return_value={"status": "ready"}):
        response = client.post("/api/episodes/ep_test/delivery/video/prepare")
        assert response.status_code == 422
        assert response.json()["detail"] == "Complete crop setup first"

        episode = json.loads((episode_dir / "episode.json").read_text())
        episode["crop_config"] = {"speakers": [{"center_x": 10, "center_y": 10}]}
        (episode_dir / "episode.json").write_text(json.dumps(episode))
        response = client.post("/api/episodes/ep_test/delivery/video/prepare")
        assert response.status_code == 422
        assert "speaker analysis" in response.json()["detail"]


def test_video_prepare_persists_explicit_caption_and_color_choices(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    episode["crop_config"] = {"speakers": [{"center_x": 10, "center_y": 10}]}
    (episode_dir / "episode.json").write_text(json.dumps(episode))
    (episode_dir / "segments.json").write_text(
        '{"segments": [{"start": 0, "end": 1, "speaker": "speaker_0"}]}'
    )
    (episode_dir / "diarized_transcript.json").write_text('{"utterances": []}')
    audio = episode_dir / "work" / "audio_mix.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(
            mod,
            "current_speaker_segments",
            return_value={"segments": [{"start": 0, "end": 1, "speaker": "speaker_0"}]},
        ),
        patch.object(
            mod, "current_diarized_transcript", return_value={"utterances": []}
        ),
        patch.object(mod, "require_render_space"),
        patch.object(mod.threading.Thread, "start"),
    ):
        response = asyncio.run(
            mod.prepare_delivery_video(
                "ep_test",
                mod.DeliveryVideoRequest(apply_lut=False, burn_captions=True),
            )
        )

    assert response["video_status"] == "preparing"
    stored = json.loads((episode_dir / "episode.json").read_text())
    assert stored["delivery_apply_lut"] is False
    assert stored["delivery_burn_captions"] is True
    assert response["video_preflight"]["encoding"]["video_max_bitrate"] == "16M"
    mod._video_running.clear()


def test_video_prepare_accepts_selected_repair_without_base_mix(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    episode["crop_config"] = {"speakers": [{"center_x": 10, "center_y": 10}]}
    (episode_dir / "episode.json").write_text(json.dumps(episode))
    selected = episode_dir / "work" / "audio_repair_selected.wav"
    selected.parent.mkdir()
    selected.write_bytes(b"selected audio")

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "selected_audio_source", return_value=selected),
        patch.object(mod, "current_speaker_segments", return_value={"segments": [{}]}),
        patch.object(
            mod, "current_diarized_transcript", return_value={"utterances": []}
        ),
        patch.object(
            mod,
            "_video_preflight",
            return_value={"safe": True, "budget": {"output_bytes": 1}},
        ),
        patch.object(mod, "require_render_space"),
        patch.object(mod.threading.Thread, "start"),
    ):
        response = asyncio.run(mod.prepare_delivery_video("ep_test"))

    assert response["video_status"] == "preparing"
    mod._video_running.clear()


def test_video_prepare_rejects_stale_selected_repair(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    episode["crop_config"] = {"speakers": [{"center_x": 10, "center_y": 10}]}
    (episode_dir / "episode.json").write_text(json.dumps(episode))

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(
            mod,
            "selected_audio_source",
            side_effect=ValueError(
                "Selected repair audio is stale for the source media"
            ),
        ),
        pytest.raises(HTTPException) as raised,
    ):
        asyncio.run(mod.prepare_delivery_video("ep_test"))

    assert raised.value.status_code == 409
    assert "stale for the source media" in raised.value.detail
    assert "ep_test" not in mod._video_running


def test_video_prepare_rejects_unsafe_storage_before_starting_job(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode = json.loads((episode_dir / "episode.json").read_text())
    episode["crop_config"] = {"speakers": [{"center_x": 10, "center_y": 10}]}
    (episode_dir / "episode.json").write_text(json.dumps(episode))
    audio = episode_dir / "work" / "audio_mix.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    preflight = {
        "schema": "cascade.video-preflight/v1",
        "safe": False,
        "budget": {"output_bytes": 12, "scratch_bytes": 24},
    }

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "current_speaker_segments", return_value={"segments": [{}]}),
        patch.object(
            mod, "current_diarized_transcript", return_value={"utterances": []}
        ),
        patch.object(mod, "_video_preflight", return_value=preflight),
        patch.object(
            mod, "require_render_space", side_effect=RuntimeError("storage is full")
        ),
        patch.object(mod.threading, "Thread") as thread,
        pytest.raises(HTTPException) as raised,
    ):
        asyncio.run(mod.prepare_delivery_video("ep_test"))

    assert raised.value.status_code == 409
    assert raised.value.detail == "storage is full"
    assert not thread.called
    assert "ep_test" not in mod._video_running
    stored = json.loads((episode_dir / "episode.json").read_text())
    assert "delivery_burn_captions" not in stored


def test_status_exposes_machine_readable_video_preflight(delivery):
    client, mod, episodes_dir = delivery
    make_episode(episodes_dir)
    storage = {
        "safe": True,
        "same_filesystem": False,
        "budget": {"output_bytes": 1, "scratch_bytes": 2},
    }

    with patch.object(mod, "render_space_status", return_value=storage):
        response = client.get("/api/episodes/ep_test/delivery")

    preflight = response.json()["video_preflight"]
    assert preflight["schema"] == "cascade.video-preflight/v1"
    assert preflight["safe"] is True
    assert preflight["profile"] == "longform"
    assert preflight["encoding"]["video_bitrate"] == "12M"
    assert preflight["encoding"]["video_max_bitrate_bps"] == 16_000_000
    assert preflight["duration_seconds"] == 3600.25


def test_trim_saves_absolute_range_and_preserves_cuts(delivery):
    client, _, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    episode_path = episode_dir / "episode.json"
    episode = json.loads(episode_path.read_text())
    episode["longform_edits"] = [
        {"type": "cut", "start_seconds": 200, "end_seconds": 220},
        {"type": "trim_start", "seconds": 5},
    ]
    episode_path.write_text(json.dumps(episode))
    response = client.put(
        "/api/episodes/ep_test/delivery/trim",
        json={"start_seconds": 104, "end_seconds": 3500},
    )
    assert response.status_code == 200
    edits = json.loads(episode_path.read_text())["longform_edits"]
    assert edits == [
        {"type": "cut", "start_seconds": 200, "end_seconds": 220},
        {"type": "trim_start", "seconds": 104.0},
        {"type": "trim_end", "seconds": 3500.0},
    ]
    assert response.json()["status"] == "not_prepared"


def test_trim_rejects_out_of_bounds_and_running_job(delivery):
    client, mod, episodes_dir = delivery
    make_episode(episodes_dir)
    response = client.put(
        "/api/episodes/ep_test/delivery/trim",
        json={"start_seconds": 10, "end_seconds": 4000},
    )
    assert response.status_code == 422
    mod._running.add("ep_test")
    response = client.put(
        "/api/episodes/ep_test/delivery/trim",
        json={"start_seconds": 10, "end_seconds": 1000},
    )
    assert response.status_code == 409
    mod._running.clear()


def test_trim_clamps_millisecond_display_rounding(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    with patch.object(mod, "get_duration", return_value=5359.8953):
        response = client.put(
            "/api/episodes/ep_test/delivery/trim",
            json={"start_seconds": 92.15, "end_seconds": 5359.896},
        )
    assert response.status_code == 200
    edits = json.loads((episode_dir / "episode.json").read_text())["longform_edits"]
    assert edits[-1] == {"type": "trim_end", "seconds": 5359.895}


def test_legacy_trim_endpoint_preserves_source_bytes_and_replaces_edit_bounds(
    delivery, monkeypatch
):
    client, delivery_mod, episodes_dir = delivery
    from server.routes import trim as trim_mod

    episode_dir = make_episode(episodes_dir)
    source = episode_dir / "source_merged.mp4"
    source.write_bytes(b"immutable source media")
    longform = episode_dir / "longform.mp4"
    longform.write_bytes(b"immutable legacy render")
    episode_path = episode_dir / "episode.json"
    episode = json.loads(episode_path.read_text())
    episode["longform_edits"] = [
        {"type": "cut", "start_seconds": 40, "end_seconds": 45},
        {"type": "trim_start", "seconds": 5},
    ]
    episode_path.write_text(json.dumps(episode))
    source_before = source.read_bytes(), source.stat().st_mtime_ns
    longform_before = longform.read_bytes(), longform.stat().st_mtime_ns
    monkeypatch.setattr(trim_mod, "get_duration", lambda _path: 100.0)
    monkeypatch.setattr(delivery_mod, "get_duration", lambda _path: 100.0)

    response = client.post(
        "/api/episodes/ep_test/trim",
        json={"trim_start_seconds": 10, "trim_end_seconds": 90},
    )

    assert response.status_code == 200
    assert response.json()["new_duration"] == 80
    assert response.json()["source_unchanged"] is True
    stored = json.loads(episode_path.read_text())
    assert stored["duration_seconds"] == 3600.25
    assert stored["longform_edits"] == [
        {"type": "cut", "start_seconds": 40, "end_seconds": 45},
        {"type": "trim_start", "seconds": 10.0},
        {"type": "trim_end", "seconds": 90.0},
    ]
    assert (source.read_bytes(), source.stat().st_mtime_ns) == source_before
    assert (longform.read_bytes(), longform.stat().st_mtime_ns) == longform_before
    assert not (episode_dir / "source_merged_original.mp4").exists()
    assert not (episode_dir / "source_merged_trimmed.mp4").exists()

    stored_before = episode_path.read_bytes()
    repeated = client.post(
        "/api/episodes/ep_test/trim",
        json={"trim_start_seconds": 10, "trim_end_seconds": 90},
    )
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "noop"
    assert episode_path.read_bytes() == stored_before
    assert (source.read_bytes(), source.stat().st_mtime_ns) == source_before


def test_worker_persists_verified_ready_status(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mix = episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"wav data" * 10)
    mp3 = episode_dir / "podcast_audio.mp3"
    mp3.write_bytes(b"mp3 data")

    fake_agent = MagicMock()
    fake_agent.prepare_local_audio.return_value = mp3
    fake_agent._get_duration.return_value = 3600.25
    metrics = {
        "integrated_lufs": -16.0,
        "true_peak_dbfs": -1.2,
        "loudness_range_lu": 5.1,
        "target_lufs": -16,
        "measured_at": "2026-01-01T00:00:00+00:00",
    }
    with (
        patch.object(
            mod,
            "load_config",
            return_value={"processing": {"lut_path": "/global/dlog.cube"}},
        ),
        patch.object(mod, "generate_audio_mix", return_value=mix),
        patch.object(mod, "PodcastFeedAgent", return_value=fake_agent),
        patch.object(mod, "current_podcast_audio", return_value=mp3),
        patch.object(mod, "measure_loudness", return_value=metrics),
        patch.object(
            mod,
            "_refresh_status",
            return_value={"status": "not_prepared", **verified_video_status()},
        ),
    ):
        mod._running.add("ep_test")
        mod._prepare_delivery("ep_test")

    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["status"] == "ready"
    assert status["duration_seconds"] == 3600.25
    assert status["integrated_lufs"] == -16.0
    assert "/delivery/audio?v=" in status["download_url"]
    for key, value in verified_video_status().items():
        assert status[key] == value
    assert "ep_test" not in mod._running


def test_worker_persists_failure(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    with (
        patch.object(mod, "generate_audio_mix", side_effect=RuntimeError("bad mix")),
        patch.object(
            mod,
            "_refresh_status",
            return_value={"status": "not_prepared", **verified_video_status()},
        ),
    ):
        mod._prepare_delivery("ep_test")
    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["status"] == "failed"
    assert status["error"] == "bad mix"
    for key, value in verified_video_status().items():
        assert status[key] == value


def test_video_worker_delegates_to_canonical_speaker_cut_renderer(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mix = episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"wav data" * 10)
    mod._write_status(episode_dir, {"status": "ready", "episode_id": "ep_test"})
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"video result")
    rendered = {
        "path": str(video),
        "filename": video.name,
        "size_bytes": video.stat().st_size,
        "render_fingerprint": "canonical-fingerprint",
    }
    with (
        patch.object(
            mod,
            "load_config",
            return_value={"processing": {"lut_path": "/global/dlog.cube"}},
        ),
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "render_longform", return_value=rendered) as render,
    ):
        mod._video_running.add("ep_test")
        mod._prepare_video("ep_test")
    assert render.call_args.args[0] == episode_dir
    assert render.call_args.args[1]["processing"]["lut_path"] == ""
    assert render.call_args.kwargs["progress"]
    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["video_source_fingerprint"] == "canonical-fingerprint"
    assert "/delivery/video?v=" in status["video_download_url"]


def test_video_audio_repair_worker_uses_public_packet_copy_adapter(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mod._write_status(episode_dir, {"status": "ready", "episode_id": "ep_test"})
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"repaired video")
    repaired = {
        "path": str(video),
        "render_fingerprint": "same-pixels-new-audio",
        "audio_repaired": True,
        "video_reencoded": False,
    }

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "repair_longform_audio", return_value=repaired) as repair,
        patch.object(mod, "render_longform") as render,
    ):
        mod._video_running.add("ep_test")
        mod._prepare_video("ep_test", repair_audio=True)

    assert repair.call_args.args[0] == episode_dir
    assert repair.call_args.kwargs["progress"]
    render.assert_not_called()
    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["video_status"] == "ready"
    assert status["video_operation"] == "repair_audio"
    assert status["video"]["video_reencoded"] is False


def test_worker_rejects_out_of_range_loudness(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mix = episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"wav data" * 10)
    mp3 = episode_dir / "podcast_audio.mp3"
    mp3.write_bytes(b"mp3 data")
    fake_agent = MagicMock()
    fake_agent.prepare_local_audio.return_value = mp3
    fake_agent._get_duration.return_value = 3600.25
    metrics = {
        "integrated_lufs": -12.0,
        "true_peak_dbfs": -1.0,
        "loudness_range_lu": 5.0,
        "target_lufs": -16,
        "measured_at": "2026-01-01T00:00:00+00:00",
    }
    with (
        patch.object(mod, "load_config", return_value={}),
        patch.object(mod, "generate_audio_mix", return_value=mix),
        patch.object(mod, "PodcastFeedAgent", return_value=fake_agent),
        patch.object(mod, "current_podcast_audio", return_value=mp3),
        patch.object(mod, "measure_loudness", return_value=metrics),
    ):
        mod._prepare_delivery("ep_test")
    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["status"] == "failed"
    assert "expected -16.0" in status["error"]


def test_worker_rejects_truncated_audio(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mix = episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"wav data" * 10)
    mp3 = episode_dir / "podcast_audio.mp3"
    mp3.write_bytes(b"mp3 data")
    fake_agent = MagicMock()
    fake_agent.prepare_local_audio.return_value = mp3
    fake_agent._get_duration.return_value = 100.0
    with (
        patch.object(mod, "load_config", return_value={}),
        patch.object(mod, "generate_audio_mix", return_value=mix),
        patch.object(mod, "PodcastFeedAgent", return_value=fake_agent),
        patch.object(mod, "current_podcast_audio", return_value=mp3),
        patch.object(mod, "measure_loudness", return_value={}),
    ):
        mod._prepare_delivery("ep_test")
    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["status"] == "failed"
    assert "expected 3600.250s" in status["error"]


def test_status_marks_interrupted_job_failed(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mod._write_status(
        episode_dir,
        {"status": "preparing", "episode_id": "ep_test", "started_at": "earlier"},
    )
    partial = episode_dir / "podcast_audio.mp3.tmp.mp3"
    partial.write_bytes(b"partial")
    response = client.get("/api/episodes/ep_test/delivery")
    assert response.json()["status"] == "failed"
    assert "interrupted" in response.json()["error"]
    assert not partial.exists()


def test_status_marks_changed_output_stale(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mix = episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"mix")
    audio = episode_dir / "podcast_audio.mp3"
    episode = json.loads((episode_dir / "episode.json").read_text())
    config = mod.load_config()

    def finish_audio(command, **_kwargs):
        Path(command[-1]).write_bytes(b"original")
        return subprocess.CompletedProcess(command, 0, stderr="")

    with patch("agents.podcast_feed.subprocess.run", side_effect=finish_audio):
        mod.PodcastFeedAgent(episode_dir, config).prepare_local_audio()
    mod._write_status(
        episode_dir,
        {
            "status": "ready",
            "episode_id": "ep_test",
            "source_fingerprint": mod._source_fingerprint(episode_dir, episode, config),
            "output_stat": mod._file_stat(audio),
        },
    )
    audio.write_bytes(b"changed output")
    response = client.get("/api/episodes/ep_test/delivery")
    assert response.json()["status"] == "not_prepared"
    assert response.json()["stale"] is True
    assert "/delivery/audio?v=" in response.json()["download_url"]


def test_status_marks_legacy_source_only_audio_proof_stale(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mix = episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"mix")
    audio = episode_dir / "podcast_audio.mp3"
    audio.write_bytes(b"legacy output")
    audio.with_suffix(".fingerprint").write_text("source-only-fingerprint")
    episode = json.loads((episode_dir / "episode.json").read_text())
    config = mod.load_config()
    mod._write_status(
        episode_dir,
        {
            "status": "ready",
            "source_fingerprint": mod._source_fingerprint(episode_dir, episode, config),
            "output_stat": mod._file_stat(audio),
        },
    )

    response = client.get("/api/episodes/ep_test/delivery")

    assert response.status_code == 200
    assert response.json()["status"] == "not_prepared"
    assert response.json()["stale"] is True


def test_status_surfaces_stale_selected_repair(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    podcast = episode_dir / "podcast_audio.mp3"
    podcast.write_bytes(b"podcast")
    episode = json.loads((episode_dir / "episode.json").read_text())
    with patch.object(mod, "selected_audio_source", return_value=None):
        source_fingerprint = mod._source_fingerprint(episode_dir, episode, {})
    mod._write_status(
        episode_dir,
        {
            "status": "ready",
            "source_fingerprint": source_fingerprint,
            "output_stat": mod._file_stat(podcast),
        },
    )

    with patch(
        "agents.podcast_feed.selected_audio_source",
        side_effect=ValueError("Selected repair audio has changed since review"),
    ):
        response = client.get("/api/episodes/ep_test/delivery")

    assert response.status_code == 200
    assert response.json()["status"] == "not_prepared"
    assert response.json()["error"] == "Selected repair audio has changed since review"


def test_status_validates_ready_video_against_selected_repair(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    selected = episode_dir / "work" / "audio_repair_selected.wav"
    selected.parent.mkdir()
    selected.write_bytes(b"selected audio")
    podcast = episode_dir / "podcast_audio.mp3"
    podcast.write_bytes(b"podcast")
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"video")
    episode = json.loads((episode_dir / "episode.json").read_text())

    with patch.object(mod, "selected_audio_source", return_value=selected):
        source_fingerprint = mod._source_fingerprint(episode_dir, episode, {})
    mod._write_status(
        episode_dir,
        {
            "status": "ready",
            "source_fingerprint": source_fingerprint,
            "output_stat": mod._file_stat(podcast),
            "video_status": "ready",
            "video_source_fingerprint": "current-video",
            "video_output_stat": mod._file_stat(video),
        },
    )
    record = {
        "render_mode": "speaker_cut",
        "fingerprint": "current-video",
        "output": {
            "size_bytes": video.stat().st_size,
            "mtime_ns": video.stat().st_mtime_ns,
        },
    }

    with (
        patch.object(mod, "selected_audio_source", return_value=selected),
        patch.object(
            mod,
            "current_speaker_segments",
            return_value={"segments": [{"start": 0, "end": 1}]},
        ),
        patch.object(mod, "current_longform_render", return_value=record) as check,
        patch.object(
            mod,
            "_video_audio_status",
            return_value={"status": "passed", "safe": True},
        ),
    ):
        response = client.get("/api/episodes/ep_test/delivery")

    assert response.status_code == 200
    assert response.json()["video_status"] == "ready"
    assert "/delivery/video?v=" in response.json()["video_download_url"]
    assert check.call_args.args[3] == selected


def test_status_blocks_unsafe_aac_without_hiding_repair_path(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    audio = episode_dir / "work" / "audio_mix.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"reviewed pixels")
    mod._write_status(
        episode_dir,
        {
            "status": "not_prepared",
            "video_status": "ready",
            "video_source_fingerprint": "current-video",
            "video_output_stat": mod._file_stat(video),
        },
    )
    unsafe = {
        "status": "failed",
        "safe": False,
        "errors": ["true peak 0.9 dBFS exceeds -1.0 dBFS"],
    }
    record = {
        "render_mode": "speaker_cut",
        "fingerprint": "current-video",
        "output": {
            "size_bytes": video.stat().st_size,
            "mtime_ns": video.stat().st_mtime_ns,
        },
    }

    with (
        patch.object(mod, "_current_video_record", return_value=record),
        patch.object(mod, "_video_audio_status", return_value=unsafe),
    ):
        status = mod._refresh_status(episode_dir)

    assert status["video_status"] == "not_prepared"
    assert status["video_stale"] is False
    assert status["video_repair_required"] is True
    assert status["video_audio"] == unsafe
    assert "true peak 0.9" in status["video_error"]
    assert video.read_bytes() == b"reviewed pixels"


def test_video_audio_status_uses_recorded_encoded_output_measurement(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    (episode_dir / "render_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "clock": "source",
                "shorts": {},
                "longform": {
                    "output": {
                        "audio_loudness": {
                            "integrated_lufs": -16.4,
                            "true_peak_dbfs": 0.9,
                        }
                    }
                },
            }
        )
    )

    result = mod._video_audio_status(episode_dir, {})

    assert result["status"] == "failed"
    assert result["safe"] is False
    assert "true peak 0.9" in result["errors"][0]


def test_video_audio_repair_route_starts_serialized_background_job(delivery):
    _, mod, episodes_dir = delivery
    make_episode(episodes_dir)
    audio_state = {
        "status": "failed",
        "safe": False,
        "errors": ["encoded output is too loud"],
    }

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "_current_video_record", return_value={"fingerprint": "fp"}),
        patch.object(mod, "_video_audio_status", return_value=audio_state),
        patch.object(mod.threading.Thread, "start") as start,
    ):
        response = asyncio.run(mod.repair_delivery_video_audio("ep_test"))

    assert response["video_status"] == "preparing"
    assert response["video_operation"] == "repair_audio"
    assert response["video_audio"] == audio_state
    start.assert_called_once()
    assert "ep_test" in mod._video_running
    mod._video_running.clear()


@pytest.mark.parametrize("failure", ["status", "thread"])
def test_video_job_start_failure_releases_serialization(delivery, failure):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    status = {"status": "ready", "episode_id": "ep_test"}
    status_error = RuntimeError("status write failed") if failure == "status" else None
    thread_error = RuntimeError("thread start failed") if failure == "thread" else None

    with (
        patch.object(mod, "_write_status", side_effect=status_error),
        patch.object(mod.threading.Thread, "start", side_effect=thread_error),
        pytest.raises(RuntimeError, match=f"{failure} .* failed"),
    ):
        mod._start_video_job("ep_test", episode_dir, status, repair_audio=True)

    assert "ep_test" not in mod._video_running


def test_video_audio_repair_route_rejects_stale_pixels(delivery):
    _, mod, episodes_dir = delivery
    make_episode(episodes_dir)

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "_current_video_record", return_value=None),
        patch.object(mod.threading.Thread, "start") as start,
        pytest.raises(HTTPException) as raised,
    ):
        asyncio.run(mod.repair_delivery_video_audio("ep_test"))

    assert raised.value.status_code == 409
    assert "current manifest-backed" in raised.value.detail
    start.assert_not_called()


def test_download_requires_ready_file(delivery):
    client, _, episodes_dir = delivery
    make_episode(episodes_dir)
    response = client.get("/api/episodes/ep_test/delivery/audio")
    assert response.status_code == 404


def test_audio_range_validation_does_not_block_other_requests(delivery):
    client, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    audio = episode_dir / "podcast_audio.mp3"
    audio.write_bytes(b"0123456789")
    started = threading.Event()
    release = threading.Event()
    observed = {}

    def slow_refresh(_episode_dir):
        started.set()
        observed["health_ran_before_refresh_returned"] = release.wait(timeout=1.0)
        return {"status": "ready"}

    with patch.object(mod, "_refresh_status", side_effect=slow_refresh):
        health_response, audio_response = request_with_concurrent_health(
            client.app,
            "/api/episodes/ep_test/delivery/audio",
            started,
            release,
            headers={"Range": "bytes=0-0"},
        )

    assert health_response.status_code == 200
    assert observed["health_ran_before_refresh_returned"] is True
    assert audio_response.status_code == 206
    assert audio_response.content == b"0"


def test_quality_snapshot_does_not_block_other_requests(delivery):
    client, mod, episodes_dir = delivery
    make_episode(episodes_dir)
    started = threading.Event()
    release = threading.Event()
    observed = {}

    def slow_quality(_episode_dir):
        started.set()
        observed["health_ran_before_snapshot_returned"] = release.wait(timeout=1.0)
        return {"release_gate": {"status": "blocked", "safe": False}}

    with (
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "quality_snapshot", side_effect=slow_quality),
    ):
        health_response, status_response = request_with_concurrent_health(
            client.app,
            "/api/episodes/ep_test/delivery",
            started,
            release,
        )

    assert health_response.status_code == 200
    assert observed["health_ran_before_snapshot_returned"] is True
    assert status_response.status_code == 200
    assert status_response.json()["quality"]["release_gate"]["status"] == "blocked"


def test_source_duration_prefers_probed_artifact(delivery):
    client, mod, episodes_dir = delivery
    make_episode(episodes_dir)
    with patch.object(mod, "get_duration", return_value=3599.75):
        response = client.get("/api/episodes/ep_test/delivery")
    assert response.json()["source_duration_seconds"] == 3599.75
