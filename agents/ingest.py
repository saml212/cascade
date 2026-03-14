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
        """Cross-correlate camera audio with external recorder to find sync offset."""
        self.logger.info("Computing audio sync offset...")
        self.report_progress(0, 1, "Syncing audio")

        # Use the first (or longest) video file for sync
        video_path = video_files[0]["dest_path"]
        if len(video_files) > 1:
            video_path = max(video_files, key=lambda f: f["duration_seconds"])["dest_path"]

        # Pick the best H6E track for correlation:
        # Prefer stereo_mix (TrLR), fallback to builtin_mic (TrMic), then first input track
        sync_track = None
        for pref in ["stereo_mix", "builtin_mic", "input"]:
            for t in audio_result["tracks"]:
                if t["track_type"] == pref:
                    sync_track = t
                    break
            if sync_track:
                break

        if not sync_track:
            self.logger.warning("No suitable audio track found for sync")
            return {"status": "no_sync_track"}

        self.logger.info(f"Syncing camera audio against {sync_track['filename']}")

        sr = 16000  # Downsample for speed
        # Only use first 3 minutes for sync — the clap is always near the start,
        # and full-length FFT on long recordings can exhaust memory
        max_sync_seconds = 180

        # Extract mono audio from camera video
        cam_audio = self._extract_audio_pcm(video_path, sr, max_duration=max_sync_seconds)
        # Extract mono audio from H6E track
        h6e_audio = self._extract_audio_pcm(sync_track["dest_path"], sr, max_duration=max_sync_seconds)

        if len(cam_audio) < sr or len(h6e_audio) < sr:
            self.logger.warning("Audio too short for reliable sync")
            return {"status": "too_short"}

        # Normalize (remove DC offset)
        cam_audio = cam_audio - np.mean(cam_audio)
        h6e_audio = h6e_audio - np.mean(h6e_audio)

        # FFT-based cross-correlation
        n = len(cam_audio) + len(h6e_audio) - 1
        fft_size = 2 ** int(np.ceil(np.log2(n)))
        cc = np.real(np.fft.ifft(
            np.fft.fft(cam_audio, fft_size) * np.conj(np.fft.fft(h6e_audio, fft_size))
        ))

        max_idx = int(np.argmax(cc))
        if max_idx > fft_size // 2:
            max_idx -= fft_size

        offset_seconds = max_idx / sr
        confidence = float(cc[max_idx % fft_size]) / (
            np.sqrt(np.sum(cam_audio**2) * np.sum(h6e_audio**2)) + 1e-10
        )

        self.logger.info(
            f"Audio sync: offset={offset_seconds:.4f}s "
            f"(H6E starts {abs(offset_seconds):.2f}s "
            f"{'after' if offset_seconds < 0 else 'before'} camera), "
            f"confidence={confidence:.4f}"
        )

        sync_result = {
            "status": "synced",
            "offset_seconds": round(offset_seconds, 4),
            "offset_samples": max_idx,
            "sync_sample_rate": sr,
            "sync_track": sync_track["filename"],
            "video_file": Path(video_path).name,
            "confidence": round(confidence, 6),
            "description": (
                f"H6E audio starts {abs(offset_seconds):.2f}s "
                f"{'after' if offset_seconds < 0 else 'before'} camera video. "
                f"To align: camera_time + offset = h6e_time"
            ),
        }

        # ── Measure drift by correlating at the END of the recording ──
        # Independent clocks drift over time; measure offset at the end
        # and compute a tempo correction factor for audio_mix.
        total_dur = None
        for t in audio_result["tracks"]:
            if t.get("duration_seconds"):
                total_dur = t["duration_seconds"]
                break
        if not total_dur:
            total_dur = 3600  # fallback

        if total_dur > 300:
            # Measure offset at the end (last 60 seconds)
            end_start = max(0, total_dur - 120)  # start extraction 120s from end
            cam_end = self._extract_audio_pcm(video_path, sr, seek=end_start, max_duration=60)
            h6e_end = self._extract_audio_pcm(sync_track["dest_path"], sr, seek=end_start, max_duration=60)

            if len(cam_end) > sr * 5 and len(h6e_end) > sr * 5:
                cam_end = cam_end - np.mean(cam_end)
                h6e_end = h6e_end - np.mean(h6e_end)
                n_end = len(cam_end) + len(h6e_end) - 1
                fft_end = 2 ** int(np.ceil(np.log2(n_end)))
                cc_end = np.real(np.fft.ifft(
                    np.fft.fft(cam_end, fft_end) * np.conj(np.fft.fft(h6e_end, fft_end))
                ))
                end_idx = int(np.argmax(cc_end))
                if end_idx > fft_end // 2:
                    end_idx -= fft_end
                end_offset = end_idx / sr

                end_conf = float(cc_end[end_idx % fft_end]) / (
                    np.sqrt(np.sum(cam_end**2) * np.sum(h6e_end**2)) + 1e-10
                )

                drift_total = end_offset - offset_seconds
                drift_rate_ppm = (drift_total / total_dur) * 1e6
                tempo_factor = 1.0 + drift_total / total_dur

                self.logger.info(
                    f"Audio drift: {drift_total:.4f}s over {total_dur:.0f}s "
                    f"({drift_rate_ppm:.1f} ppm), tempo correction: {tempo_factor:.8f}"
                )

                sync_result["end_offset_seconds"] = round(end_offset, 4)
                sync_result["end_confidence"] = round(end_conf, 6)
                sync_result["drift_total_seconds"] = round(drift_total, 6)
                sync_result["drift_rate_ppm"] = round(drift_rate_ppm, 1)
                sync_result["tempo_factor"] = tempo_factor
            else:
                self.logger.warning(
                    "End-of-recording audio too short for drift measurement"
                )
        else:
            self.logger.info(
                f"Recording too short ({total_dur:.0f}s) for drift measurement, skipping"
            )

        return sync_result

    def _extract_audio_pcm(self, input_path: str, sample_rate: int, seek: float = 0, max_duration: int = None) -> np.ndarray:
        """Extract mono audio as float32 numpy array."""
        cmd = ["ffmpeg", "-y"]
        if seek > 0:
            cmd += ["-ss", str(seek)]
        cmd += ["-i", str(input_path)]
        if max_duration:
            cmd += ["-t", str(max_duration)]
        cmd += [
            "-ar", str(sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-"
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr[:500]}")
        return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
