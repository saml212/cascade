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

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("cascade")


@dataclass
class Word:
    """A single word in a flattened transcript."""
    word: str           # the spoken text
    start: float        # seconds
    end: float          # seconds
    speaker: int        # speaker index (0, 1, 2, ...)
    utt_idx: int        # which utterance this word came from
    word_idx: int       # global word index in the flat stream


@dataclass
class Match:
    """A search result."""
    start: float                  # seconds — match window start
    end: float                    # seconds — match window end
    score: float                  # 0-100, higher is better
    matched_text: str             # the actual matched substring
    context: str                  # surrounding text (~50 chars on each side)
    speaker: int                  # speaker index of the first matched word
    word_idx_start: int           # global word index of first matched word
    word_idx_end: int             # global word index of last matched word
    method: str = "exact"         # "exact" or "fuzzy"


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
            words.append(Word(
                word=str(w.get("word", "")).lower().strip(),
                start=float(w.get("start", 0)),
                end=float(w.get("end", 0)),
                speaker=int(w.get("speaker", utt_speaker)),
                utt_idx=utt_idx,
                word_idx=len(words),
            ))
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
        matched_text = " ".join(w.word for w in words[w_start:w_end + 1])
        context = _build_context(words, w_start, w_end)
        matches.append(Match(
            start=word_first.start,
            end=word_last.end,
            score=100.0,
            matched_text=matched_text,
            context=context,
            speaker=word_first.speaker,
            word_idx_start=w_start,
            word_idx_end=w_end,
            method="exact",
        ))
        start_idx = end_char + 1

    return matches


def search_fuzzy(query: str, words: list[Word], min_score: int = 70, max_results: int = 20) -> list[Match]:
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
        chunk_words = words[i:i + window]
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
            not (w_end < s_start or w_start > s_end)
            for _, s_start, s_end in selected
        )
        if not overlaps:
            selected.append((score, w_start, w_end))
            if len(selected) >= max_results:
                break

    matches: list[Match] = []
    for score, w_start, w_end in selected:
        word_first = words[w_start]
        word_last = words[w_end]
        matched_text = " ".join(w.word for w in words[w_start:w_end + 1])
        context = _build_context(words, w_start, w_end)
        matches.append(Match(
            start=word_first.start,
            end=word_last.end,
            score=score,
            matched_text=matched_text,
            context=context,
            speaker=word_first.speaker,
            word_idx_start=w_start,
            word_idx_end=w_end,
            method="fuzzy",
        ))
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
            not (fm.word_idx_end < em.word_idx_start or fm.word_idx_start > em.word_idx_end)
            for em in exact
        )
        if not overlaps_exact:
            deduped_fuzzy.append(fm)

    # Combine: exact first (highest priority), then fuzzy by score
    exact.sort(key=lambda m: m.start)
    deduped_fuzzy.sort(key=lambda m: m.score, reverse=True)
    combined = exact + deduped_fuzzy
    return combined[:max_results]


def expand_to_sentence(match: Match, words: list[Word], pad_seconds: float = 0.5) -> tuple[float, float]:
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


def _build_context(words: list[Word], w_start: int, w_end: int, context_words: int = 8) -> str:
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
