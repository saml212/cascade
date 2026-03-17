"""Speaker cut agent — unified L/R stereo and N-speaker (H6E) modes.

BOTH detection uses independent threshold crossings with a dominance check —
if one speaker is significantly louder, they win even if others cross threshold.
"""

import subprocess
import wave
from pathlib import Path

import numpy as np

from agents.base import BaseAgent

DOMINANCE_DB = 6.0  # dB gap required to override BOTH and pick dominant speaker


class SpeakerCutAgent(BaseAgent):
    name = "speaker_cut"

    def execute(self) -> dict:
        audio_data = self.load_json("audio_analysis.json")
        total_duration = self.load_json("stitch.json")["duration_seconds"]

        if audio_data.get("audio_channels_identical", False):
            self.logger.info("Channels identical — single BOTH segment")
            seg = {"start": 0.0, "end": total_duration, "duration": total_duration, "speaker": "BOTH"}
            result = {"segments": [seg], "segment_count": 1,
                      "duration_seconds": total_duration, "channels_identical": True}
            self.save_json("segments.json", result)
            return result

        episode_data = self.load_json("episode.json")
        proc = self.config.get("processing", {})
        cut_cfg = episode_data.get("speaker_cut_config", {})
        frame_sec = cut_cfg.get("frame_seconds", proc.get("frame_seconds", 0.1))
        margin = cut_cfg.get("speech_db_margin", proc.get("speech_db_margin", 6))
        min_seg = cut_cfg.get("min_segment_seconds", proc.get("min_segment_seconds", 2.0))

        tracks, mode = self._load_tracks(episode_data, audio_data)
        n_spk = len(tracks)
        sr = 16000 if mode == "n_speaker" else audio_data.get(
            "extracted_sample_rate", audio_data.get("sample_rate", 48000))
        fsz = int(sr * frame_sec)
        n_frames = min(len(t) for t in tracks) // fsz

        # LUFS normalization — skip tracks below -60dB (essentially silent)
        mean_rms = [np.sqrt(np.mean(t[:n_frames * fsz].astype(np.float64)**2)) + 1e-10 for t in tracks]
        active_rms = [r for r in mean_rms if 20 * np.log10(r) > -60]
        if len(active_rms) >= 2:
            geo = np.exp(np.mean(np.log(active_rms)))
            gains = [geo / r if 20 * np.log10(r) > -60 else 1.0 for r in mean_rms]
        else:
            gains = [1.0] * n_spk

        # Per-frame RMS dB, smoothed with 300ms sliding window
        smooth_w = max(1, int(0.3 / frame_sec))
        kernel = np.ones(smooth_w) / smooth_w
        smoothed = []
        for i, t in enumerate(tracks):
            frames = t[:n_frames * fsz].reshape(n_frames, fsz).astype(np.float64)
            db = 20 * np.log10(np.sqrt(np.mean((frames * gains[i])**2, axis=1)) + 1e-10)
            smoothed.append(np.convolve(db, kernel, mode='same'))
            self.logger.info(f"Speaker {i}: mean={20*np.log10(mean_rms[i]):.1f}dB gain={gains[i]:.2f}x")

        thresholds = [np.percentile(s, 10) + margin for s in smoothed]

        # Classify frames with dominance check
        raw = []
        for f in range(n_frames):
            active = [(i, smoothed[i][f]) for i in range(n_spk) if smoothed[i][f] > thresholds[i]]
            if not active:
                raw.append("NONE")
            elif len(active) == 1:
                raw.append(f"speaker_{active[0][0]}")
            else:
                active.sort(key=lambda x: x[1], reverse=True)
                if active[0][1] - active[1][1] > DOMINANCE_DB:
                    raw.append(f"speaker_{active[0][0]}")
                else:
                    raw.append("BOTH")

        # Hysteresis: NONE holds current speaker; switches need hold_frames consecutive frames
        hold = max(1, int(0.5 / frame_sec))
        labels = list(raw)
        cur = labels[0] if labels[0] != "NONE" else "BOTH"
        pend, pend_n = None, 0
        for f in range(n_frames):
            if raw[f] == "NONE" or raw[f] == cur:
                labels[f] = cur; pend = None; pend_n = 0
            elif raw[f] == pend:
                pend_n += 1
                if pend_n >= hold:
                    cur = pend; labels[f] = cur; pend = None; pend_n = 0
                else:
                    labels[f] = cur
            else:
                pend = raw[f]; pend_n = 1; labels[f] = cur

        segments = self._finalize_segments(labels, frame_sec, n_frames, min_seg)

        self.logger.info(f"{mode} cut: {len(segments)} segments, {n_frames} frames, {n_spk} speakers")
        work = self.episode_dir / "work"
        for i, s in enumerate(smoothed):
            np.save(str(work / f"speaker_{i}_rms_db.npy"), s)
        self.save_json("work/rms_meta.json", {"frame_seconds": frame_sec, "n_frames": int(n_frames)})

        result = {"segments": segments, "segment_count": len(segments),
                  "duration_seconds": total_duration, "n_speakers": n_spk,
                  "frame_count": n_frames, "mode": mode}
        self.save_json("segments.json", result)
        return result

    # -- Track loading -----------------------------------------------------------

    def _load_tracks(self, episode_data: dict, audio_data: dict) -> tuple[list[np.ndarray], str]:
        speakers = episode_data.get("crop_config", {}).get("speakers", [])
        if len(speakers) >= 2 and all(s.get("track") for s in speakers):
            offset = episode_data.get("audio_sync", {}).get("offset_seconds", 0)
            path_map = {}
            for t in episode_data.get("audio_tracks", []):
                tn = t.get("track_number")
                if tn is not None:
                    p = Path(t["dest_path"])
                    if p.exists():
                        path_map[tn] = p
            work = self.episode_dir / "work"
            arrays = []
            for i, spk in enumerate(speakers):
                path = path_map.get(spk["track"])
                if not path:
                    raise RuntimeError(f"Track {spk['track']} for speaker {i} not found")
                npy = work / f"speaker_{i}_channel.npy"
                if npy.exists():
                    arrays.append(np.load(str(npy)))
                else:
                    data = self._extract_track(path, offset)
                    np.save(str(npy), data)
                    arrays.append(data)
            return arrays, "n_speaker"

        work = self.episode_dir / "work"
        for name in ("left", "right"):
            npy = work / f"{name}_channel.npy"
            if not npy.exists():
                wav_data = self._load_wav(work / f"{name}.wav")
                np.save(str(npy), wav_data)
        return [np.load(str(work / f"{n}_channel.npy")) for n in ("left", "right")], "lr"

    def _extract_track(self, path: Path, offset: float) -> np.ndarray:
        sr = 16000
        cmd = ["ffmpeg", "-y"]
        if offset >= 0:
            cmd += ["-ss", str(offset)]
        cmd += ["-i", str(path), "-ar", str(sr), "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", "-"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"Track extraction failed: {r.stderr.decode()[:300]}")
        data = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
        # For negative offset (camera started first), prepend silence so
        # timestamps align to video time, not H6E time
        if offset < 0:
            pad = np.zeros(int(abs(offset) * sr), dtype=np.float32)
            data = np.concatenate([pad, data])
        return data

    @staticmethod
    def _load_wav(path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as wf:
            return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32)

    # -- Segment helpers ---------------------------------------------------------

    def _finalize_segments(self, labels, frame_sec, n_frames, min_dur):
        """Labels to segments, absorb short ones, merge consecutive, add duration."""
        if not labels:
            return []
        # Build raw segments
        segs = []
        cur, start = labels[0], 0
        for i in range(1, len(labels)):
            if labels[i] != cur:
                segs.append({"start": round(start * frame_sec, 3),
                             "end": round(i * frame_sec, 3), "speaker": cur})
                cur, start = labels[i], i
        segs.append({"start": round(start * frame_sec, 3),
                     "end": round(n_frames * frame_sec, 3), "speaker": cur})
        # Absorb short segments into neighbors, re-merge consecutive same-speaker
        changed = True
        while changed:
            changed = False
            new = []
            for i, seg in enumerate(segs):
                if seg["end"] - seg["start"] < min_dur:
                    if new:
                        # Merge into predecessor
                        new[-1]["end"] = seg["end"]; changed = True
                    elif i + 1 < len(segs):
                        # First segment is short — merge into next by extending next's start
                        segs[i + 1]["start"] = seg["start"]; changed = True
                    else:
                        # Only segment — keep it regardless of duration
                        new.append(seg)
                else:
                    new.append(seg)
            segs = new
        merged = [segs[0]] if segs else []
        for seg in segs[1:]:
            if seg["speaker"] == merged[-1]["speaker"]:
                merged[-1]["end"] = seg["end"]
            else:
                merged.append(seg)
        for seg in merged:
            seg["duration"] = round(seg["end"] - seg["start"], 3)
        return merged
