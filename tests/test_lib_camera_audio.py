"""Tests for the camera-audio L/R split fallback."""

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from lib import camera_audio
from lib.audio_mix import CAMERA_AUDIO_TIMELINE_FILTER


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
    assert tracks[0]["clock"] == "source"
    assert tracks[0]["timeline_filter"] == CAMERA_AUDIO_TIMELINE_FILTER
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


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe are required",
)
def test_missing_aac_packets_are_padded_on_the_source_clock(tmp_path):
    source = tmp_path / "gapped.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=3",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.2*sin(2*PI*440*t)|0.2*sin(2*PI*660*t):s=48000:d=3",
            "-filter_complex",
            "[1:a]aselect='not(between(t,1,2))'[gapped]",
            "-map",
            "0:v",
            "-map",
            "[gapped]",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    collapsed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(collapsed) / 2 / 48000 == pytest.approx(2.0, abs=0.05)

    tracks = camera_audio.extract_camera_channels(source, tmp_path / "audio")

    assert len(tracks) == 2
    for track in tracks:
        probe = json.loads(
            subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    track["dest_path"],
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        assert float(probe["format"]["duration"]) == pytest.approx(3.0, abs=0.05)

    gap = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            "1.4",
            "-t",
            "0.2",
            "-i",
            tracks[0]["dest_path"],
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    samples = np.frombuffer(gap, dtype="<i2")
    assert len(samples) == pytest.approx(9600, abs=100)
    assert np.max(np.abs(samples.astype(np.int32))) == 0
