"""Transcript search — find phrases or topics in a diarized transcript.

Three search tiers:
1. **Exact substring**: literal text match. Fastest, highest precision.
2. **Fuzzy** (RapidFuzz): handles typos, word-order shuffling, partial matches.
3. **Hybrid**: runs both, deduplicates, ranks by score.

The transcript is flattened to a single word stream so phrases that span
speaker turns or utterance boundaries are still found.

Each match returns the start/end timestamps of the matched word range, plus
context (the surrounding sentence) and confidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass

from lib.clips import clip_selection_status

logger = logging.getLogger("cascade")

CLIP_BOUNDARY_CONFIDENCE_THRESHOLD = 0.8
CLIP_BOUNDARY_TOLERANCE_SECONDS = 0.03
CLIP_BOUNDARY_SCHEMA = "cascade.clip-boundary-evidence/v1"


def _public_word(word: dict) -> dict:
    result = {
        key: word[key]
        for key in (
            "word",
            "punctuated_word",
            "start_seconds",
            "end_seconds",
            "confidence",
            "speaker",
            "utterance_index",
            "word_index",
            "suspect",
            "suspect_reasons",
            "alternatives",
        )
        if key in word
    }
    return result


def _boundary_words(diarized: dict) -> tuple[list[dict], int]:
    words = []
    invalid_count = 0
    word_index = 0
    for utterance_index, utterance in enumerate(diarized.get("utterances", [])):
        if not isinstance(utterance, dict):
            continue
        utterance_speaker = utterance.get("speaker")
        for raw in utterance.get("words", []):
            if not isinstance(raw, dict):
                invalid_count += 1
                continue
            try:
                start = float(raw["start"])
                end = float(raw["end"])
                raw_confidence = raw.get("confidence")
                confidence = (
                    float(raw_confidence) if raw_confidence is not None else None
                )
            except (KeyError, TypeError, ValueError):
                invalid_count += 1
                continue
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or end <= start
                or (
                    confidence is not None
                    and (not math.isfinite(confidence) or not 0 <= confidence <= 1)
                )
            ):
                invalid_count += 1
                continue
            words.append(
                {
                    "word": str(raw.get("word") or ""),
                    "punctuated_word": str(
                        raw.get("punctuated_word") or raw.get("word") or ""
                    ),
                    "start_seconds": start,
                    "end_seconds": end,
                    "confidence": confidence,
                    "speaker": raw.get("speaker", utterance_speaker),
                    "utterance_index": utterance_index,
                    "word_index": word_index,
                    **{
                        key: raw[key]
                        for key in ("suspect", "suspect_reasons", "alternatives")
                        if key in raw
                    },
                }
            )
            word_index += 1
    words.sort(
        key=lambda word: (
            word["start_seconds"],
            word["end_seconds"],
            word["word_index"],
        )
    )
    return words, invalid_count


def _clip_bounds_revision(clip_id: str, start: float, end: float) -> str:
    encoded = json.dumps(
        {
            "clip_id": clip_id,
            "start_seconds": start if math.isfinite(start) else None,
            "end_seconds": end if math.isfinite(end) else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def clip_boundary_evidence(
    diarized: dict,
    clips: list[dict],
    *,
    confidence_threshold: float = CLIP_BOUNDARY_CONFIDENCE_THRESHOLD,
    timestamp_tolerance_seconds: float = CLIP_BOUNDARY_TOLERANCE_SECONDS,
) -> dict:
    """Report confident ASR words materially cut by source-clock boundaries."""
    if not math.isfinite(confidence_threshold) or not 0 <= confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be between 0 and 1")
    if (
        not math.isfinite(timestamp_tolerance_seconds)
        or timestamp_tolerance_seconds < 0
    ):
        raise ValueError("timestamp_tolerance_seconds cannot be negative")
    words, invalid_word_count = _boundary_words(diarized)
    clip_results = []
    all_findings = []
    actionable_findings = []
    for clip in clips:
        if not isinstance(clip, dict) or not clip.get("id"):
            continue
        clip_id = str(clip["id"])
        selection_status = clip_selection_status(clip)
        try:
            start = float(clip["start_seconds"])
            end = float(clip["end_seconds"])
        except (KeyError, TypeError, ValueError):
            start, end = math.nan, math.nan
        revision = _clip_bounds_revision(clip_id, start, end)
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            clip_results.append(
                {
                    "clip_id": clip_id,
                    "status": "invalid_bounds",
                    "selection_status": selection_status,
                    "actionable": False,
                    "clip_revision": revision,
                    "start_seconds": start if math.isfinite(start) else None,
                    "end_seconds": end if math.isfinite(end) else None,
                    "finding_count": 0,
                    "low_confidence_straddle_count": 0,
                    "timestamp_tolerance_suppressed_count": 0,
                    "findings": [],
                }
            )
            continue

        clip_findings = []
        low_confidence_count = 0
        tolerance_suppressed_count = 0
        for boundary, boundary_seconds in (("start", start), ("end", end)):
            for word in words:
                if not (word["start_seconds"] < boundary_seconds < word["end_seconds"]):
                    continue
                before_boundary = boundary_seconds - word["start_seconds"]
                after_boundary = word["end_seconds"] - boundary_seconds
                if (
                    before_boundary < timestamp_tolerance_seconds
                    or after_boundary < timestamp_tolerance_seconds
                ):
                    tolerance_suppressed_count += 1
                    continue
                confidence = word["confidence"]
                if confidence is None or confidence < confidence_threshold:
                    low_confidence_count += 1
                    continue
                overlaps = [
                    other
                    for other in words
                    if other is not word
                    and other["start_seconds"] < word["end_seconds"]
                    and word["start_seconds"] < other["end_seconds"]
                ]
                finding_payload = {
                    "clip_id": clip_id,
                    "boundary": boundary,
                    "boundary_seconds": boundary_seconds,
                    "word": _public_word(word),
                    "spoken_before_boundary_seconds": round(before_boundary, 6),
                    "spoken_after_boundary_seconds": round(after_boundary, 6),
                    "timestamp_tolerance_seconds": timestamp_tolerance_seconds,
                    "overlapping_transcript_intervals": [
                        _public_word(other) for other in overlaps[:4]
                    ],
                    "overlapping_transcript_interval_count": len(overlaps),
                }
                finding_id = hashlib.sha256(
                    json.dumps(
                        finding_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                ).hexdigest()[:16]
                finding = {"id": f"clip_boundary_{finding_id}", **finding_payload}
                clip_findings.append(finding)
                all_findings.append(finding)
                if selection_status == "selected":
                    actionable_findings.append(finding)
        clip_results.append(
            {
                "clip_id": clip_id,
                "status": "review_required" if clip_findings else "clear",
                "selection_status": selection_status,
                "actionable": bool(clip_findings and selection_status == "selected"),
                "clip_revision": revision,
                "start_seconds": start,
                "end_seconds": end,
                "finding_count": len(clip_findings),
                "low_confidence_straddle_count": low_confidence_count,
                "timestamp_tolerance_suppressed_count": tolerance_suppressed_count,
                "findings": clip_findings,
            }
        )

    affected_ids = list(
        dict.fromkeys(finding["clip_id"] for finding in actionable_findings)
    )
    return {
        "schema": CLIP_BOUNDARY_SCHEMA,
        "clock": "source",
        "status": "review_required" if actionable_findings else "clear",
        "scope": "selected_nonrejected_clips",
        "confidence_threshold": confidence_threshold,
        "timestamp_tolerance_seconds": timestamp_tolerance_seconds,
        "finding_count": len(actionable_findings),
        "detail_finding_count": len(all_findings),
        "affected_clip_ids": affected_ids,
        "word_count": len(words),
        "invalid_word_count": invalid_word_count,
        "limitations": [
            "ASR word intervals are estimates; overlapping intervals can make a natural cut ambiguous.",
            "Words below the confidence threshold or without confidence are counted but are not findings.",
            "A word must extend by at least the timestamp tolerance on both sides of a boundary to become a finding.",
            "Smaller timestamp intersections are retained as suppressed counts, not findings.",
        ],
        "clips": clip_results,
    }


@dataclass
class Word:
    """A single word in a flattened transcript."""

    word: str  # the spoken text
    start: float  # seconds
    end: float  # seconds
    speaker: int  # speaker index (0, 1, 2, ...)
    utt_idx: int  # which utterance this word came from
    word_idx: int  # global word index in the flat stream


@dataclass
class Match:
    """A search result."""

    start: float  # seconds — match window start
    end: float  # seconds — match window end
    score: float  # 0-100, higher is better
    matched_text: str  # the actual matched substring
    context: str  # surrounding text (~50 chars on each side)
    speaker: int  # speaker index of the first matched word
    word_idx_start: int  # global word index of first matched word
    word_idx_end: int  # global word index of last matched word
    method: str = "exact"  # "exact" or "fuzzy"


def flatten_transcript(diarized: dict) -> list[Word]:
    """Flatten a diarized_transcript.json structure into a single word stream.

    Each word in the stream knows its global index, the utterance it came
    from, the speaker, and absolute timestamps. This lets phrase searches
    span speaker boundaries naturally.
    """
    words: list[Word] = []
    for utt_idx, utt in enumerate(diarized.get("utterances", [])):
        utt_speaker = utt.get("speaker", 0)
        for w in utt.get("words", []):
            words.append(
                Word(
                    word=str(w.get("word", "")).lower().strip(),
                    start=float(w.get("start", 0)),
                    end=float(w.get("end", 0)),
                    speaker=int(w.get("speaker", utt_speaker)),
                    utt_idx=utt_idx,
                    word_idx=len(words),
                )
            )
    return words


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s']", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _make_full_text(words: list[Word]) -> tuple[str, list[int]]:
    """Build a single text string from words and a char-index→word-index map.

    Returns (text, char_to_word_idx) where text is the joined words and
    char_to_word_idx[i] gives the word index of the word that character i
    belongs to.
    """
    parts = []
    char_to_word: list[int] = []
    for w in words:
        if parts:
            parts.append(" ")
            char_to_word.append(w.word_idx)  # space belongs to next word
        parts.append(w.word)
        char_to_word.extend([w.word_idx] * len(w.word))
    return "".join(parts), char_to_word


def search_exact(query: str, words: list[Word]) -> list[Match]:
    """Find all exact substring occurrences of `query` in the flat word stream.

    Case-insensitive, punctuation-insensitive. Returns a Match per occurrence.
    """
    if not words:
        return []

    query_norm = _normalize(query)
    if not query_norm:
        return []

    full_text, char_to_word = _make_full_text(words)

    matches: list[Match] = []
    start_idx = 0
    while True:
        found = full_text.find(query_norm, start_idx)
        if found == -1:
            break
        end_char = found + len(query_norm) - 1
        w_start = char_to_word[found]
        w_end = char_to_word[end_char]
        word_first = words[w_start]
        word_last = words[w_end]
        matched_text = " ".join(w.word for w in words[w_start : w_end + 1])
        context = _build_context(words, w_start, w_end)
        matches.append(
            Match(
                start=word_first.start,
                end=word_last.end,
                score=100.0,
                matched_text=matched_text,
                context=context,
                speaker=word_first.speaker,
                word_idx_start=w_start,
                word_idx_end=w_end,
                method="exact",
            )
        )
        start_idx = end_char + 1

    return matches


def search_fuzzy(
    query: str, words: list[Word], min_score: int = 70, max_results: int = 20
) -> list[Match]:
    """Fuzzy phrase search using RapidFuzz partial_ratio over a sliding window.

    For each window of N words around the query length, compute the partial
    ratio. Return the top windows with score >= min_score.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        logger.warning("rapidfuzz not installed — fuzzy search disabled")
        return []

    if not words:
        return []

    query_norm = _normalize(query)
    if not query_norm:
        return []

    query_words = query_norm.split()
    if not query_words:
        return []

    # Window size: query word count + a small buffer for fuzzy boundary slop
    window = max(len(query_words), 3) + 2

    candidates: list[tuple[float, int, int]] = []  # (score, w_start, w_end)
    for i in range(len(words) - window + 1):
        chunk_words = words[i : i + window]
        chunk_text = " ".join(w.word for w in chunk_words)
        score = fuzz.partial_ratio(query_norm, chunk_text)
        if score >= min_score:
            candidates.append((float(score), i, i + window - 1))

    # Sort by score, dedupe overlapping windows by keeping highest-scoring
    candidates.sort(key=lambda c: c[0], reverse=True)
    selected: list[tuple[float, int, int]] = []
    for score, w_start, w_end in candidates:
        # Skip if this window overlaps a previously selected (higher-scoring) one
        overlaps = any(
            not (w_end < s_start or w_start > s_end) for _, s_start, s_end in selected
        )
        if not overlaps:
            selected.append((score, w_start, w_end))
            if len(selected) >= max_results:
                break

    matches: list[Match] = []
    for score, w_start, w_end in selected:
        word_first = words[w_start]
        word_last = words[w_end]
        matched_text = " ".join(w.word for w in words[w_start : w_end + 1])
        context = _build_context(words, w_start, w_end)
        matches.append(
            Match(
                start=word_first.start,
                end=word_last.end,
                score=score,
                matched_text=matched_text,
                context=context,
                speaker=word_first.speaker,
                word_idx_start=w_start,
                word_idx_end=w_end,
                method="fuzzy",
            )
        )
    return matches


