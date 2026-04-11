"""Tests for lib.transcript_search."""

import pytest

from lib.transcript_search import (
    Word,
    Match,
    flatten_transcript,
    search_exact,
    search_fuzzy,
    hybrid_search,
    expand_to_sentence,
)


def _make_diarized(text: str, speaker: int = 0, start: float = 0.0, word_dur: float = 0.3) -> dict:
    """Build a single-utterance diarized transcript from a sentence."""
    words_list = text.split()
    words = []
    t = start
    for w in words_list:
        words.append({
            "word": w,
            "start": t,
            "end": t + word_dur,
            "speaker": speaker,
            "confidence": 0.95,
        })
        t += word_dur + 0.05  # small gap between words
    return {
        "utterances": [{
            "speaker": speaker,
            "start": start,
            "end": t,
            "text": text,
            "confidence": 0.95,
            "words": words,
        }]
    }


def _make_multi_utterance_diarized() -> dict:
    """Build a transcript with two utterances and two speakers."""
    return {
        "utterances": [
            {
                "speaker": 0,
                "start": 0.0,
                "end": 2.0,
                "text": "Hello world how are you",
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.3, "speaker": 0},
                    {"word": "world", "start": 0.4, "end": 0.7, "speaker": 0},
                    {"word": "how", "start": 0.8, "end": 1.0, "speaker": 0},
                    {"word": "are", "start": 1.1, "end": 1.3, "speaker": 0},
                    {"word": "you", "start": 1.4, "end": 1.7, "speaker": 0},
                ],
            },
            {
                "speaker": 1,
                "start": 2.5,
                "end": 4.5,
                "text": "I am doing great thanks",
                "words": [
                    {"word": "i", "start": 2.5, "end": 2.6, "speaker": 1},
                    {"word": "am", "start": 2.7, "end": 2.9, "speaker": 1},
                    {"word": "doing", "start": 3.0, "end": 3.3, "speaker": 1},
                    {"word": "great", "start": 3.4, "end": 3.7, "speaker": 1},
                    {"word": "thanks", "start": 3.8, "end": 4.2, "speaker": 1},
                ],
            },
        ],
    }


class TestFlattenTranscript:
    def test_single_utterance(self):
        d = _make_diarized("hello world")
        words = flatten_transcript(d)
        assert len(words) == 2
        assert words[0].word == "hello"
        assert words[1].word == "world"
        assert words[0].word_idx == 0
        assert words[1].word_idx == 1

    def test_multi_utterance_global_indices(self):
        words = flatten_transcript(_make_multi_utterance_diarized())
        assert len(words) == 10
        assert words[0].word == "hello"
        assert words[5].word == "i"
        assert words[5].speaker == 1
        assert words[5].word_idx == 5
        assert words[5].utt_idx == 1

    def test_empty(self):
        assert flatten_transcript({}) == []
        assert flatten_transcript({"utterances": []}) == []


class TestSearchExact:
    def test_finds_single_word(self):
        words = flatten_transcript(_make_diarized("the quick brown fox"))
        matches = search_exact("brown", words)
        assert len(matches) == 1
        assert matches[0].matched_text == "brown"
        assert matches[0].score == 100.0

    def test_finds_phrase(self):
        words = flatten_transcript(_make_diarized("the quick brown fox jumps"))
        matches = search_exact("brown fox", words)
        assert len(matches) == 1
        assert matches[0].matched_text == "brown fox"

    def test_case_insensitive(self):
        words = flatten_transcript(_make_diarized("Hello World"))
        assert search_exact("hello", words)
        assert search_exact("HELLO", words)

    def test_spans_utterances(self):
        words = flatten_transcript(_make_multi_utterance_diarized())
        # "you i am" spans speaker boundary
        matches = search_exact("you i am", words)
        assert len(matches) == 1

    def test_no_match(self):
        words = flatten_transcript(_make_diarized("hello world"))
        assert search_exact("goodbye", words) == []

    def test_returns_correct_timestamps(self):
        words = flatten_transcript(_make_diarized("the quick brown fox"))
        matches = search_exact("brown", words)
        assert matches[0].start == words[2].start
        assert matches[0].end == words[2].end

    def test_multiple_occurrences(self):
        words = flatten_transcript(_make_diarized("yes yes yes maybe yes"))
        matches = search_exact("yes", words)
        assert len(matches) == 4


class TestSearchFuzzy:
    def test_finds_typo(self):
        words = flatten_transcript(_make_diarized("the quick brown fox jumps over"))
        # "qwick brown" should still match "quick brown"
        matches = search_fuzzy("qwick brown", words, min_score=70)
        assert len(matches) >= 1
        assert "brown" in matches[0].matched_text.lower()

    def test_min_score_filters(self):
        words = flatten_transcript(_make_diarized("the quick brown fox"))
        # Garbage shouldn't match even with low threshold
        matches = search_fuzzy("zzzzz qqqqq", words, min_score=80)
        assert len(matches) == 0

    def test_empty_query(self):
        words = flatten_transcript(_make_diarized("hello world"))
        assert search_fuzzy("", words) == []


class TestHybridSearch:
    def test_exact_ranks_above_fuzzy(self):
        words = flatten_transcript(_make_diarized(
            "i love eating pizza on friday and pizza on saturday"
        ))
        matches = hybrid_search("pizza on", words, max_results=5)
        assert len(matches) >= 1
        # Exact matches should be first
        assert matches[0].method == "exact"

    def test_dedupe_overlapping(self):
        words = flatten_transcript(_make_diarized("the brown fox jumps"))
        matches = hybrid_search("brown fox", words, max_results=10)
        # Shouldn't return both an exact and a fuzzy match for the same words
        method_counts = {}
        for m in matches:
            key = (m.word_idx_start, m.word_idx_end)
            assert key not in method_counts
            method_counts[key] = m.method


class TestExpandToSentence:
    def test_expands_to_pause(self):
        words = flatten_transcript(_make_multi_utterance_diarized())
        # Match "world" — should expand to the full first utterance
        matches = search_exact("world", words)
        start, end = expand_to_sentence(matches[0], words, pad_seconds=0)
        # Should cover at least "hello world"
        assert start <= words[0].start
        assert end >= words[1].end

    def test_stops_at_utterance_boundary(self):
        words = flatten_transcript(_make_multi_utterance_diarized())
        matches = search_exact("you", words)
        start, end = expand_to_sentence(matches[0], words, pad_seconds=0)
        # Should not cross into utterance 1 (different speaker)
        assert end <= words[4].end + 0.001  # last word of utterance 0
