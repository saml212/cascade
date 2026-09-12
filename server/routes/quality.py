"""Read-only quality reports and bounded evidence previews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agents.qa import AUDIO_REPORT_PATH, QUALITY_REPORT_PATH, quality_snapshot
from lib.audio_mix import clear_audio_selection, current_audio_selection
from lib.audio_qa import PREVIEW_ALGORITHM_VERSION, render_finding_preview
from lib.audio_repair import (
    AUTO_REPAIR_POLICY,
    REPAIR_CANDIDATE_PATH,
    REPAIR_PLAN_PATH,
    build_audio_repair_plan,
    render_audio_repair_candidate,
    select_audio_repair_candidate,
    select_grounded_repair_findings,
)
from lib.paths import get_episodes_dir

router = APIRouter(prefix="/api/episodes", tags=["quality"])
EPISODES_DIR = get_episodes_dir()
MAX_FINDING_PREVIEW_SECONDS = 120.0
AUDIO_REPAIR_CACHE_ROOT = Path(
    os.getenv(
        "CASCADE_AUDIO_REPAIR_CACHE",
        str(Path(tempfile.gettempdir()) / "cascade-audio-repair"),
    )
)
_preview_locks: dict[str, threading.Lock] = {}
_preview_locks_guard = threading.Lock()


class AudioRepairPlanRequest(BaseModel):
    """Optional finding selection for an evidence review iteration."""

    finding_ids: list[str] | None = None
    exclude_finding_ids: list[str] = []


def _episode_dir(episode_id: str) -> Path:
    root = EPISODES_DIR.resolve()
    episode_dir = (root / episode_id).resolve()
    if episode_dir.parent != root or not (episode_dir / "episode.json").is_file():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return episode_dir


def _read_report(path: Path, label: str) -> dict:
    try:
        report = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"{label} has not been generated"
        ) from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"{label} is unreadable") from exc
    if not isinstance(report, dict):
        raise HTTPException(status_code=500, detail=f"{label} is invalid")
    return report


def _preview_lock(key: str) -> threading.Lock:
    with _preview_locks_guard:
        return _preview_locks.setdefault(key, threading.Lock())


def _call_locked(key: str, function, *args, **kwargs):
    with _preview_lock(key):
        return function(*args, **kwargs)


async def _run_quality_job(key: str, function, *args, **kwargs):
    """Run one blocking quality operation and map its domain errors."""
    try:
        return await asyncio.to_thread(_call_locked, key, function, *args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _render_cached_preview(episode_dir: Path, report: dict, finding: dict) -> dict:
    cache_key = json.dumps(
        {
            "algorithm": PREVIEW_ALGORITHM_VERSION,
            "report": report.get("fingerprint"),
            "finding": finding,
            "source": report.get("source", {}).get("fingerprint"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(cache_key.encode()).hexdigest()[:20]
    preview_dir = episode_dir / "work" / "quality_previews" / digest
    stem = str(finding["id"]).replace(":", "-")
    rendered = {
        "original": str((preview_dir / f"{stem}-source.wav").resolve()),
        "grounded_fallback": str(
            (preview_dir / f"{stem}-grounded-fallback.wav").resolve()
        ),
    }
    with _preview_lock(f"{episode_dir.name}:{digest}"):
        if all(
            Path(path).is_file() and Path(path).stat().st_size > 0
            for path in rendered.values()
        ):
            return rendered
        return render_finding_preview(
            episode_dir / "source_merged.mp4", finding, preview_dir
        )


def _bound_audio_response(
    path: Path, root: Path, recorded: dict, filename: str, label: str
) -> FileResponse:
    path = path.resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise HTTPException(status_code=409, detail=f"{label} is unavailable")
    stat = path.stat()
    if (
        recorded.get("size_bytes") != stat.st_size
        or recorded.get("mtime_ns") != stat.st_mtime_ns
    ):
        raise HTTPException(status_code=409, detail=f"{label} evidence is stale")
    return FileResponse(path, media_type="audio/wav", filename=filename)


@router.get("/{episode_id}/quality")
async def get_quality(episode_id: str) -> dict:
    """Return the current revision, findings, artifacts, approvals, and blockers."""
    return quality_snapshot(_episode_dir(episode_id))


@router.get("/{episode_id}/quality/report")
async def get_quality_report(episode_id: str) -> dict:
    """Return the persisted QA-agent report without hiding failed checks."""
    episode_dir = _episode_dir(episode_id)
    return _read_report(episode_dir / QUALITY_REPORT_PATH, "Quality report")


@router.get("/{episode_id}/audio-qc")
async def get_audio_quality_report(episode_id: str) -> dict:
    """Return source-clock continuity evidence for agent or human review."""
    episode_dir = _episode_dir(episode_id)
    return _read_report(episode_dir / AUDIO_REPORT_PATH, "Audio quality report")


@router.get("/{episode_id}/audio-qc/repair-plan")
async def get_audio_repair_plan(episode_id: str) -> dict:
    """Return the current bounded recovery plan and its verification evidence."""
    episode_dir = _episode_dir(episode_id)
    return _read_report(episode_dir / REPAIR_PLAN_PATH, "Audio repair plan")


@router.post("/{episode_id}/audio-qc/repair-plan")
async def create_audio_repair_plan(
    episode_id: str, request: AudioRepairPlanRequest | None = None
) -> dict:
    """Build a conservative plan and fully disposition the current findings."""
    episode_dir = _episode_dir(episode_id)
    report = _read_report(episode_dir / AUDIO_REPORT_PATH, "Audio quality report")
    automatic_ids = select_grounded_repair_findings(report)
    finding_ids = (
        list(dict.fromkeys(request.finding_ids))
        if request and request.finding_ids is not None
        else automatic_ids
    )
    excluded = set(request.exclude_finding_ids if request else [])
    finding_ids = [
        finding_id for finding_id in finding_ids if finding_id not in excluded
    ]
    if not finding_ids:
        raise HTTPException(
            status_code=409,
            detail="No findings meet the grounded automatic-repair policy",
        )
    predecessor_plan = None
    plan_path = episode_dir / REPAIR_PLAN_PATH
    selected_plan_fingerprint = (
        report.get("scope", {})
        .get("selected_mix_provenance", {})
        .get("repair_selection", {})
        .get("repair_plan_fingerprint")
    )
    if selected_plan_fingerprint and plan_path.is_file():
        existing = _read_report(plan_path, "Audio repair plan")
        if existing.get("fingerprint") == selected_plan_fingerprint:
            predecessor_plan = existing
    return await _run_quality_job(
        f"{episode_id}:audio-repair-plan",
        build_audio_repair_plan,
        report,
        finding_ids,
        episode_dir / REPAIR_PLAN_PATH.parent,
        predecessor_plan=predecessor_plan,
        selection_policy=AUTO_REPAIR_POLICY,
    )


@router.get("/{episode_id}/audio-qc/repair-candidate")
async def get_audio_repair_candidate(episode_id: str) -> dict:
    """Return the preserved full-audio candidate manifest."""
    episode_dir = _episode_dir(episode_id)
    return _read_report(episode_dir / REPAIR_CANDIDATE_PATH, "Audio repair candidate")


@router.post("/{episode_id}/audio-qc/repair-candidate")
async def create_audio_repair_candidate(episode_id: str) -> dict:
    """Render a review candidate in controlled cache without replacing the master."""
    from agents.pipeline import load_config

    episode_dir = _episode_dir(episode_id)
    report = _read_report(episode_dir / AUDIO_REPORT_PATH, "Audio quality report")
    plan = _read_report(episode_dir / REPAIR_PLAN_PATH, "Audio repair plan")
    output = AUDIO_REPAIR_CACHE_ROOT / episode_id / "audio-repair-candidate.wav"
    asr_path = episode_dir / REPAIR_PLAN_PATH.parent / "asr" / "summary.json"
    return await _run_quality_job(
        f"{episode_id}:audio-repair-candidate",
        render_audio_repair_candidate,
        report,
        plan,
        output,
        load_config(),
        asr_evidence_path=asr_path if asr_path.is_file() else None,
    )


@router.get("/{episode_id}/audio-qc/repair-candidate/audio")
async def get_audio_repair_candidate_file(episode_id: str):
    """Serve only the manifest-bound candidate from the controlled cache."""
    episode_dir = _episode_dir(episode_id)
    manifest = _read_report(
        episode_dir / REPAIR_CANDIDATE_PATH, "Audio repair candidate"
    )
    try:
        candidate = Path(manifest["candidate"]["path"]).resolve()
        recorded = manifest["candidate"]["fingerprint"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=500, detail="Repair candidate is invalid"
        ) from exc
    return _bound_audio_response(
        candidate,
        AUDIO_REPAIR_CACHE_ROOT / episode_id,
        recorded,
        f"{episode_id}-audio-repair-candidate.wav",
        "Repair candidate",
    )


@router.get("/{episode_id}/audio-qc/repair-selection")
async def get_audio_repair_selection(episode_id: str) -> dict:
    """Return the revision-bound audio source selected for future renders."""
    from agents.pipeline import load_config

    episode_dir = _episode_dir(episode_id)
    try:
        episode = _read_report(episode_dir / "episode.json", "Episode")
        selection = current_audio_selection(episode_dir, episode, load_config())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return selection or {"status": "not_selected", "release_safe": False}


@router.post("/{episode_id}/audio-qc/repair-candidate/select")
async def select_audio_repair(episode_id: str) -> dict:
    """Select the proven candidate without approving or replacing the base mix."""
    from agents.pipeline import load_config

    episode_dir = _episode_dir(episode_id)
    report = _read_report(episode_dir / AUDIO_REPORT_PATH, "Audio quality report")
    plan = _read_report(episode_dir / REPAIR_PLAN_PATH, "Audio repair plan")
    manifest = _read_report(
        episode_dir / REPAIR_CANDIDATE_PATH, "Audio repair candidate"
    )
    return await _run_quality_job(
        f"{episode_id}:audio-repair-selection",
        select_audio_repair_candidate,
        episode_dir,
        report,
        plan,
        manifest,
        load_config(),
        allowed_cache_root=AUDIO_REPAIR_CACHE_ROOT / episode_id,
    )


@router.delete("/{episode_id}/audio-qc/repair-selection")
async def delete_audio_repair_selection(episode_id: str) -> dict:
    """Clear the selected repair source while leaving the base mix intact."""
    episode_dir = _episode_dir(episode_id)
    removed = await asyncio.to_thread(
        _call_locked,
        f"{episode_id}:audio-repair-selection",
        clear_audio_selection,
        episode_dir,
    )
    return {"status": "not_selected", "removed": removed, "release_safe": False}


@router.get("/{episode_id}/audio-qc/repair-plan/{entry_id}/preview")
async def get_audio_repair_preview(
    episode_id: str,
    entry_id: str,
    variant: str = Query(pattern="^(source|grounded-fallback)$"),
):
    """Serve one plan-owned preview without accepting caller paths."""
    episode_dir = _episode_dir(episode_id)
    plan_path = episode_dir / REPAIR_PLAN_PATH
    plan = _read_report(plan_path, "Audio repair plan")
    entries = plan.get("repairs", []) + plan.get("held_out_controls", [])
    entry = next((item for item in entries if item.get("id") == entry_id), None)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Repair entry {entry_id} not found"
        )
    source_key = "original" if variant == "source" else "grounded_fallback"
    try:
        path = Path(entry["preview"][source_key]).resolve()
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=500, detail="Repair preview is invalid"
        ) from exc
    fingerprint_key = (
        "original_fingerprint" if variant == "source" else "fallback_fingerprint"
    )
    recorded = entry.get("verification", {}).get(fingerprint_key, {})
    return _bound_audio_response(
        path,
        plan_path.parent,
        recorded,
        f"{entry_id}-{variant}.wav",
        "Repair preview",
    )


@router.get("/{episode_id}/audio-qc/findings/{finding_id}/preview")
async def get_audio_finding_preview(
    episode_id: str,
    finding_id: str,
    variant: str = Query(pattern="^(source|grounded-fallback)$"),
):
    """Render one report-owned preview; callers cannot provide filesystem paths."""
    episode_dir = _episode_dir(episode_id)
    report = _read_report(episode_dir / AUDIO_REPORT_PATH, "Audio quality report")
    finding = next(
        (item for item in report.get("findings", []) if item.get("id") == finding_id),
        None,
    )
    if finding is None:
        raise HTTPException(status_code=404, detail=f"Finding {finding_id} not found")
    source = episode_dir / "source_merged.mp4"
    recorded_source = report.get("source", {}).get("fingerprint", {})
    try:
        source_stat = source.stat()
    except OSError as exc:
        raise HTTPException(
            status_code=404, detail="Source media is unavailable"
        ) from exc
    if (
        recorded_source.get("size_bytes") != source_stat.st_size
        or recorded_source.get("mtime_ns") != source_stat.st_mtime_ns
    ):
        raise HTTPException(
            status_code=409,
            detail="Audio quality report is stale for the current source media",
        )
    source_time = finding.get("source_time", {})
    padding = finding.get("preview", {}).get("padding_seconds", 0)
    try:
        start = float(source_time["start_seconds"])
        end = float(source_time["end_seconds"])
        padding = float(padding)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="Finding has invalid preview bounds"
        ) from exc
    duration = end - max(0.0, start - padding) + padding
    if (
        not all(math.isfinite(value) for value in (start, end, padding, duration))
        or start < 0
        or end <= start
        or padding < 0
        or duration > MAX_FINDING_PREVIEW_SECONDS
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Finding preview must be between 0 and {MAX_FINDING_PREVIEW_SECONDS:g} seconds",
        )

    try:
        rendered = await asyncio.to_thread(
            _render_cached_preview, episode_dir, report, finding
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    key = "original" if variant == "source" else "grounded_fallback"
    path = Path(rendered[key]).resolve()
    preview_root = (episode_dir / "work" / "quality_previews").resolve()
    if not path.is_relative_to(preview_root) or not path.is_file():
        raise HTTPException(
            status_code=500, detail="Preview renderer returned an invalid path"
        )
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=f"{finding_id}-{variant}.wav",
    )
