"""Tests for the ingest agent."""

import json
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from agents.ingest import IngestAgent


def _mock_ffprobe(duration=100.0, creation_time="2026-01-01T10:00:00Z", channels=2):
    """Return a mock ffprobe result dict."""
    return {
        "format": {
            "duration": str(duration),
            "tags": {"creation_time": creation_time},
        },
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080},
            {"codec_type": "audio", "channels": channels, "sample_rate": "48000",
             "bits_per_raw_sample": "32"},
        ],
    }


class TestIngestAgent:
    def test_no_source_path_raises(self, tmp_episode_dir, sample_config):
        agent = IngestAgent(tmp_episode_dir, sample_config)
        with pytest.raises(ValueError, match="source_path not set"):
            agent.execute()

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_single_file_ingest(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=120.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)
        result = agent.execute()

        assert result["file_count"] == 1
        assert result["total_duration_seconds"] == pytest.approx(120.0, abs=1)

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_directory_ingest_filters_resource_forks(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_dir = tmp_path / "DCIM"
        source_dir.mkdir()
        (source_dir / "MVI_0001.MP4").write_bytes(b"\x00" * 100)
        (source_dir / "MVI_0002.MP4").write_bytes(b"\x00" * 100)
        (source_dir / "._MVI_0001.MP4").write_bytes(b"\x00" * 50)  # Resource fork

        mock_probe.return_value = _mock_ffprobe(duration=60.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_dir)
        result = agent.execute()

        # Should only have 2 files (resource fork filtered)
        assert result["file_count"] == 2

    @patch("agents.ingest.ffprobe")
    def test_empty_directory_raises(self, mock_probe, tmp_episode_dir, sample_config, tmp_path):
        source_dir = tmp_path / "empty"
        source_dir.mkdir()

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_dir)

        with pytest.raises(FileNotFoundError, match="No MP4 files"):
            agent.execute()

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_duration_validation_mismatch(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)

        # Return different durations on first and second call
        mock_probe.side_effect = [
            _mock_ffprobe(duration=120.0),  # Source probe
            _mock_ffprobe(duration=50.0),   # Copy validation probe (big mismatch)
        ]

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)

        with pytest.raises(RuntimeError, match="Duration mismatch"):
            agent.execute()

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_duration_validation_within_tolerance(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """A small duration difference (<1s) should not raise."""
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)

        mock_probe.side_effect = [
            _mock_ffprobe(duration=120.0),
            _mock_ffprobe(duration=120.5),  # Within 1.0s tolerance
        ]

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)
        result = agent.execute()

        assert result["file_count"] == 1
        assert result["files"][0]["copy_validated"] is True

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_files_sorted_by_creation_time(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_dir = tmp_path / "DCIM"
        source_dir.mkdir()
        (source_dir / "MVI_0002.MP4").write_bytes(b"\x00" * 100)
        (source_dir / "MVI_0001.MP4").write_bytes(b"\x00" * 100)

        # Return different creation times based on which file is probed
        def side_effect(path):
            if "0001" in str(path):
                return _mock_ffprobe(duration=60.0, creation_time="2026-01-01T10:00:00Z")
            else:
                return _mock_ffprobe(duration=60.0, creation_time="2026-01-01T11:00:00Z")

        mock_probe.side_effect = side_effect

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_dir)
        result = agent.execute()

        # First file should be the one with earlier creation time
        assert "0001" in result["files"][0]["filename"]
        assert "0002" in result["files"][1]["filename"]

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_result_structure(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)
        mock_probe.return_value = _mock_ffprobe(duration=120.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)
        result = agent.execute()

        assert "files" in result
        assert "file_count" in result
        assert "total_duration_seconds" in result
        assert "total_size_bytes" in result
        assert "duration_seconds" in result

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_dest_path_set(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        source_file = tmp_path / "test.MP4"
        source_file.write_bytes(b"\x00" * 100)
        mock_probe.return_value = _mock_ffprobe(duration=120.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_file)
        result = agent.execute()

        assert result["files"][0]["dest_path"] is not None
        assert result["files"][0]["copy_validated"] is True

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_multi_source_path_list(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """Test that source_path can be a list of paths."""
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "clip_a.MP4").write_bytes(b"\x00" * 100)
        (dir_b / "clip_b.mp4").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = [str(dir_a), str(dir_b)]
        result = agent.execute()

        assert result["file_count"] == 2

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_lowercase_mp4_extension(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """Test that lowercase .mp4 files are also discovered."""
        source_dir = tmp_path / "DCIM"
        source_dir.mkdir()
        (source_dir / "video.mp4").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=30.0)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(source_dir)
        result = agent.execute()

        assert result["file_count"] == 1


class TestAudioFileClassification:
    """Test audio track classification from Zoom H6E filenames."""

    @patch.object(IngestAgent, "_sync_audio", return_value={"status": "skipped"})
    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_builtin_mic_classification(self, mock_probe, mock_copy, mock_sync, tmp_episode_dir, sample_config, tmp_path):
        """TrMic suffix should be classified as builtin_mic."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_TrMic.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)
        result = agent.execute()

        tracks = result["audio"]["tracks"]
        assert len(tracks) == 1
        assert tracks[0]["track_type"] == "builtin_mic"

    @patch.object(IngestAgent, "_sync_audio", return_value={"status": "skipped"})
    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_stereo_mix_classification(self, mock_probe, mock_copy, mock_sync, tmp_episode_dir, sample_config, tmp_path):
        """TrLR suffix should be classified as stereo_mix."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_TrLR.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=2)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)
        result = agent.execute()

        tracks = result["audio"]["tracks"]
        assert len(tracks) == 1
        assert tracks[0]["track_type"] == "stereo_mix"

    @patch.object(IngestAgent, "_sync_audio", return_value={"status": "skipped"})
    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_input_track_classification(self, mock_probe, mock_copy, mock_sync, tmp_episode_dir, sample_config, tmp_path):
        """TrN suffix should be classified as input with track_number."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_Tr1.WAV").write_bytes(b"\x00" * 100)
        (audio_dir / "260311_143505_Tr2.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)
        result = agent.execute()

        tracks = result["audio"]["tracks"]
        assert len(tracks) == 2
        for t in tracks:
            assert t["track_type"] == "input"

    @patch.object(IngestAgent, "_sync_audio", return_value={"status": "skipped"})
    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_track_number_extraction(self, mock_probe, mock_copy, mock_sync, tmp_episode_dir, sample_config, tmp_path):
        """Track numbers should be extracted from TrN suffixes."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        for i in range(1, 5):
            (audio_dir / f"260311_143505_Tr{i}.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)
        result = agent.execute()

        tracks = result["audio"]["tracks"]
        track_numbers = sorted([t["track_number"] for t in tracks])
        assert track_numbers == [1, 2, 3, 4]

    @patch.object(IngestAgent, "_sync_audio", return_value={"status": "skipped"})
    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_audio_resource_fork_filtered(self, mock_probe, mock_copy, mock_sync, tmp_episode_dir, sample_config, tmp_path):
        """macOS ._ resource fork WAV files should be filtered out."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_Tr1.WAV").write_bytes(b"\x00" * 100)
        (audio_dir / "._260311_143505_Tr1.WAV").write_bytes(b"\x00" * 50)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)
        result = agent.execute()

        assert result["audio"]["track_count"] == 1

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_no_wav_files_raises(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """Empty audio directory should raise FileNotFoundError."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()

        mock_probe.return_value = _mock_ffprobe(duration=60.0)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)

        with pytest.raises(FileNotFoundError, match="No WAV files"):
            agent.execute()

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_missing_audio_path_raises(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """Non-existent audio_path should raise FileNotFoundError."""
        mock_probe.return_value = _mock_ffprobe(duration=60.0)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(tmp_path / "nonexistent")

        with pytest.raises(FileNotFoundError, match="Audio path not found"):
            agent.execute()

    @patch.object(IngestAgent, "_sync_audio", return_value={"status": "skipped"})
    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_mixed_track_types(self, mock_probe, mock_copy, mock_sync, tmp_episode_dir, sample_config, tmp_path):
        """All three track types should be correctly classified when mixed."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_Tr1.WAV").write_bytes(b"\x00" * 100)
        (audio_dir / "260311_143505_TrLR.WAV").write_bytes(b"\x00" * 100)
        (audio_dir / "260311_143505_TrMic.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)
        result = agent.execute()

        types = {t["track_type"] for t in result["audio"]["tracks"]}
        assert types == {"input", "stereo_mix", "builtin_mic"}


class TestAudioSyncOffset:
    """Test the audio sync cross-correlation logic."""

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_sync_prefers_stereo_mix_and_result_fields(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """Sync should prefer stereo_mix and return all required fields."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_Tr1.WAV").write_bytes(b"\x00" * 100)
        (audio_dir / "260311_143505_TrLR.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)

        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        sr = 16000
        signal = np.zeros(sr * 10, dtype=np.float32)
        signal[sr:sr + 100] = 10000

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)

        with patch.object(agent, "_extract_audio_pcm", return_value=signal):
            result = agent.execute()

        sync = result["audio_sync"]
        assert sync["status"] in ("ok", "low_confidence")
        assert sync["sync_track"] == "260311_143505_TrLR.WAV"
        for field in ("offset_seconds", "tempo_factor", "confidence", "checkpoints",
                       "video_file", "video_duration"):
            assert field in sync, f"Missing field: {field}"

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_sync_fallback_to_builtin_mic(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """If no stereo_mix, sync should fall back to builtin_mic."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_TrMic.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)
        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        signal = np.zeros(16000 * 10, dtype=np.float32)
        signal[16000:16100] = 10000

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)

        with patch.object(agent, "_extract_audio_pcm", return_value=signal):
            result = agent.execute()

        assert result["audio_sync"]["status"] in ("ok", "low_confidence")
        assert result["audio_sync"]["sync_track"] == "260311_143505_TrMic.WAV"

    @patch("shutil.copy2")
    @patch("agents.ingest.ffprobe")
    def test_sync_short_audio_returns_too_short(self, mock_probe, mock_copy, tmp_episode_dir, sample_config, tmp_path):
        """Audio shorter than 2 seconds should return too_short status."""
        audio_dir = tmp_path / "audio_src"
        audio_dir.mkdir()
        (audio_dir / "260311_143505_TrLR.WAV").write_bytes(b"\x00" * 100)

        mock_probe.return_value = _mock_ffprobe(duration=60.0, channels=1)
        video_file = tmp_path / "test.MP4"
        video_file.write_bytes(b"\x00" * 100)

        agent = IngestAgent(tmp_episode_dir, sample_config)
        agent.source_path = str(video_file)
        agent.audio_path = str(audio_dir)

        with patch.object(agent, "_extract_audio_pcm", return_value=np.zeros(100, dtype=np.float32)):
            result = agent.execute()

        assert result["audio_sync"]["status"] == "too_short"

    def test_extract_audio_pcm_failure(self, tmp_episode_dir, sample_config):
        """Failed ffmpeg extraction should raise RuntimeError."""
        agent = IngestAgent(tmp_episode_dir, sample_config)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr=b"error")
            with pytest.raises(RuntimeError, match="ffmpeg audio extraction failed"):
                agent._extract_audio_pcm("/fake/path.mp4", 16000)
