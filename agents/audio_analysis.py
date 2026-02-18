"""Audio analysis agent — determine if L/R channels are distinct (true stereo) or identical."""

import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from agents.base import BaseAgent


class AudioAnalysisAgent(BaseAgent):
    name = "audio_analysis"

    def execute(self) -> dict:
        merged_path = self.episode_dir / "source_merged.mp4"
        work_dir = self.episode_dir / "work"
        work_dir.mkdir(exist_ok=True)

        # Get channel info via ffprobe
        probe = self._ffprobe(merged_path)
        audio_stream = next(
            (s for s in probe["streams"] if s["codec_type"] == "audio"), None
        )
        if not audio_stream:
            raise RuntimeError("No audio stream found in source_merged.mp4")

        channels = int(audio_stream.get("channels", 2))
        sample_rate = int(audio_stream.get("sample_rate", 48000))

        self.logger.info(f"Audio: {channels} channels, {sample_rate} Hz")

        if channels < 2:
            # Mono source — treat as BOTH
            return {
                "channels": channels,
                "sample_rate": sample_rate,
                "classification": "mono_source",
                "audio_channels_identical": True,
                "correlation": 1.0,
                "rms_delta_db": 0.0,
            }

        # Extract L and R channels to separate mono WAV files
        left_wav = work_dir / "left.wav"
        right_wav = work_dir / "right.wav"

        self._extract_channel(merged_path, left_wav, "FL")
        self._extract_channel(merged_path, right_wav, "FR")

        # Load WAV data
        left_data = self._load_wav(left_wav)
        right_data = self._load_wav(right_wav)

        # Ensure same length
        min_len = min(len(left_data), len(right_data))
        left_data = left_data[:min_len]
        right_data = right_data[:min_len]

        # Compute Pearson correlation
        correlation = float(np.corrcoef(left_data, right_data)[0, 1])

        # Compute RMS delta
        left_rms = float(np.sqrt(np.mean(left_data.astype(np.float64) ** 2)))
        right_rms = float(np.sqrt(np.mean(right_data.astype(np.float64) ** 2)))

        if right_rms > 0 and left_rms > 0:
            rms_delta_db = 20 * np.log10(left_rms / right_rms)
        else:
            rms_delta_db = 0.0

        # Classify
        max_corr = self.config.get("processing", {}).get("max_channel_correlation", 0.95)
        max_rms_delta = self.config.get("processing", {}).get("max_channel_rms_ratio_delta", 3.0)

        channels_identical = (
            abs(correlation) > max_corr and abs(rms_delta_db) < max_rms_delta
        )

        classification = "audio_channels_identical" if channels_identical else "true_stereo"
        self.logger.info(
            f"Classification: {classification} "
            f"(corr={correlation:.4f}, rms_delta={rms_delta_db:.2f}dB)"
        )

        return {
            "channels": channels,
            "sample_rate": sample_rate,
            "classification": classification,
            "audio_channels_identical": channels_identical,
            "correlation": round(correlation, 6),
            "rms_delta_db": round(float(rms_delta_db), 2),
        }

    def _extract_channel(self, input_path: Path, output_path: Path, channel: str):
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-af", f"pan=mono|c0={channel}",
            "-ar", "48000",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)

    def _load_wav(self, path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as wf:
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            data = np.frombuffer(raw, dtype=np.int16)
        return data.astype(np.float32)

    def _ffprobe(self, path: Path) -> dict:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
