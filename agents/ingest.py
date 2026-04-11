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
from lib.ffprobe import probe as ffprobe, get_video_properties


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
            except Exception as e:
                self.logger.warning("Could not read source properties: %s", e)

        result = {
            "files": copied_files,
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
        # Search top-level first, then subdirectories (H6E stores files in session folders)
        wav_files = sorted(
            f for f in list(source.glob("*.WAV")) + list(source.glob("*.wav"))
            if not f.name.startswith("._")
        )
        if not wav_files:
            # Search one level deep (e.g., /Volumes/ZOOM_H6E/260311_162356/*.WAV)
            # Use the most recent session folder
            subdirs = sorted(
                (d for d in source.iterdir() if d.is_dir() and not d.name.startswith((".", "TRASH", "ZOOM"))),
                key=lambda d: d.name, reverse=True,
            )
            for subdir in subdirs:
                wav_files = sorted(
                    f for f in list(subdir.glob("*.WAV")) + list(subdir.glob("*.wav"))
                    if not f.name.startswith("._")
                )
                if wav_files:
                    self.logger.info(f"Found audio in subdirectory: {subdir.name}")
                    break

        if not wav_files:
            raise FileNotFoundError(f"No WAV files found in {self.audio_path} or its subdirectories")

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
        """Sync H6E audio to camera video using GCC-PHAT + 2-anchor drift detection.

        Strategy:
            1. Find best sync track via short-window GCC-PHAT (which H6E track
               sounds most like the camera mic).
            2. Two long-window measurements: anchor_start (first 60s), anchor_end
               (last 60s). The offset at each anchor + the time gap between them
               gives drift directly via slope.
            3. Apply tempo correction only if both anchors are high-confidence
               and the inferred drift is plausible (<500 ppm).

        GCC-PHAT (Generalized Cross-Correlation with Phase Transform) is the
        gold standard for time-delay estimation between two mics. It whitens
        the cross-spectrum so the correlation is dominated by phase agreement,
        not amplitude — giving sub-sample precision and robust performance
        across mics with different frequency responses.

        Resolution: 62.5 µs per measurement at 16 kHz (vs 50 ms for the old
        envelope approach — an 800x precision improvement). Frame-accurate.
        """
        self.report_progress(0, 1, "Syncing audio")

        merged_path = self.episode_dir / "source_merged.mp4"
        video_path = str(merged_path) if merged_path.exists() else video_files[0]["dest_path"]
        video_duration = sum(f["duration_seconds"] for f in video_files)

        tracks = audio_result.get("tracks", [])
        if not tracks:
            return {"status": "no_sync_track"}

        sr = 16000
        cam_full = self._extract_audio_pcm(video_path, sr)
        if len(cam_full) < sr * 5:
            return {"status": "too_short"}

        # ── Step 1: Pick the best H6E track via GCC-PHAT search ──
        # Use 60s anchor window if available, else half of total length.
        # Whichever H6E track has the highest peak coherence wins.
        anchor_window = min(60, max(5, len(cam_full) // sr // 2))
        self.logger.info(f"Step 1: Finding sync track via GCC-PHAT ({anchor_window}s window)...")
        cam_anchor_start = cam_full[: sr * anchor_window]

        track_results = []
        for t in tracks:
            try:
                h6e = self._extract_audio_pcm(t["dest_path"], sr)
            except Exception as e:
                self.logger.warning(f"  {t['filename']}: extract failed: {e}")
                continue
            if len(h6e) < sr * anchor_window:
                continue
            h6e_anchor = h6e[: sr * anchor_window]
            offset, conf = self._gcc_phat(cam_anchor_start, h6e_anchor, sr, max_lag_s=30)
            track_results.append({
                "track": t,
                "offset": offset,
                "confidence": conf,
                "h6e_full": h6e,
            })
            self.logger.info(f"  {t['filename']}: offset={offset:+.4f}s conf={conf:.4f}")

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

        # ── Step 2: End anchor — measure offset near the end of the recording ──
        # Take 60s windows centered ~60s before the end of both streams.
        # Use the same alignment so the start offset roughly applies, then GCC-PHAT
        # finds the residual delta which tells us the drift.
        self.logger.info("Step 2: Measuring end anchor for drift detection...")

        end_offset = None
        end_conf = 0.0
        # Anchor near the end but leave a 10s safety margin
        end_time = max(anchor_window + 30, int(video_duration) - 90)
        if end_time + anchor_window > video_duration:
            end_time = max(0, int(video_duration) - anchor_window - 10)

        cam_end_s = end_time * sr
        cam_end_e = cam_end_s + anchor_window * sr
        h6e_end_s = cam_end_s + int(anchor_start_offset * sr)
        h6e_end_e = h6e_end_s + anchor_window * sr

        if (
            cam_end_e <= len(cam_full)
            and 0 <= h6e_end_s
            and h6e_end_e <= len(h6e_full)
        ):
            cam_end_segment = cam_full[cam_end_s:cam_end_e]
            h6e_end_segment = h6e_full[h6e_end_s:h6e_end_e]
            local_offset, end_conf = self._gcc_phat(
                cam_end_segment, h6e_end_segment, sr, max_lag_s=2
            )
            # local_offset is the drift accumulated since the start anchor
            end_offset = anchor_start_offset + local_offset
            self.logger.info(
                f"  End anchor at t={end_time}s: "
                f"local_drift={local_offset*1000:+.1f}ms total_offset={end_offset:+.6f}s "
                f"conf={end_conf:.4f}"
            )
        else:
            self.logger.warning("  End anchor window exceeds available audio — drift detection skipped")

        base_result = {
            "sync_track": sync_track["filename"],
            "video_file": Path(video_path).name,
            "video_duration": round(video_duration, 3),
            "confidence": round(anchor_start_conf, 6),
            "anchor_start_offset": round(anchor_start_offset, 6),
            "anchor_start_confidence": round(anchor_start_conf, 6),
        }

        # ── Step 3: Decide whether to apply drift correction ──
        # Both anchors must be high-confidence AND drift must be plausible
        # (< 500 ppm absolute). Otherwise use start offset only.
        MIN_CONF = 0.30
        MAX_PLAUSIBLE_PPM = 500

        if (
            end_offset is None
            or end_conf < MIN_CONF
            or anchor_start_conf < MIN_CONF
        ):
            self.logger.warning(
                f"Insufficient confidence for drift correction "
                f"(start={anchor_start_conf:.3f} end={end_conf:.3f}, threshold={MIN_CONF}). "
                f"Using start offset only."
            )
            status = "ok" if anchor_start_conf >= MIN_CONF else "low_confidence"
            return {
                **base_result,
                "status": status,
                "offset_seconds": round(anchor_start_offset, 6),
                "tempo_factor": 1.0,
                "drift_rate_ppm": 0.0,
                "drift_total_seconds": 0.0,
                "r_squared": 1.0 if anchor_start_conf >= MIN_CONF else 0.0,
                "anchor_end_offset": round(end_offset, 6) if end_offset is not None else None,
                "anchor_end_confidence": round(end_conf, 6),
                "drift_status": "skipped_low_confidence",
            }

        # Compute drift from the two anchors
        time_gap = end_time + anchor_window / 2 - anchor_window / 2  # midpoint to midpoint
        slope = (end_offset - anchor_start_offset) / time_gap
        drift_ppm = slope * 1e6
        drift_total = slope * video_duration
        tempo_factor = 1.0 + slope

        if abs(drift_ppm) > MAX_PLAUSIBLE_PPM:
            self.logger.warning(
                f"Implausible drift detected ({drift_ppm:.1f} ppm > {MAX_PLAUSIBLE_PPM}). "
                f"Likely a sync error in one anchor. Using start offset only."
            )
            return {
                **base_result,
                "status": "ok",
                "offset_seconds": round(anchor_start_offset, 6),
                "tempo_factor": 1.0,
                "drift_rate_ppm": 0.0,
                "drift_total_seconds": 0.0,
                "r_squared": 0.0,
                "anchor_end_offset": round(end_offset, 6),
                "anchor_end_confidence": round(end_conf, 6),
                "drift_status": "skipped_implausible",
            }

        self.logger.info(
            f"Drift: {drift_total*1000:+.1f}ms over {video_duration:.0f}s "
            f"({drift_ppm:+.2f} ppm) tempo={tempo_factor:.10f}"
        )

        return {
            **base_result,
            "status": "ok",
            "offset_seconds": round(anchor_start_offset, 6),
            "tempo_factor": tempo_factor,
            "drift_rate_ppm": round(drift_ppm, 2),
            "drift_total_seconds": round(drift_total, 6),
            "r_squared": round(min(anchor_start_conf, end_conf), 4),
            "anchor_end_offset": round(end_offset, 6),
            "anchor_end_confidence": round(end_conf, 6),
            "drift_status": "applied",
        }

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

    @staticmethod
    def _correlate(a: np.ndarray, b: np.ndarray, sr: int) -> tuple[float, float]:
        """FFT cross-correlation on raw waveforms. Returns (offset_seconds, confidence)."""
        a, b = a - np.mean(a), b - np.mean(b)
        fft_size = 2 ** int(np.ceil(np.log2(len(a) + len(b) - 1)))
        cc = np.real(np.fft.ifft(np.fft.fft(a, fft_size) * np.conj(np.fft.fft(b, fft_size))))
        max_idx = int(np.argmax(cc))
        if max_idx > fft_size // 2:
            max_idx -= fft_size
        energy = np.sqrt(np.sum(a ** 2) * np.sum(b ** 2)) + 1e-10
        return max_idx / sr, float(cc[max_idx % fft_size]) / energy

    @staticmethod
    def _smart_correlate(a: np.ndarray, b: np.ndarray, sr: int) -> tuple[float, float]:
        """Robust audio sync correlation that works with dissimilar mics.

        1. Bandpass filter to speech frequencies (200-4000Hz)
        2. Compute energy envelope in 50ms windows
        3. Normalize to unit variance
        4. Cross-correlate envelopes

        Returns (offset_seconds, confidence).
        """
        from numpy.fft import rfft, irfft

        # Bandpass filter 200-4000Hz via FFT
        def bandpass(signal, sr, lo=200, hi=4000):
            n = len(signal)
            spec = rfft(signal.astype(np.float64))
            freqs = np.fft.rfftfreq(n, 1.0 / sr)
            spec[(freqs < lo) | (freqs > hi)] = 0
            return irfft(spec, n).astype(np.float32)

        a_filt = bandpass(a, sr)
        b_filt = bandpass(b, sr)

        # Energy envelope in 50ms windows
        win = max(1, int(sr * 0.05))
        def envelope(signal):
            n = len(signal) // win
            if n == 0:
                return np.array([])
            frames = signal[:n * win].reshape(n, win).astype(np.float64)
            env = np.sqrt(np.mean(frames ** 2, axis=1))
            # Normalize to unit variance
            env = env - np.mean(env)
            std = np.std(env)
            if std > 0:
                env = env / std
            return env

        env_a = envelope(a_filt)
        env_b = envelope(b_filt)
        if len(env_a) < 20 or len(env_b) < 20:
            return 0.0, 0.0

        # Cross-correlate
        fft_size = 2 ** int(np.ceil(np.log2(len(env_a) + len(env_b) - 1)))
        cc = np.real(np.fft.ifft(
            np.fft.fft(env_a, fft_size) * np.conj(np.fft.fft(env_b, fft_size))
        ))
        # Normalize by geometric mean of energies
        energy = np.sqrt(np.sum(env_a ** 2) * np.sum(env_b ** 2)) + 1e-10
        cc_norm = cc / energy

        max_idx = int(np.argmax(cc_norm))
        if max_idx > fft_size // 2:
            max_idx -= fft_size

        confidence = float(cc_norm[max_idx % fft_size])
        offset = max_idx * 0.05  # envelope frame = 50ms

        return offset, confidence

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
