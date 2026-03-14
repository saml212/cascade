"""Ingest agent — copy source files from SD card to SSD working storage.

Inputs:
    - source_path: SD card directory (e.g., /Volumes/CAMERA/DCIM/DJI_001/)
    - audio_path: External audio recorder directory (e.g., /Volumes/ZOOM_H6E/260311_143505/)
Outputs:
    - ingest.json: File manifest with paths, durations, sizes, audio sync info
    - source/: Copied MP4 files on SSD
    - audio/: Copied WAV files from external recorder on SSD
Dependencies:
    - ffprobe (duration validation), ffmpeg (audio extraction), numpy (cross-correlation)
Config:
    - paths.output_dir (episode output root)
"""

import shutil
import subprocess
from pathlib import Path

import numpy as np

from agents.base import BaseAgent
from lib.ffprobe import probe as ffprobe


class IngestAgent(BaseAgent):
    name = "ingest"

    def __init__(self, episode_dir: Path, config: dict):
        super().__init__(episode_dir, config)
        self.source_path = None  # Set by pipeline orchestrator
        self.audio_path = None   # Set by pipeline orchestrator (optional)

    def execute(self) -> dict:
        if not self.source_path:
            raise ValueError("source_path not set on IngestAgent")

        dest_dir = self.episode_dir / "source"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # ── Copy video files ──
        copied_files = self._copy_video_files(dest_dir)

        total_duration = sum(f["duration_seconds"] for f in copied_files)
        total_size = sum(f["size_bytes"] for f in copied_files)

        result = {
            "files": copied_files,
            "file_count": len(copied_files),
            "total_duration_seconds": round(total_duration, 3),
            "total_size_bytes": total_size,
            "duration_seconds": round(total_duration, 3),
        }

        # ── Copy external audio files (if provided) ──
        if self.audio_path:
            audio_result = self._copy_audio_files()
            result["audio"] = audio_result

            # ── Sync: cross-correlate camera audio with external audio ──
            if copied_files and audio_result.get("tracks"):
                sync_result = self._sync_audio(copied_files, audio_result)
                result["audio_sync"] = sync_result

        return result

    def _copy_video_files(self, dest_dir: Path) -> list:
        """Discover and copy video MP4 files."""
        # Normalize source_path to a list of paths
        if isinstance(self.source_path, list):
            raw_paths = self.source_path
        else:
            raw_paths = [self.source_path]

        # Collect MP4 files from all source paths
        files = []
        for sp in raw_paths:
            source = Path(sp)
            if source.is_dir():
                # Glob MP4 files (exclude macOS ._ resource forks)
                files.extend(sorted(
                    f for f in list(source.glob("*.MP4")) + list(source.glob("*.mp4"))
                    if not f.name.startswith("._")
                ))
            else:
                files.append(source)

        if not files:
            raise FileNotFoundError(f"No MP4 files found in {raw_paths}")

        # Extract creation_time via ffprobe and sort chronologically
        file_info = []
        for f in files:
            probe = ffprobe(f)
            creation_time = probe.get("format", {}).get("tags", {}).get("creation_time", "")
            duration = float(probe.get("format", {}).get("duration", 0))
            file_info.append({
                "source_path": str(f),
                "filename": f.name,
                "creation_time": creation_time,
                "duration_seconds": round(duration, 3),
                "size_bytes": f.stat().st_size,
            })

        file_info.sort(key=lambda x: x["creation_time"])
        self.logger.info(f"Found {len(file_info)} files, total {sum(f['duration_seconds'] for f in file_info):.1f}s")

        # Copy each file to SSD
        copied_files = []
        for idx, info in enumerate(file_info):
            src = Path(info["source_path"])
            dst = dest_dir / info["filename"]
            self.logger.info(f"Copying {info['filename']} ({info['size_bytes'] / 1e9:.2f} GB)...")
            self.report_progress(idx, len(file_info),
                f"Copying {info['filename']}")
            shutil.copy2(src, dst)

            # Validate copy with ffprobe
            probe = ffprobe(dst)
            copy_duration = float(probe.get("format", {}).get("duration", 0))
            if abs(copy_duration - info["duration_seconds"]) > 1.0:
                raise RuntimeError(
                    f"Duration mismatch after copy: {info['filename']} "
                    f"(source={info['duration_seconds']:.1f}s, copy={copy_duration:.1f}s)"
                )

            info["dest_path"] = str(dst)
            info["copy_validated"] = True
            copied_files.append(info)

        return copied_files

    def _copy_audio_files(self) -> dict:
        """Copy WAV files from external audio recorder."""
        audio_dir = self.episode_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        source = Path(self.audio_path)
        if not source.exists():
            raise FileNotFoundError(f"Audio path not found: {self.audio_path}")

        # Find WAV files (Zoom H6E naming: 260311_143505_Tr1.WAV etc.)
        wav_files = sorted(
            f for f in list(source.glob("*.WAV")) + list(source.glob("*.wav"))
            if not f.name.startswith("._")
        )

        if not wav_files:
            raise FileNotFoundError(f"No WAV files found in {self.audio_path}")

        tracks = []
        for f in wav_files:
            probe = ffprobe(f)
            audio_stream = next(
                (s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None
            )
            duration = float(probe.get("format", {}).get("duration", 0))
            channels = int(audio_stream.get("channels", 1)) if audio_stream else 1
            sample_rate = int(audio_stream.get("sample_rate", 48000)) if audio_stream else 48000
            bits = audio_stream.get("bits_per_raw_sample", "32") if audio_stream else "32"

            # Classify track type from filename
            name = f.stem
            if name.endswith("_TrMic"):
                track_type = "builtin_mic"
            elif name.endswith("_TrLR"):
                track_type = "stereo_mix"
            else:
                # Extract track number: Tr1, Tr2, etc.
                track_type = "input"

            # Copy
            dst = audio_dir / f.name
            self.logger.info(f"Copying audio {f.name} ({f.stat().st_size / 1e6:.1f} MB)")
            shutil.copy2(f, dst)

            track_info = {
                "source_path": str(f),
                "dest_path": str(dst),
                "filename": f.name,
                "track_type": track_type,
                "channels": channels,
                "sample_rate": sample_rate,
                "bits": bits,
                "duration_seconds": round(duration, 3),
                "size_bytes": f.stat().st_size,
            }

            # Extract track number for input tracks
            for suffix in ["_Tr1", "_Tr2", "_Tr3", "_Tr4", "_Tr5", "_Tr6"]:
                if name.endswith(suffix):
                    track_info["track_number"] = int(suffix[-1])
                    break

            tracks.append(track_info)

        self.logger.info(f"Copied {len(tracks)} audio tracks")
        return {
            "source_path": self.audio_path,
            "tracks": tracks,
            "track_count": len(tracks),
        }

    def _sync_audio(self, video_files: list, audio_result: dict) -> dict:
        """Multi-point cross-correlation sync between camera audio and H6E.

        Extracts full audio from both sources once, then slices numpy arrays
        for each checkpoint. Fits linear regression to drift profile for a
        single tempo correction factor.
        """
        self.report_progress(0, 1, "Syncing audio")

        # Sync against merged video (handles multi-file gaps correctly)
        merged_path = self.episode_dir / "source_merged.mp4"
        video_path = str(merged_path) if merged_path.exists() else video_files[0]["dest_path"]
        video_duration = sum(f["duration_seconds"] for f in video_files)

        # Pick best H6E track: stereo_mix > builtin_mic > input
        sync_track = None
        for pref in ["stereo_mix", "builtin_mic", "input"]:
            for t in audio_result["tracks"]:
                if t["track_type"] == pref:
                    sync_track = t
                    break
            if sync_track:
                break
        if not sync_track:
            return {"status": "no_sync_track"}

        self.logger.info(f"Syncing against {sync_track['filename']}")
        sr = 16000

        # Extract FULL audio from both sources once (2 ffmpeg calls total)
        cam_full = self._extract_audio_pcm(video_path, sr)
        h6e_full = self._extract_audio_pcm(sync_track["dest_path"], sr)

        if len(cam_full) < sr * 2 or len(h6e_full) < sr * 2:
            return {"status": "too_short"}

        # Step 1: Initial offset from first 5 minutes
        init_samples = min(sr * 300, len(cam_full), len(h6e_full))
        init_offset, init_confidence = self._correlate(
            cam_full[:init_samples], h6e_full[:init_samples], sr
        )
        self.logger.info(f"Initial offset={init_offset:.4f}s, confidence={init_confidence:.4f}")

        # Step 2: Multi-point drift measurement by slicing numpy arrays
        window = 15  # seconds per checkpoint window
        window_samples = sr * window
        interval = 10  # seconds between checkpoints
        interval_samples = sr * interval
        offset_samples = int(init_offset * sr)
        checkpoints = []

        num_checkpoints = int((video_duration - window) / interval)
        for i in range(num_checkpoints):
            cam_start = i * interval_samples
            h6e_start = cam_start + offset_samples

            if h6e_start < 0 or cam_start + window_samples > len(cam_full) \
               or h6e_start + window_samples > len(h6e_full):
                continue

            local_offset, conf = self._correlate(
                cam_full[cam_start:cam_start + window_samples],
                h6e_full[h6e_start:h6e_start + window_samples],
                sr,
            )
            checkpoints.append({
                "time": round(i * interval, 1),
                "offset": round(init_offset + local_offset, 6),
                "confidence": round(conf, 4),
            })

        self.logger.info(f"Collected {len(checkpoints)} drift checkpoints")

        # Step 3: Linear regression on high-confidence checkpoints
        good = [c for c in checkpoints if c["confidence"] > 0.10]

        base_result = {
            "sync_track": sync_track["filename"],
            "video_file": Path(video_path).name,
            "video_duration": round(video_duration, 3),
            "confidence": round(init_confidence, 6),
            "checkpoints": checkpoints,
        }

        if len(good) < 3:
            self.logger.warning(f"Only {len(good)} good checkpoints, skipping drift correction")
            return {
                **base_result,
                "status": "low_confidence" if init_confidence < 0.10 else "ok",
                "offset_seconds": round(init_offset, 6),
                "tempo_factor": 1.0,
                "drift_rate_ppm": 0.0,
                "drift_total_seconds": 0.0,
            }

        times = np.array([c["time"] for c in good])
        offsets = np.array([c["offset"] for c in good])

        # offset(t) = intercept + slope * t
        mean_t, mean_o = np.mean(times), np.mean(offsets)
        slope = float(np.sum((times - mean_t) * (offsets - mean_o))
                       / (np.sum((times - mean_t) ** 2) + 1e-15))
        intercept = float(mean_o - slope * mean_t)

        tempo_factor = 1.0 + slope
        drift_ppm = slope * 1e6
        drift_total = slope * video_duration

        # R-squared for quality assessment
        ss_res = float(np.sum((offsets - (intercept + slope * times)) ** 2))
        ss_tot = float(np.sum((offsets - mean_o) ** 2))
        r_sq = 1.0 - ss_res / (ss_tot + 1e-15) if ss_tot > 1e-15 else 1.0

        status = "ok"
        if init_confidence < 0.10:
            status = "low_confidence"
        elif r_sq < 0.5 and abs(drift_total) > 0.1:
            status = "low_confidence"

        self.logger.info(
            f"Drift: {drift_total:.4f}s over {video_duration:.0f}s "
            f"({drift_ppm:.1f}ppm), tempo={tempo_factor:.8f}, R²={r_sq:.4f}"
        )

        return {
            **base_result,
            "status": status,
            "offset_seconds": round(intercept, 6),
            "tempo_factor": tempo_factor,
            "drift_rate_ppm": round(drift_ppm, 2),
            "drift_total_seconds": round(drift_total, 6),
            "r_squared": round(r_sq, 4),
        }

    @staticmethod
    def _correlate(a: np.ndarray, b: np.ndarray, sr: int) -> tuple[float, float]:
        """FFT cross-correlation. Returns (offset_seconds, confidence)."""
        a = a - np.mean(a)
        b = b - np.mean(b)
        fft_size = 2 ** int(np.ceil(np.log2(len(a) + len(b) - 1)))
        cc = np.real(np.fft.ifft(
            np.fft.fft(a, fft_size) * np.conj(np.fft.fft(b, fft_size))
        ))
        max_idx = int(np.argmax(cc))
        if max_idx > fft_size // 2:
            max_idx -= fft_size
        energy = np.sqrt(np.sum(a ** 2) * np.sum(b ** 2)) + 1e-10
        return max_idx / sr, float(cc[max_idx % fft_size]) / energy

    def _extract_audio_pcm(self, path: str, sr: int) -> np.ndarray:
        """Extract full mono audio as float32 numpy array via ffmpeg."""
        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-ar", str(sr), "-ac", "1",
            "-f", "s16le", "-acodec", "pcm_s16le", "-",
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr[:500]}")
        return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
