"""Measure source-clock camera channels for speaker switching."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import numpy as np

from agents.base import BaseAgent
from lib.atomic_write import atomic_write_json
from lib.audio_mix import CAMERA_AUDIO_TIMELINE_FILTER, episode_audio_tracks
from lib.ffprobe import probe as ffprobe

ANALYSIS_SAMPLE_RATE = 1000
AUDIO_ANALYSIS_VERSION = "source-clock-v3"


def audio_analysis_fingerprint(episode_dir: Path, episode: dict, config: dict) -> str:
    """Identify the media clock, recorder sessions, and analysis semantics."""
    source = episode_dir / "source_merged.mp4"
    paths = [source]
    paths.extend(
        Path(track["dest_path"])
        for track in episode_audio_tracks(episode_dir, episode)
        if track.get("dest_path")
    )
    sources = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            sources.append({"path": str(path.resolve()), "missing": True})
            continue
        sources.append(
            {
                "path": str(path.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    processing = config.get("processing", {})
    payload = {
        "version": AUDIO_ANALYSIS_VERSION,
        "clock": "source",
        "timeline_filter": CAMERA_AUDIO_TIMELINE_FILTER,
        "sample_rate": ANALYSIS_SAMPLE_RATE,
        "audio_sync": episode.get("audio_sync", {}),
        "thresholds": {
            "max_channel_correlation": processing.get("max_channel_correlation", 0.95),
            "max_channel_rms_ratio_delta": processing.get(
                "max_channel_rms_ratio_delta", 3.0
            ),
        },
        "sources": sources,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class AudioAnalysisAgent(BaseAgent):
    name = "audio_analysis"

    def execute(self) -> dict:
        source = self.episode_dir / "source_merged.mp4"
        episode = self.load_json_safe("episode.json")
        source_probe = ffprobe(source)
        audio_stream = next(
            (
                stream
                for stream in source_probe["streams"]
                if stream["codec_type"] == "audio"
            ),
            None,
        )
        if not audio_stream:
            raise RuntimeError("No audio stream found in source_merged.mp4")

        channels = int(audio_stream.get("channels", 2))
        source_rate = int(audio_stream.get("sample_rate", 48000))
        source_duration = float(source_probe.get("format", {}).get("duration", 0))
        fingerprint = audio_analysis_fingerprint(self.episode_dir, episode, self.config)
        base = {
            "channels": channels,
            "sample_rate": source_rate,
            "extracted_sample_rate": ANALYSIS_SAMPLE_RATE,
            "clock": "source",
            "fingerprint": fingerprint,
            "algorithm_version": AUDIO_ANALYSIS_VERSION,
            "source_duration_seconds": source_duration,
        }
        if channels < 2:
            return {
                **base,
                "classification": "mono_source",
                "audio_channels_identical": True,
                "correlation": 1.0,
                "rms_delta_db": 0.0,
            }

        work = self.episode_dir / "work"
        work.mkdir(exist_ok=True)
        cached = self._load_cached_channels(work, fingerprint)
        if cached is None:
            left, right = self._extract_channels(source)
            self._publish_channel_cache(work, fingerprint, left, right)
        else:
            left, right = cached

        sample_count = min(len(left), len(right))
        if sample_count == 0:
            raise RuntimeError("Camera channel extraction produced no samples")
        left = left[:sample_count]
        right = right[:sample_count]
        correlation = self._correlation(left, right)
        left_rms = float(np.sqrt(np.mean(left.astype(np.float64) ** 2)))
        right_rms = float(np.sqrt(np.mean(right.astype(np.float64) ** 2)))
        rms_delta_db = (
            float(20 * np.log10(left_rms / right_rms))
            if left_rms > 0 and right_rms > 0
            else 0.0
        )
        max_corr = self.get_config(
            "processing", "max_channel_correlation", default=0.95
        )
        max_rms_delta = self.get_config(
            "processing", "max_channel_rms_ratio_delta", default=3.0
        )
        identical = abs(correlation) > max_corr and abs(rms_delta_db) < max_rms_delta
        classification = "audio_channels_identical" if identical else "true_stereo"
        decoded_duration = sample_count / ANALYSIS_SAMPLE_RATE
        self.logger.info(
            "Camera channels: %s (corr=%.4f, rms_delta=%.2fdB, %.3fs)",
            classification,
            correlation,
            rms_delta_db,
            decoded_duration,
        )
        return {
            **base,
            "classification": classification,
            "audio_channels_identical": identical,
            "correlation": round(correlation, 6),
            "rms_delta_db": round(rms_delta_db, 2),
            "decoded_duration_seconds": round(decoded_duration, 3),
            "cache": {
                "left": "work/left_channel.npy",
                "right": "work/right_channel.npy",
                "metadata": "work/audio_analysis_cache.json",
            },
        }

    @staticmethod
    def _correlation(left: np.ndarray, right: np.ndarray) -> float:
        if np.std(left) == 0 or np.std(right) == 0:
            return 1.0 if np.array_equal(left, right) else 0.0
        value = float(np.corrcoef(left, right)[0, 1])
        return value if np.isfinite(value) else 0.0

    def _extract_channels(self, source: Path) -> tuple[np.ndarray, np.ndarray]:
        """Decode a compact stereo analysis signal without collapsing AAC PTS gaps."""
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-af",
            CAMERA_AUDIO_TIMELINE_FILTER,
            "-ar",
            str(ANALYSIS_SAMPLE_RATE),
            "-ac",
            "2",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, check=True)
        interleaved = np.frombuffer(result.stdout, dtype=np.int16)
        if len(interleaved) % 2:
            interleaved = interleaved[:-1]
        stereo = interleaved.reshape(-1, 2).astype(np.float32)
        return stereo[:, 0], stereo[:, 1]

    @staticmethod
    def _load_cached_channels(
        work: Path, fingerprint: str
    ) -> tuple[np.ndarray, np.ndarray] | None:
        try:
            metadata = json.loads((work / "audio_analysis_cache.json").read_text())
            if (
                metadata.get("fingerprint") != fingerprint
                or metadata.get("sample_rate") != ANALYSIS_SAMPLE_RATE
            ):
                return None
            left = np.load(work / "left_channel.npy")
            right = np.load(work / "right_channel.npy")
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return left, right

    @staticmethod
    def _publish_channel_cache(
        work: Path, fingerprint: str, left: np.ndarray, right: np.ndarray
    ) -> None:
        for name, data in (("left", left), ("right", right)):
            destination = work / f"{name}_channel.npy"
            temp = work / f"{name}_channel.tmp.npy"
            np.save(temp, data.astype(np.float32, copy=False))
            os.replace(temp, destination)
        atomic_write_json(
            work / "audio_analysis_cache.json",
            {
                "version": AUDIO_ANALYSIS_VERSION,
                "clock": "source",
                "fingerprint": fingerprint,
                "sample_rate": ANALYSIS_SAMPLE_RATE,
                "sample_count": min(len(left), len(right)),
                "timeline_filter": CAMERA_AUDIO_TIMELINE_FILTER,
            },
        )
