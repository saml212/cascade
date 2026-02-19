"""Tests for lib.ffprobe module."""

import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def _mock_ffprobe_run(result_dict):
    """Create a mock for subprocess.run that returns ffprobe JSON."""
    mock_result = MagicMock()
    mock_result.stdout = json.dumps(result_dict)
    mock_result.returncode = 0
    return mock_result


class TestProbe:
    def test_probe_returns_dict(self, mock_ffprobe_result):
        from lib.ffprobe import probe
        with patch("subprocess.run", return_value=_mock_ffprobe_run(mock_ffprobe_result)):
            result = probe(Path("/fake/video.mp4"))
        assert isinstance(result, dict)
        assert "format" in result
        assert "streams" in result

    def test_probe_passes_correct_args(self, mock_ffprobe_result):
        from lib.ffprobe import probe
        with patch("subprocess.run", return_value=_mock_ffprobe_run(mock_ffprobe_result)) as mock_run:
            probe(Path("/fake/video.mp4"))
        args = mock_run.call_args[0][0]
        assert args[0] == "ffprobe"
        assert "-show_format" in args
        assert "-show_streams" in args
        assert "/fake/video.mp4" in args

    def test_probe_raises_on_failure(self):
        from lib.ffprobe import probe
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            with pytest.raises(subprocess.CalledProcessError):
                probe(Path("/fake/nonexistent.mp4"))

    def test_probe_handles_json_parse(self, mock_ffprobe_result):
        from lib.ffprobe import probe
        mock = MagicMock()
        mock.stdout = "not json"
        with patch("subprocess.run", return_value=mock):
            with pytest.raises(json.JSONDecodeError):
                probe(Path("/fake/video.mp4"))


class TestGetDuration:
    def test_get_duration_normal(self, mock_ffprobe_result):
        from lib.ffprobe import get_duration
        with patch("subprocess.run", return_value=_mock_ffprobe_run(mock_ffprobe_result)):
            dur = get_duration(Path("/fake/video.mp4"))
        assert dur == pytest.approx(3600.123)

    def test_get_duration_missing_format(self):
        from lib.ffprobe import get_duration
        with patch("subprocess.run", return_value=_mock_ffprobe_run({"streams": []})):
            dur = get_duration(Path("/fake/video.mp4"))
        assert dur == 0.0

    def test_get_duration_zero(self):
        from lib.ffprobe import get_duration
        result = {"format": {"duration": "0"}, "streams": []}
        with patch("subprocess.run", return_value=_mock_ffprobe_run(result)):
            dur = get_duration(Path("/fake/video.mp4"))
        assert dur == 0.0


class TestGetDimensions:
    def test_get_dimensions_normal(self, mock_ffprobe_result):
        from lib.ffprobe import get_dimensions
        with patch("subprocess.run", return_value=_mock_ffprobe_run(mock_ffprobe_result)):
            w, h = get_dimensions(Path("/fake/video.mp4"))
        assert w == 1920
        assert h == 1080

    def test_get_dimensions_no_video_stream(self):
        from lib.ffprobe import get_dimensions
        result = {"format": {}, "streams": [{"codec_type": "audio"}]}
        with patch("subprocess.run", return_value=_mock_ffprobe_run(result)):
            with pytest.raises(StopIteration):
                get_dimensions(Path("/fake/audio.mp3"))
