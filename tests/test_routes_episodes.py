"""Tests for episode API routes."""

import json
import os
import importlib
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    """Create a test client with a temp episodes directory."""
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(episodes_dir))

    # Force reimport of modules that read env at import time
    import lib.paths
    importlib.reload(lib.paths)

    import server.routes.episodes as ep_mod
    importlib.reload(ep_mod)

    import server.routes.clips as clips_mod
    importlib.reload(clips_mod)

    import server.routes.pipeline as pipe_mod
    importlib.reload(pipe_mod)

    import server.routes.chat as chat_mod
    importlib.reload(chat_mod)

    import server.app as app_mod
    importlib.reload(app_mod)

    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)

    yield client, episodes_dir

    # Cleanup
    monkeypatch.delenv("CASCADE_OUTPUT_DIR", raising=False)
    importlib.reload(lib.paths)


def _create_episode(episodes_dir, episode_id, extra_data=None):
    """Create an episode directory with episode.json."""
    ep_dir = episodes_dir / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["shorts", "subtitles", "metadata", "qa"]:
        (ep_dir / sub).mkdir(exist_ok=True)
    data = {
        "episode_id": episode_id,
        "title": "Test {}".format(episode_id),
        "status": "ready_for_review",
        "source_path": "/tmp/source",
        "duration_seconds": 3600.0,
        "created_at": "2026-01-01T12:00:00+00:00",
        "clips": [],
        "pipeline": {"started_at": "2026-01-01T12:00:00+00:00", "completed_at": None, "agents_completed": []},
    }
    if extra_data:
        data.update(extra_data)
    with open(ep_dir / "episode.json", "w") as f:
        json.dump(data, f)
    return ep_dir


