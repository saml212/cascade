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

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

from agents.base import BaseAgent
from lib.audio_mix import CAMERA_AUDIO_TIMELINE_FILTER
from lib.ffprobe import get_video_properties
from lib.ffprobe import probe as ffprobe

_DJI_VIDEO_RE = re.compile(r"^DJI_(\d{14})_(\d+)_([A-Za-z])\.mp4$", re.IGNORECASE)
_NATURAL_PART_RE = re.compile(r"(\d+)")


def video_sort_key(path_or_info) -> tuple:
    """Order discovered camera clips by stable filename sequence metadata."""
    if isinstance(path_or_info, dict):
        name = path_or_info.get("filename") or Path(path_or_info["dest_path"]).name
        source = Path(path_or_info.get("source_path") or path_or_info["dest_path"])
    else:
        source = Path(path_or_info)
        name = source.name
    dji_match = _DJI_VIDEO_RE.match(name)
    if dji_match:
        timestamp, sequence, camera = dji_match.groups()
        # Keep independent camera/session directories grouped so a sequence
        # counter reset does not interleave separate recording sessions.
        session = tuple(
            int(part) if part.isdigit() else part.lower()
            for part in _NATURAL_PART_RE.split(source.parent.name)
        )
        return (0, session, camera.lower(), int(sequence), timestamp, name.lower())
    natural = tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _NATURAL_PART_RE.split(name)
    )
    return (1, natural)


