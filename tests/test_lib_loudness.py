"""Tests for lib.loudness — ebur128 measurement and parsing."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.loudness import (
    delivery_loudness_policy,
    loudness_status,
    loudnorm_filter,
    measure_loudness,
    parse_loudnorm_analysis,
    require_delivery_loudness,
)

# ---------------------------------------------------------------------------
# Fixture: realistic ebur128 stderr output (trimmed — just the summary block)
# ---------------------------------------------------------------------------
_EBUR128_STDERR_WITH_SUMMARY = """\
[Parsed_ebur128_0 @ 0x600001c00000] Summary:

  Integrated loudness:
    I:         -14.0 LUFS
    Threshold: -54.2 LUFS

  Loudness range:
    LRA:        4.7 LU
    Threshold: -64.2 LUFS
    LRA low:   -52.8 LUFS
    LRA high:  -37.2 LUFS

  True peak:
    Peak:      -1.0 dBFS
"""

# Summary block preceded by some per-moment readings to verify we take LAST
_EBUR128_STDERR_WITH_MOMENTS = """\
[Parsed_ebur128_0 @ 0x1] t: 0.50  M: -22.9  S: -120.7  I: -70.0 LUFS  LRA:   0.0 LU  FTPK:  -6.7 dBFS  TPK:  -6.7 dBFS
[Parsed_ebur128_0 @ 0x1] t: 1.00  M: -18.0  S: -120.7  I: -60.0 LUFS  LRA:   0.0 LU  FTPK:  -5.2 dBFS  TPK:  -5.2 dBFS
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -14.0 LUFS
    Threshold: -54.2 LUFS

  Loudness range:
    LRA:        4.7 LU
    Threshold: -64.2 LUFS
    LRA low:   -52.8 LUFS
    LRA high:  -37.2 LUFS

  True peak:
    Peak:      -1.0 dBFS
"""

_EBUR128_STDERR_NO_AUDIO = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'video_only.mp4':
  Metadata:
    major_brand     : isom
  Duration: 00:00:10.00, start: 0.000000, bitrate: 2000 kb/s
    Stream #0:0: Video: h264, yuv420p, 1920x1080
"""


class TestMeasureLoudnessParsing:
    """Parsing a captured ebur128 stderr fixture."""

    def _make_mock_result(self, stderr: str, returncode: int = 0):
        m = MagicMock()
        m.returncode = returncode
        m.stderr = stderr
        return m

    def test_parses_integrated_lufs(self):
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_SUMMARY)
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert result is not None
        assert result["integrated_lufs"] == -14.0

    def test_parses_true_peak(self):
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_SUMMARY)
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert result["true_peak_dbfs"] == -1.0

    def test_parses_loudness_range(self):
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_SUMMARY)
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert result["loudness_range_lu"] == 4.7

    def test_target_lufs_is_minus_16(self):
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_SUMMARY)
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert result["target_lufs"] == -16

    def test_measured_at_is_iso_utc(self):
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_SUMMARY)
            result = measure_loudness(Path("/fake/longform.mp4"))
        # Should be a parseable ISO string with timezone info
        from datetime import datetime

        dt = datetime.fromisoformat(result["measured_at"])
        assert dt.tzinfo is not None

    def test_takes_last_occurrence_from_moments(self):
        """Summary block values override per-moment inline readings."""
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_MOMENTS)
            result = measure_loudness(Path("/fake/longform.mp4"))
        # Per-moment line has I: -70.0 and I: -60.0; summary says -14.0
        assert result["integrated_lufs"] == -14.0

    def test_result_keys(self):
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_SUMMARY)
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert set(result.keys()) == {
            "integrated_lufs",
            "true_peak_dbfs",
            "loudness_range_lu",
            "target_lufs",
            "measured_at",
        }

    def test_decodes_only_the_first_audio_stream(self):
        with patch("lib.loudness.subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock_result(_EBUR128_STDERR_WITH_SUMMARY)
            measure_loudness(Path("/fake/longform.mp4"))

        command = mock_run.call_args.args[0]
        assert command[command.index("-map") + 1] == "0:a:0"
        assert "-vn" in command


class TestMeasureLoudnessFailure:
    """Returns None on ffmpeg failure."""

    def test_returns_none_on_ffmpeg_error(self):
        m = MagicMock()
        m.returncode = 1
        m.stderr = "some error unrelated to ebur128"
        with patch("lib.loudness.subprocess.run", return_value=m):
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert result is None

    def test_returns_none_when_ffmpeg_not_found(self):
        with patch(
            "lib.loudness.subprocess.run", side_effect=FileNotFoundError("ffmpeg")
        ):
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert result is None

    def test_returns_none_on_empty_stderr(self):
        m = MagicMock()
        m.returncode = 1
        m.stderr = ""
        with patch("lib.loudness.subprocess.run", return_value=m):
            result = measure_loudness(Path("/fake/longform.mp4"))
        assert result is None


class TestMeasureLoudnessNoAudioStream:
    """Returns None when audio stream is missing."""

    def test_returns_none_when_no_audio(self):
        m = MagicMock()
        m.returncode = 1
        m.stderr = _EBUR128_STDERR_NO_AUDIO
        with patch("lib.loudness.subprocess.run", return_value=m):
            result = measure_loudness(Path("/fake/video_only.mp4"))
        assert result is None

    def test_no_ebur128_keyword_in_output(self):
        """Confirms no false positive from video-only file output."""
        assert "ebur128" not in _EBUR128_STDERR_NO_AUDIO


def test_delivery_policy_uses_configured_mastering_targets():
    policy = delivery_loudness_policy(
        {
            "processing": {
                "audio_target_lufs": -15,
                "audio_target_tp": -2,
                "audio_target_lra": 9,
            }
        },
        "shorts",
    )

    assert policy == {
        "schema": "cascade.delivery-loudness/v1",
        "profile": "shorts",
        "target_lufs": -15.0,
        "target_true_peak_dbfs": -2.0,
        "target_loudness_range_lu": 9.0,
        "integrated_tolerance_lu": 1.0,
        "max_true_peak_dbfs": -1.5,
    }


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("audio_target_lufs", -4, "loudness target"),
        ("audio_target_tp", 0.1, "true-peak target"),
        ("audio_target_lra", 0, "loudness-range target"),
        ("audio_loudness_tolerance_lu", -0.1, "tolerance"),
        ("audio_max_true_peak_dbfs", 0.1, "true-peak limit"),
    ],
)
def test_delivery_policy_rejects_invalid_encoder_targets(key, value, message):
    with pytest.raises(ValueError, match=message):
        delivery_loudness_policy({"processing": {key: value}}, "longform")


