"""Tests for source-clock audio continuity analysis."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from lib.audio_qa import (
    PREVIEW_ALGORITHM_VERSION,
    WindowStats,
    _apply_repair_resolutions,
    _mix_provenance,
    _selected_repair_output_proof,
    analyze_windows,
    decode_audio_windows,
    media_fingerprint,
    release_gate,
    render_finding_preview,
    transcript_analysis_fingerprint,
)
from lib.audio_repair import build_audio_repair_plan
from lib.ffprobe import probe as ffprobe
from lib.timeline import Timeline

FRAME_SECONDS = 0.02
FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
HAS_FFMPEG_FULL = FFMPEG_FULL.is_file() or shutil.which("ffmpeg-full") is not None


def _stats(duration: float = 10.0, default_db: float = -100.0) -> WindowStats:
    frames = round(duration / FRAME_SECONDS)
    rms = np.full((frames, 2), default_db, dtype=np.float64)
    peak = np.full((frames, 2), 10 ** (default_db / 20), dtype=np.float64)
    zero = np.zeros((frames, 2), dtype=np.float64)
    return WindowStats(FRAME_SECONDS, rms, peak, zero)


def _set_level(
    stats: WindowStats,
    channel: int,
    start: float,
    end: float,
    dbfs: float,
    *,
    digital_zero: bool = False,
) -> None:
    first = round(start / stats.frame_seconds)
    last = round(end / stats.frame_seconds)
    stats.rms_dbfs[first:last, channel] = -240 if digital_zero else dbfs
    stats.peak[first:last, channel] = 0 if digital_zero else 10 ** (dbfs / 20)
    stats.zero_fraction[first:last, channel] = 1 if digital_zero else 0


def _transcript(*entries: tuple[str, float, float, str]) -> dict:
    return {
        "utterances": [
            {
                "speaker": speaker,
                "start": start,
                "end": end,
                "words": [
                    {
                        "speaker": speaker,
                        "start": start,
                        "end": end,
                        "confidence": 0.99,
                        "word": word,
                    }
                ],
            }
            for speaker, start, end, word in entries
        ]
    }


def test_expected_speaker_dropout_is_blocking_and_maps_to_edited_clock():
    stats = _stats()
    _set_level(stats, 0, 1, 9, -20)
    _set_level(stats, 1, 1, 9, -45)
    _set_level(stats, 0, 5, 6.5, -240, digital_zero=True)
    _set_level(stats, 1, 5, 6.5, -36)
    transcript = _transcript(("guest", 1, 9, "continuous"))
    timeline = Timeline.from_edits(
        10,
        [
            {"type": "trim_start", "seconds": 1},
            {"type": "cut", "start_seconds": 3, "end_seconds": 3.5},
        ],
    )

    findings, _, speaker_channels = analyze_windows(
        stats,
        transcript=transcript,
        timeline=timeline,
        source_fingerprint="sha256:test",
        episode_id="ep_test",
    )

    assert speaker_channels["guest"]["channel"] == 0
    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "digital_zero"
    assert finding["severity"] == "error"
    assert finding["source_time"] == {
        "start_seconds": 5.0,
        "end_seconds": 6.5,
        "duration_seconds": 1.5,
    }
    assert finding["edited_time"] == {
        "status": "retained",
        "ranges": [
            {
                "start_seconds": 3.5,
                "end_seconds": 5.0,
                "source_start_seconds": 5.0,
                "source_end_seconds": 6.5,
            }
        ],
    }
    assert finding["evidence"]["expected_transcript_speakers"] == ["guest"]
    assert finding["evidence"]["expected_speech_ranges"] == [
        {
            "start_seconds": 5.0,
            "end_seconds": 6.5,
            "duration_seconds": 1.5,
        }
    ]
    assert finding["evidence"]["surviving_speech_ranges"] == []
    assert finding["evidence"]["temporal_overlap_seconds"] == 0
    assert finding["preview"]["source"].startswith(
        "/api/episodes/ep_test/audio-qc/findings/"
    )


def test_consecutive_outages_remain_separate_across_short_signal_return():
    stats = _stats(duration=20)
    _set_level(stats, 0, 1, 19, -36)
    _set_level(stats, 1, 1, 19, -20)
    _set_level(stats, 1, 5, 6.34, -240, digital_zero=True)
    _set_level(stats, 1, 6.64, 10.78, -240, digital_zero=True)
    transcript = _transcript(("guest", 1, 19, "continuous"))

    findings, _, mappings = analyze_windows(stats, transcript=transcript)

    assert mappings["guest"]["channel"] == 1
    assert [finding["source_time"] for finding in findings] == [
        {"start_seconds": 5.0, "end_seconds": 6.34, "duration_seconds": 1.34},
        {"start_seconds": 6.64, "end_seconds": 10.78, "duration_seconds": 4.14},
    ]


def test_normal_turn_taking_does_not_report_inactive_channels():
    stats = _stats()
    _set_level(stats, 0, 1, 4, -20)
    _set_level(stats, 1, 1, 4, -240, digital_zero=True)
    _set_level(stats, 0, 5, 8, -240, digital_zero=True)
    _set_level(stats, 1, 5, 8, -20)
    transcript = _transcript(
        ("host", 1, 4, "question"),
        ("guest", 5, 8, "answer"),
    )

    findings, analysis, mappings = analyze_windows(stats, transcript=transcript)

    assert mappings["host"]["channel"] == 0
    assert mappings["guest"]["channel"] == 1
    assert findings == []
    assert analysis["suppressed_candidate_count"] == 2
    assert {
        candidate["suppression_reason"]
        for candidate in analysis["suppressed_candidates"]
    } == {"transcript_supports_surviving_channel"}


def test_intentional_two_channel_silence_is_not_a_dropout():
    stats = _stats()
    _set_level(stats, 0, 0, 10, -24)
    _set_level(stats, 1, 0, 10, -25)
    for channel in (0, 1):
        _set_level(stats, channel, 3, 6, -240, digital_zero=True)

    findings, analysis, _ = analyze_windows(stats)

    assert findings == []
    assert analysis["suppressed_candidate_count"] == 0


def test_channel_relationship_reports_mixed_distinct_and_duplicated_ranges():
    stats = _stats(duration=15, default_db=-20)
    delta = np.full(len(stats.rms_dbfs), 0.1)
    delta[round(5 / FRAME_SECONDS) : round(10 / FRAME_SECONDS)] = 0
    stats = WindowStats(
        stats.frame_seconds,
        stats.rms_dbfs,
        stats.peak,
        stats.zero_fraction,
        delta,
    )

    _, analysis, _ = analyze_windows(stats)

    relationship = analysis["channel_relationship"]
    assert relationship["status"] == "mixed"
    assert relationship["ranges"] == [
        {
            "status": "distinct_samples",
            "start_seconds": 0.0,
            "end_seconds": 5.0,
            "duration_seconds": 5.0,
        },
        {
            "status": "duplicated_samples",
            "start_seconds": 5.0,
            "end_seconds": 10.0,
            "duration_seconds": 5.0,
        },
        {
            "status": "distinct_samples",
            "start_seconds": 10.0,
            "end_seconds": 15.0,
            "duration_seconds": 5.0,
        },
    ]


def test_sharp_nonzero_level_collapse_is_detected_from_context():
    stats = _stats()
    _set_level(stats, 0, 1, 9, -20)
    _set_level(stats, 1, 1, 9, -48)
    _set_level(stats, 0, 4, 5, -55)
    _set_level(stats, 1, 4, 5, -34)
    transcript = _transcript(("speaker-0", 1, 9, "speech"))

    findings, _, _ = analyze_windows(stats, transcript=transcript)

    assert len(findings) == 1
    assert findings[0]["kind"] == "sharp_level_collapse"
    assert findings[0]["source_time"]["start_seconds"] == 4
    assert findings[0]["source_time"]["end_seconds"] == 5
    assert findings[0]["evidence"]["context_continuity"] is True


def test_release_gate_ignores_findings_removed_by_edits():
    retained = {
        "id": "kept",
        "severity": "error",
        "edited_time": {"status": "retained"},
    }
    removed = {
        "id": "cut",
        "severity": "error",
        "edited_time": {"status": "removed"},
    }

    scope = {"selected_mix_provenance": {"uses_checked_source_audio": True}}
    blocked = release_gate(
        {
            "analysis": {"status": "complete"},
            "scope": scope,
            "findings": [retained, removed],
        }
    )
    passing = release_gate(
        {"analysis": {"status": "complete"}, "scope": scope, "findings": [removed]}
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_finding_ids"] == ["kept"]
    assert passing["status"] == "output_unverified"
    assert passing["safe"] is False


def test_release_gate_only_passes_with_current_successful_output_proof():
    report = {
        "fingerprint": "sha256:source-report",
        "analysis": {"status": "complete"},
        "scope": {
            "selected_mix_provenance": {
                "uses_checked_source_audio": True,
                "fingerprint": "sha256:selected-mix",
                "selected_output": {"fingerprint": {"id": "sha256:current-output"}},
            },
            "outputs_checked": [
                {
                    "role": "selected_audio_master",
                    "status": "pass",
                    "source_report_fingerprint": "sha256:source-report",
                    "selected_mix_fingerprint": "sha256:selected-mix",
                    "fingerprint": {"id": "sha256:current-output"},
                    "verification": {
                        "status": "pass",
                        "checks": [
                            {"name": "duration", "pass": True},
                            {"name": "continuity", "pass": True},
                        ],
                    },
                }
            ],
        },
        "findings": [],
    }

    assert release_gate(report)["safe"] is True

    report["scope"]["outputs_checked"][0]["fingerprint"]["id"] = "sha256:old"
    stale = release_gate(report)
    assert stale["status"] == "output_stale"
    assert stale["safe"] is False

    proof = report["scope"]["outputs_checked"][0]
    proof["fingerprint"]["id"] = "sha256:current-output"
    proof["verification"]["checks"][1]["pass"] = False
    failed = release_gate(report)
    assert failed["status"] == "output_failed"
    assert failed["safe"] is False


def test_release_gate_does_not_trust_bare_resolution_statuses():
    finding = {
        "id": "dropout",
        "fingerprint": "dropout",
        "severity": "error",
        "edited_time": {"status": "retained"},
        "resolution": {"status": "repaired"},
    }
    report = {
        "fingerprint": "sha256:report",
        "analysis": {"status": "complete"},
        "scope": {
            "selected_mix_provenance": {
                "uses_checked_source_audio": True,
                "fingerprint": "sha256:mix",
                "selected_output": {"fingerprint": {"id": "sha256:output"}},
            },
            "outputs_checked": [],
        },
        "findings": [finding],
    }

    assert release_gate(report)["status"] == "blocked"

    finding["resolution"] = {
        "status": "false_positive",
        "finding_fingerprint": "dropout",
        "reviewed_by": "editor",
        "reviewed_at": "2026-09-11T00:00:00Z",
        "evidence": "Audible review confirms normal turn-taking.",
    }
    assert release_gate(report)["status"] == "output_unverified"


def test_selected_repair_proof_requires_current_finding_fingerprints():
    selection = {
        "fingerprint": "sha256:selection",
        "candidate_manifest_fingerprint": "sha256:candidate",
        "selected_output": {"fingerprint": {"id": "sha256:selected-output"}},
        "repaired_findings": [{"id": "dropout", "fingerprint": "dropout-v1"}],
        "verification": {
            "status": "pass",
            "checks": [{"name": "selected_copy", "pass": True}],
        },
    }
    finding = {
        "id": "dropout",
        "fingerprint": "dropout-v1",
        "severity": "error",
        "edited_time": {"status": "retained"},
        "resolution": {"status": "unresolved"},
    }
    report = {
        "fingerprint": "sha256:report-v2",
        "analysis": {"status": "complete"},
        "scope": {
            "selected_mix_provenance": {
                "uses_checked_source_audio": True,
                "fingerprint": "sha256:selected-mix",
                "selected_output": selection["selected_output"],
            },
            "outputs_checked": [],
        },
        "findings": [finding],
    }
    _apply_repair_resolutions(report["findings"], selection)
    report["scope"]["outputs_checked"] = [
        _selected_repair_output_proof(report, selection)
    ]

    assert release_gate(report)["status"] == "pass"

    changed = {**finding, "fingerprint": "dropout-v2", "resolution": {}}
    stale_report = {**report, "findings": [changed]}
    _apply_repair_resolutions(stale_report["findings"], selection)
    stale_report["scope"] = {
        **report["scope"],
        "outputs_checked": [_selected_repair_output_proof(stale_report, selection)],
    }

    gate = release_gate(stale_report)
    assert gate["status"] == "blocked"
    assert gate["blocking_finding_ids"] == ["dropout"]


def test_release_gate_never_claims_unchecked_or_unused_audio_is_safe():
    unchecked = release_gate(
        {
            "analysis": {"status": "not_applicable"},
            "scope": {"selected_mix_provenance": {"uses_checked_source_audio": True}},
            "findings": [],
        }
    )
    recorder_mix = release_gate(
        {
            "analysis": {"status": "complete"},
            "scope": {"selected_mix_provenance": {"uses_checked_source_audio": False}},
            "findings": [{"id": "camera-only", "severity": "error"}],
        }
    )

    assert unchecked["status"] == "unknown"
    assert unchecked["safe"] is False
    assert recorder_mix["status"] == "not_applicable"
    assert recorder_mix["safe"] is None


def test_mix_provenance_uses_selected_tracks_instead_of_inventory():
    recorder = {
        "track_number": 1,
        "track_type": "input",
        "filename": "guest.wav",
        "dest_path": "/episode/audio/guest.wav",
    }

    unselected = _mix_provenance({"audio_tracks": [recorder]})
    selected = _mix_provenance(
        {
            "audio_tracks": [recorder],
            "crop_config": {"speakers": [{"track": 1}]},
        }
    )

    assert unselected["kind"] == "embedded_camera"
    assert unselected["uses_checked_source_audio"] is True
    assert selected["kind"] == "external_recorder"
    assert selected["uses_checked_source_audio"] is False


def test_transcript_audio_fingerprint_ignores_copy_but_binds_timing_and_speaker(
    tmp_path,
):
    path = tmp_path / "transcript.json"
    transcript = {
        "utterances": [
            {
                "speaker": 0,
                "words": [
                    {
                        "word": "hello",
                        "punctuated_word": "Hello,",
                        "start": 1.0,
                        "end": 1.4,
                        "confidence": 0.99,
                    }
                ],
            }
        ]
    }
    path.write_text(json.dumps(transcript))
    original = transcript_analysis_fingerprint(path, transcript, 0.55)

    transcript["utterances"][0]["words"][0].update(
        word="welcome", punctuated_word="Welcome!"
    )
    path.write_text(json.dumps(transcript))
    copy_edit = transcript_analysis_fingerprint(path, transcript, 0.55)
    transcript["utterances"][0]["words"][0]["speaker"] = 1
    speaker_edit = transcript_analysis_fingerprint(path, transcript, 0.55)

    assert copy_edit["id"] == original["id"]
    assert (
        copy_edit["content_fingerprint"]["id"] != original["content_fingerprint"]["id"]
    )
    assert speaker_edit["id"] != original["id"]


@pytest.mark.skipif(not HAS_FFMPEG_FULL, reason="ffmpeg-full is not installed")
def test_pts_discontinuity_is_materialized_on_source_clock(tmp_path):
    source = tmp_path / "packet-gap.mka"
    decoder = FFMPEG_FULL if FFMPEG_FULL.is_file() else "ffmpeg-full"
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
            "aevalsrc=0.2*sin(2*PI*220*t)|0.2*sin(2*PI*330*t):s=8000:d=3",
            "-af",
            "aselect='not(between(t,1,1.5))'",
            "-c:a",
            "pcm_f32le",
            str(source),
        ],
        check=True,
    )

    probed = ffprobe(source)
    stats = decode_audio_windows(source, decoder)

    assert float(probed["format"]["duration"]) == pytest.approx(3, abs=0.01)
    assert stats.duration == pytest.approx(3, abs=0.04)
    gap = stats.rms_dbfs[round(1.1 / FRAME_SECONDS) : round(1.4 / FRAME_SECONDS)]
    assert np.max(gap) < -100


@pytest.mark.skipif(not HAS_FFMPEG_FULL, reason="ffmpeg-full is not installed")
def test_grounded_preview_uses_only_surviving_source_channel(tmp_path):
    source = tmp_path / "source.wav"
    decoder = FFMPEG_FULL if FFMPEG_FULL.is_file() else "ffmpeg-full"
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
                "aevalsrc='if(between(t,1,2),0,0.1*sin(2*PI*220*t))|"
                "0.01*sin(2*PI*220*t)':s=8000:d=3"
            ),
            "-c:a",
            "pcm_f32le",
            str(source),
        ],
        check=True,
    )
    finding = {
        "id": "aq_preview",
        "channel": 0,
        "source_time": {
            "start_seconds": 1,
            "end_seconds": 2,
            "duration_seconds": 1,
        },
        "evidence": {"estimated_recovery_gain_db": 6},
        "preview": {"padding_seconds": 0.5},
    }

    result = render_finding_preview(
        source, finding, tmp_path / "previews", ffmpeg_bin=decoder
    )

    assert Path(result["original"]).is_file()
    assert Path(result["grounded_fallback"]).is_file()
    assert result["fallback_source_channel"] == 1
    assert result["synthetic_audio_used"] is False
    assert float(
        ffprobe(Path(result["grounded_fallback"]))["format"]["duration"]
    ) == pytest.approx(2, abs=0.03)

    def decode(path: str) -> np.ndarray:
        decoded = subprocess.run(
            [
                str(decoder),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                path,
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-c:a",
                "pcm_f32le",
                "-f",
                "f32le",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        )
        return np.frombuffer(decoded.stdout, dtype="<f4")

    original = decode(result["original"])
    fallback = decode(result["grounded_fallback"])
    assert len(fallback) == pytest.approx(len(original), abs=1)
    for start, end in ((0.1, 0.4), (1.6, 1.9)):
        section = slice(round(start * 8000), round(end * 8000))
        np.testing.assert_allclose(fallback[section], original[section], atol=2e-6)


@pytest.mark.skipif(not HAS_FFMPEG_FULL, reason="ffmpeg-full is not installed")
def test_repair_plan_is_explicit_bounded_and_checks_held_out_context(tmp_path):
    source = tmp_path / "source.wav"
    decoder = FFMPEG_FULL if FFMPEG_FULL.is_file() else "ffmpeg-full"
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
                "if(between(t,14,15),0,0.02*sin(2*PI*330*t))':s=8000:d=20"
            ),
            "-c:a",
            "pcm_f32le",
            str(source),
        ],
        check=True,
    )
    fingerprint = media_fingerprint(source)
    finding = {
        "id": "aq_repair",
        "classification": "probable_dropout",
        "kind": "digital_zero",
        "severity": "error",
        "confidence": 0.99,
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
    control = {
        "id": "aq_control",
        "kind": "digital_zero",
        "channel": 1,
        "source_time": {
            "start_seconds": 14,
            "end_seconds": 15,
            "duration_seconds": 1,
        },
        "edited_time": {"status": "retained", "ranges": []},
        "evidence": {"estimated_recovery_gain_db": 0},
        "preview": {"padding_seconds": 0.5},
        "suppression_reason": "transcript_supports_surviving_channel",
    }
    report = {
        "fingerprint": "sha256:report",
        "source": {"path": str(source), "fingerprint": fingerprint},
        "scope": {"selected_mix_provenance": {"fingerprint": "sha256:selected-mix"}},
        "findings": [finding],
        "analysis": {"suppressed_candidates": [control]},
    }

    with pytest.raises(ValueError, match="At least one"):
        build_audio_repair_plan(report, [], tmp_path / "empty", ffmpeg_bin=decoder)

    plan = build_audio_repair_plan(
        report,
        ["aq_repair"],
        tmp_path / "plan",
        ffmpeg_bin=decoder,
        held_out_count=1,
    )

    assert plan["status"] == "preview_ready"
    assert plan["policy"]["preview_algorithm_version"] == PREVIEW_ALGORITHM_VERSION
    assert [entry["finding_ids"] for entry in plan["repairs"]] == [["aq_repair"]]
    assert [entry["finding_ids"] for entry in plan["held_out_controls"]] == [
        ["aq_control"]
    ]
    assert all(check["pass"] for check in plan["checks"])
    assert (tmp_path / "plan" / "audio-repair-plan.json").is_file()
