"""Tests for lib.srt module."""

import pytest
from pathlib import Path

from lib.srt import fmt_timecode, escape_srt_path, generate_srt_from_diarized, parse_srt, parse_srt_time


class TestFmtTimecode:
    def test_zero(self):
        assert fmt_timecode(0) == "00:00:00,000"

    def test_fractional_seconds(self):
        assert fmt_timecode(1.5) == "00:00:01,500"

    def test_minutes(self):
        assert fmt_timecode(90.0) == "00:01:30,000"

    def test_hours(self):
        assert fmt_timecode(3661.5) == "01:01:01,500"

    def test_negative_clamped_to_zero(self):
        assert fmt_timecode(-5.0) == "00:00:00,000"

    def test_large_value(self):
        assert fmt_timecode(86400.0) == "24:00:00,000"

    def test_millisecond_precision(self):
        result = fmt_timecode(1.234)
        assert result == "00:00:01,234"

    def test_very_small_positive(self):
        result = fmt_timecode(0.001)
        assert result == "00:00:00,001"

    def test_just_under_one_second(self):
        result = fmt_timecode(0.999)
        assert result == "00:00:00,999"

    def test_sub_millisecond_truncated(self):
        """Sub-millisecond precision should be truncated (not rounded)."""
        result = fmt_timecode(1.9999)
        assert result == "00:00:01,999"

    def test_exactly_one_hour(self):
        assert fmt_timecode(3600.0) == "01:00:00,000"

    def test_format_structure(self):
        """Verify the HH:MM:SS,mmm format."""
        result = fmt_timecode(5025.678)
        parts = result.split(",")
        assert len(parts) == 2
        time_parts = parts[0].split(":")
        assert len(time_parts) == 3
        assert len(parts[1]) == 3  # milliseconds always 3 digits

    def test_negative_large_value(self):
        assert fmt_timecode(-100.0) == "00:00:00,000"


class TestEscapeSrtPath:
    def test_simple_path(self):
        result = escape_srt_path(Path("/tmp/test.srt"))
        assert result == "/tmp/test.srt"

    def test_colon_in_path(self):
        result = escape_srt_path(Path("/tmp/file:name.srt"))
        assert "\\:" in result

    def test_backslash_in_path(self):
        result = escape_srt_path(Path("/tmp/test.srt"))
        # On Unix, no backslashes, so should be unchanged
        assert "\\\\" not in result

    def test_quote_in_path(self):
        result = escape_srt_path(Path("/tmp/it's.srt"))
        assert "\\'" in result

    def test_space_in_path(self):
        result = escape_srt_path(Path("/tmp/my file.srt"))
        assert "my file" in result

    def test_complex_path_with_multiple_special_chars(self):
        """Multiple special characters should all be escaped."""
        result = escape_srt_path(Path("/tmp/it's:file.srt"))
        assert "\\'" in result
        assert "\\:" in result

    def test_path_object_input(self):
        """Should accept Path objects."""
        p = Path("/usr/local/test.srt")
        result = escape_srt_path(p)
        assert isinstance(result, str)

    def test_deeply_nested_path(self):
        result = escape_srt_path(Path("/a/b/c/d/e/f/test.srt"))
        assert result == "/a/b/c/d/e/f/test.srt"

    def test_empty_filename(self):
        result = escape_srt_path(Path("/tmp/"))
        assert isinstance(result, str)


class TestGenerateSrtFromDiarized:
    def _make_diarized(self, words):
        """Helper: wrap word list into diarized format."""
        return {"utterances": [{"words": words}]}

    def test_basic_generation(self, tmp_path):
        words = [
            {"word": "Hello", "start": 1.0, "end": 1.5},
            {"word": "world", "start": 1.5, "end": 2.0},
            {"word": "this", "start": 2.0, "end": 2.5},
            {"word": "is", "start": 2.5, "end": 3.0},
            {"word": "a", "start": 3.0, "end": 3.5},
            {"word": "test", "start": 3.5, "end": 4.0},
        ]
        diarized = self._make_diarized(words)
        srt_path = tmp_path / "test.srt"
        generate_srt_from_diarized(diarized, 1.0, 4.0, srt_path)

        content = srt_path.read_text()
        assert "Hello world this is" in content
        assert "a test" in content
        assert "00:00:00,000" in content  # First chunk starts at 0

    def test_time_offset_relative_to_start(self, tmp_path):
        words = [
            {"word": "word1", "start": 10.0, "end": 10.5},
            {"word": "word2", "start": 10.5, "end": 11.0},
        ]
        diarized = self._make_diarized(words)
        srt_path = tmp_path / "test.srt"
        generate_srt_from_diarized(diarized, 10.0, 11.0, srt_path)

        content = srt_path.read_text()
        assert "00:00:00,000" in content  # 10.0 - 10.0 = 0.0
        assert "00:00:01,000" in content  # 11.0 - 10.0 = 1.0

    def test_filters_words_outside_range(self, tmp_path):
        words = [
            {"word": "before", "start": 0.0, "end": 0.5},
            {"word": "inside", "start": 1.0, "end": 1.5},
            {"word": "after", "start": 5.0, "end": 5.5},
        ]
        diarized = self._make_diarized(words)
        srt_path = tmp_path / "test.srt"
        generate_srt_from_diarized(diarized, 1.0, 2.0, srt_path)

        content = srt_path.read_text()
        assert "inside" in content
        assert "before" not in content
        assert "after" not in content

    def test_empty_range(self, tmp_path):
        diarized = self._make_diarized([])
        srt_path = tmp_path / "test.srt"
        generate_srt_from_diarized(diarized, 0.0, 10.0, srt_path)
        assert srt_path.read_text() == ""


class TestParseSrt:
    def test_basic_parse(self, tmp_path):
        srt_path = tmp_path / "test.srt"
        srt_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello world\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nGoodbye\n"
        )
        entries = parse_srt(srt_path)
        assert len(entries) == 2
        assert entries[0]["text"] == "Hello world"
        assert entries[0]["start"] == 0.0
        assert entries[0]["end"] == 1.0
        assert entries[1]["text"] == "Goodbye"

    def test_missing_file(self, tmp_path):
        entries = parse_srt(tmp_path / "nonexistent.srt")
        assert entries == []

    def test_empty_file(self, tmp_path):
        srt_path = tmp_path / "empty.srt"
        srt_path.write_text("")
        entries = parse_srt(srt_path)
        assert entries == []


class TestParseSrtTime:
    def test_basic(self):
        assert parse_srt_time("00:00:01,000") == 1.0

    def test_with_hours(self):
        assert parse_srt_time("01:30:00,500") == 5400.5

    def test_invalid_format(self):
        assert parse_srt_time("invalid") == 0.0
