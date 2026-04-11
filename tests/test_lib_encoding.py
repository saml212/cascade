"""Tests for lib.encoding — VideoToolbox detection, encoder args, color metadata, and LUT filter."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from lib.encoding import (
    has_videotoolbox,
    get_video_encoder_args,
    get_color_metadata_args,
    get_lut_filter,
    get_scale_filter,
    get_video_polish_filters,
)


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

    def test_returns_false_on_file_not_found(self):
        with patch("lib.encoding.sys") as mock_sys, \
             patch("lib.encoding.subprocess.run", side_effect=FileNotFoundError):
            mock_sys.platform = "darwin"
            assert has_videotoolbox() is False

    def test_returns_false_on_windows(self):
        with patch("lib.encoding.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert has_videotoolbox() is False


class TestGetVideoEncoderArgs:
    def test_software_fallback_when_hw_disabled(self):
        config = {"processing": {"use_hardware_accel": False, "video_crf": 22}}
        args = get_video_encoder_args(config)
        assert args[0:2] == ["-c:v", "libx264"]
        assert "-crf" in args
        assert "22" in args

    def test_software_fallback_uses_crf_key(self):
        config = {"processing": {"use_hardware_accel": False, "shorts_crf": 20}}
        args = get_video_encoder_args(config, crf_key="shorts_crf")
        assert "-crf" in args
        assert "20" in args

    def test_default_crf_22(self):
        """Default CRF should be 22 when not configured."""
        config = {"processing": {"use_hardware_accel": False}}
        args = get_video_encoder_args(config)
        assert "22" in args

    def test_default_preset_medium(self):
        """Default encode preset should be 'medium'."""
        config = {"processing": {"use_hardware_accel": False}}
        args = get_video_encoder_args(config)
        assert "medium" in args

    def test_custom_preset(self):
        config = {"processing": {"use_hardware_accel": False, "encode_preset": "ultrafast"}}
        args = get_video_encoder_args(config)
        assert "ultrafast" in args

    def test_videotoolbox_when_hw_accel_enabled(self):
        """VideoToolbox should use H.264 for universal platform compatibility."""
        config = {"processing": {"use_hardware_accel": True}}
        with patch("lib.encoding.has_videotoolbox", return_value=True):
            args = get_video_encoder_args(config)
            assert args[0:2] == ["-c:v", "h264_videotoolbox"]
            assert "-q:v" in args
            assert "-profile:v" in args
            assert "high" in args

    def test_videotoolbox_default_quality_45(self):
        """Default VideoToolbox quality should be 45 (when not configured)."""
        config = {"processing": {"use_hardware_accel": True}}
        with patch("lib.encoding.has_videotoolbox", return_value=True):
            args = get_video_encoder_args(config)
            assert "45" in args  # default fallback in get_video_encoder_args

    def test_videotoolbox_custom_quality(self):
        """VideoToolbox quality should be configurable."""
        config = {"processing": {"use_hardware_accel": True, "videotoolbox_quality": 90}}
        with patch("lib.encoding.has_videotoolbox", return_value=True):
            args = get_video_encoder_args(config)
            assert args[0:2] == ["-c:v", "h264_videotoolbox"]
            assert "90" in args

    def test_software_when_hw_accel_disabled(self):
        """Should use software encoding when use_hardware_accel is False."""
        config = {"processing": {"use_hardware_accel": False}}
        with patch("lib.encoding.has_videotoolbox", return_value=True):
            args = get_video_encoder_args(config)
            assert args[0:2] == ["-c:v", "libx264"]

    def test_software_when_hw_not_available(self):
        config = {"processing": {"use_hardware_accel": True}}
        with patch("lib.encoding.has_videotoolbox", return_value=False):
            args = get_video_encoder_args(config)
            assert args[0:2] == ["-c:v", "libx264"]

    def test_empty_processing_config(self):
        """Empty config with VideoToolbox available should use hardware."""
        config = {}
        with patch("lib.encoding.has_videotoolbox", return_value=True):
            args = get_video_encoder_args(config)
            assert args[0:2] == ["-c:v", "h264_videotoolbox"]

    def test_empty_config_no_hw(self):
        """Empty config without VideoToolbox should use software defaults."""
        with patch("lib.encoding.has_videotoolbox", return_value=False):
            args = get_video_encoder_args({})
            assert "-c:v" in args
            assert "libx264" in args


class TestGetColorMetadataArgs:
    def test_returns_bt709_flags(self):
        args = get_color_metadata_args()
        assert "-color_primaries" in args
        assert "bt709" in args
        assert "-color_trc" in args
        assert "-colorspace" in args
        assert "-color_range" in args
        assert "tv" in args

    def test_returns_list(self):
        args = get_color_metadata_args()
        assert isinstance(args, list)
        assert len(args) == 8  # 4 flag pairs


class TestGetScaleFilter:
    def test_basic_dimensions(self):
        f = get_scale_filter(1920, 1080)
        assert "scale=1920:1080" in f

    def test_uses_lanczos(self):
        f = get_scale_filter(1080, 1920)
        assert "lanczos" in f

    def test_includes_dither(self):
        f = get_scale_filter(1920, 1080)
        assert "sws_dither=ed" in f  # error-diffusion dither

    def test_includes_accurate_rnd(self):
        f = get_scale_filter(1920, 1080)
        assert "accurate_rnd" in f

    def test_includes_full_chroma(self):
        f = get_scale_filter(1920, 1080)
        assert "full_chroma_int" in f

    def test_lanczos_taps_5(self):
        f = get_scale_filter(1920, 1080)
        assert "param0=5" in f


class TestGetVideoPolishFilters:
    def test_default_includes_all(self):
        f = get_video_polish_filters({})
        assert "hqdn3d" in f
        assert "cas=" in f
        assert "eq=" in f

    def test_default_chain_count(self):
        f = get_video_polish_filters({})
        assert len(f.split(",")) == 3

    def test_disable_denoise(self):
        f = get_video_polish_filters({"processing": {"video_denoise": False}})
        assert "hqdn3d" not in f

    def test_disable_sharpen(self):
        f = get_video_polish_filters({"processing": {"video_sharpen": False}})
        assert "cas=" not in f

    def test_disable_polish(self):
        f = get_video_polish_filters({"processing": {"video_polish": False}})
        assert "eq=" not in f

    def test_custom_sharpen_strength(self):
        f = get_video_polish_filters({"processing": {"video_sharpen_strength": 0.6}})
        assert "cas=strength=0.6" in f

    def test_default_sharpen_strength(self):
        f = get_video_polish_filters({})
        assert "cas=strength=0.3" in f  # Gentle default

    def test_default_eq_minimal(self):
        """Default eq should be minimal — contrast=1.0, subtle sat/gamma."""
        f = get_video_polish_filters({})
        assert "contrast=1.0" in f
        assert "saturation=1.04" in f
        assert "gamma=1.01" in f

    def test_custom_eq(self):
        f = get_video_polish_filters({"processing": {
            "video_contrast": 1.1,
            "video_saturation": 1.15,
            "video_gamma": 1.05,
        }})
        assert "contrast=1.1" in f
        assert "saturation=1.15" in f
        assert "gamma=1.05" in f

    def test_all_disabled_returns_empty(self):
        f = get_video_polish_filters({"processing": {
            "video_denoise": False,
            "video_sharpen": False,
            "video_polish": False,
        }})
        assert f == ""


class TestGetLutFilter:
    def test_no_lut_configured(self):
        config = {"processing": {}}
        assert get_lut_filter(config) == ""

    def test_empty_lut_path(self):
        config = {"processing": {"lut_path": ""}}
        assert get_lut_filter(config) == ""

    def test_nonexistent_lut_file(self):
        config = {"processing": {"lut_path": "/nonexistent/path.cube"}}
        assert get_lut_filter(config) == ""

    def test_absolute_lut_path_exists(self, tmp_path):
        lut_file = tmp_path / "test.cube"
        lut_file.write_text("# Fake LUT\nLUT_3D_SIZE 17\n")
        config = {"processing": {"lut_path": str(lut_file)}}
        result = get_lut_filter(config)
        assert result.startswith("lut3d=")
        assert "test.cube" in result

    def test_default_interpolation_tetrahedral(self, tmp_path):
        lut_file = tmp_path / "test.cube"
        lut_file.write_text("# Fake LUT\n")
        config = {"processing": {"lut_path": str(lut_file)}}
        result = get_lut_filter(config)
        assert "interp=tetrahedral" in result

    def test_custom_interpolation(self, tmp_path):
        lut_file = tmp_path / "test.cube"
        lut_file.write_text("# Fake LUT\n")
        config = {"processing": {
            "lut_path": str(lut_file),
            "lut_interpolation": "trilinear",
        }}
        result = get_lut_filter(config)
        assert "interp=trilinear" in result

    def test_relative_lut_path_nonexistent(self):
        """Relative path to a non-existent file should return empty."""
        config = {"processing": {"lut_path": "config/luts/fake_nonexistent.cube"}}
        result = get_lut_filter(config)
        assert result == ""

    def test_no_processing_key(self):
        config = {}
        assert get_lut_filter(config) == ""

    def test_lut_filter_format(self, tmp_path):
        """Verify the filter string has the expected lut3d=...interp=... format."""
        lut_file = tmp_path / "grade.cube"
        lut_file.write_text("# LUT\n")
        config = {"processing": {"lut_path": str(lut_file)}}
        result = get_lut_filter(config)
        assert result.startswith("lut3d=")
        assert ":interp=" in result
