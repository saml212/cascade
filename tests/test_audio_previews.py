"""Regression tests for logical multi-segment audio previews."""

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routes import episodes


@pytest.fixture
def preview_client(tmp_path, monkeypatch):
    episodes_dir = tmp_path / "episodes"
    episode_dir = episodes_dir / "ep_test"
    (episode_dir / "audio").mkdir(parents=True)
    monkeypatch.setattr(episodes, "EPISODES_DIR", episodes_dir)
    app = FastAPI()
    app.include_router(episodes.router)
    return TestClient(app), episode_dir


def _write_episode(episode_dir: Path, tracks: list[dict], offset: float = 0) -> None:
    (episode_dir / "episode.json").write_text(
        json.dumps(
            {
                "episode_id": "ep_test",
                "audio_tracks": tracks,
                "audio_sync": {"offset_seconds": offset},
            }
        )
    )


def _track(episode_dir: Path, name: str, number: int, duration: float) -> dict:
    path = episode_dir / "audio" / name
    path.write_bytes(b"wav")
    return {
        "filename": name,
        "dest_path": str(path),
        "track_number": number,
        "duration_seconds": duration,
    }


def test_logical_preview_crosses_segment_boundary_once(preview_client, monkeypatch):
    client, episode_dir = preview_client
    tracks = [
        _track(episode_dir, "session_a_Tr1.WAV", 1, 100),
        _track(episode_dir, "session_b_Tr1.WAV", 1, 50),
    ]
    _write_episode(episode_dir, tracks, offset=5)

    async def fake_ffmpeg(cmd):
        Path(cmd[-1]).write_bytes(b"mp3")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    run = AsyncMock(side_effect=fake_ffmpeg)
    monkeypatch.setattr(episodes, "_run_ffmpeg", run)
    response = client.get(
        "/api/episodes/ep_test/audio-preview/track/1?start=85&duration=30"
    )

    assert response.status_code == 200
    cmd = run.await_args.args[0]
    first_seek = cmd[cmd.index("-ss") + 1]
    second_seek_index = cmd.index("-ss", cmd.index("-ss") + 1)
    assert first_seek == "90.0"
    assert cmd[second_seek_index + 1] == "0"
    assert "concat=n=2:v=0:a=1" in cmd[cmd.index("-filter_complex") + 1]


def test_preview_cache_changes_when_source_changes(preview_client, monkeypatch):
    client, episode_dir = preview_client
    tracks = [_track(episode_dir, "session_Tr2.WAV", 2, 100)]
    _write_episode(episode_dir, tracks)

    async def fake_ffmpeg(cmd):
        Path(cmd[-1]).write_bytes(b"mp3")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    run = AsyncMock(side_effect=fake_ffmpeg)
    monkeypatch.setattr(episodes, "_run_ffmpeg", run)
    url = "/api/episodes/ep_test/audio-preview/track/2?start=10&duration=20"
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    assert run.await_count == 1

    Path(tracks[0]["dest_path"]).write_bytes(b"changed source")
    assert client.get(url).status_code == 200
    assert run.await_count == 2


def test_negative_sync_offset_keeps_leading_silence(preview_client, monkeypatch):
    client, episode_dir = preview_client
    tracks = [_track(episode_dir, "session_Tr1.WAV", 1, 100)]
    _write_episode(episode_dir, tracks, offset=-2.5)

    async def fake_ffmpeg(cmd):
        Path(cmd[-1]).write_bytes(b"mp3")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    run = AsyncMock(side_effect=fake_ffmpeg)
    monkeypatch.setattr(episodes, "_run_ffmpeg", run)
    response = client.get(
        "/api/episodes/ep_test/audio-preview/track/1?start=0&duration=10"
    )

    assert response.status_code == 200
    cmd = run.await_args.args[0]
    assert "anullsrc=r=44100:cl=mono" in cmd
    silence_input = cmd.index("anullsrc=r=44100:cl=mono")
    assert cmd[silence_input - 2 : silence_input] == ["2.5", "-i"]
    assert "concat=n=2:v=0:a=1" in cmd[cmd.index("-filter_complex") + 1]


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("start=-1&duration=20", "start must be non-negative"),
        ("start=0&duration=121", "duration must be between"),
        ("start=0&duration=0", "duration must be between"),
    ],
)
def test_preview_rejects_unreasonable_windows(preview_client, query, detail):
    client, episode_dir = preview_client
    tracks = [_track(episode_dir, "session_Tr1.WAV", 1, 100)]
    _write_episode(episode_dir, tracks)

    response = client.get(f"/api/episodes/ep_test/audio-preview/track/1?{query}")

    assert response.status_code == 400
    assert detail in response.json()["detail"]


def test_preview_after_logical_track_end_returns_416(preview_client):
    client, episode_dir = preview_client
    tracks = [_track(episode_dir, "session_Tr1.WAV", 1, 10)]
    _write_episode(episode_dir, tracks)

    response = client.get(
        "/api/episodes/ep_test/audio-preview/track/1?start=20&duration=10"
    )

    assert response.status_code == 416


def test_sync_preview_rejects_unbounded_duration(preview_client):
    client, episode_dir = preview_client
    _write_episode(episode_dir, [], offset=0)

    response = client.get("/api/episodes/ep_test/sync-preview?duration=600")

    assert response.status_code == 400
    assert "duration must be between" in response.json()["detail"]
