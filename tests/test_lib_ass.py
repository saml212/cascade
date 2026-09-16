"""Tests for the ASS subtitle generator.

These tests pin the structural correctness of the .ass files we generate so
the shorts_render agent can rely on them. The end-to-end render test (which
actually runs ffmpeg + libass against a synthetic video) lives in
test_lib_ass_render.py — kept separate because it requires ffmpeg.
"""

import pytest

from lib.ass import (
    CaptionPlacement,
    CaptionStyle,
    build_ass,
    escape_ass_text,
    fmt_ass_time,
    generate_ass_from_diarized,
    group_words_into_phrases,
    requires_single_lane_caption_timing,
    resolve_caption_speaker_targets,
)
from lib.timeline import Timeline, rebase_diarized

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

    def test_overlapping_speaker_words_use_one_caption_lane(self):
        words = [
            _word("main", 0.0, 1.0, speaker=0),
            _word("reply", 0.7, 1.1, speaker=1),
        ]

        legacy = group_words_into_phrases(words, clip_start=0.0, resolve_overlaps=False)
        phrases = group_words_into_phrases(words, clip_start=0.0)

        assert legacy[0]["end"] > legacy[1]["start"]
        assert phrases[0]["end"] < phrases[1]["start"]
        assert [phrase["text"] for phrase in phrases] == ["main", "reply"]

    def test_overlapping_same_speaker_phrases_share_the_transition(self):
        words = [
            _word("map", 0.0, 0.4, speaker=2),
            _word("the", 0.4, 0.64, speaker=2),
            _word("whole", 0.64, 1.28, speaker=2),
            _word("path", 0.805, 1.205, speaker=2),
            _word("of", 1.205, 1.445, speaker=2),
            _word("the", 1.445, 1.605, speaker=2),
        ]

        phrases = group_words_into_phrases(words, clip_start=0.0)

        assert phrases[0]["text"] == "map the whole"
        assert phrases[1]["text"] == "path of the"
        assert phrases[0]["end"] == pytest.approx(1.0325)
        assert phrases[1]["start"] == pytest.approx(1.0525)

    def test_detects_when_new_caption_timing_changes_output(self):
        diarized = {
            "utterances": [
                {"speaker": 0, "words": [_word("main", 10.0, 11.0, 0)]},
                {"speaker": 1, "words": [_word("reply", 10.7, 11.1, 1)]},
            ]
        }

        assert requires_single_lane_caption_timing(diarized, 10.0, 12.0)
        assert not requires_single_lane_caption_timing(diarized, 20.0, 21.0)

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
        assert (
            "Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,first phrase here" in ass
        )
        assert "Dialogue: 0,0:00:01.60,0:00:03.00,Default,,0,0,0,,second phrase" in ass

    def test_section_order(self, sample_phrases):
        ass = build_ass(sample_phrases)
        idx_info = ass.index("[Script Info]")
        idx_styles = ass.index("[V4+ Styles]")
        idx_events = ass.index("[Events]")
        # Spec requires this order
        assert idx_info < idx_styles < idx_events

    def test_custom_style(self, sample_phrases):
        style = CaptionStyle(
            font="Inter", font_size=96, margin_v=400, words_per_phrase=4
        )
        ass = build_ass(sample_phrases, style)
        assert "Style: Default,Inter," in ass
        assert ",96," in ass

    def test_text_with_braces_escaped(self):
        phrases = [
            {"start": 0.0, "end": 1.0, "text": "use {format} string", "speaker": 0}
        ]
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
                        {
                            "word": "realized",
                            "start": 101.2,
                            "end": 101.7,
                            "speaker": 0,
                        },
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

    def test_uses_punctuation_and_chronological_word_order(self, tmp_path):
        diarized = {
            "utterances": [
                {
                    "speaker": 0,
                    "words": [
                        {
                            "word": "later",
                            "punctuated_word": "Later.",
                            "start": 102.0,
                            "end": 102.3,
                        }
                    ],
                },
                {
                    "speaker": 0,
                    "words": [
                        {
                            "word": "hello",
                            "punctuated_word": "Hello,",
                            "start": 100.0,
                            "end": 100.2,
                        },
                        {
                            "word": "world",
                            "punctuated_word": "world!",
                            "start": 100.3,
                            "end": 100.6,
                        },
                    ],
                },
            ]
        }
        out = tmp_path / "ordered.ass"

        generate_ass_from_diarized(diarized, start=100.0, end=103.0, ass_path=out)

        events = [
            line
            for line in out.read_text().splitlines()
            if line.startswith("Dialogue:")
        ]
        assert events[0].endswith("Hello, world!")
        assert events[1].endswith("Later.")

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

    def test_speaker_positions_preserve_text_and_timing_across_edits(self, tmp_path):
        source = {
            "clock": "source",
            "utterances": [
                {
                    "speaker": 5,
                    "words": [
                        {"word": "outside", "start": 9.0, "end": 9.2, "speaker": 5},
                        {"word": "top", "start": 10.1, "end": 10.4, "speaker": 5},
                        {"word": "switch", "start": 11.0, "end": 11.3, "speaker": 7},
                        {"word": "removed", "start": 13.0, "end": 13.3, "speaker": 7},
                        {"word": "bottom", "start": 14.2, "end": 14.5, "speaker": 9},
                    ],
                }
            ],
        }
        rebased = rebase_diarized(source, Timeline(20, [(10, 12), (14, 15)]))
        legacy = tmp_path / "legacy.ass"
        placed = tmp_path / "placed.ass"
        generate_ass_from_diarized(rebased, 0, 3, legacy)
        generate_ass_from_diarized(
            rebased,
            0,
            3,
            placed,
            speaker_targets={5: "speaker_2", 7: "speaker_1", 9: "speaker_0"},
            speaker_placements={
                "speaker_2": CaptionPlacement(540, 357),
                "speaker_1": CaptionPlacement(540, 737),
                "speaker_0": CaptionPlacement(540, 1120),
            },
            fallback_placement=CaptionPlacement(540, 36, alignment=5),
        )

        def dialogue_fields(path):
            fields = []
            for line in path.read_text().splitlines():
                if not line.startswith("Dialogue:"):
                    continue
                parts = line.split(",", 9)
                text = parts[9]
                if text.startswith("{"):
                    text = text.split("}", 1)[1]
                fields.append((parts[1], parts[2], text))
            return fields

        assert dialogue_fields(placed) == dialogue_fields(legacy)
        placed_text = placed.read_text()
        assert "outside" not in placed_text
        assert "removed" not in placed_text
        assert r"\pos(540,357)" in placed_text
        assert r"\pos(540,737)" in placed_text
        assert r"\pos(540,1120)" in placed_text


