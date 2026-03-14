"""Speaker cut agent — segment audio by speaker using per-channel or per-track RMS energy.

Supports two modes:
  1. L/R mode (legacy): Uses left/right stereo channels from audio_analysis.
     Labels: L, R, BOTH, NONE.
  2. N-speaker mode: When crop_config has speakers with H6E track assignments,
     uses individual track audio for much better speaker differentiation.
     Labels: speaker_0, speaker_1, ..., BOTH, NONE.

Inputs:
    - audio_analysis.json, stitch.json, episode.json (for track assignments)
    - work/left.wav, work/right.wav (L/R mode)
    - H6E track files via audio_tracks (N-speaker mode)
Outputs:
    - segments.json (speaker segments with start/end/speaker)
    - work/rms_data.json (per-frame RMS for silence snapping)
Dependencies:
    - numpy, ffmpeg (for track extraction in N-speaker mode)
Config:
    - processing.frame_seconds, processing.speech_db_margin
    - processing.min_segment_seconds, processing.both_db_range
    - episode.json speaker_cut_config overrides (from UI)
"""

import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from agents.base import BaseAgent


class SpeakerCutAgent(BaseAgent):
    name = "speaker_cut"

    def execute(self) -> dict:
        audio_data = self.load_json("audio_analysis.json")
        stitch_data = self.load_json("stitch.json")
        total_duration = stitch_data["duration_seconds"]

        # If channels are identical, return a single BOTH segment
        if audio_data.get("audio_channels_identical", False):
            self.logger.info("Channels identical — single BOTH segment")
            segments = [{
                "start": 0.0,
                "end": total_duration,
                "duration": total_duration,
                "speaker": "BOTH",
            }]
            result = {
                "segments": segments,
                "segment_count": 1,
                "duration_seconds": total_duration,
                "channels_identical": True,
            }
            self.save_json("segments.json", result)
            return result

        # Check if we have per-track speaker assignments for N-speaker mode
        episode_data = self.load_json("episode.json")
        crop_config = episode_data.get("crop_config", {})
        speakers = crop_config.get("speakers", [])
        all_have_tracks = (
            len(speakers) >= 2
            and all(s.get("track") for s in speakers)
        )

        if all_have_tracks:
            return self._n_speaker_cut(
                speakers, episode_data, audio_data, total_duration
            )

        return self._lr_cut(audio_data, total_duration)

    def _get_sensitivity_params(self, episode_data: dict) -> tuple:
        """Get sensitivity params, preferring episode overrides over config."""
        cut_cfg = episode_data.get("speaker_cut_config", {})
        proc = self.config.get("processing", {})
        return (
            cut_cfg.get("frame_seconds", proc.get("frame_seconds", 0.1)),
            cut_cfg.get("speech_db_margin", proc.get("speech_db_margin", 6)),
            cut_cfg.get("min_segment_seconds", proc.get("min_segment_seconds", 2.0)),
            cut_cfg.get("both_db_range", proc.get("both_db_range", 6.0)),
        )

    # ── N-speaker mode ────────────────────────────────────────────────

    def _n_speaker_cut(
        self, speakers, episode_data, audio_data, total_duration
    ) -> dict:
        """Speaker detection using per-track audio from dedicated mics."""
        audio_sync = episode_data.get("audio_sync", {})
        offset = audio_sync.get("offset_seconds", 0)
        frame_seconds, speech_db_margin, min_segment_seconds, both_db_range = (
            self._get_sensitivity_params(episode_data)
        )

        # Resolve track files
        audio_tracks = episode_data.get("audio_tracks", [])
        track_path_map = {}
        for t in audio_tracks:
            tn = t.get("track_number")
            if tn is not None:
                p = Path(t["dest_path"])
                if p.exists():
                    track_path_map[tn] = p

        work_dir = self.episode_dir / "work"
        n_speakers = len(speakers)
        sample_rate = 16000
        frame_size = int(sample_rate * frame_seconds)

        # Extract per-track audio
        track_arrays = []
        for i, spk in enumerate(speakers):
            tn = spk["track"]
            path = track_path_map.get(tn)
            if not path:
                self.logger.warning(
                    f"Track {tn} for speaker {i} not found, falling back to L/R"
                )
                return self._lr_cut(
                    self.load_json("audio_analysis.json"), total_duration
                )

            npy_path = work_dir / f"speaker_{i}_channel.npy"
            if npy_path.exists():
                data = np.load(str(npy_path))
            else:
                data = self._extract_track(path, offset, sample_rate)
                np.save(str(npy_path), data)
            track_arrays.append(data)

        # Compute per-track per-frame RMS with LUFS normalization
        min_len = min(len(d) for d in track_arrays)
        n_frames = min_len // frame_size

        # First pass: compute mean RMS per track for normalization
        mean_rms = []
        for data in track_arrays:
            trimmed = data[:n_frames * frame_size].astype(np.float64)
            track_rms = np.sqrt(np.mean(trimmed**2)) + 1e-10
            mean_rms.append(track_rms)

        # Compute gain factors to equalize all tracks to geometric mean
        geo_mean = np.exp(np.mean(np.log(mean_rms)))
        gains = [geo_mean / r for r in mean_rms]
        for i, (r, g) in enumerate(zip(mean_rms, gains)):
            self.logger.info(
                f"Speaker {i}: mean RMS={20*np.log10(r):.1f}dB, gain={g:.3f}x"
            )

        # Second pass: compute normalized per-frame RMS
        rms_db_list = []
        thresholds = []
        for i, data in enumerate(track_arrays):
            frames = (
                data[: n_frames * frame_size]
                .reshape(n_frames, frame_size)
                .astype(np.float64)
            )
            # Apply gain normalization before RMS computation
            rms = np.sqrt(np.mean((frames * gains[i])**2, axis=1)) + 1e-10
            db = 20 * np.log10(rms)
            rms_db_list.append(db)
            floor = np.percentile(db, 10)
            thresholds.append(floor + speech_db_margin)

        # Tighter both_db_range for normalized tracks (3dB vs 6dB)
        norm_both_range = min(both_db_range, 3.0)

        # Classify each frame
        labels = []
        for f in range(n_frames):
            active = []
            for i, db in enumerate(rms_db_list):
                if db[f] > thresholds[i]:
                    active.append((i, db[f]))

            if not active:
                labels.append("NONE")
            elif len(active) == 1:
                labels.append(f"speaker_{active[0][0]}")
            else:
                active.sort(key=lambda x: x[1], reverse=True)
                if active[0][1] - active[1][1] > norm_both_range:
                    labels.append(f"speaker_{active[0][0]}")
                else:
                    labels.append("BOTH")

        # Debounce NONE frames
        for i in range(1, len(labels) - 1):
            if labels[i] == "NONE" and labels[i - 1] == labels[i + 1]:
                labels[i] = labels[i - 1]

        # Merge consecutive same-label frames into segments
        raw_segments = self._labels_to_segments(labels, frame_seconds, n_frames)
        segments = self._absorb_short_segments(raw_segments, min_segment_seconds)

        for seg in segments:
            seg["duration"] = round(seg["end"] - seg["start"], 3)

        self.logger.info(
            f"N-speaker cut: {len(segments)} segments, "
            f"{n_frames} frames, {n_speakers} speakers"
        )

        # Save RMS data
        for i, db in enumerate(rms_db_list):
            np.save(str(work_dir / f"speaker_{i}_rms_db.npy"), db)

        rms_meta = {"frame_seconds": frame_seconds, "n_frames": int(n_frames)}
        self.save_json("work/rms_meta.json", rms_meta)

        result = {
            "segments": segments,
            "segment_count": len(segments),
            "duration_seconds": total_duration,
            "n_speakers": n_speakers,
            "frame_count": n_frames,
            "mode": "n_speaker",
        }
        self.save_json("segments.json", result)
        return result

    def _extract_track(
        self, path: Path, offset: float, sample_rate: int
    ) -> np.ndarray:
        """Extract audio track as numpy array, applying sync offset."""
        cmd = ["ffmpeg", "-y"]
        if offset >= 0:
            cmd += ["-ss", str(offset)]
        cmd += [
            "-i", str(path),
            "-ar", str(sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Track extraction failed: {result.stderr.decode()[:300]}"
            )
        return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)

    # ── L/R mode (legacy) ─────────────────────────────────────────────

    def _lr_cut(self, audio_data, total_duration) -> dict:
        """Original L/R speaker cut using stereo camera channels."""
        work_dir = self.episode_dir / "work"
        left_npy = work_dir / "left_channel.npy"
        right_npy = work_dir / "right_channel.npy"

        if left_npy.exists() and right_npy.exists():
            left_data = np.load(str(left_npy))
            right_data = np.load(str(right_npy))
        else:
            left_data = self._load_wav(work_dir / "left.wav")
            right_data = self._load_wav(work_dir / "right.wav")

        episode_data = self.load_json("episode.json")
        sample_rate = audio_data.get(
            "extracted_sample_rate", audio_data.get("sample_rate", 48000)
        )
        frame_seconds, speech_db_margin, min_segment_seconds, both_db_range = (
            self._get_sensitivity_params(episode_data)
        )
        frame_size = int(sample_rate * frame_seconds)

        min_len = min(len(left_data), len(right_data))
        n_frames = min_len // frame_size

        left_frames = (
            left_data[: n_frames * frame_size]
            .reshape(n_frames, frame_size)
            .astype(np.float64)
        )
        right_frames = (
            right_data[: n_frames * frame_size]
            .reshape(n_frames, frame_size)
            .astype(np.float64)
        )
        left_rms = np.sqrt(np.mean(left_frames**2, axis=1)) + 1e-10
        right_rms = np.sqrt(np.mean(right_frames**2, axis=1)) + 1e-10

        left_db = 20 * np.log10(left_rms)
        right_db = 20 * np.log10(right_rms)

        left_floor = np.percentile(left_db, 10)
        right_floor = np.percentile(right_db, 10)

        left_thresh = left_floor + speech_db_margin
        right_thresh = right_floor + speech_db_margin

        l_active = left_db > left_thresh
        r_active = right_db > right_thresh
        both_active = l_active & r_active
        diff = np.abs(left_db - right_db)

        label_arr = np.full(n_frames, 3, dtype=np.int8)
        label_arr[l_active & ~r_active] = 0
        label_arr[r_active & ~l_active] = 1
        label_arr[both_active & (diff <= both_db_range)] = 2
        label_arr[both_active & (diff > both_db_range) & (left_db > right_db)] = 0
        label_arr[both_active & (diff > both_db_range) & (left_db <= right_db)] = 1

        label_map = {0: "L", 1: "R", 2: "BOTH", 3: "NONE"}
        labels = [label_map[v] for v in label_arr]

        for i in range(1, len(labels) - 1):
            if labels[i] == "NONE" and labels[i - 1] == labels[i + 1]:
                labels[i] = labels[i - 1]

        raw_segments = self._labels_to_segments(labels, frame_seconds, n_frames)
        segments = self._absorb_short_segments(raw_segments, min_segment_seconds)

        for seg in segments:
            seg["duration"] = round(seg["end"] - seg["start"], 3)

        self.logger.info(
            f"L/R cut: {len(segments)} segments from {n_frames} frames"
        )

        np.save(str(work_dir / "left_rms_db.npy"), left_db)
        np.save(str(work_dir / "right_rms_db.npy"), right_db)
        rms_meta = {"frame_seconds": frame_seconds, "n_frames": int(n_frames)}
        self.save_json("work/rms_meta.json", rms_meta)
        rms_data = {
            "frame_seconds": frame_seconds,
            "left_rms_db": left_db.tolist(),
            "right_rms_db": right_db.tolist(),
        }
        self.save_json("work/rms_data.json", rms_data)

        result = {
            "segments": segments,
            "segment_count": len(segments),
            "duration_seconds": total_duration,
            "channels_identical": False,
            "frame_count": n_frames,
            "mode": "lr",
        }
        self.save_json("segments.json", result)
        return result

    # ── Shared helpers ────────────────────────────────────────────────

    def _labels_to_segments(
        self, labels: list[str], frame_seconds: float, n_frames: int
    ) -> list[dict]:
        """Convert frame labels to raw segments."""
        if not labels:
            return []
        segments = []
        current_label = labels[0]
        current_start = 0
        for i in range(1, len(labels)):
            if labels[i] != current_label:
                segments.append({
                    "start": round(current_start * frame_seconds, 3),
                    "end": round(i * frame_seconds, 3),
                    "speaker": current_label,
                })
                current_label = labels[i]
                current_start = i
        segments.append({
            "start": round(current_start * frame_seconds, 3),
            "end": round(n_frames * frame_seconds, 3),
            "speaker": current_label,
        })
        return segments

    def _absorb_short_segments(
        self, segments: list, min_duration: float
    ) -> list:
        """Merge segments shorter than min_duration into their neighbors."""
        if len(segments) <= 1:
            return segments

        merged = True
        while merged:
            merged = False
            new_segments = []
            i = 0
            while i < len(segments):
                seg = segments[i]
                duration = seg["end"] - seg["start"]
                if duration < min_duration and len(new_segments) > 0:
                    new_segments[-1]["end"] = seg["end"]
                    merged = True
                elif duration < min_duration and i + 1 < len(segments):
                    segments[i + 1]["start"] = seg["start"]
                    merged = True
                else:
                    new_segments.append(seg)
                i += 1
            segments = new_segments

        return segments

    def _load_wav(self, path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
            data = np.frombuffer(raw, dtype=np.int16)
        return data.astype(np.float32)
