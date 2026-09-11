"""Read-only quality reports and bounded evidence previews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from agents.qa import AUDIO_REPORT_PATH, QUALITY_REPORT_PATH, quality_snapshot
from lib.audio_qa import render_finding_preview
from lib.paths import get_episodes_dir

router = APIRouter(prefix="/api/episodes", tags=["quality"])
EPISODES_DIR = get_episodes_dir()
MAX_FINDING_PREVIEW_SECONDS = 120.0
_preview_locks: dict[str, threading.Lock] = {}
_preview_locks_guard = threading.Lock()


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


def _render_cached_preview(episode_dir: Path, report: dict, finding: dict) -> dict:
    cache_key = json.dumps(
        {
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