def test_caption_speaker_targets_prefer_bindings_and_conservative_fallbacks():
    crop_config = {
        "speakers": [
            {"label": "PJ", "track": 1},
            {"label": "Christopher", "track": 2},
            {"label": "Ty", "track": 3},
        ]
    }
    segments = {
        "clock": "source",
        "track_mapping": [
            {"speaker": "speaker_0", "person": "PJ", "logical_track": 1},
            {
                "speaker": "speaker_1",
                "person": "Christopher",
                "logical_track": 2,
            },
            {"speaker": "speaker_2", "person": "Ty", "logical_track": 3},
        ],
    }
    transcript = {
        "clock": "source",
        "speaker_map": [
            {
                "index": 0,
                "target_speaker": "speaker_1",
                "person": "PJ",
                "logical_track": 1,
            },
            {"index": 1, "target_speaker": "speaker_2", "logical_track": 3},
            {"index": 3, "logical_track": 3},
            {"index": 2, "target_speaker": "BOTH", "person": "Laura"},
            {"index": 4, "person": "Christopher"},
            {"index": 5, "logical_track": 1, "mapping_confidence": 0.2},
        ],
    }

    assert resolve_caption_speaker_targets(transcript, segments, crop_config) == {
        0: "speaker_1",
        1: "speaker_2",
        2: "BOTH",
        3: "speaker_2",
        4: "speaker_1",
        5: "BOTH",
    }
    assert (
        resolve_caption_speaker_targets(
            {**transcript, "clock": "output"}, segments, crop_config
        )
        == {}
    )