class IngestAgent(BaseAgent):
    name = "ingest"

    def __init__(self, episode_dir: Path, config: dict):
        super().__init__(episode_dir, config)
        self.source_path = None  # Set by pipeline orchestrator
        self.audio_path = None  # Set by pipeline orchestrator (optional)

    def execute(self) -> dict:
        if not self.source_path:
            raise ValueError("source_path not set on IngestAgent")

        dest_dir = self.episode_dir / "source"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # ── Copy video files ──
        copied_files = self._copy_video_files(dest_dir)

        total_duration = sum(f["duration_seconds"] for f in copied_files)
        total_size = sum(f["size_bytes"] for f in copied_files)

        # Capture source video properties (fps, codec, pix_fmt, color space)
        # from the first file — DJI files within a session share settings.
        source_properties = {}
        if copied_files:
            try:
                first_file = Path(copied_files[0]["dest_path"])
                source_properties = get_video_properties(first_file)
                self.logger.info(
                    "Source: %dx%d %s %.3f fps %s",
                    source_properties.get("width", 0),
                    source_properties.get("height", 0),
                    source_properties.get("codec", "?"),
                    source_properties.get("fps", 0),
                    source_properties.get("pix_fmt", "?"),
                )
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                KeyError,
                ValueError,
                StopIteration,
            ) as e:
                self.logger.warning("Could not read source properties: %s", e)

        result = {
            "files": copied_files,
            "source_order_authoritative": self._source_order_is_authoritative(),
            "file_count": len(copied_files),
            "total_duration_seconds": round(total_duration, 3),
            "total_size_bytes": total_size,
            "duration_seconds": round(total_duration, 3),
            "source_properties": source_properties,
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
                files.extend(
                    sorted(
                        [
                            f
                            for f in list(source.glob("*.MP4"))
                            + list(source.glob("*.mp4"))
                            if not f.name.startswith("._")
                        ],
                        key=video_sort_key,
                    )
                )
            else:
                files.append(source)

        if not files:
            raise FileNotFoundError(f"No MP4 files found in {raw_paths}")

        # Probe metadata for the manifest. Embedded creation_time is retained
        # for diagnostics but is not trusted for ordering; cameras sometimes
        # write a wrong tag on one split clip.
        file_info = []
        for f in files:
            probe = ffprobe(f)
            creation_time = (
                probe.get("format", {}).get("tags", {}).get("creation_time", "")
            )
            duration = float(probe.get("format", {}).get("duration", 0))
            file_info.append(
                {
                    "source_path": str(f),
                    "filename": f.name,
                    "creation_time": creation_time,
                    "duration_seconds": round(duration, 3),
                    "size_bytes": f.stat().st_size,
                }
            )

        if not self._source_order_is_authoritative():
            file_info.sort(key=video_sort_key)
        self.logger.info(
            f"Found {len(file_info)} files, total {sum(f['duration_seconds'] for f in file_info):.1f}s"
        )

        # Copy each file to SSD
        copied_files = []
        for idx, info in enumerate(file_info):
            src = Path(info["source_path"])
            dst = dest_dir / info["filename"]
            self.logger.info(
                f"Copying {info['filename']} ({info['size_bytes'] / 1e9:.2f} GB)..."
            )
            self.report_progress(idx, len(file_info), f"Copying {info['filename']}")
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

    def _source_order_is_authoritative(self) -> bool:
        """An explicit list of files is a caller-supplied edit order."""
        return isinstance(self.source_path, list) and all(
            not Path(path).is_dir() for path in self.source_path
        )

    def _copy_audio_files(self) -> dict:
        """Copy WAV files from external audio recorder."""
        audio_dir = self.episode_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        source = Path(self.audio_path)
        if not source.exists():
            raise FileNotFoundError(f"Audio path not found: {self.audio_path}")

        # Find WAV files (Zoom H6E naming: 260311_143505_Tr1.WAV etc.)
        # Search top-level first, then subdirectories (H6E stores files in session folders)
        wav_files = sorted(
            f
            for f in list(source.glob("*.WAV")) + list(source.glob("*.wav"))
            if not f.name.startswith("._")
        )
        if not wav_files:
            # Search one level deep (e.g., /Volumes/ZOOM_H6E/260311_162356/*.WAV)
            # Use the most recent session folder
            subdirs = sorted(
                (
                    d
                    for d in source.iterdir()
                    if d.is_dir() and not d.name.startswith((".", "TRASH", "ZOOM"))
                ),
                key=lambda d: d.name,
                reverse=True,
            )
            for subdir in subdirs:
                wav_files = sorted(
                    f
                    for f in list(subdir.glob("*.WAV")) + list(subdir.glob("*.wav"))
                    if not f.name.startswith("._")
                )
                if wav_files:
                    self.logger.info(f"Found audio in subdirectory: {subdir.name}")
                    break

        if not wav_files:
            raise FileNotFoundError(
                f"No WAV files found in {self.audio_path} or its subdirectories"
            )

        tracks = []
        for f in wav_files:
            probe = ffprobe(f)
            audio_stream = next(
                (s for s in probe.get("streams", []) if s.get("codec_type") == "audio"),
                None,
            )
            duration = float(probe.get("format", {}).get("duration", 0))
            channels = int(audio_stream.get("channels", 1)) if audio_stream else 1
            sample_rate = (
                int(audio_stream.get("sample_rate", 48000)) if audio_stream else 48000
            )
            bits = (
                audio_stream.get("bits_per_raw_sample", "32") if audio_stream else "32"
            )

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
            self.logger.info(
                f"Copying audio {f.name} ({f.stat().st_size / 1e6:.1f} MB)"
            )
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
        """Sync H6E audio to camera video using GCC-PHAT and robust clock fitting.

        Strategy:
            1. Find best sync track via short-window GCC-PHAT (which H6E track
               sounds most like the camera mic).
            2. Measure bounded windows throughout the overlapping camera and
               recorder duration, rejecting silent and inconsistent anchors.
            3. Fit offset and drift across the retained anchors, then apply tempo
               correction only when the fit is confident and plausible.

        GCC-PHAT (Generalized Cross-Correlation with Phase Transform) is the
        gold standard for time-delay estimation between two mics. It whitens
        the cross-spectrum so the correlation is dominated by phase agreement,
        not amplitude — giving sub-sample precision and robust performance
        across mics with different frequency responses.

        Resolution: 62.5 µs per measurement at 16 kHz (vs 50 ms for the old
        envelope approach — an 800x precision improvement). Frame-accurate.
        """
        self.report_progress(0, 1, "Syncing audio")

        video_duration = sum(f["duration_seconds"] for f in video_files)

        tracks = audio_result.get("tracks", [])
        if not tracks:
            return {"status": "no_sync_track"}

        sr = 16000
        # Sync runs during ingest, before the current manifest has been stitched.
        # Build the camera clock from every current clip in manifest order; an
        # existing source_merged.mp4 may belong to an earlier ingest attempt.
        camera_chunks = []
        decoded_camera_samples = 0
        for video_file in video_files:
            chunk = self._extract_audio_pcm(video_file["dest_path"], sr)
            decoded_camera_samples += len(chunk)
            expected_samples = round(float(video_file["duration_seconds"]) * sr)
            if len(chunk) < expected_samples:
                chunk = np.pad(chunk, (0, expected_samples - len(chunk)))
            else:
                chunk = chunk[:expected_samples]
            camera_chunks.append(chunk)
        if decoded_camera_samples < sr * 5:
            return {"status": "too_short"}
        cam_full = np.concatenate(camera_chunks)
        if len(cam_full) < sr * 5:
            return {"status": "too_short"}

        # ── Step 1: Pick the best H6E track via GCC-PHAT search ──
        # Use 60s anchor window if available, else half of total length.
        # Whichever H6E track has the highest peak coherence wins.
        anchor_window = min(60, max(5, len(cam_full) // sr // 2))
        self.logger.info(
            f"Step 1: Finding sync track via GCC-PHAT ({anchor_window}s window)..."
        )
        cam_anchor_start = cam_full[: sr * anchor_window]

        # Consecutive recorder sessions repeat Tr1/Tr2/etc. Treat each logical
        # microphone as one continuous signal in manifest order.
        track_groups = {}
        for track in tracks:
            key = (
                track.get("track_number")
                or track.get("track_type")
                or track["filename"]
            )
            track_groups.setdefault(key, []).append(track)

        track_results = []
        for group in track_groups.values():
            chunks = []
            try:
                for track in group:
                    chunks.append(self._extract_audio_pcm(track["dest_path"], sr))
            except (OSError, RuntimeError) as e:
                self.logger.warning("  %s: extract failed: %s", group[0]["filename"], e)
                continue
            h6e = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
            if len(h6e) < sr * anchor_window:
                continue
            h6e_anchor = h6e[: sr * anchor_window]
            offset, conf = self._gcc_phat(
                cam_anchor_start, h6e_anchor, sr, max_lag_s=30
            )
            track_results.append(
                {
                    "track": group[0],
                    "offset": offset,
                    "confidence": conf,
                    "h6e_full": h6e,
                }
            )
            self.logger.info(
                "  %s (%d segment%s): offset=%+.4fs conf=%.4f",
                group[0]["filename"],
                len(group),
                "s" if len(group) != 1 else "",
                offset,
                conf,
            )

        if not track_results:
            return {"status": "no_sync_track"}

        # Best track = highest confidence (GCC-PHAT coherence)
        best = max(track_results, key=lambda r: r["confidence"])
        sync_track = best["track"]
        h6e_full = best["h6e_full"]
        anchor_start_offset = best["offset"]
        anchor_start_conf = best["confidence"]

        self.logger.info(
            f"Selected: {sync_track['filename']} "
            f"start_offset={anchor_start_offset:+.6f}s conf={anchor_start_conf:.4f}"
        )

        base_result = {
            "sync_track": sync_track["filename"],
            "video_file": video_files[0].get("filename")
            or Path(video_files[0]["dest_path"]).name,
            "video_duration": round(video_duration, 3),
            "confidence": round(anchor_start_conf, 6),
            "anchor_start_offset": round(anchor_start_offset, 6),
            "anchor_start_confidence": round(anchor_start_conf, 6),
        }
        fit = self._fit_sync_anchors(
            cam_full, h6e_full, sr, anchor_start_offset, video_duration
        )
        if fit is not None:
            offset, slope, r_squared, points, end_conf = fit
            drift_ppm = slope * 1e6
            if (
                anchor_start_conf >= 0.30
                and abs(drift_ppm) <= 500
                and r_squared >= 0.80
            ):
                return {
                    **base_result,
                    "status": "ok",
                    "offset_seconds": round(offset, 6),
                    "tempo_factor": 1.0 + slope,
                    "drift_rate_ppm": round(drift_ppm, 4),
                    "drift_total_seconds": round(slope * video_duration, 6),
                    "r_squared": round(r_squared, 4),
                    "anchor_end_offset": round(offset + slope * video_duration, 6),
                    "anchor_end_confidence": round(end_conf, 6),
                    "drift_status": "applied",
                    "calibration_points": points,
                }
            self.logger.warning(
                "Rejected multi-anchor fit: %.1f ppm, R² %.3f", drift_ppm, r_squared
            )
            return {
                **base_result,
                "status": "ok" if anchor_start_conf >= 0.30 else "low_confidence",
                "offset_seconds": round(anchor_start_offset, 6),
                "tempo_factor": 1.0,
                "drift_rate_ppm": 0.0,
                "drift_total_seconds": 0.0,
                "r_squared": round(r_squared, 4),
                "anchor_end_offset": None,
                "anchor_end_confidence": round(end_conf, 6),
                "drift_status": "skipped_unreliable_fit",
                "calibration_points": points,
            }
        return {
            **base_result,
            "status": "ok" if anchor_start_conf >= 0.30 else "low_confidence",
            "offset_seconds": round(anchor_start_offset, 6),
            "tempo_factor": 1.0,
            "drift_rate_ppm": 0.0,
            "drift_total_seconds": 0.0,
            "r_squared": 0.0,
            "anchor_end_offset": None,
            "anchor_end_confidence": 0.0,
            "drift_status": "skipped_insufficient_anchors",
            "calibration_points": 0,
        }

    @classmethod
    def _fit_sync_anchors(
        cls,
        camera: np.ndarray,
        recorder: np.ndarray,
        sr: int,
        initial_offset: float,
        video_duration: float,
    ) -> tuple[float, float, float, int, float] | None:
        """Fit offset and recorder clock drift from bounded windows."""
        available_video = min(video_duration, len(camera) / sr)
        window = min(30.0, max(5.0, available_video / 12))
        last_start = available_video - window
        if last_start <= window:
            return None

        anchors = []
        for time_s in np.linspace(0, last_start, 9):
            sample_count = int(window * sr)
            cam_start = int(time_s * sr)
            rec_start = int((time_s + initial_offset) * sr)
            if rec_start < 0 or rec_start + sample_count > len(recorder):
                continue
            local_offset, confidence = cls._gcc_phat(
                camera[cam_start : cam_start + sample_count],
                recorder[rec_start : rec_start + sample_count],
                sr,
                max_lag_s=0.75,
            )
            if confidence >= 0.15:
                anchors.append((time_s, initial_offset + local_offset, confidence))

        if len(anchors) < 3:
            return None
        times = np.array([anchor[0] for anchor in anchors])
        offsets = np.array([anchor[1] for anchor in anchors])
        weights = np.array([anchor[2] for anchor in anchors])
        slope, offset = np.polyfit(times, offsets, 1, w=weights)
        predicted = offset + slope * times
        residuals = offsets - predicted
        keep = np.abs(residuals - np.median(residuals)) <= 0.02
        if keep.sum() < 3:
            return None
        if not np.all(keep):
            times, offsets, weights = times[keep], offsets[keep], weights[keep]
            slope, offset = np.polyfit(times, offsets, 1, w=weights)
            predicted = offset + slope * times

        mean_offset = np.average(offsets, weights=weights)
        denominator = np.sum(weights * (offsets - mean_offset) ** 2)
        error = np.sum(weights * (offsets - predicted) ** 2)
        r_squared = 1.0 - error / denominator if denominator > 1e-12 else 1.0
        return (
            float(offset),
            float(slope),
            float(r_squared),
            len(times),
            float(weights[-1]),
        )

    @staticmethod
    def _gcc_phat(
        ref: np.ndarray,
        sig: np.ndarray,
        sr: int,
        max_lag_s: float = 30.0,
    ) -> tuple[float, float]:
        """GCC-PHAT (Generalized Cross-Correlation with Phase Transform).

        The standard time-delay estimation method for two microphones picking
        up the same source. Whitens the cross-spectrum (divides by magnitude)
        so the correlation is dominated by PHASE agreement rather than amplitude.
        This makes it robust to mics with different frequency responses — exactly
        the camera-vs-H6E case.

        Returns (offset_seconds, confidence). Offset is positive if `sig` lags
        `ref` (i.e. shift sig forward in time to align with ref). Confidence is
        the normalized peak height in [0, 1] — values > 0.3 indicate a confident
        match.

        Reference: Knapp & Carter, "The Generalized Correlation Method for
        Estimation of Time Delay" (IEEE 1976).
        """
        if len(ref) == 0 or len(sig) == 0:
            return 0.0, 0.0
        if float(np.std(ref)) < 1e-8 or float(np.std(sig)) < 1e-8:
            return 0.0, 0.0
        n = max(len(ref), len(sig))
        # Zero-pad to next power of 2 for FFT efficiency
        fft_size = 2 ** int(np.ceil(np.log2(2 * n)))

        REF = np.fft.rfft(ref.astype(np.float64), fft_size)
        SIG = np.fft.rfft(sig.astype(np.float64), fft_size)

        # Cross-spectrum
        cross = REF * np.conj(SIG)
        # Phase Transform: divide by magnitude (whitens the spectrum)
        magnitude = np.abs(cross) + 1e-12
        cross_white = cross / magnitude

        # Inverse FFT to get the GCC-PHAT correlation in time domain
        cc = np.fft.irfft(cross_white, fft_size)

        # Limit search to ±max_lag_s
        max_lag_samples = min(int(max_lag_s * sr), fft_size // 2)

        # The IFFT output is "circular" — positive lags 0..N/2, negative lags wrap
        cc_pos = cc[:max_lag_samples]
        cc_neg = cc[-max_lag_samples:]
        cc_combined = np.concatenate([cc_neg, cc_pos])
        # Now indices [0..2*max_lag] correspond to lags [-max_lag..+max_lag]

        peak_idx = int(np.argmax(np.abs(cc_combined)))
        peak_val = float(cc_combined[peak_idx])
        lag_samples = peak_idx - max_lag_samples

        # Sign convention: positive offset means h6e (sig) started BEFORE
        # camera (ref) by N seconds — caller will skip first N seconds of h6e
        # via `-ss N`. Negative offset means camera started before h6e — caller
        # will pad h6e with N seconds of silence via `adelay`.
        # The raw GCC-PHAT lag is opposite this convention, so we negate.
        offset_seconds = -lag_samples / sr

        # Confidence = peak height normalized by stddev of the correlation
        stddev = float(np.std(cc_combined)) + 1e-12
        confidence = abs(peak_val) / (stddev * 10)  # /10 to roughly fit 0-1 range
        confidence = min(1.0, confidence)

        return offset_seconds, confidence

    def _extract_audio_pcm(self, path: str, sr: int) -> np.ndarray:
        """Extract full mono audio as float32 numpy array via ffmpeg."""
        audio_filter = []
        if Path(path).suffix.lower() in {".mp4", ".mov", ".m4v"}:
            audio_filter = ["-af", CAMERA_AUDIO_TIMELINE_FILTER]
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            *audio_filter,
            "-ar",
            str(sr),
            "-ac",
            "1",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr[:500]}")
        return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