class TestListEpisodes:
    def test_empty_list(self, test_client):
        client, _ = test_client
        resp = client.get("/api/episodes/")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_episodes(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _create_episode(episodes_dir, "ep_002")
        resp = client.get("/api/episodes/")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_returns_summary_fields(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {
            "guest_name": "John Doe",
            "episode_name": "Test Episode",
        })
        resp = client.get("/api/episodes/")
        data = resp.json()
        assert data[0]["guest_name"] == "John Doe"
        assert data[0]["episode_name"] == "Test Episode"

    def test_list_skips_invalid_json(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        # Create a directory with invalid JSON
        bad_dir = episodes_dir / "ep_bad"
        bad_dir.mkdir()
        (bad_dir / "episode.json").write_text("not valid json")
        resp = client.get("/api/episodes/")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestGetEpisode:
    def test_get_existing(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.get("/api/episodes/ep_001")
        assert resp.status_code == 200
        assert resp.json()["episode_id"] == "ep_001"

    def test_get_not_found(self, test_client):
        client, _ = test_client
        resp = client.get("/api/episodes/nonexistent")
        assert resp.status_code == 404

    def test_get_loads_clips_from_clips_json(self, test_client):
        """If episode.json has no clips but clips.json exists, load from it."""
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clips = [{"id": "clip_01", "start_seconds": 10.0, "end_seconds": 20.0}]
        with open(ep_dir / "clips.json", "w") as f:
            json.dump({"clips": clips}, f)
        resp = client.get("/api/episodes/ep_001")
        data = resp.json()
        assert len(data["clips"]) == 1
        # Should be normalized
        assert data["clips"][0]["start"] == 10.0

    def test_get_normalizes_inline_clips(self, test_client):
        """Clips stored in episode.json should be normalized."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {
            "clips": [{"id": "clip_01", "start": 5.0, "end": 15.0}]
        })
        resp = client.get("/api/episodes/ep_001")
        data = resp.json()
        assert data["clips"][0]["start_seconds"] == 5.0


class TestCreateEpisode:
    def test_create(self, test_client):
        client, _ = test_client
        resp = client.post("/api/episodes/", json={"source_path": "/tmp/source"})
        assert resp.status_code == 200
        data = resp.json()
        assert "episode_id" in data
        assert data["status"] == "processing"

    def test_create_with_audio_path(self, test_client):
        client, _ = test_client
        resp = client.post("/api/episodes/", json={
            "source_path": "/tmp/source",
            "audio_path": "/tmp/audio",
            "speaker_count": 2,
        })
        assert resp.status_code == 200

    def test_create_without_source_path(self, test_client):
        client, _ = test_client
        resp = client.post("/api/episodes/", json={})
        assert resp.status_code == 200


class TestUpdateEpisode:
    def test_update_title(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.patch("/api/episodes/ep_001", json={"title": "New Title"})
        assert resp.status_code == 200

        # Verify
        resp2 = client.get("/api/episodes/ep_001")
        assert resp2.json()["title"] == "New Title"

    def test_update_multiple_fields(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.patch("/api/episodes/ep_001", json={
            "guest_name": "Jane Doe",
            "guest_title": "Engineer",
            "episode_name": "The Interview",
        })
        assert resp.status_code == 200
        resp2 = client.get("/api/episodes/ep_001")
        data = resp2.json()
        assert data["guest_name"] == "Jane Doe"
        assert data["guest_title"] == "Engineer"
        assert data["episode_name"] == "The Interview"

    def test_update_not_found(self, test_client):
        client, _ = test_client
        resp = client.patch("/api/episodes/nonexistent", json={"title": "X"})
        assert resp.status_code == 404


class TestDeleteEpisode:
    def test_delete_existing(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.delete("/api/episodes/ep_001")
        assert resp.status_code == 200
        assert not (episodes_dir / "ep_001").exists()

    def test_delete_not_found(self, test_client):
        client, _ = test_client
        resp = client.delete("/api/episodes/nonexistent")
        assert resp.status_code == 404


class TestCropConfig:
    def test_save_legacy_lr_format(self, test_client):
        """Test saving crop config with legacy L/R format."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speaker_l_center_x": 480,
            "speaker_l_center_y": 540,
            "speaker_r_center_x": 1440,
            "speaker_r_center_y": 540,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "saved"

        # Verify stored values
        config = resp.json()["crop_config"]
        assert config["speaker_l_center_x"] == 480
        assert config["speaker_r_center_x"] == 1440

    def test_save_n_speaker_format(self, test_client):
        """Test saving crop config with N-speaker format."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speakers": [
                {"label": "Host", "center_x": 480, "center_y": 540, "zoom": 1.2},
                {"label": "Guest", "center_x": 1440, "center_y": 540, "zoom": 1.0},
            ],
        })
        assert resp.status_code == 200
        config = resp.json()["crop_config"]
        assert len(config["speakers"]) == 2
        assert config["speakers"][0]["label"] == "Host"
        assert config["speakers"][1]["label"] == "Guest"

    def test_n_speaker_generates_legacy_fields(self, test_client):
        """N-speaker format should generate backward-compatible L/R fields."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speakers": [
                {"label": "Host", "center_x": 480, "center_y": 540, "zoom": 1.2},
                {"label": "Guest", "center_x": 1440, "center_y": 540, "zoom": 1.5},
            ],
        })
        config = resp.json()["crop_config"]
        # Legacy fields should be populated from first two speakers
        assert config["speaker_l_center_x"] == 480
        assert config["speaker_l_center_y"] == 540
        assert config["speaker_r_center_x"] == 1440
        assert config["speaker_r_center_y"] == 540
        assert config["speaker_l_zoom"] == 1.2
        assert config["speaker_r_zoom"] == 1.5

    def test_single_speaker_duplicates_lr(self, test_client):
        """Single speaker should set both L and R to the same values."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speakers": [
                {"label": "Solo", "center_x": 960, "center_y": 540, "zoom": 1.0},
            ],
        })
        config = resp.json()["crop_config"]
        assert config["speaker_l_center_x"] == 960
        assert config["speaker_r_center_x"] == 960
        assert config["speaker_l_zoom"] == 1.0
        assert config["speaker_r_zoom"] == 1.0

    def test_ambient_tracks_stored(self, test_client):
        """Ambient track config should be stored in crop_config."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speakers": [
                {"label": "Host", "center_x": 480, "center_y": 540},
                {"label": "Guest", "center_x": 1440, "center_y": 540},
            ],
            "ambient_tracks": [
                {"track_number": 3, "volume": 0.15},
                {"track_number": 4, "volume": 0.2},
            ],
        })
        config = resp.json()["crop_config"]
        assert len(config["ambient_tracks"]) == 2
        assert config["ambient_tracks"][0]["track_number"] == 3
        assert config["ambient_tracks"][0]["volume"] == 0.15

    def test_wide_shot_config_stored(self, test_client):
        """Wide shot center and zoom should be stored."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speakers": [
                {"label": "Host", "center_x": 480, "center_y": 540},
                {"label": "Guest", "center_x": 1440, "center_y": 540},
            ],
            "wide_center_x": 960,
            "wide_center_y": 540,
            "wide_zoom": 1.3,
        })
        config = resp.json()["crop_config"]
        assert config["wide_center_x"] == 960
        assert config["wide_center_y"] == 540
        assert config["wide_zoom"] == 1.3

    def test_crop_config_transitions_status(self, test_client):
        """Saving crop config should transition from awaiting_crop_setup to processing."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        client.post("/api/episodes/ep_001/crop-config", json={
            "speaker_l_center_x": 480,
            "speaker_l_center_y": 540,
            "speaker_r_center_x": 1440,
            "speaker_r_center_y": 540,
        })
        resp = client.get("/api/episodes/ep_001")
        assert resp.json()["status"] == "processing"

    def test_crop_config_no_status_change_if_not_awaiting(self, test_client):
        """If status is not awaiting_crop_setup, it should not change."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "ready_for_review"})
        client.post("/api/episodes/ep_001/crop-config", json={
            "speaker_l_center_x": 480,
            "speaker_l_center_y": 540,
            "speaker_r_center_x": 1440,
            "speaker_r_center_y": 540,
        })
        resp = client.get("/api/episodes/ep_001")
        assert resp.json()["status"] == "ready_for_review"

    def test_legacy_format_generates_speakers_array(self, test_client):
        """Legacy format should also store a speakers array."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speaker_l_center_x": 480,
            "speaker_l_center_y": 540,
            "speaker_r_center_x": 1440,
            "speaker_r_center_y": 540,
            "speaker_l_zoom": 1.2,
            "speaker_r_zoom": 1.5,
        })
        config = resp.json()["crop_config"]
        assert len(config["speakers"]) == 2
        assert config["speakers"][0]["center_x"] == 480
        assert config["speakers"][1]["center_x"] == 1440

    def test_crop_frame_not_found(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.get("/api/episodes/ep_001/crop-frame")
        assert resp.status_code == 404

    def test_crop_frame_served(self, test_client):
        """When crop_frame.jpg exists, it should be served."""
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        (ep_dir / "crop_frame.jpg").write_bytes(b"\xff\xd8\xff\xe0")  # JPEG magic bytes
        resp = client.get("/api/episodes/ep_001/crop-frame")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"

    def test_n_speaker_with_track_assignment(self, test_client):
        """Speakers with audio track assignments should be stored."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post("/api/episodes/ep_001/crop-config", json={
            "speakers": [
                {"label": "Host", "center_x": 480, "center_y": 540, "track": 1, "volume": 1.0},
                {"label": "Guest", "center_x": 1440, "center_y": 540, "track": 2, "volume": 0.8},
            ],
        })
        config = resp.json()["crop_config"]
        assert config["speakers"][0]["track"] == 1
        assert config["speakers"][1]["volume"] == 0.8


class TestAudioPreview:
    def test_audio_preview_track_not_found(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {
            "audio_tracks": [{"filename": "track_Tr1.WAV", "dest_path": "/tmp/audio/track.WAV"}],
        })
        resp = client.get("/api/episodes/ep_001/audio-preview/nonexistent")
        assert resp.status_code == 404

    def test_audio_preview_episode_not_found(self, test_client):
        client, _ = test_client
        resp = client.get("/api/episodes/nonexistent/audio-preview/track")
        assert resp.status_code == 404

    @patch("subprocess.run")
    def test_audio_preview_applies_sync_offset(self, mock_run, test_client):
        """Audio preview should add sync offset to the start time."""
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001", {
            "audio_tracks": [
                {"filename": "260311_TrLR.WAV", "dest_path": str(episodes_dir / "ep_001" / "audio" / "260311_TrLR.WAV")},
            ],
            "audio_sync": {"offset_seconds": 2.5},
        })
        # Create the audio file
        audio_dir = ep_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        (audio_dir / "260311_TrLR.WAV").write_bytes(b"\x00" * 1000)

        # Create work directory and cache file to avoid actual ffmpeg call
        cache_dir = ep_dir / "work" / "audio_preview"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "260311_TrLR_30_60.mp3"
        cache_file.write_bytes(b"\xff\xfb\x90")  # MP3 header bytes

        resp = client.get("/api/episodes/ep_001/audio-preview/260311_TrLR?start=30&duration=60")
        assert resp.status_code == 200


class TestApproveEpisode:
    def test_approve(self, test_client):
        client, episodes_dir = test_client
        clips = [{"id": "clip_01", "status": "pending"}]
        _create_episode(episodes_dir, "ep_001", {"clips": clips})
        resp = client.post("/api/episodes/ep_001/approve")
        assert resp.status_code == 200

        resp2 = client.get("/api/episodes/ep_001")
        data = resp2.json()
        assert data["status"] == "approved"
        assert all(c["status"] == "approved" for c in data["clips"])

    def test_approve_updates_clips_json(self, test_client):
        """Approving should also update clips.json if it exists."""
        client, episodes_dir = test_client
        clips = [{"id": "clip_01", "status": "pending"}]
        ep_dir = _create_episode(episodes_dir, "ep_001", {"clips": clips})
        with open(ep_dir / "clips.json", "w") as f:
            json.dump({"clips": clips}, f)

        client.post("/api/episodes/ep_001/approve")

        with open(ep_dir / "clips.json") as f:
            data = json.load(f)
        assert data["clips"][0]["status"] == "approved"