def test_two_pass_filter_uses_exact_analysis_measurements():
    policy = delivery_loudness_policy({}, "longform")
    analysis = parse_loudnorm_analysis(
        '{"input_i":"-18.2","input_lra":"5.0","input_tp":"-0.7",'
        '"input_thresh":"-28.3","target_offset":"0.1"}'
    )

    value = loudnorm_filter(policy, analysis)

    assert "I=-16.0:TP=-1.5:LRA=11.0" in value
    assert "measured_I=-18.2" in value
    assert "measured_TP=-0.7" in value
    assert "linear=true" in value


@pytest.mark.parametrize(
    ("measurement", "problem"),
    [
        (None, "not measured"),
        ({"integrated_lufs": -13, "true_peak_dbfs": -1.5}, "integrated"),
        ({"integrated_lufs": -16, "true_peak_dbfs": 0.9}, "true peak"),
    ],
)
def test_delivery_loudness_rejects_missing_or_unsafe_measurement(measurement, problem):
    policy = delivery_loudness_policy({}, "longform")

    status = loudness_status(measurement, policy)

    assert status["safe"] is False
    with pytest.raises(RuntimeError, match=problem):
        require_delivery_loudness(measurement, policy)


def test_delivery_loudness_accepts_encoded_result_inside_policy():
    measurement = {
        "integrated_lufs": -16.4,
        "true_peak_dbfs": -1.1,
        "loudness_range_lu": 7.0,
    }

    verified = require_delivery_loudness(
        measurement, delivery_loudness_policy({}, "longform")
    )

    assert verified["verification"]["status"] == "passed"


def test_delivery_verification_records_active_profile_targets():
    policy = delivery_loudness_policy(
        {
            "processing": {
                "audio_target_lufs": -15,
                "audio_target_tp": -2,
            }
        },
        "shorts",
    )

    verified = require_delivery_loudness(
        {
            "integrated_lufs": -15,
            "true_peak_dbfs": -2,
            "target_lufs": -16,
        },
        policy,
    )

    assert verified["target_lufs"] == -15
    assert verified["target_true_peak_dbfs"] == -2
    assert verified["verification"]["measurement"]["target_lufs"] == -15
