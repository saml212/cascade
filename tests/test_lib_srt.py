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


class TestEscapeSrtPath:
    def test_simple_path(self):
        result = escape_srt_path(Path("/tmp/test.srt"))
        assert result == "/tmp/test.srt"

    def test_colon_in_path(self):
        # Path objects may normalize colons, so test with a string-like path
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
