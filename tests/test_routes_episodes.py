"""Tests for episode API routes."""

import json
import importlib
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch

from agents.qa import canonical_release_metadata, quality_revision


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
        "pipeline": {
            "started_at": "2026-01-01T12:00:00+00:00",
            "completed_at": None,
            "agents_completed": [],
        },
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
        _create_episode(
            episodes_dir,
            "ep_001",
            {
                "guest_name": "John Doe",
                "episode_name": "Test Episode",
            },
        )
        resp = client.get("/api/episodes/")
        data = resp.json()
        assert data[0]["guest_name"] == "John Doe"
        assert data[0]["episode_name"] == "Test Episode"

    def test_list_uses_canonical_clips_and_explicit_counts(self, test_client):
        client, episodes_dir = test_client
        ep_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {"clips": [{"id": "stale", "status": "approved"}]},
        )
        canonical = [
            {"id": "selected", "selection_status": "selected"},
            {"id": "approved", "status": "approved"},
            {"id": "candidate", "status": "pending"},
            {
                "id": "rejected",
                "selection_status": "selected",
                "status": "rejected",
            },
        ]
        (ep_dir / "clips.json").write_text(json.dumps({"clips": canonical}))

        summary = client.get("/api/episodes/").json()[0]

        assert [clip["id"] for clip in summary["clips"]] == [
            "selected",
            "approved",
            "candidate",
            "rejected",
        ]
        assert summary["clip_count"] == 4
        assert summary["selected_clip_count"] == 2
        assert summary["nonrejected_clip_count"] == 3
        assert summary["rejected_clip_count"] == 1
        assert json.loads((ep_dir / "episode.json").read_text())["clips"] == [
            {"id": "stale", "status": "approved"}
        ]

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

    def test_list_recovers_verified_video_missing_from_cached_delivery(
        self, test_client
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        audio = ep_dir / "podcast_audio.mp3"
        video = ep_dir / "upload_video.mp4"
        canonical = ep_dir / "work" / "audio_mix.wav"
        canonical.parent.mkdir()
        audio.write_bytes(b"audio")
        video.write_bytes(b"video")
        canonical.write_bytes(b"canonical")
        import server.routes.episodes as episodes_mod
        from agents.podcast_feed import PodcastFeedAgent
        from server.routes.delivery import _source_fingerprint

        episode = json.loads((ep_dir / "episode.json").read_text())
        config = episodes_mod.load_config()

        def finish_audio(command, **_kwargs):
            Path(command[-1]).write_bytes(b"audio")
            return subprocess.CompletedProcess(command, 0, stderr="")

        with patch("agents.podcast_feed.subprocess.run", side_effect=finish_audio):
            PodcastFeedAgent(ep_dir, config).prepare_local_audio()
        cached_status = {
            "status": "ready",
            "duration_seconds": 120.0,
            "download_url": "/api/episodes/ep_001/delivery/audio",
            "output_stat": {
                "size": audio.stat().st_size,
                "mtime_ns": audio.stat().st_mtime_ns,
            },
            "source_fingerprint": _source_fingerprint(ep_dir, episode, config),
        }
        (ep_dir / "delivery.json").write_text(json.dumps(cached_status))

        def current_video_fields(_dir, _current, _config):
            return {
                "video_status": "ready",
                "video_download_url": "/api/episodes/ep_001/delivery/video?v=current",
                "video_output_stat": {
                    "size": video.stat().st_size,
                    "mtime_ns": video.stat().st_mtime_ns,
                },
                "video_source_fingerprint": "video-current",
                "video": {
                    "duration_seconds": 120.0,
                    "width": 1920,
                    "height": 1080,
                },
            }

        with patch.object(
            episodes_mod,
            "current_delivery_video_fields",
            side_effect=current_video_fields,
        ):
            delivery = client.get("/api/episodes/").json()[0]["delivery"]
            assert delivery["status"] == "ready"
            assert delivery["video_status"] == "ready"
            assert delivery["video"]["duration_seconds"] == 120.0

            detail = client.get("/api/episodes/ep_001").json()["delivery"]
            assert detail["video_status"] == "ready"
        assert json.loads((ep_dir / "delivery.json").read_text()) == cached_status

    def test_list_does_not_recover_unproven_video(self, test_client):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        (ep_dir / "upload_video.mp4").write_bytes(b"unproven")
        cached_status = {"status": "not_prepared", "episode_id": "ep_001"}
        (ep_dir / "delivery.json").write_text(json.dumps(cached_status))

        delivery = client.get("/api/episodes/").json()[0]["delivery"]

        assert delivery["video_status"] == "not_prepared"
        assert "video_download_url" not in delivery
        assert json.loads((ep_dir / "delivery.json").read_text()) == cached_status


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

    def test_get_downgrades_missing_delivery_artifact_without_mutating_status(
        self, test_client
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        status = {
            "status": "preparing",
            "video_status": "ready",
            "video_download_url": "/api/episodes/ep_001/delivery/video",
            "video_output_stat": {"size": 10, "mtime_ns": 20},
        }
        (ep_dir / "delivery.json").write_text(json.dumps(status))

        delivery = client.get("/api/episodes/ep_001").json()["delivery"]
        assert delivery["status"] == "preparing"
        assert delivery["video_status"] == "not_prepared"
        assert json.loads((ep_dir / "delivery.json").read_text()) == status

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
        _create_episode(
            episodes_dir,
            "ep_001",
            {"clips": [{"id": "clip_01", "start": 5.0, "end": 15.0}]},
        )
        resp = client.get("/api/episodes/ep_001")
        data = resp.json()
        assert data["clips"][0]["start_seconds"] == 5.0


class TestCreateEpisode:
    def test_create(self, test_client, tmp_path):
        client, _ = test_client
        resp = client.post("/api/episodes/", json={"source_path": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert "episode_id" in data
        assert data["status"] == "processing"

    def test_create_with_audio_path(self, test_client, tmp_path):
        client, _ = test_client
        resp = client.post(
            "/api/episodes/",
            json={
                "source_path": str(tmp_path),
                "audio_path": str(tmp_path),
                "speaker_count": 2,
            },
        )
        assert resp.status_code == 200

    def test_missing_source_does_not_create_broken_episode(self, test_client, tmp_path):
        client, episodes_dir = test_client
        response = client.post(
            "/api/episodes/", json={"source_path": str(tmp_path / "missing")}
        )
        assert response.status_code == 422
        assert "does not exist" in response.json()["detail"]
        assert not list(episodes_dir.iterdir())

    def test_create_without_source_path(self, test_client):
        client, _ = test_client
        resp = client.post("/api/episodes/", json={})
        assert resp.status_code == 200

    def test_create_rejects_episode_id_collision(self, test_client):
        client, _ = test_client
        first = client.post("/api/episodes/", json={})
        second = client.post("/api/episodes/", json={})

        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["detail"].endswith("already exists")


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
        resp = client.patch(
            "/api/episodes/ep_001",
            json={
                "guest_name": "Jane Doe",
                "guest_title": "Engineer",
                "episode_name": "The Interview",
            },
        )
        assert resp.status_code == 200
        resp2 = client.get("/api/episodes/ep_001")
        data = resp2.json()
        assert data["guest_name"] == "Jane Doe"
        assert data["guest_title"] == "Engineer"
        assert data["episode_name"] == "The Interview"

    def test_update_release_copy_changes_quality_revision(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {"description": "Draft description", "tags": ["draft"]},
        )
        before = quality_revision(episode_dir)

        response = client.patch(
            "/api/episodes/ep_001",
            json={
                "title": "Final title",
                "description": "Final description",
                "tags": ["final"],
            },
        )

        assert response.status_code == 200
        assert quality_revision(episode_dir) != before
        metadata = canonical_release_metadata(episode_dir)
        assert metadata["longform"] == {
            "title": "Final title",
            "description": "Final description",
            "tags": ["final"],
        }

    def test_supplied_youtube_url_replaces_receipt_provenance(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {
                "youtube_longform_url": "https://youtu.be/receipt",
                "youtube_longform_url_source": "upload_post_receipt",
                "youtube_longform_url_captured_at": "2026-01-01T00:00:00+00:00",
            },
        )

        response = client.patch(
            "/api/episodes/ep_001",
            json={"youtube_longform_url": "https://youtu.be/supplied"},
        )

        assert response.status_code == 200
        episode = json.loads((episode_dir / "episode.json").read_text())
        assert episode["youtube_longform_url_source"] == "supplied"
        assert "youtube_longform_url_captured_at" not in episode

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
    @patch("server.routes.episodes.get_dimensions", return_value=(3840, 2160))
    def test_crop_dimensions_use_source_merged(self, get_dimensions_mock, test_client):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        merged = ep_dir / "source_merged.mp4"
        merged.write_bytes(b"video")
        (ep_dir / "stitch.json").write_text(
            json.dumps({"output_path": "/stale/moved/output.mp4"})
        )

        response = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speaker_l_center_x": 480,
                "speaker_l_center_y": 540,
                "speaker_r_center_x": 1440,
                "speaker_r_center_y": 540,
            },
        )

        assert response.status_code == 200
        assert response.json()["crop_config"]["source_width"] == 3840
        assert response.json()["crop_config"]["source_height"] == 2160
        get_dimensions_mock.assert_called_once_with(merged)

    def test_save_legacy_lr_format(self, test_client):
        """Test saving crop config with legacy L/R format."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speaker_l_center_x": 480,
                "speaker_l_center_y": 540,
                "speaker_r_center_x": 1440,
                "speaker_r_center_y": 540,
            },
        )
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
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speakers": [
                    {"label": "Host", "center_x": 480, "center_y": 540, "zoom": 1.2},
                    {"label": "Guest", "center_x": 1440, "center_y": 540, "zoom": 1.0},
                ],
            },
        )
        assert resp.status_code == 200
        config = resp.json()["crop_config"]
        assert len(config["speakers"]) == 2
        assert config["speakers"][0]["label"] == "Host"
        assert config["speakers"][1]["label"] == "Guest"

    def test_n_speaker_generates_legacy_fields(self, test_client):
        """N-speaker format should generate backward-compatible L/R fields."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speakers": [
                    {"label": "Host", "center_x": 480, "center_y": 540, "zoom": 1.2},
                    {"label": "Guest", "center_x": 1440, "center_y": 540, "zoom": 1.5},
                ],
            },
        )
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
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speakers": [
                    {"label": "Solo", "center_x": 960, "center_y": 540, "zoom": 1.0},
                ],
            },
        )
        config = resp.json()["crop_config"]
        assert config["speaker_l_center_x"] == 960
        assert config["speaker_r_center_x"] == 960
        assert config["speaker_l_zoom"] == 1.0
        assert config["speaker_r_zoom"] == 1.0

    def test_ambient_tracks_stored(self, test_client):
        """Ambient track config should be stored in crop_config."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speakers": [
                    {"label": "Host", "center_x": 480, "center_y": 540},
                    {"label": "Guest", "center_x": 1440, "center_y": 540},
                ],
                "ambient_tracks": [
                    {"track_number": 3, "volume": 0.15},
                    {"track_number": 4, "volume": 0.2},
                ],
            },
        )
        config = resp.json()["crop_config"]
        assert len(config["ambient_tracks"]) == 2
        assert config["ambient_tracks"][0]["track_number"] == 3
        assert config["ambient_tracks"][0]["volume"] == 0.15

    def test_wide_shot_config_stored(self, test_client):
        """Wide shot center and zoom should be stored."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speakers": [
                    {"label": "Host", "center_x": 480, "center_y": 540},
                    {"label": "Guest", "center_x": 1440, "center_y": 540},
                ],
                "wide_center_x": 960,
                "wide_center_y": 540,
                "wide_zoom": 1.3,
            },
        )
        config = resp.json()["crop_config"]
        assert config["wide_center_x"] == 960
        assert config["wide_center_y"] == 540
        assert config["wide_zoom"] == 1.3

    def test_crop_config_transitions_status(self, test_client):
        """Saving crop config marks the episode ready for an explicit render."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "awaiting_crop_setup"})
        client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speaker_l_center_x": 480,
                "speaker_l_center_y": 540,
                "speaker_r_center_x": 1440,
                "speaker_r_center_y": 540,
            },
        )
        resp = client.get("/api/episodes/ep_001")
        assert resp.json()["status"] == "ready_to_render"

    def test_crop_config_reopens_completed_episode(self, test_client):
        """Editing crop on a completed episode marks its renders stale."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001", {"status": "ready_for_review"})
        client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speaker_l_center_x": 480,
                "speaker_l_center_y": 540,
                "speaker_r_center_x": 1440,
                "speaker_r_center_y": 540,
            },
        )
        resp = client.get("/api/episodes/ep_001")
        assert resp.json()["status"] == "ready_to_render"

    @patch("lib.audio_mix.generate_audio_mix")
    def test_crop_change_preserves_deliverables_and_does_not_render(
        self, generate_mix, test_client
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {
                "status": "awaiting_longform_approval",
                "audio_tracks": [{"filename": "Tr1.wav", "track_number": 1}],
                "pipeline": {
                    "agents_completed": [
                        "ingest",
                        "speaker_cut",
                        "longform_render",
                        "publish",
                        "backup",
                    ],
                    "errors": {"qa": "old error"},
                },
            },
        )
        preserved = [
            ep_dir / "longform.mp4",
            ep_dir / "publish.json",
            ep_dir / "shorts" / "clip_01.mp4",
        ]
        for path in preserved:
            path.write_bytes(b"finished")
        work = ep_dir / "work"
        work.mkdir()
        disposable = work / "speaker_0_rms_db.npy"
        disposable.write_bytes(b"cache")

        response = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speakers": [
                    {"label": "Host", "center_x": 480, "center_y": 540, "track": 1}
                ]
            },
        )

        assert response.status_code == 200
        assert response.json()["changed"] is True
        assert all(path.read_bytes() == b"finished" for path in preserved)
        assert not disposable.exists()
        generate_mix.assert_not_called()
        saved = client.get("/api/episodes/ep_001").json()
        assert saved["status"] == "ready_to_render"
        assert saved["pipeline"]["agents_completed"] == [
            "ingest",
            "publish",
            "backup",
        ]

    def test_noop_crop_save_preserves_status_and_completed_agents(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        payload = {
            "speakers": [
                {"label": "Host", "center_x": 480, "center_y": 540, "track": 1}
            ]
        }
        assert (
            client.post("/api/episodes/ep_001/crop-config", json=payload).status_code
            == 200
        )
        episode_file = episodes_dir / "ep_001" / "episode.json"
        saved = json.loads(episode_file.read_text())
        saved["status"] = "awaiting_longform_approval"
        saved["pipeline"]["agents_completed"] = ["longform_render", "publish"]
        episode_file.write_text(json.dumps(saved))

        response = client.post("/api/episodes/ep_001/crop-config", json=payload)

        assert response.status_code == 200
        assert response.json()["changed"] is False
        unchanged = json.loads(episode_file.read_text())
        assert unchanged["status"] == "awaiting_longform_approval"
        assert unchanged["pipeline"]["agents_completed"] == [
            "longform_render",
            "publish",
        ]

    def test_longform_only_crop_keeps_short_renders_and_speaker_cache(
        self, test_client
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        base_payload = {
            "speakers": [
                {
                    "label": "Host",
                    "center_x": 480,
                    "center_y": 540,
                    "longform_center_x": 500,
                    "longform_center_y": 500,
                    "track": 1,
                },
                {
                    "label": "Guest",
                    "center_x": 1440,
                    "center_y": 540,
                    "longform_center_x": 1420,
                    "longform_center_y": 500,
                    "track": 2,
                },
            ]
        }
        assert (
            client.post(
                "/api/episodes/ep_001/crop-config", json=base_payload
            ).status_code
            == 200
        )
        episode_path = ep_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        episode["pipeline"] = {
            "agents_completed": [
                "speaker_cut",
                "longform_render",
                "shorts_render",
                "qa",
                "podcast_feed",
            ],
            "errors": {},
        }
        episode_path.write_text(json.dumps(episode))
        work = ep_dir / "work"
        work.mkdir(exist_ok=True)
        (work / "audio_mix.wav").write_bytes(b"audio")
        rms = work / "speaker_0_rms_db.npy"
        rms.write_bytes(b"rms")
        (ep_dir / "clips.json").write_text(json.dumps({"clips": [{"id": "clip_01"}]}))
        changed_payload = json.loads(json.dumps(base_payload))
        changed_payload["speakers"][0]["longform_center_y"] = 620
        rebound = {"segments": [{"start": 0, "end": 10, "speaker": "speaker_0"}]}

        with (
            patch(
                "server.routes.episodes.rebind_visual_crop_segments",
                return_value=rebound,
            ),
            patch(
                "server.routes.episodes.current_speaker_segments",
                return_value=rebound,
            ),
            patch(
                "server.routes.episodes.migrate_unchanged_short_crop_fingerprints",
                return_value=["clip_01"],
            ),
            patch(
                "server.routes.episodes.migrate_unchanged_delivery_audio_fingerprint",
                return_value=True,
            ),
        ):
            response = client.post(
                "/api/episodes/ep_001/crop-config", json=changed_payload
            )

        assert response.status_code == 200
        result = response.json()
        assert result["invalidated_agents"] == ["longform_render", "qa"]
        assert result["speaker_segments_preserved"] is True
        assert result["migrated_short_render_ids"] == ["clip_01"]
        assert result["delivery_audio_preserved"] is True
        assert rms.read_bytes() == b"rms"
        saved = json.loads(episode_path.read_text())
        assert saved["pipeline"]["agents_completed"] == [
            "speaker_cut",
            "shorts_render",
            "podcast_feed",
        ]

    def test_legacy_format_generates_speakers_array(self, test_client):
        """Legacy format should also store a speakers array."""
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speaker_l_center_x": 480,
                "speaker_l_center_y": 540,
                "speaker_r_center_x": 1440,
                "speaker_r_center_y": 540,
                "speaker_l_zoom": 1.2,
                "speaker_r_zoom": 1.5,
            },
        )
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
        resp = client.post(
            "/api/episodes/ep_001/crop-config",
            json={
                "speakers": [
                    {
                        "label": "Host",
                        "center_x": 480,
                        "center_y": 540,
                        "track": 1,
                        "volume": 1.0,
                    },
                    {
                        "label": "Guest",
                        "center_x": 1440,
                        "center_y": 540,
                        "track": 2,
                        "volume": 0.8,
                    },
                ],
            },
        )
        config = resp.json()["crop_config"]
        assert config["speakers"][0]["track"] == 1
        assert config["speakers"][1]["volume"] == 0.8


