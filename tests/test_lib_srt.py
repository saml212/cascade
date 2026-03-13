"""Tests for lib.srt module."""

import pytest
from pathlib import Path

from lib.srt import fmt_timecode, escape_srt_path


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
