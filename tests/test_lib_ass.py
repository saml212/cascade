"""Tests for the ASS subtitle generator.

These tests pin the structural correctness of the .ass files we generate so
the shorts_render agent can rely on them. The end-to-end render test (which
actually runs ffmpeg + libass against a synthetic video) lives in
test_lib_ass_render.py — kept separate because it requires ffmpeg.
"""

from pathlib import Path

import pytest

from lib.ass import (
    CaptionStyle,
    build_ass,
    escape_ass_text,
    fmt_ass_time,
    generate_ass_from_diarized,
    group_words_into_phrases,
)


# ── timecode formatting ─────────────────────────────────────────────────────


class TestFmtAssTime:
    def test_zero(self):
        assert fmt_ass_time(0.0) == "0:00:00.00"

    def test_basic_seconds(self):
        assert fmt_ass_time(3.456) == "0:00:03.46"

    def test_minutes(self):
        assert fmt_ass_time(125.5) == "0:02:05.50"

    def test_hours(self):
        assert fmt_ass_time(3725.99) == "1:02:05.99"

    def test_negative_clamped_to_zero(self):
        # Defensive: ASS rejects negative timecodes.
        assert fmt_ass_time(-5.0) == "0:00:00.00"

    def test_centisecond_rounding_carry(self):
        # 59.999s rounds to 60.00 → must carry into the minutes column,
        # not produce "0:00:59.100" or "0:00:60.00".
        assert fmt_ass_time(59.999) == "0:01:00.00"

    def test_centisecond_carry_into_hours(self):
        # 3599.999 should carry all the way to 1:00:00.00, not 0:60:00.00.
        assert fmt_ass_time(3599.999) == "1:00:00.00"


# ── text escaping ───────────────────────────────────────────────────────────


class TestEscapeAssText:
    def test_passthrough(self):
        assert escape_ass_text("hello world") == "hello world"

    def test_curly_braces_escaped(self):
        # Bare braces would be interpreted as override blocks.
        assert escape_ass_text("a {b} c") == "a \\{b\\} c"

    def test_backslash_escaped(self):
        assert escape_ass_text("a\\b") == "a\\\\b"

    def test_newline_to_ass_break(self):
        assert escape_ass_text("a\nb") == "a\\Nb"


# ── phrase grouping ─────────────────────────────────────────────────────────


def _word(text: str, start: float, end: float, speaker: int = 0) -> dict:
    return {"word": text, "start": start, "end": end, "speaker": speaker}