class TestAudioPreview:
    def test_audio_preview_track_not_found(self, test_client):
        client, episodes_dir = test_client
        _create_episode(
            episodes_dir,
            "ep_001",
            {
                "audio_tracks": [
                    {"filename": "track_Tr1.WAV", "dest_path": "/tmp/audio/track.WAV"}
                ],
            },
        )
        resp = client.get("/api/episodes/ep_001/audio-preview/nonexistent")
        assert resp.status_code == 404

    def test_audio_preview_episode_not_found(self, test_client):
        client, _ = test_client
        resp = client.get("/api/episodes/nonexistent/audio-preview/track")
        assert resp.status_code == 404

    @patch("server.routes.episodes._run_ffmpeg")
    def test_audio_preview_applies_sync_offset(self, mock_run, test_client):
        """Audio preview should add sync offset to the start time."""
        client, episodes_dir = test_client
        ep_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {
                "audio_tracks": [
                    {
                        "filename": "260311_TrLR.WAV",
                        "dest_path": str(
                            episodes_dir / "ep_001" / "audio" / "260311_TrLR.WAV"
                        ),
                    },
                ],
                "audio_sync": {"offset_seconds": 2.5},
            },
        )
        # Create the audio file
        audio_dir = ep_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        (audio_dir / "260311_TrLR.WAV").write_bytes(b"\x00" * 1000)

        async def fake_ffmpeg(cmd):
            Path(cmd[-1]).write_bytes(b"\xff\xfb\x90")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        mock_run.side_effect = fake_ffmpeg

        resp = client.get(
            "/api/episodes/ep_001/audio-preview/260311_TrLR?start=30&duration=60"
        )
        assert resp.status_code == 200
        cmd = mock_run.await_args.args[0]
        assert cmd[cmd.index("-ss") + 1] == "32.5"


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
