from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib import apfs_clone
from lib.apfs_clone import CloneSafetyError, clone_to_new_path, snapshot_path

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires APFS")

EPISODE_ID = "ep_generic_source_recovery"
CURRENT_BYTES = b"legacy-trimmed-source\n" * 32_768
CANDIDATE_BYTES = b"preserved-original-source\n" * 32_768
SOURCE_CLOCK = {
    "offset_seconds": -6.5,
    "tempo_factor": 0.99999,
    "drift_rate_ppm": -10.0,
    "sync_track": "recorder.wav",
    "evidence": {
        "method": "multi-anchor correlation",
        "anchor_count": 10,
        "track_count": 3,
        "summary": "Three recorder tracks agree across ten source-clock anchors.",
        "fit_r_squared": 0.999,
        "artifact_references": ["audit/sync-fit.json"],
    },
}


def _probe_data(path: Path) -> dict:
    original = path.read_bytes() == CANDIDATE_BYTES
    return {
        "format": {"duration": "20.25" if original else "17.5"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 1280,
                "height": 720,
                "r_frame_rate": "30000/1001",
                "color_space": "bt709",
                "color_primaries": "bt709",
                "color_transfer": "bt709",
            }
        ],
    }


def _post_body(inspection: dict, source_clock: dict | None = None) -> dict:
    return {
        **inspection["apply_binding"],
        "expected_candidate_revision": inspection["candidate_source"]["revision"],
        "source_clock": deepcopy(source_clock or SOURCE_CLOCK),
    }


@pytest.fixture
def recovery_api(tmp_path, monkeypatch):
    episodes_dir = tmp_path / "episodes"
    episode_dir = episodes_dir / EPISODE_ID
    audio_dir = episode_dir / "audio"
    audio_dir.mkdir(parents=True)
    monkeypatch.setenv("CASCADE_OUTPUT_DIR", str(episodes_dir))

    recorder = audio_dir / "recorder.wav"
    recorder.write_bytes(b"recorder")
    episode = {
        "episode_id": EPISODE_ID,
        "duration_seconds": 17.5,
        "source_properties": {"width": 640},
        "audio_sync": None,
        "audio_tracks": [
            {
                "filename": recorder.name,
                "dest_path": str(recorder),
                "track_type": "stereo_mix",
            }
        ],
        "status": "ready_for_review",
        "pipeline": {
            "agents_completed": [
                "ingest",
                "stitch",
                "audio_analysis",
                "speaker_cut",
                "transcribe",
                "longform_render",
                "qa",
                "publish",
            ],
            "errors": {"stitch": "historical", "qa": "stale"},
            "current_agent": "stale-marker",
            "completed_at": "2026-01-01T00:00:00+00:00",
        },
    }
    (episode_dir / "episode.json").write_text(json.dumps(episode))
    current = episode_dir / "source_merged.mp4"
    candidate = episode_dir / "source_merged_original.mp4"
    current.write_bytes(CURRENT_BYTES)
    candidate.write_bytes(CANDIDATE_BYTES)
    candidate.chmod(0o640)
    os.utime(candidate, ns=(1_700_000_000_123_456_789,) * 2)
    subprocess.run(
        ["xattr", "-w", "user.recovery-test", "preserve", candidate], check=True
    )
    (episode_dir / "longform.mp4").write_bytes(b"derived artifact")

    import lib.paths
    from server.routes import source_recovery

    importlib.reload(lib.paths)
    importlib.reload(source_recovery)
    monkeypatch.setattr(source_recovery, "probe", _probe_data)

    app = FastAPI()
    app.include_router(source_recovery.router)
    yield TestClient(app, raise_server_exceptions=False), source_recovery, episode_dir


def test_inspection_binds_generic_sources_and_requires_an_audited_fit(recovery_api):
    client, _, episode_dir = recovery_api

    response = client.get(f"/api/episodes/{EPISODE_ID}/inspection/source-recovery")

    assert response.status_code == 200
    result = response.json()
    assert result["eligible"] is True
    assert result["current_source"]["revision"] == (
        "sha256:" + hashlib.sha256(CURRENT_BYTES).hexdigest()
    )
    assert result["candidate_source"]["revision"] == (
        "sha256:" + hashlib.sha256(CANDIDATE_BYTES).hexdigest()
    )
    assert result["candidate_source"]["duration_seconds"] == 20.25
    assert result["candidate_episode_values"]["source_properties"]["width"] == 1280
    assert result["apply_binding"] == {
        "expected_revision": result["revision"],
        "candidate": "source_merged_original.mp4",
    }
    assert result["source_clock_contract"]["server_infers_fit"] is False
    assert (
        result["candidate_confirmation"][
            "revision_to_verify_against_the_external_audit"
        ]
        == result["candidate_source"]["revision"]
    )
    plan = result["preservation_plan"]
    assert plan["predecessor_filename"].startswith("source_merged.pre-recovery-")
    assert plan["delete_paths"] == []
    assert plan["pipeline_agents_to_invalidate"] == [
        "audio_analysis",
        "speaker_cut",
        "transcribe",
        "longform_render",
        "qa",
        "publish",
    ]
    assert not (episode_dir / plan["predecessor_filename"]).exists()