class TestGroupWordsIntoPhrases:
    def test_empty_input(self):
        assert group_words_into_phrases([], clip_start=0.0) == []

    def test_groups_three_per_phrase_default(self):
        words = [_word(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(6)]
        phrases = group_words_into_phrases(words, clip_start=0.0)
        assert len(phrases) == 2
        assert phrases[0]["text"] == "w0 w1 w2"
        assert phrases[1]["text"] == "w3 w4 w5"

    def test_breaks_on_speaker_change(self):
        words = [
            _word("a", 0.0, 0.2, speaker=0),
            _word("b", 0.3, 0.5, speaker=0),
            _word("c", 0.6, 0.8, speaker=1),  # speaker change
            _word("d", 0.9, 1.1, speaker=1),
        ]
        phrases = group_words_into_phrases(words, clip_start=0.0)
        assert len(phrases) == 2
        assert phrases[0]["text"] == "a b"
        assert phrases[0]["speaker"] == 0
        assert phrases[1]["text"] == "c d"
        assert phrases[1]["speaker"] == 1

    def test_breaks_on_long_pause(self):
        # 0.6s gap between word 1 and 2 is > 0.5s break threshold
        words = [
            _word("a", 0.0, 0.2),
            _word("b", 0.3, 0.5),
            _word("c", 1.2, 1.4),
            _word("d", 1.5, 1.7),
        ]
        phrases = group_words_into_phrases(words, clip_start=0.0)
        assert len(phrases) == 2
        assert phrases[0]["text"] == "a b"
        assert phrases[1]["text"] == "c d"

    def test_times_relative_to_clip_start(self):
        # Clip starts at episode time 100s. Word at 102.5s should appear at
        # phrase-relative time 2.5s.
        words = [
            _word("hello", 102.5, 102.8),
            _word("world", 102.9, 103.2),
        ]
        phrases = group_words_into_phrases(words, clip_start=100.0)
        assert phrases[0]["start"] == pytest.approx(2.5, abs=0.01)
        assert phrases[0]["end"] >= 3.2 - 100.0

    def test_min_phrase_duration_floor(self):
        # A single very-short word still gets a readable display duration.
        words = [_word("yes", 0.0, 0.05)]
        phrases = group_words_into_phrases(words, clip_start=0.0)
        assert phrases[0]["end"] - phrases[0]["start"] >= 0.4

    def test_max_phrase_duration_cap(self):
        # A single word with a 10-second duration shouldn't linger 10 seconds.
        words = [_word("uhhhh", 0.0, 10.0)]
        phrases = group_words_into_phrases(words, clip_start=0.0)
        assert phrases[0]["end"] - phrases[0]["start"] <= 2.5 + 0.01

    def test_phrases_dont_overlap(self):
        words = [_word(f"w{i}", i * 0.4, i * 0.4 + 0.3) for i in range(6)]
        phrases = group_words_into_phrases(words, clip_start=0.0)
        for i in range(len(phrases) - 1):
            assert phrases[i]["end"] <= phrases[i + 1]["start"]

    def test_negative_relative_time_clamped(self):
        # Defensive: if a word ends up before clip_start due to fp error,
        # the relative time must not go negative (ffmpeg subtitles rejects).
        words = [_word("oops", 99.99, 100.1)]
        phrases = group_words_into_phrases(words, clip_start=100.0)
        assert phrases[0]["start"] >= 0.0


# ── full ASS file structure ─────────────────────────────────────────────────


class TestBuildAss:
    @pytest.fixture
    def sample_phrases(self):
        return [
            {"start": 0.0, "end": 1.5, "text": "first phrase here", "speaker": 0},
            {"start": 1.6, "end": 3.0, "text": "second phrase", "speaker": 0},
        ]

    def test_has_script_info_section(self, sample_phrases):
        ass = build_ass(sample_phrases)
        assert "[Script Info]" in ass
        assert "ScriptType: v4.00+" in ass
        assert "PlayResX: 1080" in ass
        assert "PlayResY: 1920" in ass

    def test_has_styles_section(self, sample_phrases):
        ass = build_ass(sample_phrases)
        assert "[V4+ Styles]" in ass
        assert "Format: Name, Fontname, Fontsize" in ass
        assert "Style: Default,Helvetica," in ass

    def test_has_events_section(self, sample_phrases):
        ass = build_ass(sample_phrases)
        assert "[Events]" in ass
        assert "Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,first phrase here" in ass
        assert "Dialogue: 0,0:00:01.60,0:00:03.00,Default,,0,0,0,,second phrase" in ass

    def test_section_order(self, sample_phrases):
        ass = build_ass(sample_phrases)
        idx_info = ass.index("[Script Info]")
        idx_styles = ass.index("[V4+ Styles]")
        idx_events = ass.index("[Events]")
        # Spec requires this order
        assert idx_info < idx_styles < idx_events

    def test_custom_style(self, sample_phrases):
        style = CaptionStyle(font="Inter", font_size=96, margin_v=400, words_per_phrase=4)
        ass = build_ass(sample_phrases, style)
        assert "Style: Default,Inter," in ass
        assert ",96," in ass

    def test_text_with_braces_escaped(self):
        phrases = [{"start": 0.0, "end": 1.0, "text": "use {format} string", "speaker": 0}]
        ass = build_ass(phrases)
        assert "use \\{format\\} string" in ass
        # Bare unescaped braces would be parsed as override block
        assert "{format}" not in ass.split("[Events]")[1]

    def test_empty_phrases_still_produces_valid_header(self):
        ass = build_ass([])
        # Must still have all three sections; libass tolerates zero events
        assert "[Script Info]" in ass
        assert "[V4+ Styles]" in ass
        assert "[Events]" in ass
        # No Dialogue: lines
        assert "Dialogue:" not in ass


# ── end-to-end: diarized → file ─────────────────────────────────────────────


class TestGenerateAssFromDiarized:
    @pytest.fixture
    def diarized(self):
        return {
            "utterances": [
                {
                    "speaker": 0,
                    "start": 100.0,
                    "end": 102.5,
                    "text": "and that's when I realized",
                    "words": [
                        {"word": "and", "start": 100.0, "end": 100.2, "speaker": 0},
                        {"word": "that's", "start": 100.3, "end": 100.6, "speaker": 0},
                        {"word": "when", "start": 100.7, "end": 100.9, "speaker": 0},
                        {"word": "I", "start": 101.0, "end": 101.1, "speaker": 0},
                        {"word": "realized", "start": 101.2, "end": 101.7, "speaker": 0},
                    ],
                },
                {
                    "speaker": 1,
                    "start": 102.0,
                    "end": 103.5,
                    "text": "yeah exactly",
                    "words": [
                        {"word": "yeah", "start": 102.0, "end": 102.3, "speaker": 1},
                        {"word": "exactly", "start": 102.4, "end": 102.9, "speaker": 1},
                    ],
                },
            ]
        }

    def test_writes_file(self, tmp_path, diarized):
        out = tmp_path / "test.ass"
        n = generate_ass_from_diarized(diarized, start=100.0, end=103.0, ass_path=out)
        assert n > 0
        assert out.exists()
        content = out.read_text()
        assert "[Events]" in content

    def test_only_words_in_range_included(self, tmp_path, diarized):
        # Range excludes "exactly" (102.4-102.9) only if end < 102.9. Use a
        # tight range to exclude speaker-1 entirely.
        out = tmp_path / "test.ass"
        generate_ass_from_diarized(diarized, start=100.0, end=101.8, ass_path=out)
        content = out.read_text()
        assert "and" in content
        assert "realized" in content
        assert "yeah" not in content
        assert "exactly" not in content

    def test_speaker_change_creates_separate_phrases(self, tmp_path, diarized):
        out = tmp_path / "test.ass"
        generate_ass_from_diarized(diarized, start=100.0, end=103.5, ass_path=out)
        content = out.read_text()
        # Phrase from speaker 0 must not contain "yeah" — break on speaker change
        events_section = content.split("[Events]")[1]
        # Find lines containing "realized" — the phrase on that dialogue should
        # NOT also contain "yeah"
        for line in events_section.split("\n"):
            if "realized" in line:
                assert "yeah" not in line

    def test_relative_times_start_at_zero(self, tmp_path, diarized):
        # Clip starts at episode time 100s. The first dialogue line's start
        # time must be 0:00:00.00, not 0:01:40.00.
        out = tmp_path / "test.ass"
        generate_ass_from_diarized(diarized, start=100.0, end=103.5, ass_path=out)
        content = out.read_text()
        first_dialogue = next(
            line for line in content.split("\n") if line.startswith("Dialogue:")
        )
        # Start time field is the second comma-separated field
        start_tc = first_dialogue.split(",")[1]
        assert start_tc == "0:00:00.00"
