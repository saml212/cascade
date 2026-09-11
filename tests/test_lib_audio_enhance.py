"""Tests for lib.audio_enhance — ML denoise + ffmpeg audio enhancement pipeline."""

from unittest.mock import MagicMock, patch

from lib.audio_enhance import (
    _apply_clearervoice,
    _apply_deepfilternet,
    _build_ffmpeg_enhance_filter,
    _build_static_filter_chain,
    enhance_audio,
)


class TestBuildStaticFilterChain:
    def test_default_filter_chain(self):
        af = _build_static_filter_chain({})
        assert af == "highpass=f=80:p=2"

    def test_custom_highpass(self):
        af = _build_static_filter_chain({"audio_highpass_hz": 120})
        assert "highpass=f=120:p=2" in af

    def test_custom_lowpass(self):
        af = _build_static_filter_chain(
            {"audio_enhance_mode": "restoration", "audio_lowpass_hz": 12000}
        )
        assert "lowpass=f=12000" in af

    def test_disabled_highpass(self):
        af = _build_static_filter_chain({"audio_highpass_hz": 0})
        assert "highpass" not in af

    def test_disabled_lowpass(self):
        af = _build_static_filter_chain({"audio_lowpass_hz": 0})
        assert "lowpass" not in af

    def test_disabled_afftdn(self):
        af = _build_static_filter_chain(
            {"audio_enhance_mode": "restoration", "audio_afftdn": False}
        )
        assert "afftdn" not in af

    def test_disabled_declick(self):
        af = _build_static_filter_chain(
            {"audio_enhance_mode": "restoration", "audio_declick": False}
        )
        assert "adeclick" not in af

    def test_disabled_deesser(self):
        af = _build_static_filter_chain(
            {"audio_enhance_mode": "restoration", "audio_deesser": False}
        )
        assert "deesser" not in af

    def test_afftdn_lighter_with_deepfilternet(self):
        """When DeepFilterNet is active, afftdn should run lighter."""
        af = _build_static_filter_chain(
            {
                "audio_enhance_mode": "restoration",
                "audio_denoise_model": "deepfilternet",
            }
        )
        assert "nr=6" in af

    def test_afftdn_full_when_no_ml(self):
        af = _build_static_filter_chain(
            {"audio_enhance_mode": "restoration", "audio_denoise_model": "none"}
        )
        assert "nr=12" in af

    def test_filter_count(self):
        """Restoration enables cleanup but keeps compression opt-in."""
        af = _build_static_filter_chain({"audio_enhance_mode": "restoration"})
        parts = af.split(",")
        assert len(parts) == 5

    def test_restoration_compressor_is_explicit(self):
        af = _build_static_filter_chain(
            {"audio_enhance_mode": "restoration", "audio_compressor": True}
        )
        assert "acompressor=" in af


class TestBuildFfmpegEnhanceFilter:
    """Backwards-compat wrapper tests."""

    def test_includes_loudnorm(self):
        af = _build_ffmpeg_enhance_filter({})
        assert "loudnorm=I=-16" in af  # New default

    def test_custom_lufs_target(self):
        af = _build_ffmpeg_enhance_filter({"audio_target_lufs": -14})
        assert "loudnorm=I=-14" in af

    def test_custom_lra(self):
        af = _build_ffmpeg_enhance_filter({"audio_target_lra": 6})
        assert "LRA=6" in af

    def test_custom_true_peak(self):
        af = _build_ffmpeg_enhance_filter({"audio_target_tp": -2})
        assert "TP=-2" in af


class TestEnhanceAudio:
    def test_disabled_returns_input(self, tmp_path):
        input_wav = tmp_path / "input.wav"
        input_wav.write_bytes(b"fake wav")
        output_wav = tmp_path / "output.wav"
        config = {"processing": {"audio_enhance": False}}
        result = enhance_audio(input_wav, output_wav, config)
        assert result == input_wav

    def test_calls_ffmpeg_with_filter(self, tmp_path):
        input_wav = tmp_path / "input.wav"
        input_wav.write_bytes(b"fake wav")
        output_wav = tmp_path / "output.wav"
        # Disable ML denoise to avoid trying to load DeepFilterNet
        config = {"processing": {"audio_enhance": True, "audio_denoise_model": "none"}}

        with patch("lib.audio_enhance.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            enhance_audio(input_wav, output_wav, config)
            # Two-pass loudnorm: pass 1 (measurement) + pass 2 (normalization)
            assert mock_run.call_count == 2
            # Pass 1 should be analysis with print_format=json
            pass1_cmd = mock_run.call_args_list[0][0][0]
            assert "print_format=json" in pass1_cmd[pass1_cmd.index("-af") + 1]
            # Pass 2 should be the actual rendering pass
            pass2_cmd = mock_run.call_args_list[1][0][0]
            assert pass2_cmd[0] == "ffmpeg"
            af_value = pass2_cmd[pass2_cmd.index("-af") + 1]
            assert "loudnorm" in af_value
            assert af_value.startswith("highpass=f=80:p=2,loudnorm")
            assert "acompressor" not in af_value
            assert "afftdn" not in af_value
            assert "deesser" not in af_value
            assert "alimiter" not in af_value

    def test_ffmpeg_failure_returns_input(self, tmp_path):
        input_wav = tmp_path / "input.wav"
        input_wav.write_bytes(b"fake wav")
        output_wav = tmp_path / "output.wav"
        config = {"processing": {"audio_enhance": True, "audio_denoise_model": "none"}}

        with patch("lib.audio_enhance.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            result = enhance_audio(input_wav, output_wav, config)
            assert result == input_wav  # Falls back to unenhanced

    def test_none_config_uses_defaults(self, tmp_path):
        input_wav = tmp_path / "input.wav"
        input_wav.write_bytes(b"fake wav")
        output_wav = tmp_path / "output.wav"

        with patch("lib.audio_enhance.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            result = enhance_audio(input_wav, output_wav, None)
            assert result == input_wav  # Disabled when config is None


class TestApplyClearervoice:
    def test_skips_when_not_installed(self, tmp_path):
        input_wav = tmp_path / "input.wav"
        output_wav = tmp_path / "output.wav"
        # clearvoice is not installed in test environment
        result = _apply_clearervoice(input_wav, output_wav, "MossFormer2_SE_48K")
        assert result is False


class TestApplyDeepFilterNet:
    def test_handles_missing_input_gracefully(self, tmp_path):
        """If DeepFilterNet is installed but input file is bad, it should return False not crash."""
        input_wav = tmp_path / "nonexistent.wav"
        output_wav = tmp_path / "output.wav"
        # Either DeepFilterNet isn't installed (returns False) or it fails gracefully
        result = _apply_deepfilternet(input_wav, output_wav)
        assert result is False
