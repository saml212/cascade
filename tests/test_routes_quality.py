"""Quality-report API and release-state regression tests."""

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
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
from lib.audio_qa import (
    AUDIO_FINDING_REVIEWS_PATH,
    OUTPUT_CONTINUITY_SCHEMA,
    OUTPUT_CONTINUITY_VERSION,
    OUTPUT_SOURCE_MAPPING_SCHEMA,
    OutputContinuityConfig,
    WindowStats,
    analyze_output_continuity,
    output_continuity_report_fingerprint,
)
from lib.audio_qa import release_gate as audio_release_gate
from lib.delivery_video import (
    RENDER_PIPELINE_VERSION,
    aac_content_timing_proof,
    longform_render_fingerprint,
    read_render_manifest,
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


def _reviewable_finding(finding_id: str, start: float) -> dict:
    return {
        "id": finding_id,
        "fingerprint": f"fingerprint-{finding_id}",
        "kind": "sharp_level_collapse",
        "classification": "candidate_discontinuity",
        "severity": "warning",
        "confidence": 0.8,
        "source_time": {
            "start_seconds": start,
            "end_seconds": start + 1,
            "duration_seconds": 1,
        },
        "edited_time": {
            "status": "retained",
            "ranges": [
                {
                    "start_seconds": start,
                    "end_seconds": start + 1,
                    "source_start_seconds": start,
                    "source_end_seconds": start + 1,
                }
            ],
        },
        "resolution": {"status": "unresolved"},
    }


def _install_reviewable_report(episode_dir: Path, findings: list[dict]) -> dict:
    report = _audio_report(episode_dir, findings)
    report.update(
        analysis={"status": "complete", "finding_count": len(findings)},
        scope={
            "selected_mix_provenance": {
                "uses_checked_source_audio": True,
                "fingerprint": "mix-current",
                "selected_output": {"fingerprint": {"id": "master-current"}},
            },
            "outputs_checked": [
                {
                    "role": "selected_audio_master",
                    "status": "pass",
                    "source_report_fingerprint": "report-1",
                    "selected_mix_fingerprint": "mix-current",
                    "fingerprint": {"id": "master-current"},
                    "verification": {
                        "status": "pass",
                        "checks": [{"name": "exact_bytes", "pass": True}],
                    },
                }
            ],
        },
    )
    report["release_gate"] = audio_release_gate(report)
    _write_json(episode_dir / "qa" / "audio-quality.json", report)
    episode = json.loads((episode_dir / "episode.json").read_text())
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    output_report = _output_continuity_report(episode_dir, [])
    qa_report.update(
        overall="fail",
        quality_revision=quality_revision(episode_dir, episode, config=load_config()),
        checks=[
            {
                "name": "audio_continuity",
                "status": report["release_gate"]["status"],
                "pass": False,
                "detail": report["release_gate"]["reason"],
            },
            {
                "name": "selected_master_output_continuity",
                "status": "pass",
                "pass": True,
                "detail": "Current selected and rendered audio passed.",
            },
        ],
        audio_quality=report,
        selected_master_output_continuity=output_report,
    )
    _write_json(episode_dir / "qa" / "qa.json", qa_report)
    return report


def _review_request(finding: dict, decision: str = "accepted") -> dict:
    review = finding["review"]
    return {
        "decision": decision,
        "reviewer": "Editorial reviewer",
        "evidence_note": "Compared the complete retained passage in the current render.",
        "expected_report_fingerprint": review["report_fingerprint"],
        "expected_finding_fingerprint": review["finding_fingerprint"],
        "expected_output_revision": review["output_revision"],
    }


def _output_finding(finding_id: str, role: str, revision: str, start=10.0) -> dict:
    return {
        "id": finding_id,
        "fingerprint": f"sha256:{finding_id}",
        "kind": "digital_zero",
        "classification": "speech_overlapping_whole_output_silence",
        "severity": "error",
        "role": role,
        "revision": revision,
        "artifact_time": {
            "clock": "source" if role == "selected_audio_master" else "output",
            "start_seconds": start,
            "end_seconds": start + 0.5,
        },
        "source_ranges": [{"start_seconds": start, "end_seconds": start + 0.5}],
        "evidence": {
            "maximum_channel_median_dbfs": -240.0,
            "minimum_channel_zero_sample_fraction": 1.0,
            "speech_overlap_seconds": 0.4,
            "required_speech_overlap_seconds": 0.12,
            "transcript_word_count": 2,
            "transcript_excerpt": "timed words",
        },
        "resolution": {"status": "unresolved"},
    }


def _install_output_review_report(
    episode_dir: Path,
    findings: list[dict],
    *,
    artifact_statuses: dict[str, str] | None = None,
) -> dict:
    report = _output_continuity_report(
        episode_dir, findings, artifact_statuses=artifact_statuses
    )
    episode = json.loads((episode_dir / "episode.json").read_text())
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    qa_report.update(
        overall="fail" if report["status"] != "pass" else "pass",
        quality_revision=quality_revision(episode_dir, episode, config=load_config()),
        checks=[
            {
                "name": "audio_continuity",
                "status": "pass",
                "pass": True,
                "detail": "Source continuity passed.",
            },
            {
                "name": "selected_master_output_continuity",
                "status": report["status"],
                "pass": report["safe"],
                "detail": report["detail"],
            },
        ],
        selected_master_output_continuity=report,
    )
    _write_json(episode_dir / "qa" / "qa.json", qa_report)
    return report


def _output_continuity_report(
    episode_dir: Path,
    findings: list[dict],
    *,
    artifact_statuses: dict[str, str] | None = None,
) -> dict:
    artifact_statuses = artifact_statuses or {}
    findings_by_role: dict[tuple[str, str | None], list[dict]] = {}
    for finding in findings:
        findings_by_role.setdefault(
            (finding["role"], finding.get("clip_id")), []
        ).append(finding)
    artifacts = []
    for (role, clip_id), members in findings_by_role.items():
        status = artifact_statuses.get(role, "failed")
        path = (
            episode_dir / "work" / "audio_mix.wav"
            if role == "selected_audio_master"
            else episode_dir / "upload_video.mp4"
            if role == "upload_video"
            else episode_dir / "shorts" / f"{clip_id}.mp4"
        )
        stat = path.stat()
        artifact = {
            "role": role,
            "path": str(path.resolve()),
            "required": True,
            "revision": members[0]["revision"],
            "status": status,
            "currentness": "current" if status in {"pass", "failed"} else status,
            "detail": "One semantic prediction." if status == "failed" else status,
            "findings": members if status == "failed" else [],
        }
        if clip_id:
            artifact["clip_id"] = clip_id
        if status in {"pass", "failed"}:
            artifact.update(
                decoded_duration_seconds=60.0,
                expected_duration_seconds=60.0,
                scan_identity={
                    "resolved_path": str(path.resolve()),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                },
            )
        artifacts.append(artifact)
    roles = {artifact["role"] for artifact in artifacts}
    for role, path in (
        ("selected_audio_master", episode_dir / "work" / "audio_mix.wav"),
        ("upload_video", episode_dir / "upload_video.mp4"),
    ):
        if role in roles:
            continue
        status = artifact_statuses.get(role, "pass")
        stat = path.stat()
        artifact = {
            "role": role,
            "path": str(path.resolve()),
            "required": True,
            "revision": f"{role}-revision",
            "status": status,
            "currentness": "current" if status == "pass" else status,
            "detail": "No semantic prediction." if status == "pass" else status,
            "findings": [],
        }
        if status == "pass":
            artifact.update(
                decoded_duration_seconds=60.0,
                expected_duration_seconds=60.0,
                scan_identity={
                    "resolved_path": str(path.resolve()),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                },
            )
        artifacts.append(artifact)
    blocking = [artifact for artifact in artifacts if artifact["status"] != "pass"]
    precedence = ("failed", "error", "stale", "missing", "unavailable")
    status = next(
        (
            candidate
            for candidate in precedence
            if any(artifact["status"] == candidate for artifact in blocking)
        ),
        "error" if blocking else "pass",
    )
    report = {
        "schema": OUTPUT_CONTINUITY_SCHEMA,
        "detector_version": OUTPUT_CONTINUITY_VERSION,
        "status": status,
        "safe": status == "pass",
        "detail": "Semantic output predictions require review.",
        "transcript_fingerprint": "sha256:transcript",
        "timeline": {
            "keep_intervals": [[0, 60]],
            "output_duration_seconds": 60,
        },
        "settings": {"duration_tolerance_seconds": 0.25},
        "artifacts": artifacts,
        "findings": findings,
    }
    report["fingerprint"] = output_continuity_report_fingerprint(report)
    return report


def _output_review_request(event: dict, decision="false_positive") -> dict:
    review = event["review"]
    return {
        "decision": decision,
        "reviewer": "Editorial reviewer",
        "evidence_note": "Reviewed the exact current full-output passage.",
        "expected_report_fingerprint": review["report_fingerprint"],
        "expected_event_fingerprint": review["event_fingerprint"],
        "expected_output_revision": review["output_revision"],
    }


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


def test_detector_output_report_is_reviewable_after_persistence(
    quality_client, monkeypatch
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    timeline = Timeline(60, [(0, 60)])
    short_timeline = Timeline(60, [(0, 30)])
    master = episode_dir / "work" / "audio_mix.wav"
    video = episode_dir / "upload_video.mp4"
    short = episode_dir / "shorts" / "clip_1.mp4"

    def scan_identity(path: Path) -> dict:
        stat = path.stat()
        return {
            "resolved_path": str(path.resolve()),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }

    master_identity = scan_identity(master)
    video_identity = scan_identity(video)
    short_identity = scan_identity(short)
    timing = aac_content_timing_proof()

    def source_mapping(target_timeline: Timeline, identity: dict) -> dict:
        return {
            "schema": OUTPUT_SOURCE_MAPPING_SCHEMA,
            "pipeline_version": RENDER_PIPELINE_VERSION,
            "source_intervals": [list(item) for item in target_timeline.keep_intervals],
            "audio_codec": "aac",
            "audio_sample_rate_hz": timing["sample_rate_hz"],
            "audio_timing": timing,
            "timing_provenance": {
                "method": "render-manifest/v1",
                "media_identity": identity,
                "stream": {
                    "codec_name": "aac",
                    "sample_rate_hz": timing["sample_rate_hz"],
                    "start_pts": 0,
                    "time_base": "1/48000",
                    "initial_padding": 0,
                },
            },
        }

    rms = np.full((600, 2), -20.0)
    peak = np.full((600, 2), 0.1)
    zero = np.zeros((600, 2))
    rms[100:105] = -240.0
    peak[100:105] = 0.0
    zero[100:105] = 1.0
    stats = WindowStats(0.1, rms, peak, zero)
    monkeypatch.setattr("lib.audio_qa.decode_audio_windows", lambda *_args: stats)

    report = analyze_output_continuity(
        [
            {
                "role": "selected_audio_master",
                "path": str(master),
                "clock": "source",
                "required": True,
                "revision": "sha256:master",
                "status": "current",
                "detail": "current",
                "timeline": timeline,
                "scan_identity": master_identity,
            },
            {
                "role": "upload_video",
                "path": str(video),
                "clock": "output",
                "required": True,
                "revision": "sha256:video",
                "status": "current",
                "detail": "current",
                "timeline": timeline,
                "source_mapping": source_mapping(timeline, video_identity),
                "scan_identity": video_identity,
            },
            {
                "role": "short",
                "clip_id": "clip_1",
                "path": str(short),
                "clock": "output",
                "required": True,
                "revision": "sha256:short",
                "status": "current",
                "detail": "current",
                "timeline": short_timeline,
                "source_mapping": source_mapping(short_timeline, short_identity),
                "scan_identity": short_identity,
            },
        ],
        {
            "utterances": [
                {
                    "speaker": "host",
                    "words": [{"start": 10.05, "end": 10.45, "word": "speech"}],
                }
            ]
        },
        episode_timeline=timeline,
        transcript_fingerprint="sha256:transcript",
        config=OutputContinuityConfig(
            frame_seconds=0.1,
            min_issue_seconds=0.3,
            bridge_seconds=0,
        ),
    )
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    qa_report.update(
        overall="fail",
        selected_master_output_continuity=report,
    )
    _write_json(episode_dir / "qa" / "qa.json", qa_report)

    response = client.get("/api/episodes/ep_test/quality")

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["quality"]["status"] == "blocked"
    assert (
        snapshot["quality"]["report_revision"]
        == snapshot["quality"]["current_revision"]
    )
    continuity = snapshot["audio_quality"]["selected_master_output_continuity"]
    assert continuity["reviewable"] is True
    assert continuity["status"] == "failed"
    assert {finding.get("revision") for finding in continuity["findings"]} == {
        "sha256:master",
        "sha256:short",
        "sha256:video",
    }
    assert continuity["review_events"]
    assert all(event["review"]["allowed"] for event in continuity["review_events"])
    assert any(
        member.get("clip_id") == "clip_1" and member["revision"] == "sha256:short"
        for event in continuity["review_events"]
        for member in event["members"]
    )


def test_quality_snapshot_does_not_block_other_requests(quality_client, monkeypatch):
    client, episodes_dir = quality_client
    _write_json(episodes_dir / "ep_test" / "episode.json", {"episode_id": "ep_test"})
    started = threading.Event()
    release = threading.Event()
    observed = {}

    def slow_snapshot(_episode_dir):
        started.set()
        observed["health_ran_before_snapshot_returned"] = release.wait(timeout=1.0)
        return {"status": "ok"}

    @client.app.get("/health-for-quality-test")
    async def health():
        release.set()
        return {"status": "ok"}

    async def exercise():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as async_client:
            quality_request = asyncio.create_task(
                async_client.get("/api/episodes/ep_test/quality")
            )
            assert await asyncio.wait_for(
                asyncio.to_thread(started.wait, 1.0), timeout=2.0
            )
            health_response = await asyncio.wait_for(
                async_client.get("/health-for-quality-test"), timeout=2.0
            )
            quality_response = await asyncio.wait_for(quality_request, timeout=2.0)
            return health_response, quality_response

    monkeypatch.setattr(quality, "quality_snapshot", slow_snapshot)
    health_response, quality_response = asyncio.run(exercise())

    assert health_response.status_code == 200
    assert observed["health_ran_before_snapshot_returned"] is True
    assert quality_response.status_code == 200


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


def test_finding_review_is_explicit_and_exposes_current_output_inspection(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _install_reviewable_report(episode_dir, [_reviewable_finding("aq_one", 10)])

    body = client.get("/api/episodes/ep_test/quality").json()
    finding = body["audio_quality"]["findings"][0]

    assert finding["review"]["allowed"] is True
    assert finding["review"]["inspection_request"] == {
        "method": "GET",
        "endpoint": "/api/episodes/ep_test/inspection/preview",
        "query": {
            "target": "longform",
            "clock": "source",
            "seconds": 8.0,
            "duration_seconds": 5.0,
        },
    }
    assert finding["review"]["output_revision"].startswith("sha256:")
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()

    missing_action = client.post(
        "/api/episodes/ep_test/audio-qc/findings/aq_one/review"
    )

    assert missing_action.status_code == 422
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()


def test_finding_review_preserves_unrelated_findings_and_updates_quality_gate(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    report = _install_reviewable_report(
        episode_dir,
        [_reviewable_finding("aq_one", 10), _reviewable_finding("aq_two", 20)],
    )
    raw_report = (episode_dir / "qa" / "audio-quality.json").read_bytes()
    before = client.get("/api/episodes/ep_test/quality").json()
    by_id = {item["id"]: item for item in before["audio_quality"]["findings"]}

    first = client.post(
        "/api/episodes/ep_test/audio-qc/findings/aq_one/review",
        json=_review_request(by_id["aq_one"]),
    )

    assert first.status_code == 200
    assert first.json()["resolution"]["status"] == "accepted"
    assert first.json()["publish_approval_current"] is False
    assert (episode_dir / "qa" / "audio-quality.json").read_bytes() == raw_report
    current = client.get("/api/episodes/ep_test/quality").json()
    assert current["release_gate"]["revision"] != before["release_gate"]["revision"]
    current_by_id = {item["id"]: item for item in current["audio_quality"]["findings"]}
    assert current_by_id["aq_one"]["resolution"]["status"] == "accepted"
    assert current_by_id["aq_two"]["resolution"]["status"] == "unresolved"
    document = json.loads((episode_dir / AUDIO_FINDING_REVIEWS_PATH).read_text())
    assert set(document["reviews"]) == {"aq_one"}
    assert (
        document["reviews"]["aq_one"]["source_report_fingerprint"]
        == report["fingerprint"]
    )

    second = client.post(
        "/api/episodes/ep_test/audio-qc/findings/aq_two/review",
        json=_review_request(by_id["aq_two"], "false_positive"),
    )

    assert second.status_code == 200
    assert second.json()["audio_release_gate"]["status"] == "pass"
    assert second.json()["quality"]["overall"] == "pass"
    assert second.json()["quality"]["status"] == "passed"
    effective = client.get("/api/episodes/ep_test/audio-qc").json()
    effective_by_id = {item["id"]: item for item in effective["findings"]}
    assert effective_by_id["aq_one"]["resolution"]["status"] == "accepted"
    assert effective_by_id["aq_two"]["resolution"]["status"] == "false_positive"


def test_finding_review_rejects_changed_output_and_stale_report(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _install_reviewable_report(episode_dir, [_reviewable_finding("aq_one", 10)])
    finding = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "findings"
    ][0]
    request = _review_request(finding)

    manifest = read_render_manifest(episode_dir)
    (episode_dir / "upload_video.mp4").write_bytes(b"replacement output")
    record_longform_render(
        episode_dir,
        fingerprint=manifest["longform"]["fingerprint"],
        render_mode="speaker_cut",
        timeline=Timeline(60, [(0, 60)]),
        media={"duration_seconds": 60},
    )
    episode = json.loads((episode_dir / "episode.json").read_text())
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    qa_report["quality_revision"] = quality_revision(
        episode_dir, episode, config=load_config()
    )
    _write_json(episode_dir / "qa" / "qa.json", qa_report)
    changed_output = client.post(
        "/api/episodes/ep_test/audio-qc/findings/aq_one/review", json=request
    )

    assert changed_output.status_code == 409
    assert "Rendered output changed" in changed_output.json()["detail"]
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()

    episode_dir = _seed_release(episodes_dir)
    report = _install_reviewable_report(
        episode_dir, [_reviewable_finding("aq_one", 10)]
    )
    finding = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "findings"
    ][0]
    request = _review_request(finding)
    report["fingerprint"] = "report-replaced"
    _write_json(episode_dir / "qa" / "audio-quality.json", report)

    stale_report = client.post(
        "/api/episodes/ep_test/audio-qc/findings/aq_one/review", json=request
    )

    assert stale_report.status_code == 409
    assert "report changed" in stale_report.json()["detail"]
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()


def test_finding_review_rejects_split_ranges_and_missing_output_proof(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    split = _reviewable_finding("aq_split", 10)
    split["edited_time"] = {
        "status": "split",
        "ranges": [
            {
                "start_seconds": 10,
                "end_seconds": 10.4,
                "source_start_seconds": 10,
                "source_end_seconds": 10.4,
            },
            {
                "start_seconds": 10.4,
                "end_seconds": 11,
                "source_start_seconds": 10.6,
                "source_end_seconds": 11.2,
            },
        ],
    }
    _install_reviewable_report(episode_dir, [split])
    finding = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "findings"
    ][0]
    assert finding["review"]["allowed"] is False
    assert "multiple retained ranges" in finding["review"]["reason"]
    rejected = client.post(
        "/api/episodes/ep_test/audio-qc/findings/aq_split/review",
        json=_review_request(finding),
    )
    assert rejected.status_code == 409

    episode_dir = _seed_release(episodes_dir)
    report = _install_reviewable_report(
        episode_dir, [_reviewable_finding("aq_unverified", 10)]
    )
    report["scope"]["outputs_checked"] = []
    report["release_gate"] = audio_release_gate(report)
    _write_json(episode_dir / "qa" / "audio-quality.json", report)
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    qa_report["audio_quality"] = report
    _write_json(episode_dir / "qa" / "qa.json", qa_report)

    unverified = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "findings"
    ][0]
    assert unverified["review"]["allowed"] is False
    assert "verified current selected audio master" in unverified["review"]["reason"]


def test_output_finding_review_is_explicit_and_preserves_raw_evidence(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _install_reviewable_report(episode_dir, [])
    report = _install_output_review_report(
        episode_dir,
        [
            _output_finding("oc_master", "selected_audio_master", "master-rev"),
            _output_finding("oc_video", "upload_video", "video-rev"),
        ],
    )
    raw_qa = (episode_dir / "qa" / "qa.json").read_bytes()

    before = client.get("/api/episodes/ep_test/quality").json()
    events = before["audio_quality"]["selected_master_output_continuity"][
        "review_events"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["review"]["allowed"] is True
    assert event["review"]["inspection_request"] == {
        "method": "GET",
        "endpoint": "/api/episodes/ep_test/inspection/preview",
        "query": {
            "target": "longform",
            "clock": "source",
            "seconds": 8.0,
            "duration_seconds": 4.5,
        },
    }
    assert {member["id"] for member in event["members"]} == {
        "oc_master",
        "oc_video",
    }
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()

    missing_action = client.post(
        f"/api/episodes/ep_test/audio-qc/output-findings/{event['id']}/review"
    )
    assert missing_action.status_code == 422
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()

    recorded = client.post(
        f"/api/episodes/ep_test/audio-qc/output-findings/{event['id']}/review",
        json=_output_review_request(event),
    )

    assert recorded.status_code == 200
    assert recorded.json()["resolution"]["status"] == "false_positive"
    assert recorded.json()["output_continuity"]["status"] == "pass"
    assert recorded.json()["quality"]["overall"] == "pass"
    assert recorded.json()["publish_approval_current"] is False
    assert (episode_dir / "qa" / "qa.json").read_bytes() == raw_qa
    document = json.loads((episode_dir / AUDIO_FINDING_REVIEWS_PATH).read_text())
    assert document["reviews"] == {}
    assert set(document["output_reviews"]) == {event["id"]}
    assert (
        document["output_reviews"][event["id"]]["output_report_fingerprint"]
        == report["fingerprint"]
    )
    after = client.get("/api/episodes/ep_test/quality").json()
    findings = after["audio_quality"]["selected_master_output_continuity"]["findings"]
    assert len(findings) == 2
    assert {finding["resolution"]["status"] for finding in findings} == {
        "false_positive"
    }


def test_output_review_preserves_unrelated_semantic_event_and_source_gate(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    source = _install_reviewable_report(
        episode_dir, [_reviewable_finding("source_open", 30)]
    )
    output = _install_output_review_report(
        episode_dir,
        [
            _output_finding("oc_one", "upload_video", "video-rev", 10),
            _output_finding("oc_two", "upload_video", "video-rev", 20),
        ],
    )
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    qa_report["audio_quality"] = source
    qa_report["selected_master_output_continuity"] = output
    _write_json(episode_dir / "qa" / "qa.json", qa_report)

    before = client.get("/api/episodes/ep_test/quality").json()
    events = before["audio_quality"]["selected_master_output_continuity"][
        "review_events"
    ]
    assert len(events) == 2
    first = client.post(
        f"/api/episodes/ep_test/audio-qc/output-findings/{events[0]['id']}/review",
        json=_output_review_request(events[0], "accepted"),
    )

    assert first.status_code == 200
    after = client.get("/api/episodes/ep_test/quality").json()
    output_after = after["audio_quality"]["selected_master_output_continuity"]
    assert output_after["status"] == "failed"
    resolutions = {
        finding["id"]: finding["resolution"]["status"]
        for finding in output_after["findings"]
    }
    reviewed_ids = {member["id"] for member in events[0]["members"]}
    assert {resolutions[finding_id] for finding_id in reviewed_ids} == {"accepted"}
    assert any(
        status == "unresolved"
        for finding_id, status in resolutions.items()
        if finding_id not in reviewed_ids
    )
    assert after["audio_quality"]["release_gate"]["status"] != "pass"
    assert after["quality"]["overall"] == "fail"


@pytest.mark.parametrize("mechanical_status", ["error", "stale", "missing"])
def test_output_review_rejects_mechanical_failure(quality_client, mechanical_status):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _install_output_review_report(
        episode_dir,
        [_output_finding("oc_video", "upload_video", "video-rev")],
        artifact_statuses={"selected_audio_master": mechanical_status},
    )

    body = client.get("/api/episodes/ep_test/quality").json()
    event = body["audio_quality"]["selected_master_output_continuity"]["review_events"][
        0
    ]
    assert event["review"]["allowed"] is False
    rejected = client.post(
        f"/api/episodes/ep_test/audio-qc/output-findings/{event['id']}/review",
        json=_output_review_request(event),
    )
    assert rejected.status_code == 409
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()


def test_output_review_rejects_same_stat_replacement(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _install_output_review_report(
        episode_dir,
        [_output_finding("oc_video", "upload_video", "video-rev")],
    )
    event = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "selected_master_output_continuity"
    ]["review_events"][0]
    video = episode_dir / "upload_video.mp4"
    original = video.stat()
    replacement = episode_dir / "replacement.mp4"
    replacement.write_bytes(b"changed video")
    assert replacement.stat().st_size == original.st_size
    replacement.replace(video)
    os.utime(video, ns=(original.st_atime_ns, original.st_mtime_ns))

    rejected = client.post(
        f"/api/episodes/ep_test/audio-qc/output-findings/{event['id']}/review",
        json=_output_review_request(event),
    )

    assert rejected.status_code == 409
    assert "incomplete mechanical checks" in rejected.json()["detail"]
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()


def test_output_review_rejects_old_page_revision_after_current_remaster(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _install_output_review_report(
        episode_dir,
        [_output_finding("oc_video", "upload_video", "video-rev")],
    )
    old_event = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "selected_master_output_continuity"
    ]["review_events"][0]
    video = episode_dir / "upload_video.mp4"
    original = video.stat()
    replacement = episode_dir / "replacement.mp4"
    replacement.write_bytes(b"changed video")
    replacement.replace(video)
    os.utime(video, ns=(original.st_atime_ns, original.st_mtime_ns))
    stat = video.stat()
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    report = qa_report["selected_master_output_continuity"]
    video_artifact = next(
        artifact
        for artifact in report["artifacts"]
        if artifact["role"] == "upload_video"
    )
    video_artifact["scan_identity"] = {
        "resolved_path": str(video.resolve()),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }
    report["fingerprint"] = output_continuity_report_fingerprint(report)
    _write_json(episode_dir / "qa" / "qa.json", qa_report)
    current_event = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "selected_master_output_continuity"
    ]["review_events"][0]
    request = _output_review_request(current_event)
    request["expected_output_revision"] = old_event["review"]["output_revision"]

    rejected = client.post(
        f"/api/episodes/ep_test/audio-qc/output-findings/{current_event['id']}/review",
        json=request,
    )

    assert rejected.status_code == 409
    assert "Rendered output changed" in rejected.json()["detail"]


def test_output_review_rejects_replaced_report(quality_client):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    _install_output_review_report(
        episode_dir,
        [_output_finding("oc_video", "upload_video", "video-rev")],
    )
    before = client.get("/api/episodes/ep_test/quality").json()
    event = before["audio_quality"]["selected_master_output_continuity"][
        "review_events"
    ][0]
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    replacement = qa_report["selected_master_output_continuity"]
    replacement["transcript_fingerprint"] = "sha256:replacement"
    replacement["fingerprint"] = output_continuity_report_fingerprint(replacement)
    _write_json(episode_dir / "qa" / "qa.json", qa_report)

    response = client.post(
        f"/api/episodes/ep_test/audio-qc/output-findings/{event['id']}/review",
        json=_output_review_request(event),
    )

    assert response.status_code == 409
    assert "report changed" in response.json()["detail"]
    assert not (episode_dir / AUDIO_FINDING_REVIEWS_PATH).exists()


def test_rejected_review_transaction_restores_release_revision(quality_client):
    _client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    sidecar = episode_dir / AUDIO_FINDING_REVIEWS_PATH
    _write_json(
        sidecar,
        {
            "schema": "cascade.audio-finding-reviews/v1",
            "episode_id": "ep_test",
            "reviews": {"existing": {"decision": "accepted"}},
            "output_reviews": {},
        },
    )
    before_bytes = sidecar.read_bytes()
    before_revision = release_revision(episode_dir)

    with pytest.raises(RuntimeError, match="concurrent replacement"):
        quality._write_finding_review(
            episode_dir,
            "output_reviews",
            "new",
            {"decision": "false_positive"},
            lambda _reviewed_at: (_ for _ in ()).throw(
                RuntimeError("concurrent replacement")
            ),
        )

    assert sidecar.read_bytes() == before_bytes
    assert release_revision(episode_dir) == before_revision


def test_output_continuity_failures_expose_bounded_canonical_inspection(
    quality_client,
):
    client, episodes_dir = quality_client
    episode_dir = _seed_release(episodes_dir)
    qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
    qa_report["overall"] = "fail"
    qa_report["selected_master_output_continuity"] = {
        "status": "failed",
        "safe": False,
        "detail": "Current upload contains speech-overlapping silence.",
        "artifacts": [
            {
                "role": "upload_video",
                "status": "failed",
                "detail": "One span detected.",
            }
        ],
        "findings": [
            {
                "id": "oc_one",
                "fingerprint": "oc-fingerprint",
                "role": "upload_video",
                "artifact_time": {
                    "clock": "output",
                    "start_seconds": 10,
                    "end_seconds": 11,
                },
            }
        ],
    }
    _write_json(episode_dir / "qa" / "qa.json", qa_report)

    continuity = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "selected_master_output_continuity"
    ]

    assert continuity["findings"][0]["inspection_request"] == {
        "method": "GET",
        "endpoint": "/api/episodes/ep_test/inspection/preview",
        "query": {
            "target": "longform",
            "clock": "output",
            "seconds": 8.0,
            "duration_seconds": 5.0,
        },
    }
    assert continuity["artifacts"][0]["status"] == "failed"

    (episode_dir / "upload_video.mp4").write_bytes(b"replacement")
    stale = client.get("/api/episodes/ep_test/quality").json()["audio_quality"][
        "selected_master_output_continuity"
    ]
    assert stale["current"] is False
    assert "inspection_request" not in stale["findings"][0]


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
