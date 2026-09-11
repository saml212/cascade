"""Tests for local delivery preparation routes."""

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(episodes_dir))

    import lib.paths
    import server.routes.delivery as delivery_mod

    importlib.reload(lib.paths)
    importlib.reload(delivery_mod)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(delivery_mod.router)
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


def test_status_defaults_to_not_prepared(delivery):
    client, _, episodes_dir = delivery
    make_episode(episodes_dir)
    response = client.get("/api/episodes/ep_test/delivery")
    assert response.status_code == 200
    assert response.json()["status"] == "not_prepared"


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


def test_video_prepare_requires_ready_audio(delivery):
    client, _, episodes_dir = delivery
    make_episode(episodes_dir)
    response = client.post("/api/episodes/ep_test/delivery/video/prepare")
    assert response.status_code == 409


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
        patch.object(mod, "measure_loudness", return_value=metrics),
    ):
        mod._running.add("ep_test")
        mod._prepare_delivery("ep_test")

    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["status"] == "ready"
    assert status["duration_seconds"] == 3600.25
    assert status["integrated_lufs"] == -16.0
    assert status["download_url"].endswith("/delivery/audio")
    assert "ep_test" not in mod._running


def test_worker_persists_failure(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    with patch.object(mod, "generate_audio_mix", side_effect=RuntimeError("bad mix")):
        mod._prepare_delivery("ep_test")
    status = json.loads((episode_dir / "delivery.json").read_text())
    assert status["status"] == "failed"
    assert status["error"] == "bad mix"


def test_video_worker_uses_untrimmed_canonical_wav(delivery):
    _, mod, episodes_dir = delivery
    episode_dir = make_episode(episodes_dir)
    mix = episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"wav data" * 10)
    mp3 = episode_dir / "podcast_audio.mp3"
    mp3.write_bytes(b"already trimmed mp3")
    mod._write_status(episode_dir, {"status": "ready", "episode_id": "ep_test"})
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"video result")
    rendered = {
        "path": str(video),
        "filename": video.name,
        "size_bytes": video.stat().st_size,
    }
    with (
        patch.object(
            mod,
            "load_config",
            return_value={"processing": {"lut_path": "/global/dlog.cube"}},
        ),
        patch.object(mod, "_refresh_status", return_value={"status": "ready"}),
        patch.object(mod, "render_delivery_video", return_value=rendered) as render,
    ):
        mod._video_running.add("ep_test")
        mod._prepare_video("ep_test")
    assert render.call_args.args[3] == mix
    assert render.call_args.args[3] != mp3
    assert render.call_args.args[2]["processing"]["lut_path"] == ""


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
    audio = episode_dir / "podcast_audio.mp3"
    audio.write_bytes(b"original")
    episode = json.loads((episode_dir / "episode.json").read_text())
    mod._write_status(
        episode_dir,
        {
            "status": "ready",
            "episode_id": "ep_test",
            "source_fingerprint": mod._source_fingerprint(episode_dir, episode),
            "output_stat": mod._file_stat(audio),
        },
    )
    audio.write_bytes(b"changed output")
    response = client.get("/api/episodes/ep_test/delivery")
    assert response.json()["status"] == "not_prepared"
    assert response.json()["stale"] is True


def test_download_requires_ready_file(delivery):
    client, _, episodes_dir = delivery
    make_episode(episodes_dir)
    response = client.get("/api/episodes/ep_test/delivery/audio")
    assert response.status_code == 404


def test_source_duration_prefers_probed_artifact(delivery):
    client, mod, episodes_dir = delivery
    make_episode(episodes_dir)
    with patch.object(mod, "get_duration", return_value=3599.75):
        response = client.get("/api/episodes/ep_test/delivery")
    assert response.json()["source_duration_seconds"] == 3599.75