def hybrid_search(query: str, words: list[Word], max_results: int = 10) -> list[Match]:
    """Run exact + fuzzy search, dedupe, return ranked matches.

    Exact matches always rank above fuzzy. Within each tier, results are
    sorted by score (then by start time for stability).
    """
    exact = search_exact(query, words)
    fuzzy = search_fuzzy(query, words, min_score=70, max_results=max_results * 2)

    # Dedupe: drop fuzzy matches that overlap an exact match
    deduped_fuzzy: list[Match] = []
    for fm in fuzzy:
        overlaps_exact = any(
            not (
                fm.word_idx_end < em.word_idx_start
                or fm.word_idx_start > em.word_idx_end
            )
            for em in exact
        )
        if not overlaps_exact:
            deduped_fuzzy.append(fm)

    # Combine: exact first (highest priority), then fuzzy by score
    exact.sort(key=lambda m: m.start)
    deduped_fuzzy.sort(key=lambda m: m.score, reverse=True)
    combined = exact + deduped_fuzzy
    return combined[:max_results]


def expand_to_sentence(
    match: Match, words: list[Word], pad_seconds: float = 0.5
) -> tuple[float, float]:
    """Expand a match's time range to nearest sentence-like boundaries.

    Walks outward from the matched word range until it hits a long pause
    (>0.5s gap between words) or the start/end of an utterance. Returns
    (start, end) seconds with `pad_seconds` of padding on each side.

    This is what you want for cuts: tighter than a full utterance, but
    natural enough that the cut doesn't sound abrupt.
    """
    if not words:
        return match.start, match.end

    PAUSE_THRESHOLD = 0.5  # seconds — gap that defines a sentence boundary

    # Walk left from word_idx_start
    left = match.word_idx_start
    while left > 0:
        prev = words[left - 1]
        cur = words[left]
        gap = cur.start - prev.end
        if gap >= PAUSE_THRESHOLD or prev.utt_idx != cur.utt_idx:
            break
        left -= 1

    # Walk right from word_idx_end
    right = match.word_idx_end
    while right < len(words) - 1:
        cur = words[right]
        nxt = words[right + 1]
        gap = nxt.start - cur.end
        if gap >= PAUSE_THRESHOLD or nxt.utt_idx != cur.utt_idx:
            break
        right += 1

    start = max(0.0, words[left].start - pad_seconds)
    end = words[right].end + pad_seconds
    return round(start, 3), round(end, 3)


def _build_context(
    words: list[Word], w_start: int, w_end: int, context_words: int = 8
) -> str:
    """Build a context string of words surrounding the match for display."""
    ctx_start = max(0, w_start - context_words)
    ctx_end = min(len(words), w_end + context_words + 1)
    parts = []
    for i in range(ctx_start, ctx_end):
        if i == w_start:
            parts.append("«")
        parts.append(words[i].word)
        if i == w_end:
            parts.append("»")
    return " ".join(parts)
