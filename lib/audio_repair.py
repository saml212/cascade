"""Evidence-grounded recovery planning for source-channel audio defects."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

import numpy as np

from lib.atomic_write import atomic_write_json
from lib.audio_mix import (
    AUDIO_SELECTION_SCHEMA,
    CAMERA_AUDIO_TIMELINE_FILTER,
    SELECTED_REPAIR_AUDIO_PATH,
    audio_processing_settings,
    audio_selection_settings,
    document_fingerprint,
    json_fingerprint,
    publish_audio_selection,
)
from lib.audio_qa import (
    PREVIEW_ALGORITHM_VERSION,
    _checked_ffmpeg,
    _mix_provenance,
    _read_json,
    render_finding_preview,
    repair_envelope_weight,
    resolve_ffmpeg,
    transcript_analysis_fingerprint,
)
from lib.ffprobe import file_fingerprint, get_audio_stream, media_fingerprint
from lib.ffprobe import probe as ffprobe

REPAIR_PLAN_SCHEMA = "cascade.audio-repair-plan/v1"
REPAIR_PLAN_PATH = Path("qa/audio-repair/audio-repair-plan.json")
REPAIR_CANDIDATE_SCHEMA = "cascade.audio-repair-candidate/v1"
REPAIR_CANDIDATE_PATH = Path("qa/audio-repair/audio-repair-candidate.json")
CANDIDATE_ALGORITHM_VERSION = "grounded-channel-envelope/v3"
AUTO_REPAIR_POLICY = "grounded_probable_dropout/v1"
MIN_FREE_BYTES_AFTER_CANDIDATE = 10 * 1024**3
UNCHANGED_SURVIVOR_GAIN_DB = 20 * math.log10(0.5)


def _check(name: str, passed: bool, **evidence) -> dict:
    return {"name": name, "pass": bool(passed), **evidence}


def select_grounded_repair_findings(report: dict) -> list[str]:
    """Choose findings supported strongly enough for an automatic candidate."""
    return [
        finding["id"]
        for finding in report.get("findings", [])
        if _automatic_repair_eligibility(finding)[0]
    ]


def build_audio_repair_plan(
    report: dict,
    finding_ids: list[str],
    output_dir: str | Path,
    *,
    predecessor_plan: dict | None = None,
    ffmpeg_bin: str | Path | None = None,
    held_out_count: int = 2,
    group_gap_seconds: float = 0.35,
    selection_policy: str = "explicit_current_report_findings",
) -> dict:
    """Render a bounded, reviewable recovery plan from explicit report findings."""
    if not finding_ids:
        raise ValueError("At least one current audio finding ID is required")
    if held_out_count < 0 or held_out_count > 10:
        raise ValueError("held_out_count must be between 0 and 10")
    if group_gap_seconds < 0 or group_gap_seconds > 1:
        raise ValueError("group_gap_seconds must be between 0 and 1")

    source, source_fingerprint, current_provenance = _current_report_context(report)
    recorded_provenance = report.get("scope", {}).get("selected_mix_provenance", {})
    if (
        recorded_provenance.get("kind")
        and recorded_provenance.get("kind") != current_provenance["kind"]
    ):
        raise ValueError("Audio quality report is stale for the selected mix")
    predecessor_entries = _validated_predecessor_entries(
        predecessor_plan, current_provenance
    )

    findings = {item.get("id"): item for item in report.get("findings", [])}
    missing = [finding_id for finding_id in finding_ids if finding_id not in findings]
    if missing:
        raise ValueError(f"Unknown audio finding IDs: {', '.join(missing)}")
    selected = [findings[finding_id] for finding_id in dict.fromkeys(finding_ids)]
    for finding in selected:
        _validate_repair_finding(finding)
    selected = [
        _prepare_repair_finding(
            finding, predecessor_entry=predecessor_entries.get(finding["id"])
        )
        for finding in selected
    ]

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    decoder = resolve_ffmpeg(ffmpeg_bin)
    groups = _group_repair_findings(selected, group_gap_seconds)
    repairs = []
    failed_repair_ids: dict[str, str] = {}
    for group in groups:
        try:
            entry = _render_plan_entry(
                source, group, destination, decoder, control=False
            )
        except (ValueError, RuntimeError) as exc:
            failed_repair_ids.update({finding["id"]: str(exc) for finding in group})
            continue
        if entry["verification"]["status"] != "pass" and any(
            item.get("_repair_scope") == "full_finding"
            and item.get("evidence", {}).get("expected_speech_ranges")
            and not item.get("_predecessor_entry_id")
            for item in group
        ):
            activity_group = [
                _prepare_repair_finding(item, force_activity_scope=True)
                for item in group
            ]
            entry = _render_plan_entry(
                source, activity_group, destination, decoder, control=False
            )
        if entry["verification"]["status"] == "pass":
            repairs.append(entry)
        else:
            reason = _preview_failure_reason(entry["verification"]["metrics"])
            failed_repair_ids.update({finding["id"]: reason for finding in group})
    controls = [
        _render_plan_entry(source, [candidate], destination, decoder, control=True)
        for candidate in _held_out_candidates(report, groups, held_out_count)
    ]
    all_entries = repairs + controls
    repaired_ids = {
        finding_id for entry in repairs for finding_id in entry["finding_ids"]
    }
    selected_ids = [item["id"] for item in selected]
    dispositions = _repair_dispositions(
        report,
        selected_ids=set(selected_ids),
        repaired_ids=repaired_ids,
        failures=failed_repair_ids,
    )
    unresolved_probable = [
        item
        for item in dispositions
        if item["classification"] == "probable_dropout"
        and item["status"] == "unresolved"
    ]
    checks = [
        _check("source_fingerprint_current", True, actual=source_fingerprint["id"]),
        _check(
            "preview_context_preserved",
            all(e["verification"]["status"] == "pass" for e in all_entries),
            checked_count=len(all_entries),
        ),
        _check(
            "every_proposed_repair_verified",
            bool(repairs)
            and all(e["verification"]["status"] == "pass" for e in repairs),
            verified_count=len(repaired_ids),
        ),
        _check(
            "grounded_source_only",
            all(not e["preview"]["synthetic_audio_used"] for e in all_entries),
            checked_count=len(all_entries),
        ),
        _check(
            "all_report_findings_dispositioned",
            len(dispositions) == len(report.get("findings", [])),
            checked_count=len(dispositions),
        ),
    ]
    checks_pass = all(item["pass"] for item in checks)
    if not checks_pass:
        status = "failed"
    elif unresolved_probable:
        status = "preview_ready_with_unresolved"
    else:
        status = "preview_ready"
    plan = {
        "schema": REPAIR_PLAN_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source_report_fingerprint": report.get("fingerprint"),
        "source": {
            "path": str(source.resolve()),
            "fingerprint": source_fingerprint,
        },
        "selected_mix_fingerprint": current_provenance["fingerprint"],
        "selected_mix_provenance": current_provenance,
        "policy": {
            "selection": selection_policy,
            "automatic_policy": AUTO_REPAIR_POLICY,
            "repair_kinds": ["digital_zero", "near_zero"],
            "classification": "probable_dropout",
            "maximum_group_gap_seconds": group_gap_seconds,
            "preview_algorithm_version": PREVIEW_ALGORITHM_VERSION,
            "predecessor_plan_fingerprint": (
                predecessor_plan.get("fingerprint") if predecessor_plan else None
            ),
            "synthetic_audio": False,
        },
        "repairs": repairs,
        "held_out_controls": controls,
        "dispositions": dispositions,
        "coverage": {
            "total_finding_count": len(dispositions),
            "selected_finding_count": len(selected_ids),
            "proposed_repair_count": len(repaired_ids),
            "selection_rejected_count": len(failed_repair_ids),
            "excluded_count": sum(
                item["status"] == "excluded" for item in dispositions
            ),
            "unresolved_probable_count": len(unresolved_probable),
            "unresolved_candidate_count": sum(
                item["classification"] != "probable_dropout"
                and item["status"] == "unresolved"
                for item in dispositions
            ),
        },
        "checks": checks,
    }
    plan["fingerprint"] = document_fingerprint(plan)
    atomic_write_json(destination / "audio-repair-plan.json", plan)
    return plan


def _preview_failure_reason(metrics: dict) -> str:
    failures = []
    limits = {
        "duration_error_seconds": (lambda value: value <= 0.02),
        "sample_count_delta": (lambda value: value <= 1),
        "context_max_absolute_delta": (lambda value: value <= 2e-6),
        "repair_delta_rms": (lambda value: value > 1e-6),
        "fallback_peak": (lambda value: value <= 0.951),
        "limited_sample_fraction": (lambda value: value <= 0.005),
        "healthy_gap_max_absolute_delta": (lambda value: value <= 2e-6),
        "level_delta_db": (lambda value: abs(value) <= 1.5),
        "surviving_contribution_change_db": (lambda value: value >= -0.05),
    }
    for name, passes in limits.items():
        if name not in metrics or not passes(metrics[name]):
            failures.append(name)
    return "preview_verification_failed:" + ",".join(failures)


def _automatic_repair_eligibility(finding: dict) -> tuple[bool, str]:
    edited_status = finding.get("edited_time", {}).get("status")
    if edited_status == "removed":
        return False, "removed_by_edit"
    if edited_status not in {"retained", "split"}:
        return False, "finding_is_not_retained_in_the_edit"
    if finding.get("classification") != "probable_dropout":
        return False, "candidate_requires_review"
    if finding.get("severity") not in {"error", "critical"}:
        return False, "finding_is_not_release_blocking"
    evidence = finding.get("evidence", {})
    if finding.get("kind") not in {"digital_zero", "near_zero"}:
        return False, "unsupported_finding_kind"
    if float(evidence.get("zero_sample_fraction", 0)) < 0.999:
        return False, "source_loss_not_exactly_established"
    duration = max(
        float(finding.get("source_time", {}).get("duration_seconds", 0)), 1e-9
    )
    expected = float(evidence.get("expected_speech_seconds", 0))
    surviving = float(evidence.get("surviving_speech_seconds", 0))
    expected_ranges = evidence.get("expected_speech_ranges") or []
    temporal_overlap = evidence.get("temporal_overlap_seconds")
    activity_scoped = bool(expected_ranges) and temporal_overlap is not None
    if activity_scoped:
        if finding.get("confidence", 0) < 0.70:
            return False, "detector_confidence_below_0.70"
        if expected < 0.10:
            return False, "expected_speech_below_0.10_seconds"
        if float(temporal_overlap) > 0.02:
            return False, "transcript_speakers_overlap_in_repair_interval"
    else:
        if finding.get("confidence", 0) < 0.90:
            return False, "detector_confidence_below_0.90"
        if expected < 0.50:
            return False, "expected_speech_below_0.50_seconds"
        if expected / duration < 0.50 and not evidence.get("context_continuity"):
            return False, "sparse_transcript_support_without_two_sided_continuity"
        if surviving > max(0.20, expected * 0.25):
            return False, "overlapping_surviving_speaker_risks_overboost"
    if finding.get("recovery", {}).get("grounded") is not True:
        return False, "grounded_surviving_channel_unavailable"
    if finding.get("recovery", {}).get("synthetic_audio") is not False:
        return False, "synthetic_recovery_disallowed"
    if activity_scoped and (
        finding.get("confidence", 0) < 0.90
        or expected < 0.50
        or surviving > max(0.20, expected * 0.25)
        or (expected / duration < 0.50 and not evidence.get("context_continuity"))
    ):
        return True, "meets_nonoverlapping_transcript_activity_policy"
    return True, "meets_grounded_probable_dropout_policy"


def _validated_predecessor_entries(
    predecessor_plan: dict | None, current_provenance: dict
) -> dict[str, dict]:
    """Return previously selected, verified operations that may be replayed."""
    if predecessor_plan is None:
        return {}
    if predecessor_plan.get("fingerprint") != document_fingerprint(predecessor_plan):
        raise ValueError("Predecessor audio repair plan fingerprint is invalid")
    selection = current_provenance.get("repair_selection") or {}
    if selection.get("repair_plan_fingerprint") != predecessor_plan.get("fingerprint"):
        raise ValueError("Predecessor audio repair plan is not the selected plan")
    selected_ids = {item.get("id") for item in selection.get("repaired_findings", [])}
    entries: dict[str, dict] = {}
    for entry in predecessor_plan.get("repairs", []):
        finding_ids = entry.get("finding_ids") or []
        if (
            entry.get("verification", {}).get("status") != "pass"
            or not finding_ids
            or any(finding_id not in selected_ids for finding_id in finding_ids)
        ):
            continue
        for finding_id in finding_ids:
            if finding_id in entries:
                raise ValueError("Predecessor audio repair plan has duplicate findings")
            entries[finding_id] = entry
    return entries


def _prepare_repair_finding(
    finding: dict,
    *,
    force_activity_scope: bool = False,
    predecessor_entry: dict | None = None,
) -> dict:
    prepared = dict(finding)
    if predecessor_entry is not None and not force_activity_scope:
        prepared["_repair_scope"] = predecessor_entry.get(
            "repair_scope", "full_finding"
        )
        prepared["_repair_intervals"] = json.loads(
            json.dumps(predecessor_entry["repair_intervals"])
        )
        prepared["_fade_mode"] = predecessor_entry.get("fade_mode", "cross_boundary")
        prepared["_predecessor_entry_id"] = predecessor_entry["id"]
        return prepared
    _, reason = _automatic_repair_eligibility(finding)
    activity_ranges = finding.get("evidence", {}).get("expected_speech_ranges") or []
    use_activity = force_activity_scope or reason == (
        "meets_nonoverlapping_transcript_activity_policy"
    )
    prepared["_repair_scope"] = (
        "transcript_activity" if use_activity else "full_finding"
    )
    prepared["_repair_intervals"] = (
        activity_ranges if use_activity else [finding["source_time"]]
    )
    prepared["_fade_mode"] = "contained" if use_activity else "cross_boundary"
    return prepared


def _repair_dispositions(
    report: dict,
    *,
    selected_ids: set[str],
    repaired_ids: set[str],
    failures: dict[str, str],
) -> list[dict]:
    dispositions = []
    for finding in report.get("findings", []):
        eligible, reason = _automatic_repair_eligibility(finding)
        finding_id = finding["id"]
        if finding.get("edited_time", {}).get("status") == "removed":
            status = "excluded"
        elif finding_id in repaired_ids:
            status = "proposed_repair"
            reason = "grounded_preview_passed_mechanical_checks"
        else:
            status = "unresolved"
            if finding_id in failures:
                reason = failures[finding_id]
            elif eligible and finding_id not in selected_ids:
                reason = "eligible_but_not_selected"
        dispositions.append(
            {
                "finding_id": finding_id,
                "classification": finding.get("classification"),
                "severity": finding.get("severity"),
                "source_time": finding.get("source_time"),
                "edited_time": finding.get("edited_time"),
                "status": status,
                "reason": reason,
            }
        )
    return dispositions


def render_audio_repair_candidate(
    report: dict,
    plan: dict,
    output_path: str | Path,
    config: dict,
    *,
    manifest_path: str | Path | None = None,
    asr_evidence_path: str | Path | None = None,
    ffmpeg_bin: str | Path | None = None,
) -> dict:
    """Render a full source-clock candidate while preserving the selected master."""
    source, current_provenance = _validate_current_plan(report, plan)
    current_master = Path(current_provenance.get("selected_output", {}).get("path", ""))
    if not current_master.is_file():
        raise FileNotFoundError("The currently selected audio master is unavailable")

    destination = Path(output_path).resolve()
    if destination == current_master.resolve():
        raise ValueError(
            "A repair candidate cannot overwrite the selected audio master"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    decoder = resolve_ffmpeg(ffmpeg_bin)
    entries = plan.get("repairs", [])
    if not entries:
        raise ValueError("The repair plan contains no verified proposed repairs")
    if any(
        entry.get("verification", {}).get("status") != "pass"
        or entry.get("preview", {}).get("synthetic_audio_used") is not False
        for entry in entries
    ):
        raise ValueError("The repair plan contains unverified repair entries")
    _validate_non_overlapping_intervals(entries)
    _ensure_candidate_space(destination, source)
    asr_evidence = _bounded_asr_evidence(plan, asr_evidence_path)

    master_before = file_fingerprint(current_master)
    with tempfile.TemporaryDirectory(
        prefix=".cascade-audio-repair-", dir=destination.parent
    ) as temporary:
        temporary_dir = Path(temporary)
        raw_candidate = temporary_dir / "candidate-raw.wav"
        _render_grounded_candidate(source, entries, raw_candidate, decoder)
        processed_candidate = _master_candidate(raw_candidate, temporary_dir, config)
        os.replace(processed_candidate, destination)

    master_after = file_fingerprint(current_master)
    candidate_fingerprint = file_fingerprint(destination)
    verification = _verify_full_candidate(
        destination,
        current_master,
        entries,
        plan.get("held_out_controls", []),
        master_before,
        master_after,
        decoder,
        config,
    )
    repaired_ids = [
        finding_id for entry in entries for finding_id in entry["finding_ids"]
    ]
    unresolved_ids = [
        item["finding_id"]
        for item in plan.get("dispositions", [])
        if item.get("status") == "unresolved"
    ]
    manifest = {
        "schema": REPAIR_CANDIDATE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "review_required"
            if verification["status"] == "pass"
            else "failed_verification"
        ),
        "release_safe": False,
        "source_report_fingerprint": report.get("fingerprint"),
        "repair_plan_fingerprint": plan.get("fingerprint"),
        "render_algorithm": CANDIDATE_ALGORITHM_VERSION,
        "selected_mix_fingerprint": current_provenance["fingerprint"],
        "predecessor_selection": current_provenance.get("repair_selection"),
        "processing_config_fingerprint": _config_fingerprint(config),
        "source": {
            "path": str(source.resolve()),
            "fingerprint": media_fingerprint(source),
        },
        "selected_master_before": {
            "path": str(current_master.resolve()),
            "fingerprint": master_before,
        },
        "candidate": {
            "path": str(destination),
            "fingerprint": candidate_fingerprint,
        },
        "repaired_finding_ids": repaired_ids,
        "excluded_finding_ids": [
            item["finding_id"]
            for item in plan.get("dispositions", [])
            if item.get("status") == "excluded"
        ],
        "unresolved_finding_ids": unresolved_ids,
        "synthetic_audio_used": False,
        "asr_evidence": asr_evidence,
        "perceptual_review": {
            "status": "not_performed",
            "claim": "No human listening or perceptual-quality conclusion is recorded.",
        },
        "verification": verification,
    }
    manifest["fingerprint"] = document_fingerprint(manifest)
    destination_manifest = Path(manifest_path or source.parent / REPAIR_CANDIDATE_PATH)
    destination_manifest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(destination_manifest, manifest)
    return manifest


def select_audio_repair_candidate(
    episode_dir: str | Path,
    report: dict,
    plan: dict,
    manifest: dict,
    config: dict,
    *,
    allowed_cache_root: str | Path,
) -> dict:
    """Select a proven candidate without replacing the preserved base master."""
    episode_dir = Path(episode_dir).resolve()
    _, provenance = _validate_current_plan(report, plan)
    if manifest.get("schema") != REPAIR_CANDIDATE_SCHEMA:
        raise ValueError("Audio repair candidate manifest is invalid")
    if manifest.get("fingerprint") != document_fingerprint(manifest):
        raise ValueError("Audio repair candidate manifest fingerprint is invalid")
    if (
        manifest.get("source_report_fingerprint") != report.get("fingerprint")
        or manifest.get("repair_plan_fingerprint") != plan.get("fingerprint")
        or manifest.get("selected_mix_fingerprint") != provenance.get("fingerprint")
    ):
        raise ValueError("Audio repair candidate is stale for the current plan")
    verification = manifest.get("verification") or {}
    checks = verification.get("checks") or []
    if (
        manifest.get("status") == "failed_verification"
        or verification.get("status") != "pass"
        or not checks
        or any(check.get("pass") is not True for check in checks)
        or manifest.get("synthetic_audio_used") is not False
    ):
        raise ValueError("Audio repair candidate has not passed objective verification")

    try:
        candidate = Path(manifest["candidate"]["path"]).resolve()
        recorded_candidate = manifest["candidate"]["fingerprint"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Audio repair candidate manifest is incomplete") from exc
    cache_root = Path(allowed_cache_root).resolve()
    if not candidate.is_relative_to(cache_root) or not candidate.is_file():
        raise ValueError("Audio repair candidate is outside the controlled cache")
    actual_candidate = file_fingerprint(candidate)
    if actual_candidate.get("id") != recorded_candidate.get("id"):
        raise ValueError("Audio repair candidate bytes have changed since verification")

    findings = {item.get("id"): item for item in report.get("findings", [])}
    repaired_ids = list(dict.fromkeys(manifest.get("repaired_finding_ids") or []))
    if not repaired_ids or any(
        finding_id not in findings for finding_id in repaired_ids
    ):
        raise ValueError("Audio repair candidate references unknown repaired findings")
    repaired = [
        {
            "id": finding_id,
            "fingerprint": findings[finding_id].get("fingerprint") or finding_id,
        }
        for finding_id in repaired_ids
    ]
    unresolved = [
        {
            "id": finding_id,
            "fingerprint": findings[finding_id].get("fingerprint") or finding_id,
        }
        for finding_id in dict.fromkeys(manifest.get("unresolved_finding_ids") or [])
        if finding_id in findings
    ]

    work_dir = episode_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    selected_path = (episode_dir / SELECTED_REPAIR_AUDIO_PATH).resolve()
    if shutil.disk_usage(work_dir).free - candidate.stat().st_size < (
        MIN_FREE_BYTES_AFTER_CANDIDATE
    ):
        raise OSError(
            "Selecting repair audio must preserve at least "
            f"{MIN_FREE_BYTES_AFTER_CANDIDATE // 1024**3} GiB free"
        )
    fd, staged_name = tempfile.mkstemp(
        prefix=".audio-repair-selected-", suffix=".wav", dir=work_dir
    )
    os.close(fd)
    staged = Path(staged_name)
    try:
        shutil.copy2(candidate, staged)
        selected_fingerprint = file_fingerprint(staged)
        if selected_fingerprint["id"] != actual_candidate["id"]:
            raise RuntimeError("Selected repair audio copy failed content verification")
        selected_verification = json.loads(json.dumps(verification))
        selected_verification.setdefault("checks", []).append(
            _check(
                "selected_copy_matches_verified_candidate",
                True,
                candidate_fingerprint=actual_candidate["id"],
            )
        )
        record = {
            "schema": AUDIO_SELECTION_SCHEMA,
            "selected_at": datetime.now(timezone.utc).isoformat(),
            "status": "review_required" if unresolved else "selected",
            "release_safe": False,
            "source_report_fingerprint": report.get("fingerprint"),
            "repair_plan_fingerprint": plan.get("fingerprint"),
            "candidate_manifest_fingerprint": manifest.get("fingerprint"),
            "source": manifest.get("source"),
            "base_mix_provenance": provenance,
            "audio_selection_settings": audio_selection_settings(
                _read_json(episode_dir / "episode.json") or {}
            ),
            "audio_processing_settings": audio_processing_settings(config),
            "selected_output": {
                "path": str(selected_path),
                "fingerprint": selected_fingerprint,
            },
            "candidate": {
                "path": str(candidate),
                "fingerprint": actual_candidate,
            },
            "repaired_findings": repaired,
            "unresolved_findings": unresolved,
            "verification": selected_verification,
            "perceptual_review": manifest.get("perceptual_review"),
        }
        record["fingerprint"] = document_fingerprint(record)
        return publish_audio_selection(episode_dir, staged, record)
    finally:
        staged.unlink(missing_ok=True)


def _validate_current_plan(report: dict, plan: dict) -> tuple[Path, dict]:
    source, _, provenance = _current_report_context(report)
    if plan.get("source_report_fingerprint") != report.get("fingerprint"):
        raise ValueError("Audio repair plan is stale for the quality report")
    if plan.get("fingerprint") != document_fingerprint(plan):
        raise ValueError("Audio repair plan fingerprint is invalid")
    if plan.get("policy", {}).get("preview_algorithm_version") != (
        PREVIEW_ALGORITHM_VERSION
    ):
        raise ValueError("Audio repair plan uses an obsolete repair algorithm")

    if provenance["fingerprint"] != plan.get("selected_mix_fingerprint"):
        raise ValueError("Audio repair plan is stale for the selected mix")
    planned_output = plan.get("selected_mix_provenance", {}).get("selected_output", {})
    current_output = provenance.get("selected_output", {})
    if planned_output.get("path") != current_output.get("path") or planned_output.get(
        "fingerprint", {}
    ).get("id") != current_output.get("fingerprint", {}).get("id"):
        raise ValueError("Audio repair plan is stale for the selected audio master")
    return source, provenance


def _current_report_context(report: dict) -> tuple[Path, dict, dict]:
    source = Path(report.get("source", {}).get("path", ""))
    if not source.is_file():
        raise FileNotFoundError(f"Source media not found: {source}")
    fingerprint = media_fingerprint(source)
    if fingerprint["id"] != report.get("source", {}).get("fingerprint", {}).get("id"):
        raise ValueError("Audio quality report is stale for the source media")
    episode = _read_json(source.parent / "episode.json") or {}
    _validate_report_dependencies(report, episode)
    provenance = _mix_provenance(
        episode, episode_dir=source.parent, source_fingerprint=fingerprint
    )
    return source, fingerprint, provenance


def _ensure_candidate_space(destination: Path, source: Path) -> None:
    source_probe = ffprobe(source)
    source_stream = get_audio_stream(source_probe)
    duration = float(
        source_stream.get("duration")
        or source_probe.get("format", {}).get("duration", 0)
    )
    if duration <= 0:
        raise ValueError("Source audio duration is unavailable")
    raw_bytes = math.ceil(duration * 48000 * 2 * 4)
    mastered_bytes = math.ceil(duration * 48000 * 2 * 3)
    existing_bytes = destination.stat().st_size if destination.is_file() else 0
    required = raw_bytes + mastered_bytes + existing_bytes
    free = shutil.disk_usage(destination.parent).free
    if free - required < MIN_FREE_BYTES_AFTER_CANDIDATE:
        raise OSError(
            "Audio repair candidate needs scratch space while preserving at least "
            f"{MIN_FREE_BYTES_AFTER_CANDIDATE // 1024**3} GiB free"
        )


def _validate_non_overlapping_intervals(entries: list[dict]) -> None:
    intervals = sorted(
        (
            float(interval["start_seconds"]),
            float(interval["end_seconds"]),
            entry["id"],
        )
        for entry in entries
        for interval in entry["repair_intervals"]
    )
    for previous, current in pairwise(intervals):
        if current[0] < previous[1]:
            raise ValueError(
                f"Repair intervals overlap between {previous[2]} and {current[2]}"
            )


def _entry_weight(entry: dict) -> str:
    weights = []
    for interval in entry["repair_intervals"]:
        start = float(interval["start_seconds"])
        end = float(interval["end_seconds"])
        weights.append(
            "("
            + repair_envelope_weight(
                start,
                end,
                contained=entry.get("fade_mode") == "contained",
            )
            + ")"
        )
    return f"min(1,{'+'.join(weights)})"


def _render_grounded_candidate(
    source: Path, entries: list[dict], destination: Path, decoder: Path
) -> None:
    weighted_entries = [(entry, _entry_weight(entry)) for entry in entries]
    by_channel = {
        channel: [
            (entry, weight)
            for entry, weight in weighted_entries
            if entry["surviving_channel"] == channel
        ]
        for channel in (0, 1)
    }
    active_channels = [channel for channel, items in by_channel.items() if items]
    split_labels = ["[normal]", *[f"[source{channel}]" for channel in active_channels]]
    weights = [weight for _, weight in weighted_entries]
    any_repair = f"min(1,{'+'.join(f'({weight})' for weight in weights)})"
    filters = [
        (
            f"[0:a]{CAMERA_AUDIO_TIMELINE_FILTER},aformat=channel_layouts=stereo,"
            f"asplit={len(split_labels)}{''.join(split_labels)}"
        ),
        (
            "[normal]pan=mono|c0=0.5*c0+0.5*c1,"
            f"volume='1-({any_repair})':eval=frame[base]"
        ),
    ]
    output_labels = ["[base]"]
    for channel in active_channels:
        gain = "+".join(
            f"({weight})*{10 ** (float(entry['gain_db']) / 20):.8f}"
            for entry, weight in by_channel[channel]
        )
        filters.append(
            f"[source{channel}]pan=mono|c0=c{channel},"
            f"volume='{gain}':eval=frame,"
            "alimiter=limit=0.95:level=false:latency=true"
            f"[repair{channel}]"
        )
        output_labels.append(f"[repair{channel}]")
    filters.append(
        f"{''.join(output_labels)}amix=inputs={len(output_labels)}:"
        "duration=first:normalize=0,"
        "pan=stereo|c0=c0|c1=c0[out]"
    )
    command = [
        str(decoder),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-c:a",
        "pcm_f32le",
        "-ar",
        "48000",
        str(destination),
    ]
    _checked_ffmpeg(command, "Audio repair candidate render failed")


def _master_candidate(raw: Path, work_dir: Path, config: dict) -> Path:
    from lib.audio_enhance import enhance_audio

    mastered = work_dir / "candidate-mastered.wav"
    result = enhance_audio(raw, mastered, config)
    if not result.is_file():
        raise RuntimeError("Audio repair candidate mastering did not produce output")
    if audio_processing_settings(config).get("audio_enhance", True) and (
        result.resolve() != mastered.resolve()
    ):
        raise RuntimeError("Audio repair candidate mastering failed")
    return result


def _config_fingerprint(config: dict) -> str:
    return json_fingerprint(audio_processing_settings(config))


def _bounded_asr_evidence(plan: dict, path: str | Path | None) -> dict:
    if path is None:
        return {"status": "not_performed"}
    evidence_path = Path(path)
    try:
        evidence = json.loads(evidence_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Bounded ASR evidence is unreadable: {evidence_path}"
        ) from exc
    entries = {
        entry["id"]: entry
        for entry in plan.get("repairs", []) + plan.get("held_out_controls", [])
    }
    results = evidence.get("results", [])
    result_ids = {item.get("entry_id") for item in results}
    if not result_ids or not result_ids.issubset(entries):
        raise ValueError("Bounded ASR evidence does not match the repair plan")
    expected_key = {
        "source": "original_fingerprint",
        "grounded_fallback": "fallback_fingerprint",
    }
    fingerprints_bound = all(
        item.get("variant") in expected_key
        and item.get("preview_fingerprint")
        == entries[item["entry_id"]]["verification"][expected_key[item["variant"]]][
            "id"
        ]
        for item in results
    )
    paired_ids = {
        entry_id
        for entry_id in result_ids
        if {item.get("variant") for item in results if item.get("entry_id") == entry_id}
        == set(expected_key)
    }
    if evidence.get("repair_plan_fingerprint") != plan.get("fingerprint") or not (
        fingerprints_bound and paired_ids == result_ids
    ):
        raise ValueError("Bounded ASR evidence is not bound to current preview bytes")
    return {
        "status": "bounded_samples_checked",
        "full_episode_transcription": False,
        "checked_entry_count": len(result_ids),
        "repair_entry_count": len(plan.get("repairs", [])),
        "provider": evidence.get("provider"),
        "model": evidence.get("model"),
        "path": str(evidence_path.resolve()),
        "fingerprint": file_fingerprint(evidence_path),
        "comparisons": evidence.get("comparisons", []),
    }


def _verify_full_candidate(
    candidate: Path,
    current_master: Path,
    repairs: list[dict],
    controls: list[dict],
    master_before: dict,
    master_after: dict,
    decoder: Path,
    config: dict,
) -> dict:
    candidate_probe, master_probe = ffprobe(candidate), ffprobe(current_master)
    candidate_stream, master_stream = (
        get_audio_stream(candidate_probe),
        get_audio_stream(master_probe),
    )
    duration = lambda probe, stream: float(
        stream.get("duration") or probe.get("format", {}).get("duration", 0)
    )
    candidate_duration, master_duration = (
        duration(candidate_probe, candidate_stream),
        duration(master_probe, master_stream),
    )
    sample_rate = 2000
    existing = _decode_samples(current_master, decoder, sample_rate)
    repaired = _decode_samples(candidate, decoder, sample_rate)
    count = min(len(existing), len(repaired))
    existing, repaired = existing[:count], repaired[:count]
    repair_windows = []
    for entry in repairs:
        mask = _interval_mask(count, sample_rate, entry["repair_intervals"])
        delta_rms, output_rms = (
            _rms(repaired[mask] - existing[mask]),
            _rms(repaired[mask]),
        )
        peak = float(np.max(np.abs(repaired[mask]))) if np.any(mask) else math.inf
        predecessor = bool(entry.get("predecessor_entry_id"))
        window = {
            "entry_id": entry["id"],
            "finding_ids": entry["finding_ids"],
            "expectation": (
                "preserve_selected_repair" if predecessor else "apply_new_repair"
            ),
            "delta_rms": round(delta_rms, 9),
            "rms_dbfs": _amplitude_dbfs(output_rms),
            "peak": round(peak, 9),
        }
        if predecessor:
            correlation = _correlation(existing[mask], repaired[mask])
            level_delta = _amplitude_dbfs(output_rms) - _amplitude_dbfs(
                _rms(existing[mask])
            )
            passed = correlation >= 0.999 and abs(level_delta) <= 0.25
            window.update(
                correlation=round(correlation, 9),
                level_delta_db=round(level_delta, 4),
            )
        else:
            passed = delta_rms > 1e-5 and output_rms > 10 ** (-60 / 20) and peak <= 1.0
        window["status"] = "pass" if passed else "failed"
        repair_windows.append(window)
    control_windows = []
    for entry in controls:
        mask = _interval_mask(count, sample_rate, [entry["source_time"]], padding=0.5)
        correlation = _correlation(existing[mask], repaired[mask])
        level_delta = _amplitude_dbfs(_rms(repaired[mask])) - _amplitude_dbfs(
            _rms(existing[mask])
        )
        passed = correlation >= 0.999 and abs(level_delta) <= 0.25
        control_windows.append(
            {
                "entry_id": entry["id"],
                "status": "pass" if passed else "failed",
                "correlation": round(correlation, 9),
                "level_delta_db": round(level_delta, 4),
            }
        )
    format_ok = (
        candidate_stream.get("codec_name") in {"pcm_s24le", "pcm_f32le"}
        and int(candidate_stream.get("sample_rate", 0)) == 48000
        and int(candidate_stream.get("channels", 0)) == 2
    )
    processing = audio_processing_settings(config)
    loudness = {"status": "not_required"}
    loudness_ok = True
    if processing.get("audio_enhance", True):
        from lib.audio_enhance import _measure_loudness

        targets = {
            "integrated_lufs": float(processing.get("audio_target_lufs", -16)),
            "true_peak_dbtp": float(processing.get("audio_target_tp", -1.0)),
            "loudness_range_lu": float(processing.get("audio_target_lra", 7)),
        }
        measured = _measure_loudness(
            candidate,
            "",
            targets["integrated_lufs"],
            targets["true_peak_dbtp"],
            targets["loudness_range_lu"],
        )
        loudness_ok = bool(
            measured
            and abs(float(measured["input_i"]) - targets["integrated_lufs"]) <= 0.5
            and float(measured["input_tp"]) <= targets["true_peak_dbtp"] + 0.2
        )
        loudness = {
            "status": "pass" if loudness_ok else "failed",
            "targets": targets,
            "measured": measured,
        }
    checks = [
        _check("selected_master_preserved", master_before == master_after),
        _check(
            "duration_matches_selected_master",
            abs(candidate_duration - master_duration) <= 0.02,
            candidate_seconds=round(candidate_duration, 6),
            selected_master_seconds=round(master_duration, 6),
        ),
        _check("audio_format_is_delivery_safe_pcm", format_ok),
        _check("master_loudness_and_true_peak", loudness_ok),
        _check(
            "every_repair_operation_verified",
            bool(repair_windows) and all(x["status"] == "pass" for x in repair_windows),
            checked_count=len(repair_windows),
            new_count=sum(
                item["expectation"] == "apply_new_repair" for item in repair_windows
            ),
            preserved_count=sum(
                item["expectation"] == "preserve_selected_repair"
                for item in repair_windows
            ),
        ),
        _check(
            "held_out_timing_and_level_preserved",
            bool(control_windows)
            and all(x["status"] == "pass" for x in control_windows),
            checked_count=len(control_windows),
        ),
    ]
    return {
        "status": "pass" if all(x["pass"] for x in checks) else "failed",
        "clock": "source",
        "sample_rate": sample_rate,
        "checks": checks,
        "repair_windows": repair_windows,
        "held_out_controls": control_windows,
        "loudness": loudness,
        "limitations": [
            "Signal, timing, level, and artifact checks are objective; human listening was not performed."
        ],
    }


def _interval_mask(
    count: int, sample_rate: int, intervals: list[dict], padding: float = 0
) -> np.ndarray:
    mask = np.zeros(count, dtype=bool)
    for interval in intervals:
        start = max(
            0, round((float(interval["start_seconds"]) - padding) * sample_rate)
        )
        end = min(
            count, round((float(interval["end_seconds"]) + padding) * sample_rate)
        )
        mask[start:end] = True
    return mask


def _rms(samples: np.ndarray) -> float:
    if not len(samples):
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def _amplitude_dbfs(value: float) -> float:
    return round(20 * math.log10(max(value, 1e-12)), 3)


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) < 2 or np.std(left) == 0 or np.std(right) == 0:
        return 0.0
    return float(np.corrcoef(left, right)[0, 1])


def _validate_repair_finding(finding: dict) -> None:
    eligible, reason = _automatic_repair_eligibility(finding)
    if not eligible:
        raise ValueError(
            f"Finding {finding.get('id')} is not eligible for repair: {reason}"
        )


def _validate_report_dependencies(report: dict, episode: dict) -> None:
    for checked in report.get("scope", {}).get("checked_inputs", []):
        role = checked.get("role")
        if role == "transcript":
            path = Path(checked.get("path", ""))
            transcript = _read_json(path)
            confidence = float(
                report.get("detector", {})
                .get("settings", {})
                .get("transcript_confidence", 0.55)
            )
            if not transcript or transcript_analysis_fingerprint(
                path, transcript, confidence
            )["id"] != checked.get("fingerprint"):
                raise ValueError("Audio quality report is stale for the transcript")
        elif role == "source_timeline_edits" and checked.get("value") != episode.get(
            "longform_edits", []
        ):
            raise ValueError("Audio quality report is stale for the edit timeline")


def _group_repair_findings(
    findings: list[dict], maximum_gap: float
) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for finding in sorted(
        findings, key=lambda item: item["source_time"]["start_seconds"]
    ):
        if (
            groups
            and groups[-1][-1]["channel"] == finding["channel"]
            and groups[-1][-1].get("_repair_scope") == "full_finding"
            and finding.get("_repair_scope") == "full_finding"
            and finding["source_time"]["start_seconds"]
            - groups[-1][-1]["source_time"]["end_seconds"]
            <= maximum_gap
        ):
            groups[-1].append(finding)
        else:
            groups.append([finding])
    return groups


def _combined_finding(group: list[dict]) -> dict:
    first, last = group[0], group[-1]
    ids = [item["id"] for item in group]
    signature = json.dumps(ids, separators=(",", ":"))
    start = float(first["source_time"]["start_seconds"])
    end = float(last["source_time"]["end_seconds"])
    gains = [
        float(item.get("evidence", {}).get("estimated_recovery_gain_db", 0))
        for item in group
    ]
    intervals = []
    seen_intervals = set()
    for item in group:
        for interval in item.get("_repair_intervals", [item["source_time"]]):
            key = (
                float(interval["start_seconds"]),
                float(interval["end_seconds"]),
            )
            if key not in seen_intervals:
                intervals.append(interval)
                seen_intervals.add(key)
    return {
        "id": "repair_" + hashlib.sha256(signature.encode()).hexdigest()[:16],
        "channel": first["channel"],
        "source_time": {
            "start_seconds": start,
            "end_seconds": end,
            "duration_seconds": round(end - start, 6),
        },
        "repair_intervals": intervals,
        "evidence": {"estimated_recovery_gain_db": float(np.median(gains))},
        "preview": {
            "padding_seconds": max(
                float(item.get("preview", {}).get("padding_seconds", 2))
                for item in group
            )
        },
    }


def _render_plan_entry(
    source: Path,
    group: list[dict],
    output_dir: Path,
    decoder: Path,
    *,
    control: bool,
) -> dict:
    combined = _combined_finding(group)
    calibration = _calibrate_recovery_gain(
        source,
        combined,
        decoder,
        allow_adaptive_gain=(
            not control
            and all(_automatic_repair_eligibility(item)[0] for item in group)
            and all(item.get("_repair_scope") == "full_finding" for item in group)
        ),
    )
    combined["evidence"]["estimated_recovery_gain_db"] = calibration["applied_gain_db"]
    activity_scoped = any(
        item.get("_repair_scope") == "transcript_activity" for item in group
    )
    fade_modes = {
        item.get("_fade_mode", "contained" if activity_scoped else "cross_boundary")
        for item in group
    }
    if len(fade_modes) != 1:
        raise ValueError("Grouped repair findings use incompatible fade modes")
    fade_mode = fade_modes.pop()
    combined["recovery"] = {
        "maximum_gain_db": calibration["maximum_gain_db"],
        "fade_mode": fade_mode,
    }
    preview = render_finding_preview(source, combined, output_dir, ffmpeg_bin=decoder)
    verification = _verify_preview_pair(
        combined,
        preview,
        decoder,
        target_voiced_dbfs=calibration["target_mix_voiced_dbfs"],
    )
    contribution_change = calibration["applied_gain_db"] - UNCHANGED_SURVIVOR_GAIN_DB
    verification["metrics"]["surviving_contribution_change_db"] = round(
        contribution_change, 3
    )
    if contribution_change < -0.05:
        verification["status"] = "failed"
    return {
        "id": combined["id"],
        "finding_ids": [item["id"] for item in group],
        "source_time": combined["source_time"],
        "repair_intervals": combined["repair_intervals"],
        "dropped_channel": combined["channel"],
        "surviving_channel": 1 - combined["channel"],
        "gain_db": round(float(combined["evidence"]["estimated_recovery_gain_db"]), 2),
        "gain_calibration": calibration,
        "repair_scope": "transcript_activity" if activity_scoped else "full_finding",
        "fade_mode": fade_mode,
        "predecessor_entry_id": next(
            (
                item.get("_predecessor_entry_id")
                for item in group
                if item.get("_predecessor_entry_id")
            ),
            None,
        ),
        "suppression_reason": group[0].get("suppression_reason") if control else None,
        "preview": preview,
        "verification": verification,
    }


def _held_out_candidates(
    report: dict, repair_groups: list[list[dict]], count: int
) -> list[dict]:
    if count == 0:
        return []
    repair_ranges = [
        (
            group[0]["source_time"]["start_seconds"],
            group[-1]["source_time"]["end_seconds"],
        )
        for group in repair_groups
    ]
    candidates = [
        item
        for item in report.get("analysis", {}).get("suppressed_candidates", [])
        if item.get("edited_time", {}).get("status") in {"retained", "split"}
        and item.get("suppression_reason") == "transcript_supports_surviving_channel"
        and all(
            abs(item["source_time"]["start_seconds"] - start) >= 10
            and abs(item["source_time"]["end_seconds"] - end) >= 10
            for start, end in repair_ranges
        )
    ]
    candidates.sort(
        key=lambda item: (
            -item["source_time"]["duration_seconds"],
            item["source_time"]["start_seconds"],
        )
    )
    selected = []
    for channel in (0, 1):
        match = next((item for item in candidates if item["channel"] == channel), None)
        if match is not None and len(selected) < count:
            selected.append(match)
    selected_ids = {item["id"] for item in selected}
    for item in candidates:
        if len(selected) >= count:
            break
        if item["id"] not in selected_ids:
            selected.append(item)
    return selected


def _verify_preview_pair(
    finding: dict, preview: dict, decoder: Path, *, target_voiced_dbfs: float
) -> dict:
    original_path, fallback_path = (
        Path(preview["original"]),
        Path(preview["grounded_fallback"]),
    )
    original_probe, fallback_probe = ffprobe(original_path), ffprobe(fallback_path)
    sample_rate = int(get_audio_stream(fallback_probe)["sample_rate"])
    original = _decode_samples(original_path, decoder, sample_rate)
    fallback = _decode_samples(fallback_path, decoder, sample_rate)
    count = min(len(original), len(fallback))
    original, fallback = original[:count], fallback[:count]
    source_time, padding = (
        finding["source_time"],
        float(finding["preview"]["padding_seconds"]),
    )
    window_start = max(0.0, float(source_time["start_seconds"]) - padding)
    intervals = [
        (
            float(x["start_seconds"]) - window_start,
            float(x["end_seconds"]) - window_start,
        )
        for x in finding.get("repair_intervals") or [source_time]
    ]
    issue = np.zeros(count, dtype=bool)
    context = np.ones(count, dtype=bool)
    healthy = np.zeros(count, dtype=bool)
    margin = 0.1
    for left, right in intervals:
        first, last = (
            max(0, round((left - margin) * sample_rate)),
            min(count, round((right + margin) * sample_rate)),
        )
        context[first:last] = False
        issue[
            max(0, round(left * sample_rate)) : min(count, round(right * sample_rate))
        ] = True
    for (_, left), (right, _) in pairwise(intervals):
        first, last = (
            round((left + margin) * sample_rate),
            round((right - margin) * sample_rate),
        )
        if last > first:
            healthy[first:last] = True
    level_delta = (
        _voiced_percentile_dbfs(fallback, issue, sample_rate, percentile=75)
        - target_voiced_dbfs
    )
    metrics = {
        "duration_error_seconds": round(
            max(
                abs(
                    float(original_probe["format"]["duration"])
                    - (float(source_time["end_seconds"]) + padding - window_start)
                ),
                abs(
                    float(fallback_probe["format"]["duration"])
                    - (float(source_time["end_seconds"]) + padding - window_start)
                ),
            ),
            6,
        ),
        "sample_count_delta": abs(len(original) - len(fallback)),
        "context_max_absolute_delta": round(
            float(np.max(np.abs(fallback[context] - original[context])))
            if np.any(context)
            else math.inf,
            9,
        ),
        "repair_delta_rms": round(_rms(fallback[issue] - original[issue]), 9),
        "fallback_peak": round(float(np.max(np.abs(fallback))), 9),
        "limited_sample_fraction": round(
            float(np.mean(np.abs(fallback[issue]) >= 0.949)) if np.any(issue) else 1.0,
            9,
        ),
        "healthy_gap_max_absolute_delta": round(
            float(np.max(np.abs(fallback[healthy] - original[healthy])))
            if np.any(healthy)
            else 0.0,
            9,
        ),
        "level_delta_db": round(level_delta, 3),
    }
    contained = finding.get("recovery", {}).get("fade_mode") == "contained"
    if contained:
        metrics["outside_repair_max_absolute_delta"] = round(
            float(np.max(np.abs(fallback[~issue] - original[~issue])))
            if np.any(~issue)
            else math.inf,
            9,
        )
    passed = (
        metrics["duration_error_seconds"] <= 0.02
        and metrics["sample_count_delta"] <= 1
        and metrics["context_max_absolute_delta"] <= 2e-6
        and metrics["repair_delta_rms"] > 1e-6
        and metrics["fallback_peak"] <= 0.951
        and metrics["limited_sample_fraction"] <= 0.005
        and metrics["healthy_gap_max_absolute_delta"] <= 2e-6
        and abs(level_delta) <= 1.5
        and (not contained or metrics["outside_repair_max_absolute_delta"] <= 2e-6)
    )
    return {
        "status": "pass" if passed else "failed",
        "clock": "source",
        "sample_rate": sample_rate,
        "metrics": metrics,
        "original_fingerprint": media_fingerprint(original_path, original_probe),
        "fallback_fingerprint": media_fingerprint(fallback_path, fallback_probe),
    }


def _calibrate_recovery_gain(
    source: Path,
    finding: dict,
    decoder: Path,
    *,
    allow_adaptive_gain: bool,
) -> dict:
    source_time = finding["source_time"]
    overall_start = float(source_time["start_seconds"])
    overall_end = float(source_time["end_seconds"])
    window_start = max(0.0, overall_start - 8)
    window_end = overall_end + 8
    sample_rate = 8000
    samples = _decode_samples(
        source,
        decoder,
        sample_rate,
        window_start,
        window_end - window_start,
        stereo=True,
        source_timeline=True,
    )
    samples_per_frame = round(sample_rate * 0.02)
    frame_count = len(samples) // samples_per_frame
    frames = samples[: frame_count * samples_per_frame].reshape(
        frame_count, samples_per_frame, 2
    )
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    dbfs = 20 * np.log10(np.maximum(rms, 1e-12))
    mixed = np.mean(frames, axis=2)
    mixed_rms = np.sqrt(np.mean(mixed.astype(np.float64) ** 2, axis=1))
    mixed_dbfs = 20 * np.log10(np.maximum(mixed_rms, 1e-12))
    frame_times = window_start + (np.arange(frame_count) + 0.5) * 0.02
    dropped = int(finding["channel"])
    survivor = 1 - dropped
    issue_mask = np.zeros(frame_count, dtype=bool)
    sample_issue_mask = np.zeros(len(samples), dtype=bool)
    for interval in finding.get("repair_intervals") or [source_time]:
        start = float(interval["start_seconds"])
        end = float(interval["end_seconds"])
        issue_mask |= (frame_times >= start) & (frame_times < end)
        sample_issue_mask[
            max(0, round((start - window_start) * sample_rate)) : min(
                len(samples), round((end - window_start) * sample_rate)
            )
        ] = True
    context = ~issue_mask
    dominant_context = (
        context & (dbfs[:, dropped] >= dbfs[:, survivor] + 3) & (dbfs[:, dropped] > -55)
    )
    if np.count_nonzero(dominant_context) < 10:
        dominant_context = context & (dbfs[:, dropped] > -55)
    if np.count_nonzero(dominant_context) < 10 or not np.any(issue_mask):
        raise ValueError(f"Finding {finding.get('id')} lacks voiced gain context")

    target_dbfs = float(np.percentile(mixed_dbfs[dominant_context], 75))
    survivor_levels = dbfs[issue_mask, survivor]
    survivor_dbfs = float(np.percentile(survivor_levels, 75))
    survivor_p20_dbfs = float(np.percentile(survivor_levels, 20))
    level_spread_db = survivor_dbfs - survivor_p20_dbfs
    requested_gain = target_dbfs - survivor_dbfs
    survivor_peak = float(np.max(np.abs(samples[sample_issue_mask, survivor])))
    peak_limited_gain = (
        20 * math.log10(0.95 / survivor_peak) if survivor_peak > 0 else -math.inf
    )
    maximum_gain = (
        18.0
        if allow_adaptive_gain and requested_gain > 13.5 and level_spread_db >= 8.0
        else 12.0
    )
    applied_gain = min(maximum_gain, max(-12.0, requested_gain))
    return {
        "method": "p75_clean_dominant_mix_to_surviving_channel/v2",
        "context_seconds": 8,
        "normal_mix_channel_weight": 0.5,
        "target_mix_voiced_dbfs": round(target_dbfs, 3),
        "surviving_channel_voiced_dbfs": round(survivor_dbfs, 3),
        "surviving_channel_p20_dbfs": round(survivor_p20_dbfs, 3),
        "within_interval_level_spread_db": round(level_spread_db, 3),
        "adaptive_gain_evidence": "heuristic_level_spread_with_transcript_and_preview_checks",
        "requested_gain_db": round(requested_gain, 3),
        "peak_limited_gain_db": round(peak_limited_gain, 3),
        "applied_gain_db": round(applied_gain, 3),
        "maximum_gain_db": maximum_gain,
        "peak_control": "lookahead_limiter_0.95_no_auto_level",
        "context_frame_count": int(np.count_nonzero(dominant_context)),
        "issue_frame_count": int(np.count_nonzero(issue_mask)),
    }


def _voiced_percentile_dbfs(
    samples: np.ndarray,
    mask: np.ndarray,
    sample_rate: int,
    *,
    percentile: float,
) -> float:
    samples_per_frame = round(sample_rate * 0.02)
    frame_count = min(len(samples), len(mask)) // samples_per_frame
    if frame_count == 0:
        return -math.inf
    frames = samples[: frame_count * samples_per_frame].reshape(
        frame_count, samples_per_frame
    )
    frame_mask = mask[: frame_count * samples_per_frame].reshape(
        frame_count, samples_per_frame
    )
    selected = np.any(frame_mask, axis=1)
    rms = np.sqrt(np.mean(frames[selected].astype(np.float64) ** 2, axis=1))
    return float(np.percentile(20 * np.log10(np.maximum(rms, 1e-12)), percentile))


def _decode_samples(
    path: Path,
    decoder: Path,
    sample_rate: int,
    start: float | None = None,
    duration: float | None = None,
    *,
    stereo: bool = False,
    source_timeline: bool = False,
) -> np.ndarray:
    command = [str(decoder), "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start is not None:
        command += ["-ss", f"{start:.6f}"]
    if duration is not None:
        command += ["-t", f"{duration:.6f}"]
    channels = 2 if stereo else 1
    filters = []
    if source_timeline:
        filters.append(CAMERA_AUDIO_TIMELINE_FILTER)
    if stereo:
        filters.append(
            f"aformat=sample_fmts=flt:sample_rates={sample_rate}:channel_layouts=stereo"
        )
    else:
        # The artifacts decoded here are dual-mono. FFmpeg's default stereo
        # downmix applies a +3 dB coefficient; an explicit average preserves
        # the scalar level used by the source-channel calibration.
        filters.append(
            f"aformat=sample_fmts=flt:sample_rates={sample_rate}:"
            "channel_layouts=stereo,pan=mono|c0=0.5*c0+0.5*c1"
        )
    command += [
        "-i",
        str(path),
        "-vn",
        "-af",
        ",".join(filters),
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(
            "Audio evidence decode failed: "
            + result.stderr.decode(errors="replace")[-1000:]
        )
    values = np.frombuffer(result.stdout, dtype="<f4")
    if not stereo:
        return values
    return values[: len(values) // 2 * 2].reshape(-1, 2)
