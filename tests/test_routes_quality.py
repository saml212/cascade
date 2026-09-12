"""Quality-report API and release-state regression tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.pipeline import load_config
from agents.qa import (
    clip_review_revision,
    editorial_revision,
    quality_revision,
    release_revision,
)
from lib.audio_mix import (
    AUDIO_SELECTION_PATH,
    AUDIO_SELECTION_SCHEMA,
    SELECTED_REPAIR_AUDIO_PATH,
    audio_processing_settings,
    audio_selection_settings,
    document_fingerprint,
)
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
            "quality_revision": quality_revision(episode_dir, episode, config=config),
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


def test_preview_cache_changes_with_renderer_algorithm_version(
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
    output_dirs = []

    def render(_source, _selected, output_dir):
        output_dirs.append(output_dir)
        source = output_dir / "aq_one-source.wav"
        fallback = output_dir / "aq_one-grounded-fallback.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        fallback.write_bytes(b"fallback")
        return {"original": str(source), "grounded_fallback": str(fallback)}

    monkeypatch.setattr(quality, "render_finding_preview", render)
    url = "/api/episodes/ep_test/audio-qc/findings/aq_one/preview?variant=source"
    monkeypatch.setattr(quality, "PREVIEW_ALGORITHM_VERSION", "old")
    assert client.get(url).status_code == 200
    monkeypatch.setattr(quality, "PREVIEW_ALGORITHM_VERSION", "new")
    assert client.get(url).status_code == 200

    assert len(output_dirs) == 2
    assert output_dirs[0] != output_dirs[1]


def test_repair_plan_and_preview_are_agent_accessible_without_path_input(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    plan_dir = episode_dir / "qa" / "audio-repair"
    preview = plan_dir / "repair_one-source.wav"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"review audio")
    stat = preview.stat()
    _write_json(
        plan_dir / "audio-repair-plan.json",
        {
            "status": "preview_ready",
            "repairs": [
                {
                    "id": "repair_one",
                    "preview": {"original": str(preview)},
                    "verification": {
                        "original_fingerprint": {
                            "size_bytes": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                    },
                }
            ],
            "held_out_controls": [],
        },
    )

    plan = client.get("/api/episodes/ep_test/audio-qc/repair-plan")
    audio = client.get(
        "/api/episodes/ep_test/audio-qc/repair-plan/repair_one/preview?variant=source"
    )

    assert plan.status_code == 200
    assert plan.json()["status"] == "preview_ready"
    assert audio.status_code == 200
    assert audio.content == b"review audio"


def test_repair_plan_replays_the_current_selected_plan(quality_client, monkeypatch):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    report = {
        "fingerprint": "report-current",
        "scope": {
            "selected_mix_provenance": {
                "repair_selection": {"repair_plan_fingerprint": "plan-selected"}
            }
        },
    }
    selected_plan = {"fingerprint": "plan-selected", "repairs": []}
    _write_json(episode_dir / "qa" / "audio-quality.json", report)
    _write_json(
        episode_dir / "qa" / "audio-repair" / "audio-repair-plan.json",
        selected_plan,
    )
    seen = {}

    monkeypatch.setattr(
        quality, "select_grounded_repair_findings", lambda _report: ["aq_one"]
    )

    def fake_build(_report, finding_ids, _output_dir, **kwargs):
        seen.update(finding_ids=finding_ids, predecessor=kwargs["predecessor_plan"])
        return {"status": "preview_ready"}

    monkeypatch.setattr(quality, "build_audio_repair_plan", fake_build)

    response = client.post("/api/episodes/ep_test/audio-qc/repair-plan")

    assert response.status_code == 200
    assert response.json()["status"] == "preview_ready"
    assert seen == {"finding_ids": ["aq_one"], "predecessor": selected_plan}


def test_repair_candidate_audio_is_bound_to_controlled_cache(
    quality_client, monkeypatch, tmp_path
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    cache_root = tmp_path / "repair-cache"
    candidate = cache_root / "ep_test" / "audio-repair-candidate.wav"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"full candidate")
    stat = candidate.stat()
    monkeypatch.setattr(quality, "AUDIO_REPAIR_CACHE_ROOT", cache_root)
    _write_json(
        episode_dir / "qa" / "audio-repair" / "audio-repair-candidate.json",
        {
            "candidate": {
                "path": str(candidate),
                "fingerprint": {
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                },
            }
        },
    )

    response = client.get("/api/episodes/ep_test/audio-qc/repair-candidate/audio")

    assert response.status_code == 200
    assert response.content == b"full candidate"


def test_repair_selection_is_inspectable_and_reversibly_cleared(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    source = episode_dir / "source_merged.mp4"
    selected = episode_dir / SELECTED_REPAIR_AUDIO_PATH
    selected.write_bytes(b"selected repair")
    episode = json.loads((episode_dir / "episode.json").read_text())
    source_stat, selected_stat = source.stat(), selected.stat()
    record = {
        "schema": AUDIO_SELECTION_SCHEMA,
        "status": "review_required",
        "release_safe": False,
        "source": {
            "path": str(source.resolve()),
            "fingerprint": {
                "id": "sha256:source",
                "size_bytes": source_stat.st_size,
                "mtime_ns": source_stat.st_mtime_ns,
            },
        },
        "audio_selection_settings": audio_selection_settings(episode),
        "audio_processing_settings": audio_processing_settings(load_config()),
        "selected_output": {
            "path": str(selected.resolve()),
            "fingerprint": {
                "id": "sha256:selected",
                "size_bytes": selected_stat.st_size,
                "mtime_ns": selected_stat.st_mtime_ns,
            },
        },
    }
    record["fingerprint"] = document_fingerprint(record)
    _write_json(episode_dir / AUDIO_SELECTION_PATH, record)

    current = client.get("/api/episodes/ep_test/audio-qc/repair-selection")
    cleared = client.delete("/api/episodes/ep_test/audio-qc/repair-selection")
    absent = client.get("/api/episodes/ep_test/audio-qc/repair-selection")

    assert current.status_code == 200
    assert current.json()["status"] == "review_required"
    assert cleared.json() == {
        "status": "not_selected",
        "removed": True,
        "release_safe": False,
    }
    assert absent.json() == {"status": "not_selected", "release_safe": False}
    assert not selected.exists()
    assert (episode_dir / "work" / "audio_mix.wav").read_bytes() == b"master"


def test_repair_selection_action_uses_only_current_canonical_documents(
    quality_client, monkeypatch, tmp_path
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    cache_root = tmp_path / "repair-cache"
    monkeypatch.setattr(quality, "AUDIO_REPAIR_CACHE_ROOT", cache_root)
    report = {"fingerprint": "report-current"}
    plan = {"fingerprint": "plan-current"}
    manifest = {"fingerprint": "candidate-current"}
    _write_json(episode_dir / "qa" / "audio-quality.json", report)
    _write_json(episode_dir / "qa" / "audio-repair" / "audio-repair-plan.json", plan)
    _write_json(
        episode_dir / "qa" / "audio-repair" / "audio-repair-candidate.json",
        manifest,
    )
    seen = {}

    def fake_select(
        selected_episode,
        selected_report,
        selected_plan,
        selected_manifest,
        _config,
        **kw,
    ):
        seen.update(
            episode=selected_episode,
            report=selected_report,
            plan=selected_plan,
            manifest=selected_manifest,
            cache=kw["allowed_cache_root"],
        )
        return {"status": "review_required", "release_safe": False}

    monkeypatch.setattr(quality, "select_audio_repair_candidate", fake_select)

    response = client.post("/api/episodes/ep_test/audio-qc/repair-candidate/select")

    assert response.status_code == 200
    assert response.json()["release_safe"] is False
    assert seen == {
        "episode": episode_dir,
        "report": report,
        "plan": plan,
        "manifest": manifest,
        "cache": cache_root / "ep_test",
    }


def test_repair_candidate_audio_rejects_manifest_path_outside_cache(
    quality_client, monkeypatch, tmp_path
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    outside = episode_dir / "source_merged.mp4"
    stat = outside.stat()
    monkeypatch.setattr(quality, "AUDIO_REPAIR_CACHE_ROOT", tmp_path / "cache")
    _write_json(
        episode_dir / "qa" / "audio-repair" / "audio-repair-candidate.json",
        {
            "candidate": {
                "path": str(outside),
                "fingerprint": {
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                },
            }
        },
    )

    response = client.get("/api/episodes/ep_test/audio-qc/repair-candidate/audio")

    assert response.status_code == 409


def test_repair_preview_rejects_paths_outside_plan_directory(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    outside = episode_dir / "source_merged.mp4"
    stat = outside.stat()
    _write_json(
        episode_dir / "qa" / "audio-repair" / "audio-repair-plan.json",
        {
            "repairs": [
                {
                    "id": "repair_one",
                    "preview": {"original": str(outside)},
                    "verification": {
                        "original_fingerprint": {
                            "size_bytes": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                    },
                }
            ],
            "held_out_controls": [],
        },
    )

    response = client.get(
        "/api/episodes/ep_test/audio-qc/repair-plan/repair_one/preview?variant=source"
    )

    assert response.status_code == 409


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
