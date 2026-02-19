"""Speaker cut agent — segment audio into L/R/BOTH/NONE based on per-channel RMS energy.

Inputs:
    - audio_analysis.json, stitch.json
    - work/left.wav, work/right.wav (from audio_analysis)
Outputs:
    - segments.json (speaker segments with start/end/speaker)
    - work/rms_data.json (per-frame RMS for silence snapping)
Dependencies:
    - numpy
Config:
    - processing.frame_seconds, processing.speech_db_margin
    - processing.min_segment_seconds, processing.both_db_range
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

        # Load .npy if available (from audio_analysis), fall back to WAV
        work_dir = self.episode_dir / "work"
        left_npy = work_dir / "left_channel.npy"
        right_npy = work_dir / "right_channel.npy"
        if left_npy.exists() and right_npy.exists():
            left_data = np.load(str(left_npy))
            right_data = np.load(str(right_npy))
        else:
            left_data = self._load_wav(work_dir / "left.wav")
            right_data = self._load_wav(work_dir / "right.wav")

        sample_rate = audio_data.get("extracted_sample_rate", audio_data.get("sample_rate", 48000))
        frame_seconds = self.config.get("processing", {}).get("frame_seconds", 0.1)
        speech_db_margin = self.config.get("processing", {}).get("speech_db_margin", 12)
        min_segment_seconds = self.config.get("processing", {}).get("min_segment_seconds", 2.0)
        both_db_range = self.config.get("processing", {}).get("both_db_range", 6.0)

        frame_size = int(sample_rate * frame_seconds)

        # Compute per-frame RMS for both channels
        min_len = min(len(left_data), len(right_data))
        n_frames = min_len // frame_size

        # Vectorized RMS computation (replaces Python for-loop)
        left_frames = left_data[:n_frames * frame_size].reshape(n_frames, frame_size).astype(np.float64)
        right_frames = right_data[:n_frames * frame_size].reshape(n_frames, frame_size).astype(np.float64)
        left_rms = np.sqrt(np.mean(left_frames ** 2, axis=1)) + 1e-10
        right_rms = np.sqrt(np.mean(right_frames ** 2, axis=1)) + 1e-10

        # Convert to dB
        left_db = 20 * np.log10(left_rms)
        right_db = 20 * np.log10(right_rms)

        # Noise floor = 10th percentile
        left_floor = np.percentile(left_db, 10)
        right_floor = np.percentile(right_db, 10)

        left_thresh = left_floor + speech_db_margin
        right_thresh = right_floor + speech_db_margin

        # Vectorized classification (replaces Python for-loop)
        l_active = left_db > left_thresh
        r_active = right_db > right_thresh
        both_active = l_active & r_active
        diff = np.abs(left_db - right_db)

        # Default to NONE, then layer on
        label_arr = np.full(n_frames, 3, dtype=np.int8)  # 0=L, 1=R, 2=BOTH, 3=NONE
        label_arr[l_active & ~r_active] = 0  # L only
        label_arr[r_active & ~l_active] = 1  # R only
        label_arr[both_active & (diff <= both_db_range)] = 2  # BOTH
        label_arr[both_active & (diff > both_db_range) & (left_db > right_db)] = 0  # L louder
        label_arr[both_active & (diff > both_db_range) & (left_db <= right_db)] = 1  # R louder

        label_map = {0: "L", 1: "R", 2: "BOTH", 3: "NONE"}
        labels = [label_map[v] for v in label_arr]

        # Debounce: replace NONE frames surrounded by same label
        for i in range(1, len(labels) - 1):
            if labels[i] == "NONE" and labels[i - 1] == labels[i + 1]:
                labels[i] = labels[i - 1]

        # Merge consecutive same-label frames into segments
        raw_segments = []
        if labels:
            current_label = labels[0]
            current_start = 0
            for i in range(1, len(labels)):
                if labels[i] != current_label:
                    raw_segments.append({
                        "start": round(current_start * frame_seconds, 3),
                        "end": round(i * frame_seconds, 3),
                        "speaker": current_label,
                    })
                    current_label = labels[i]
                    current_start = i
            # Final segment
            raw_segments.append({
                "start": round(current_start * frame_seconds, 3),
                "end": round(n_frames * frame_seconds, 3),
                "speaker": current_label,
            })

        # Absorb short segments (< min_segment_seconds) into neighbors
        segments = self._absorb_short_segments(raw_segments, min_segment_seconds)

        # Add duration to each segment
        for seg in segments:
            seg["duration"] = round(seg["end"] - seg["start"], 3)

        self.logger.info(f"Generated {len(segments)} segments from {n_frames} frames")

        # Save RMS data for clip_miner silence-snapping (.npy + small metadata)
        np.save(str(work_dir / "left_rms_db.npy"), left_db)
        np.save(str(work_dir / "right_rms_db.npy"), right_db)
        rms_meta = {"frame_seconds": frame_seconds, "n_frames": int(n_frames)}
        self.save_json("work/rms_meta.json", rms_meta)
        # Also save JSON for backward compatibility
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
        }
        self.save_json("segments.json", result)
        return result

    def _absorb_short_segments(self, segments: list, min_duration: float) -> list:
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
                    # Absorb into previous segment
                    new_segments[-1]["end"] = seg["end"]
                    merged = True
                elif duration < min_duration and i + 1 < len(segments):
                    # Absorb into next segment
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