def test_recovery_clones_original_preserves_predecessor_and_updates_episode(
    recovery_api, monkeypatch
):
    client, route, episode_dir = recovery_api
    current = episode_dir / "source_merged.mp4"
    candidate = episode_dir / "source_merged_original.mp4"
    current_inode = current.stat().st_ino
    candidate_inode = candidate.stat().st_ino
    candidate_mtime = candidate.stat().st_mtime_ns
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    write = route.atomic_write_json

    def write_while_job_registration_is_blocked(*args, **kwargs):
        assert route.delivery._running_lock.locked()
        assert route.clips._render_jobs_lock.locked()
        write(*args, **kwargs)

    monkeypatch.setattr(
        route, "atomic_write_json", write_while_job_registration_is_blocked
    )

    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery",
        json=_post_body(inspection),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    predecessor = episode_dir / result["preserved_predecessor"]["filename"]
    assert result["source"]["revision"] == inspection["candidate_source"]["revision"]
    assert result["system_xattr_changes"] == []
    assert current.read_bytes() == CANDIDATE_BYTES
    assert current.stat().st_ino not in {current_inode, candidate_inode}
    assert current.stat().st_nlink == candidate.stat().st_nlink == 1
    assert current.stat().st_mode & 0o777 == 0o640
    assert current.stat().st_mtime_ns == candidate_mtime
    assert (
        subprocess.run(
            ["xattr", "-p", "user.recovery-test", current],
            check=True,
            capture_output=True,
        ).stdout.rstrip(b"\n")
        == b"preserve"
    )
    assert candidate.read_bytes() == CANDIDATE_BYTES
    assert candidate.stat().st_ino == candidate_inode
    assert predecessor.read_bytes() == CURRENT_BYTES
    assert predecessor.stat().st_ino == current_inode
    assert (episode_dir / "longform.mp4").read_bytes() == b"derived artifact"

    episode = json.loads((episode_dir / "episode.json").read_text())
    assert episode["duration_seconds"] == 20.25
    assert episode["source_properties"]["width"] == 1280
    assert episode["audio_sync"]["offset_seconds"] == -6.5
    assert episode["audio_sync"]["tempo_factor"] == 0.99999
    assert episode["audio_sync"]["sync_track"] == "recorder.wav"
    assert episode["status"] == "ready_to_render"
    assert episode["pipeline"]["agents_completed"] == ["ingest", "stitch"]
    assert episode["pipeline"]["errors"] == {"stitch": "historical"}
    assert "current_agent" not in episode["pipeline"]
    assert "completed_at" not in episode["pipeline"]
    assert (
        result["invalidated_agents"]
        == inspection["preservation_plan"]["pipeline_agents_to_invalidate"]
    )
    assert episode["source_recovery"]["predecessor_filename"] == predecessor.name
    assert (
        episode["source_recovery"]["installed_source"]["revision"]
        == result["source"]["revision"]
    )

    with current.open("r+b") as output:
        output.write(b"changed clone")
    assert candidate.read_bytes() == CANDIDATE_BYTES


def test_recovery_rejects_stale_revision_without_writes(recovery_api):
    client, _, episode_dir = recovery_api
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    current = episode_dir / "source_merged.mp4"
    current.write_bytes(b"changed after inspection")

    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery",
        json=_post_body(inspection),
    )

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert current.read_bytes() == b"changed after inspection"
    assert not list(episode_dir.glob("source_merged.pre-recovery-*.mp4"))


def test_recovery_requires_external_candidate_confirmation(recovery_api):
    client, _, episode_dir = recovery_api
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    body = _post_body(inspection)
    body["expected_candidate_revision"] = "sha256:" + "0" * 64

    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery", json=body
    )

    assert response.status_code == 409
    assert "reviewed audit" in response.json()["detail"]
    assert (episode_dir / "source_merged.mp4").read_bytes() == CURRENT_BYTES


