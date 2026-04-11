"""Edit list management — load/save/manipulate longform_edits in episode.json.

The edit list is a flat ordered list stored in episode.json["longform_edits"].
Each entry is one of:

    {"type": "cut",        "start_seconds": X, "end_seconds": Y, "reason": "..."}
    {"type": "trim_start", "seconds": X,                          "reason": "..."}
    {"type": "trim_end",   "seconds": X,                          "reason": "..."}

These are consumed by `_apply_edits()` in agents/longform_render.py at render time.

This module provides a thin wrapper for safe load/append/remove/list operations
plus search-driven helpers (find_and_propose_cut) and dry-run preview support.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib.atomic_write import atomic_write_json

logger = logging.getLogger("cascade")


VALID_EDIT_TYPES = {"cut", "trim_start", "trim_end"}


def load_edits(episode_dir: Path) -> list[dict]:
    """Load the current edit list from episode.json. Returns [] if none."""
    ep_file = episode_dir / "episode.json"
    if not ep_file.exists():
        return []
    try:
        with open(ep_file) as f:
            ep = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read episode.json: %s", e)
        return []
    return list(ep.get("longform_edits", []))


def save_edits(episode_dir: Path, edits: list[dict]) -> None:
    """Atomically save the edit list back into episode.json.

    Reads the file, updates the longform_edits key, writes atomically.
    """
    ep_file = episode_dir / "episode.json"
    if not ep_file.exists():
        raise FileNotFoundError(f"episode.json not found at {ep_file}")

    with open(ep_file) as f:
        ep = json.load(f)
    ep["longform_edits"] = edits
    atomic_write_json(ep_file, ep)


def add_cut(
    episode_dir: Path,
    start_seconds: float,
    end_seconds: float,
    reason: str = "",
) -> dict:
    """Append a cut edit. Returns the appended edit dict."""
    if end_seconds <= start_seconds:
        raise ValueError(f"end_seconds ({end_seconds}) must be > start_seconds ({start_seconds})")
    edits = load_edits(episode_dir)
    edit = {
        "type": "cut",
        "start_seconds": round(float(start_seconds), 3),
        "end_seconds": round(float(end_seconds), 3),
        "duration_removed": round(float(end_seconds - start_seconds), 3),
        "reason": reason,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    edits.append(edit)
    save_edits(episode_dir, edits)
    return edit


def add_trim_start(episode_dir: Path, seconds: float, reason: str = "") -> dict:
    """Set/replace the trim_start edit (only one allowed)."""
    edits = [e for e in load_edits(episode_dir) if e.get("type") != "trim_start"]
    edit = {
        "type": "trim_start",
        "seconds": round(float(seconds), 3),
        "reason": reason,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    edits.append(edit)
    save_edits(episode_dir, edits)
    return edit


def add_trim_end(episode_dir: Path, seconds: float, reason: str = "") -> dict:
    """Set/replace the trim_end edit (only one allowed)."""
    edits = [e for e in load_edits(episode_dir) if e.get("type") != "trim_end"]
    edit = {
        "type": "trim_end",
        "seconds": round(float(seconds), 3),
        "reason": reason,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    edits.append(edit)
    save_edits(episode_dir, edits)
    return edit


def remove_edit(episode_dir: Path, index: int) -> dict | None:
    """Remove the edit at the given index. Returns the removed edit or None."""
    edits = load_edits(episode_dir)
    if not 0 <= index < len(edits):
        return None
    removed = edits.pop(index)
    save_edits(episode_dir, edits)
    return removed


def clear_edits(episode_dir: Path) -> int:
    """Remove all edits. Returns the number cleared."""
    edits = load_edits(episode_dir)
    n = len(edits)
    save_edits(episode_dir, [])
    return n


def list_edits(episode_dir: Path) -> list[dict]:
    """Return the current edit list (alias for load_edits for clarity)."""
    return load_edits(episode_dir)


def total_time_removed(edits: list[dict]) -> float:
    """Sum of all cut durations (excluding trim_start/trim_end)."""
    return sum(
        e.get("duration_removed", e.get("end_seconds", 0) - e.get("start_seconds", 0))
        for e in edits
        if e.get("type") == "cut"
    )


def load_diarized(episode_dir: Path) -> dict | None:
    """Load diarized_transcript.json. Returns None if not yet generated."""
    path = episode_dir / "diarized_transcript.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def find_and_propose_cut(
    episode_dir: Path,
    query: str,
    max_results: int = 5,
) -> list[dict]:
    """Search the diarized transcript for `query` and propose cut ranges.

    Returns a list of proposal dicts:
        {
            "start_seconds": X,        # proposed cut start (sentence-aligned)
            "end_seconds": Y,          # proposed cut end
            "duration": Z,
            "speaker": int,
            "matched_text": "...",
            "context": "... «matched» ...",
            "score": 0-100,
            "method": "exact" | "fuzzy",
        }

    User picks one and passes to add_cut() to commit.
    """
    from lib.transcript_search import (
        flatten_transcript,
        hybrid_search,
        expand_to_sentence,
    )

    diarized = load_diarized(episode_dir)
    if not diarized:
        raise FileNotFoundError(
            "diarized_transcript.json not found. Run the transcribe agent first."
        )

    words = flatten_transcript(diarized)
    matches = hybrid_search(query, words, max_results=max_results)

    proposals = []
    for m in matches:
        cut_start, cut_end = expand_to_sentence(m, words, pad_seconds=0.3)
        proposals.append({
            "start_seconds": cut_start,
            "end_seconds": cut_end,
            "duration": round(cut_end - cut_start, 3),
            "speaker": m.speaker,
            "matched_text": m.matched_text,
            "context": m.context,
            "score": round(m.score, 1),
            "method": m.method,
        })
    return proposals
