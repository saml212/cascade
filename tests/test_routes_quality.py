"""Quality-report API and release-state regression tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.qa import (
    clip_review_revision,
    editorial_revision,
    quality_revision,
    release_revision,
)
from agents.pipeline import load_config
from lib.delivery_video import (
    longform_render_fingerprint,
    record_longform_render,
    record_short_render,
    short_render_fingerprint,
)
from lib.timeline import Timeline
from server.routes import quality


@pytest.fixture
def quality_client(tmp_path, monkeypatch):
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    monkeypatch.setattr(quality, "EPISODES_DIR", episodes_dir)
    app = FastAPI()
    app.include_router(quality.router)
    return TestClient(app), episodes_dir


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _audio_report(episode_dir: Path, findings: list[dict]) -> dict:
    stat = (episode_dir / "source_merged.mp4").stat()
    return {
        "fingerprint": "report-1",
        "source": {
            "fingerprint": {
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        },
        "findings": findings,
    }


def _seed_release(episodes_dir: Path, *, qa_overall: str = "pass") -> Path:
    episode_dir = episodes_dir / "ep_test"
    for name in ("work", "shorts", "metadata", "qa"):
        (episode_dir / name).mkdir(parents=True, exist_ok=True)
    (episode_dir / "source_merged.mp4").write_bytes(b"source")
    (episode_dir / "work" / "audio_mix.wav").write_bytes(b"master")
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"release video")
    (episode_dir / "longform.mp4").write_bytes(b"legacy review")
    (episode_dir / "shorts" / "clip_1.mp4").write_bytes(b"short")
    clips = [
        {
            "id": "clip_1",
            "status": "approved",
            "start_seconds": 0,
            "end_seconds": 30,
            "metadata": {
                "youtube": {"title": "One", "description": "One description"},
                "tiktok": {"caption": "One caption"},
                "instagram": {"caption": "One caption"},
                "x": {"text": "One post"},
            },
        }
    ]
    _write_json(episode_dir / "clips.json", {"clips": clips})
    _write_json(
        episode_dir / "metadata" / "metadata.json",
        {
            "longform": {"title": "Episode", "description": "Description"},
            "clips": clips,
        },
    )
    episode = {
        "episode_id": "ep_test",
        "crop_config": {"speakers": [{"label": "Host"}]},
        "longform_edits": [],
    }
    _write_json(episode_dir / "episode.json", episode)
    segments = [{"start": 0, "end": 60, "speaker": "BOTH"}]
    _write_json(episode_dir / "segments.json", {"segments": segments})
    config = load_config()
    audio = episode_dir / "work" / "audio_mix.wav"
    timeline = Timeline(60, [(0, 60)])
    record_longform_render(
        episode_dir,
        fingerprint=longform_render_fingerprint(
            episode_dir, episode, config, audio, segments
        ),
        render_mode="speaker_cut",
        timeline=timeline,
        media={"duration_seconds": 60},
    )
    clip_timeline = Timeline(60, [(0, 30)])
    short_record = record_short_render(
        episode_dir,
        "clip_1",
        fingerprint=short_render_fingerprint(
            episode_dir, episode, config, audio, segments, clips[0]
        ),
        timeline=clip_timeline,
        media={"duration_seconds": 30},
    )
    clips[0]["approved_render_fingerprint"] = short_record["fingerprint"]
    clips[0]["approved_revision"] = clip_review_revision(
        clips[0],
        short_record,
        json.loads((episode_dir / "metadata" / "metadata.json").read_text())["clips"][
            0
        ],
    )
    _write_json(episode_dir / "clips.json", {"clips": clips})
    episode["editorial_approval"] = {
        "revision": editorial_revision(episode_dir, episode),
        "approved_at": "2026-01-01T00:00:00+00:00",
    }
    _write_json(episode_dir / "episode.json", episode)
    revision = release_revision(episode_dir, episode)
    episode["publish_approval"] = {
        "revision": revision,
        "approved_at": "2026-01-01T00:01:00+00:00",
    }
    _write_json(episode_dir / "episode.json", episode)
    stat = video.stat()
    _write_json(
        episode_dir / "delivery.json",
        {
            "video_status": "ready",
            "video_output_stat": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
            "video": {"render_mode": "speaker_cut"},
        },
    )
    audio = {
        "analysis": {"status": "complete", "finding_count": 0},
        "release_gate": {"status": "pass", "safe": True},
        "findings": [],
    }
    _write_json(episode_dir / "qa" / "audio-quality.json", audio)
    _write_json(
        episode_dir / "qa" / "qa.json",
        {
            "overall": qa_overall,
            "quality_revision": quality_revision(episode_dir, episode),
            "generated_at": "2026-01-01T00:00:00+00:00",
            "checks": [],
            "audio_quality": audio,
        },
    )
    return episode_dir


def test_quality_api_reports_current_release_ready(quality_client):
    client, episodes_dir = quality_client
    _seed_release(episodes_dir)

    response = client.get("/api/episodes/ep_test/quality")

    assert response.status_code == 200
    body = response.json()
    assert body["quality"]["status"] == "passed"
    assert body["release_gate"]["status"] == "ready"
    assert body["release_gate"]["safe"] is True
    assert body["artifacts"]["legacy_longform"]["release_candidate"] is False


def test_quality_api_marks_changed_inputs_stale(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _write_json(
        episode_dir / "metadata" / "metadata.json",
        {"longform": {"title": "Changed"}, "clips": []},
    )

    body = client.get("/api/episodes/ep_test/quality").json()

    assert body["quality"]["status"] == "stale"
    codes = {item["code"] for item in body["release_gate"]["blockers"]}
    assert "quality_report_stale" in codes
    assert body["release_gate"]["safe"] is False


def test_quality_api_exposes_failed_report_and_findings(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir, qa_overall="fail")
    report = json.loads((episode_dir / "qa" / "audio-quality.json").read_text())
    report["findings"] = [{"id": "aq_blocked", "severity": "error"}]
    report["release_gate"] = {
        "status": "blocked",
        "safe": False,
        "blocking_finding_ids": ["aq_blocked"],
    }
    _write_json(episode_dir / "qa" / "audio-quality.json", report)
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    qa_report["audio_quality"] = report
    _write_json(episode_dir / "qa" / "qa.json", qa_report)

    body = client.get("/api/episodes/ep_test/quality").json()

    assert body["quality"]["status"] == "blocked"
    assert body["audio_quality"]["findings"][0]["id"] == "aq_blocked"
    assert body["release_gate"]["safe"] is False


def test_finding_preview_uses_report_owned_source_and_scoped_output(
    quality_client, monkeypatch
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    finding = {
        "id": "aq_one",
        "source_time": {"start_seconds": 10, "end_seconds": 12},
        "preview": {"padding_seconds": 2},
    }
    _write_json(
        episode_dir / "qa" / "audio-quality.json",
        _audio_report(episode_dir, [finding]),
    )

    def render(source, selected, output_dir):
        assert source == episode_dir / "source_merged.mp4"
        assert selected == finding
        path = output_dir / "aq_one-source.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wav")
        fallback = output_dir / "aq_one-grounded-fallback.wav"
        fallback.write_bytes(b"wav")
        return {"original": str(path), "grounded_fallback": str(fallback)}

    monkeypatch.setattr(quality, "render_finding_preview", render)
    response = client.get(
        "/api/episodes/ep_test/audio-qc/findings/aq_one/preview?variant=source"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")


def test_concurrent_preview_variants_share_one_cached_render(
    quality_client, monkeypatch
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    finding = {
        "id": "aq_one",
        "source_time": {"start_seconds": 10, "end_seconds": 12},
        "preview": {"padding_seconds": 2},
    }
    _write_json(
        episode_dir / "qa" / "audio-quality.json",
        _audio_report(episode_dir, [finding]),
    )
    calls = 0

    def render(_source, _selected, output_dir):
        nonlocal calls
        calls += 1
        source = output_dir / "aq_one-source.wav"
        fallback = output_dir / "aq_one-grounded-fallback.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        fallback.write_bytes(b"fallback")
        return {"original": str(source), "grounded_fallback": str(fallback)}

    monkeypatch.setattr(quality, "render_finding_preview", render)
    urls = [
        "/api/episodes/ep_test/audio-qc/findings/aq_one/preview?variant=source",
        "/api/episodes/ep_test/audio-qc/findings/aq_one/preview?variant=grounded-fallback",
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(client.get, urls))

    assert [response.status_code for response in responses] == [200, 200]
    assert calls == 1


def test_finding_preview_rejects_report_for_changed_source(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    finding = {
        "id": "aq_one",
        "source_time": {"start_seconds": 10, "end_seconds": 12},
        "preview": {"padding_seconds": 2},
    }
    _write_json(
        episode_dir / "qa" / "audio-quality.json",
        _audio_report(episode_dir, [finding]),
    )
    (episode_dir / "source_merged.mp4").write_bytes(b"changed source")

    response = client.get(
        "/api/episodes/ep_test/audio-qc/findings/aq_one/preview?variant=source"
    )

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]


@pytest.mark.parametrize(
    "source_time,padding",
    [
        ({"start_seconds": float("nan"), "end_seconds": 2}, 1),
        ({"start_seconds": 1, "end_seconds": 200}, 0),
        ({"start_seconds": 2, "end_seconds": 1}, 0),
    ],
)
def test_finding_preview_rejects_invalid_or_unbounded_report_ranges(
    quality_client, source_time, padding
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _write_json(
        episode_dir / "qa" / "audio-quality.json",
        _audio_report(
            episode_dir,
            [
                {
                    "id": "aq_bad",
                    "source_time": source_time,
                    "preview": {"padding_seconds": padding},
                }
            ],
        ),
    )

    response = client.get(
        "/api/episodes/ep_test/audio-qc/findings/aq_bad/preview?variant=source"
    )

    assert response.status_code == 422