def test_recovery_detects_source_race_after_clone(recovery_api, monkeypatch):
    client, route, episode_dir = recovery_api
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    clone = route.clone_to_new_path

    def clone_then_change_current(source, destination):
        result = clone(source, destination)
        (episode_dir / "source_merged.mp4").write_bytes(b"concurrent change")
        return result

    monkeypatch.setattr(route, "clone_to_new_path", clone_then_change_current)
    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery",
        json=_post_body(inspection),
    )

    assert response.status_code == 409
    assert "no longer matches snapshot" in response.json()["detail"]
    assert not list(episode_dir.glob("source_merged.pre-recovery-*.mp4"))
    assert not list(episode_dir.glob(".source_merged.mp4.recovery-*"))


@pytest.mark.parametrize("field", ["candidate", "source_clock"])
def test_recovery_rejects_arbitrary_candidate_and_inconsistent_fit(recovery_api, field):
    client, _, episode_dir = recovery_api
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    request = _post_body(inspection)
    if field == "candidate":
        request[field] = "../source.mp4"
    else:
        request[field]["drift_rate_ppm"] = 25

    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery", json=request
    )

    assert response.status_code == 422
    assert (episode_dir / "source_merged.mp4").read_bytes() == CURRENT_BYTES
    assert not list(episode_dir.glob("source_merged.pre-recovery-*.mp4"))


def test_recovery_requires_registered_sync_track(recovery_api):
    client, _, episode_dir = recovery_api
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    request = _post_body(inspection)
    request["source_clock"]["sync_track"] = "unknown.wav"

    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery", json=request
    )

    assert response.status_code == 409
    assert "not registered" in response.json()["detail"]
    assert (episode_dir / "source_merged.mp4").read_bytes() == CURRENT_BYTES


def test_recovery_accepts_only_an_identity_fit_without_a_sync_track(recovery_api):
    client, _, episode_dir = recovery_api
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    identity = deepcopy(SOURCE_CLOCK)
    identity.update(
        offset_seconds=0,
        tempo_factor=1,
        drift_rate_ppm=0,
        sync_track=None,
    )
    identity["evidence"]["track_count"] = 0
    invalid = deepcopy(identity)
    invalid["offset_seconds"] = 0.25

    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery",
        json=_post_body(inspection, invalid),
    )

    assert response.status_code == 422
    assert (episode_dir / "source_merged.mp4").read_bytes() == CURRENT_BYTES


def test_recovery_rejects_active_episode_job(recovery_api, monkeypatch):
    client, route, episode_dir = recovery_api
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    monkeypatch.setattr(route, "_registered_job_reason", lambda *_args: "pipeline")

    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery",
        json=_post_body(inspection),
    )

    assert response.status_code == 409
    assert "pipeline is active" in response.json()["detail"]
    assert (episode_dir / "source_merged.mp4").read_bytes() == CURRENT_BYTES


def test_episode_write_commit_then_error_rolls_back_json_and_source(
    recovery_api, monkeypatch
):
    client, route, episode_dir = recovery_api
    current = episode_dir / "source_merged.mp4"
    current_inode = current.stat().st_ino
    episode_path = episode_dir / "episode.json"
    original_episode = episode_path.read_bytes()
    original_mode = episode_path.stat().st_mode & 0o777
    inspection = client.get(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery"
    ).json()
    write = route.atomic_write_json

    def commit_then_raise(*args, **kwargs):
        write(*args, **kwargs)
        raise OSError("injected error after commit")

    monkeypatch.setattr(route, "atomic_write_json", commit_then_raise)
    response = client.post(
        f"/api/episodes/{EPISODE_ID}/inspection/source-recovery",
        json=_post_body(inspection),
    )

    assert response.status_code == 500
    assert "rolled back" in response.json()["detail"]
    assert current.read_bytes() == CURRENT_BYTES
    assert current.stat().st_ino == current_inode
    assert episode_path.read_bytes() == original_episode
    assert episode_path.stat().st_mode & 0o777 == original_mode
    assert not list(episode_dir.glob("source_merged.pre-recovery-*.mp4"))
    assert not list(episode_dir.glob(".source_merged.mp4.recovery-*"))


def test_clone_to_new_path_has_no_byte_copy_fallback(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source bytes")
    source_snapshot = snapshot_path(source)

    def unsupported_clone(*_args):
        ctypes.set_errno(errno.ENOTSUP)
        return -1

    monkeypatch.setattr(apfs_clone, "_fclonefileat", unsupported_clone)
    with pytest.raises(CloneSafetyError, match="no byte-copy fallback"):
        clone_to_new_path(source_snapshot, destination)
    assert source.read_bytes() == b"source bytes"
    assert not destination.exists()
