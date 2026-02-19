"""Tests for lib.encoding — VideoToolbox detection and encoder argument selection."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

# Must clear lru_cache between tests
from lib.encoding import has_videotoolbox, get_video_encoder_args


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the lru_cache before each test."""
    has_videotoolbox.cache_clear()
    yield
    has_videotoolbox.cache_clear()


class TestHasVideoToolbox:
    def test_returns_false_on_linux(self):
        with patch("lib.encoding.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert has_videotoolbox() is False

    def test_returns_true_when_available_on_macos(self):
        with patch("lib.encoding.sys") as mock_sys, \
             patch("lib.encoding.subprocess.run") as mock_run:
            mock_sys.platform = "darwin"
            mock_run.return_value = MagicMock(stdout="... h264_videotoolbox ...")
            assert has_videotoolbox() is True

    def test_returns_false_when_not_available_on_macos(self):
        with patch("lib.encoding.sys") as mock_sys, \
             patch("lib.encoding.subprocess.run") as mock_run:
            mock_sys.platform = "darwin"
            mock_run.return_value = MagicMock(stdout="libx264 libx265")
            assert has_videotoolbox() is False

    def test_result_is_cached(self):
        with patch("lib.encoding.sys") as mock_sys, \
             patch("lib.encoding.subprocess.run") as mock_run:
            mock_sys.platform = "darwin"
            mock_run.return_value = MagicMock(stdout="h264_videotoolbox")
            has_videotoolbox()
            has_videotoolbox()
            assert mock_run.call_count == 1

    def test_returns_false_on_subprocess_error(self):
        with patch("lib.encoding.sys") as mock_sys, \
             patch("lib.encoding.subprocess.run", side_effect=subprocess.SubprocessError):
            mock_sys.platform = "darwin"
            assert has_videotoolbox() is False


class TestGetVideoEncoderArgs:
    def test_software_fallback_when_disabled(self):
        config = {"processing": {"use_hardware_accel": False, "video_crf": 22}}
        args = get_video_encoder_args(config)
        assert args == ["-c:v", "libx264", "-crf", "22", "-preset", "fast"]

    def test_software_fallback_uses_crf_key(self):
        config = {"processing": {"use_hardware_accel": False, "shorts_crf": 20}}
        args = get_video_encoder_args(config, crf_key="shorts_crf")
        assert args == ["-c:v", "libx264", "-crf", "20", "-preset", "fast"]

    def test_videotoolbox_when_available(self):
        config = {"processing": {"use_hardware_accel": True}}
        with patch("lib.encoding.has_videotoolbox", return_value=True):
            args = get_video_encoder_args(config)
            assert args == ["-c:v", "h264_videotoolbox", "-q:v", "65"]

    def test_default_crf_22(self):
        config = {"processing": {"use_hardware_accel": False}}
        args = get_video_encoder_args(config)
        assert args == ["-c:v", "libx264", "-crf", "22", "-preset", "fast"]
