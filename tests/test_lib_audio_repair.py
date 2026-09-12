"""Tests for evidence-bound source-channel repair candidates."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.audio_mix import (
    CAMERA_AUDIO_TIMELINE_FILTER,
    SELECTED_REPAIR_AUDIO_PATH,
    document_fingerprint,
    generate_audio_mix,
)
from lib.audio_qa import file_fingerprint, media_fingerprint
from lib.audio_repair import (
    _combined_finding,
    _prepare_repair_finding,
    _validated_predecessor_entries,
    build_audio_repair_plan,
    render_audio_repair_candidate,
    select_audio_repair_candidate,
    select_grounded_repair_findings,
)

FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
HAS_FFMPEG_FULL = FFMPEG_FULL.is_file() or shutil.which("ffmpeg-full") is not None


def _finding(finding_id: str, *, confidence: float = 0.99) -> dict:
    return {
        "id": finding_id,
        "classification": "probable_dropout",
        "kind": "digital_zero",
        "severity": "error",
        "confidence": confidence,
        "channel": 0,
        "source_time": {
            "start_seconds": 2,
            "end_seconds": 3,
            "duration_seconds": 1,
        },
        "edited_time": {"status": "retained", "ranges": []},
        "evidence": {
            "zero_sample_fraction": 1,
            "expected_speech_seconds": 0.9,
            "surviving_speech_seconds": 0,
            "context_continuity": True,
            "estimated_recovery_gain_db": 6,
        },
        "recovery": {"grounded": True, "synthetic_audio": False},
        "preview": {"padding_seconds": 0.5},
    }


def test_grounded_selection_leaves_weak_probable_findings_unresolved():
    strong = _finding("aq_strong")
    weak = _finding("aq_weak", confidence=0.89)

    assert select_grounded_repair_findings({"findings": [strong, weak]}) == [
        "aq_strong"
    ]


def test_grounded_selection_accepts_only_nonoverlapping_activity_evidence():
    separated = _finding("aq_separated", confidence=0.82)
    separated["evidence"].update(
        {
            "expected_speech_seconds": 0.3,
            "surviving_speech_seconds": 0.6,
            "expected_speech_ranges": [
                {
                    "start_seconds": 2.0,
                    "end_seconds": 2.3,
                    "duration_seconds": 0.3,
                }
            ],
            "surviving_speech_ranges": [
                {
                    "start_seconds": 2.4,
                    "end_seconds": 3.0,
                    "duration_seconds": 0.6,
                }
            ],
            "temporal_overlap_seconds": 0,
        }
    )
    overlapping = json.loads(json.dumps(separated))
    overlapping["id"] = "aq_overlapping"
    overlapping["evidence"]["temporal_overlap_seconds"] = 0.1

    assert select_grounded_repair_findings({"findings": [separated, overlapping]}) == [
        "aq_separated"
    ]


def test_selected_predecessor_preserves_its_verified_operation():
    entry = {
        "id": "repair_old",
        "finding_ids": ["aq_old"],
        "repair_scope": "transcript_activity",
        "repair_intervals": [
            {"start_seconds": 2.02, "end_seconds": 2.77, "duration_seconds": 0.75}
        ],
        "verification": {"status": "pass"},
    }
    plan = {"schema": "cascade.audio-repair-plan/v1", "repairs": [entry]}
    plan["fingerprint"] = document_fingerprint(plan)
    provenance = {
        "repair_selection": {
            "repair_plan_fingerprint": plan["fingerprint"],
            "repaired_findings": [{"id": "aq_old"}],
        }
    }

    predecessors = _validated_predecessor_entries(plan, provenance)
    prepared = _prepare_repair_finding(
        _finding("aq_old"), predecessor_entry=predecessors["aq_old"]
    )

    assert prepared["_repair_intervals"] == entry["repair_intervals"]
    assert prepared["_repair_scope"] == "transcript_activity"
    assert prepared["_fade_mode"] == "cross_boundary"
    assert prepared["_predecessor_entry_id"] == "repair_old"


def test_grouped_predecessor_intervals_are_not_repeated_per_finding():
    intervals = [
        {"start_seconds": 2.02, "end_seconds": 2.77, "duration_seconds": 0.75},
        {"start_seconds": 3.02, "end_seconds": 3.77, "duration_seconds": 0.75},
    ]
    entry = {
        "id": "repair_group",
        "finding_ids": ["aq_one", "aq_two"],
        "repair_scope": "full_finding",
        "repair_intervals": intervals,
        "verification": {"status": "pass"},
    }
    plan = {"schema": "cascade.audio-repair-plan/v1", "repairs": [entry]}
    plan["fingerprint"] = document_fingerprint(plan)
    provenance = {
        "repair_selection": {
            "repair_plan_fingerprint": plan["fingerprint"],
            "repaired_findings": [{"id": "aq_one"}, {"id": "aq_two"}],
        }
    }
    predecessors = _validated_predecessor_entries(plan, provenance)
    one = _prepare_repair_finding(
        _finding("aq_one"), predecessor_entry=predecessors["aq_one"]
    )
    two_finding = _finding("aq_two")
    two_finding["source_time"] = {
        "start_seconds": 3,
        "end_seconds": 4,
        "duration_seconds": 1,
    }
    two = _prepare_repair_finding(two_finding, predecessor_entry=predecessors["aq_two"])

    combined = _combined_finding([one, two])

    assert combined["repair_intervals"] == intervals


@pytest.mark.skipif(not HAS_FFMPEG_FULL, reason="ffmpeg-full is not installed")
def test_activity_repair_envelope_does_not_change_adjacent_speaker(tmp_path):
    decoder = FFMPEG_FULL if FFMPEG_FULL.is_file() else Path("ffmpeg-full")
    episode_dir = tmp_path / "episode"
    episode_dir.mkdir()
    (episode_dir / "episode.json").write_text("{}")
    source = episode_dir / "source_merged.mp4"
    subprocess.run(
        [
            str(decoder),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                "aevalsrc='if(between(t,2,3),0,0.08*sin(2*PI*220*t))|"
                "0.02*sin(2*PI*330*t)':s=48000:d=6"
            ),
            "-c:a",
            "pcm_f32le",
            str(source),
        ],
        check=True,
    )
    finding = _finding("aq_activity", confidence=0.82)
    finding["evidence"].update(
        {
            "expected_speech_seconds": 0.8,
            "surviving_speech_seconds": 0.2,
            "expected_speech_ranges": [
                {
                    "start_seconds": 2.0,
                    "end_seconds": 2.8,
                    "duration_seconds": 0.8,
                }
            ],
            "surviving_speech_ranges": [
                {
                    "start_seconds": 2.8,
                    "end_seconds": 3.0,
                    "duration_seconds": 0.2,
                }
            ],
            "temporal_overlap_seconds": 0,
        }
    )
    report = {
        "fingerprint": "sha256:report",
        "source": {"path": str(source), "fingerprint": media_fingerprint(source)},
        "scope": {"selected_mix_provenance": {"kind": "embedded_camera"}},
        "findings": [finding],
        "analysis": {"suppressed_candidates": []},
    }

    plan = build_audio_repair_plan(
        report,
        [finding["id"]],
        episode_dir / "qa" / "audio-repair",
        ffmpeg_bin=decoder,
        held_out_count=0,
    )

    repair = plan["repairs"][0]
    assert repair["repair_scope"] == "transcript_activity"
    assert repair["fade_mode"] == "contained"
    assert repair["preview"]["envelope"] == {
        "mode": "contained",
        "maximum_fade_seconds": 0.08,
        "frame_guard_seconds": 0.03,
    }
    assert (
        repair["verification"]["metrics"]["outside_repair_max_absolute_delta"] <= 2e-6
    )


@pytest.mark.skipif(not HAS_FFMPEG_FULL, reason="ffmpeg-full is not installed")
def test_full_candidate_preserves_master_and_records_actual_verification(tmp_path):
    decoder = FFMPEG_FULL if FFMPEG_FULL.is_file() else Path("ffmpeg-full")
    episode_dir = tmp_path / "episode"
    work_dir = episode_dir / "work"
    work_dir.mkdir(parents=True)
    (episode_dir / "episode.json").write_text("{}")
    source = episode_dir / "source_merged.mp4"
    subprocess.run(
        [
            str(decoder),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                "aevalsrc='if(between(t,2,3),0,0.08*sin(2*PI*220*t))|"
                "if(between(t,14,15),0,0.02*sin(2*PI*330*t))':s=48000:d=20"
            ),
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    current_master = work_dir / "audio_mix.wav"
    subprocess.run(
        [
            str(decoder),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            (
                f"{CAMERA_AUDIO_TIMELINE_FILTER},"
                "aformat=channel_layouts=stereo,"
                "pan=mono|c0=0.5*c0+0.5*c1,pan=stereo|c0=c0|c1=c0"
            ),
            "-c:a",
            "pcm_f32le",
            str(current_master),
        ],
        check=True,
    )
    master_bytes = current_master.read_bytes()
    finding = _finding("aq_repair")
    control = {
        **_finding("aq_control"),
        "channel": 1,
        "source_time": {
            "start_seconds": 14,
            "end_seconds": 15,
            "duration_seconds": 1,
        },
        "suppression_reason": "transcript_supports_surviving_channel",
    }
    weak = _finding("aq_unresolved", confidence=0.89)
    weak["source_time"] = {
        "start_seconds": 10,
        "end_seconds": 11,
        "duration_seconds": 1,
    }
    report = {
        "fingerprint": "sha256:report",
        "source": {"path": str(source), "fingerprint": media_fingerprint(source)},
        "scope": {"selected_mix_provenance": {"kind": "embedded_camera"}},
        "findings": [finding, weak],
        "analysis": {"suppressed_candidates": [control]},
    }
    plan_dir = episode_dir / "qa" / "audio-repair"
    plan = build_audio_repair_plan(
        report,
        [finding["id"]],
        plan_dir,
        ffmpeg_bin=decoder,
        held_out_count=1,
    )
    asr_path = plan_dir / "asr.json"
    repair = plan["repairs"][0]
    asr_path.write_text(
        json.dumps(
            {
                "provider": "test",
                "model": "bounded",
                "repair_plan_fingerprint": plan["fingerprint"],
                "results": [
                    {
                        "entry_id": repair["id"],
                        "variant": "source",
                        "transcript": "bounded sample",
                        "preview_fingerprint": repair["verification"][
                            "original_fingerprint"
                        ]["id"],
                    },
                    {
                        "entry_id": repair["id"],
                        "variant": "grounded_fallback",
                        "transcript": "bounded sample",
                        "preview_fingerprint": repair["verification"][
                            "fallback_fingerprint"
                        ]["id"],
                    },
                ],
            }
        )
    )
    candidate = tmp_path / "internal-cache" / "candidate.wav"

    manifest = render_audio_repair_candidate(
        report,
        plan,
        candidate,
        {"processing": {"audio_enhance": False}},
        asr_evidence_path=asr_path,
        ffmpeg_bin=decoder,
    )

    assert manifest["status"] == "review_required"
    assert manifest["release_safe"] is False
    assert manifest["repaired_finding_ids"] == ["aq_repair"]
    assert manifest["unresolved_finding_ids"] == ["aq_unresolved"]
    assert plan["status"] == "preview_ready_with_unresolved"
    assert plan["coverage"]["unresolved_probable_count"] == 1
    assert manifest["verification"]["status"] == "pass"
    assert manifest["verification"]["loudness"]["status"] == "not_required"
    assert manifest["perceptual_review"]["status"] == "not_performed"
    assert (
        manifest["selected_master_before"]["fingerprint"]["method"] == "sha256-full/v1"
    )
    assert manifest["candidate"]["fingerprint"] == file_fingerprint(candidate)
    assert manifest["predecessor_selection"] is None
    assert current_master.read_bytes() == master_bytes
    assert (
        episode_dir / "qa" / "audio-repair" / "audio-repair-candidate.json"
    ).is_file()
    with pytest.raises(ValueError, match="cannot overwrite"):
        render_audio_repair_candidate(
            report,
            plan,
            current_master,
            {"processing": {"audio_enhance": False}},
            ffmpeg_bin=decoder,
        )

    current_master.write_bytes(master_bytes + b"changed")
    with pytest.raises(ValueError, match="selected audio master"):
        render_audio_repair_candidate(
            report,
            plan,
            tmp_path / "stale-candidate.wav",
            {"processing": {"audio_enhance": False}},
            ffmpeg_bin=decoder,
        )
    current_master.write_bytes(master_bytes)

    selection = select_audio_repair_candidate(
        episode_dir,
        report,
        plan,
        manifest,
        {"processing": {"audio_enhance": False}},
        allowed_cache_root=candidate.parent,
    )
    selected = episode_dir / SELECTED_REPAIR_AUDIO_PATH
    assert selection["release_safe"] is False
    assert selection["status"] == "review_required"
    assert selection["repaired_findings"] == [
        {"id": "aq_repair", "fingerprint": "aq_repair"}
    ]
    assert selection["selected_output"]["fingerprint"] == file_fingerprint(selected)
    assert (
        generate_audio_mix(episode_dir, {}, {"processing": {"audio_enhance": False}})
        == selected
    )
    assert current_master.read_bytes() == master_bytes


@pytest.mark.skipif(not HAS_FFMPEG_FULL, reason="ffmpeg-full is not installed")
def test_plan_rejects_a_fallback_quieter_than_the_existing_mix(tmp_path):
    decoder = FFMPEG_FULL if FFMPEG_FULL.is_file() else Path("ffmpeg-full")
    episode_dir = tmp_path / "episode"
    episode_dir.mkdir()
    source = episode_dir / "source_merged.mp4"
    subprocess.run(
        [
            str(decoder),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                "aevalsrc='if(between(t,2,3),0,0.01*sin(2*PI*220*t))|"
                "if(between(t,2,3),0.2*sin(2*PI*330*t),0.001*sin(2*PI*330*t))'"
                ":s=8000:d=6"
            ),
            "-c:a",
            "pcm_f32le",
            str(source),
        ],
        check=True,
    )
    (episode_dir / "episode.json").write_text("{}")
    finding = _finding("aq_turn_taking")
    report = {
        "fingerprint": "sha256:report",
        "source": {"path": str(source), "fingerprint": media_fingerprint(source)},
        "scope": {"selected_mix_provenance": {"kind": "embedded_camera"}},
        "findings": [finding],
        "analysis": {"suppressed_candidates": []},
    }

    plan = build_audio_repair_plan(
        report,
        [finding["id"]],
        episode_dir / "qa" / "audio-repair",
        ffmpeg_bin=decoder,
        held_out_count=0,
    )

    assert plan["repairs"] == []
    assert plan["status"] == "failed"
    assert "surviving_contribution_change_db" in plan["dispositions"][0]["reason"]
