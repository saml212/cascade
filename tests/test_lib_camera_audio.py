"""Tests for the camera-audio L/R split fallback."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from lib import camera_audio


def _stereo_probe():
    return {
        "format": {"duration": "120.0"},
        "streams": [
            {
                "codec_type": "audio",
                "channels": 2,
                "sample_rate": "48000",
            }
        ],
    }


def _mono_probe():
    return {
        "format": {"duration": "120.0"},
        "streams": [
            {
                "codec_type": "audio",
                "channels": 1,
                "sample_rate": "48000",
            }
        ],
    }


def _noaudio_probe():
    return {"format": {"duration": "120.0"}, "streams": []}


@patch("lib.camera_audio.subprocess.run")
@patch("lib.camera_audio.ffprobe")
def test_stereo_emits_two_mono_tracks(mock_probe, mock_run, tmp_path):
    mock_probe.return_value = _stereo_probe()
    mock_run.return_value = MagicMock(returncode=0)
    # Pre-create the output WAVs so the .stat().st_size lookup works
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "camera_Tr1.WAV").write_bytes(b"x" * 1024)
    (audio_dir / "camera_Tr2.WAV").write_bytes(b"x" * 1024)

    tracks = camera_audio.extract_camera_channels(
        tmp_path / "source_merged.mp4", audio_dir
    )

    assert len(tracks) == 2
    assert tracks[0]["filename"] == "camera_Tr1.WAV"
    assert tracks[0]["track_number"] == 1
    assert tracks[0]["track_type"] == "camera_channel"
    assert tracks[1]["track_number"] == 2
    # Frontend mixer regex needs stems ending in _Tr1 / _Tr2 to match — this
    # is the contract that lets camera audio surface in the existing UI.
    assert Path(tracks[0]["filename"]).stem.endswith("_Tr1")
    assert Path(tracks[1]["filename"]).stem.endswith("_Tr2")


@patch("lib.camera_audio.ffprobe")
def test_mono_returns_empty(mock_probe, tmp_path):
    mock_probe.return_value = _mono_probe()
    tracks = camera_audio.extract_camera_channels(
        tmp_path / "source_merged.mp4", tmp_path / "audio"
    )
    assert tracks == []


@patch("lib.camera_audio.ffprobe")
def test_no_audio_stream_returns_empty(mock_probe, tmp_path):
    mock_probe.return_value = _noaudio_probe()
    tracks = camera_audio.extract_camera_channels(
        tmp_path / "source_merged.mp4", tmp_path / "audio"
    )
    assert tracks == []


@patch("lib.camera_audio.subprocess.run")
@patch("lib.camera_audio.ffprobe")
def test_ffmpeg_failure_returns_empty(mock_probe, mock_run, tmp_path):
    mock_probe.return_value = _stereo_probe()
    mock_run.return_value = MagicMock(returncode=1, stderr="boom")
    tracks = camera_audio.extract_camera_channels(
        tmp_path / "source_merged.mp4", tmp_path / "audio"
    )
    assert tracks == []
