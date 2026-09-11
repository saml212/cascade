"""Build source-clock camera decisions from camera or logical recorder tracks."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import numpy as np

from agents.audio_analysis import (
    ANALYSIS_SAMPLE_RATE,
    AudioAnalysisAgent,
    audio_analysis_fingerprint,
)
from agents.base import BaseAgent
from lib.atomic_write import atomic_write_json
from lib.audio_mix import logical_track_groups
from lib.crop import compute_crop, resolve_speaker

DOMINANCE_DB = 6.0
SPEAKER_CUT_VERSION = "source-clock-v3"


def strict_bool(value: object) -> bool:
    """Parse persisted booleans without Python's truthy-string behavior."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0", ""}:
            return False
    return False


def speaker_cut_fingerprint(
    episode_dir: Path, episode: dict, audio_analysis: dict, config: dict
) -> str:
    """Identify all inputs that can change source-clock speaker decisions."""
    sources = []
    source_paths = [episode_dir / "source_merged.mp4"]
    for group in logical_track_groups(
        episode_dir, episode, recorder_only=True, existing_only=False
    ).values():
        source_paths.extend(
            Path(track["dest_path"]) for track in group if track.get("dest_path")
        )
    for path in source_paths:
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
        "version": SPEAKER_CUT_VERSION,
        "clock": "source",
        "audio_analysis_fingerprint": audio_analysis_fingerprint(
            episode_dir, episode, config
        ),
        "audio_channels_identical": strict_bool(
            audio_analysis.get("audio_channels_identical")
        ),
        "audio_sync": episode.get("audio_sync", {}),
        "crop_config": episode.get("crop_config", {}),
        "settings": {
            "frame_seconds": episode.get("speaker_cut_config", {}).get(
                "frame_seconds", processing.get("frame_seconds", 0.1)
            ),
            "speech_db_margin": episode.get("speaker_cut_config", {}).get(
                "speech_db_margin", processing.get("speech_db_margin", 6)
            ),
            "min_segment_seconds": episode.get("speaker_cut_config", {}).get(
                "min_segment_seconds", processing.get("min_segment_seconds", 2.0)
            ),
            "dominance_db": DOMINANCE_DB,
            "sample_rate": ANALYSIS_SAMPLE_RATE,
        },
        "sources": sources,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def current_speaker_segments(
    episode_dir: Path, episode: dict, config: dict
) -> dict | None:
    """Return speaker decisions only when their full provenance is current."""
    try:
        audio_analysis = json.loads((episode_dir / "audio_analysis.json").read_text())
        segments = json.loads((episode_dir / "segments.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    expected = speaker_cut_fingerprint(episode_dir, episode, audio_analysis, config)
    if (
        segments.get("clock") != "source"
        or segments.get("algorithm_version") != SPEAKER_CUT_VERSION
        or segments.get("fingerprint") != expected
        or segments.get("crop_validation", {}).get("distinct") is not True
    ):
        return None
    return segments


def validate_speaker_crops(episode: dict) -> dict:
    """Measure whether configured longform speaker crops are visually distinct."""
    properties = episode.get("source_properties", {})
    width = int(properties.get("width") or 0)
    height = int(properties.get("height") or 0)
    crop = episode.get("crop_config", {})
    speakers = crop.get("speakers", [])
    if width <= 0 or height <= 0 or len(speakers) < 2:
        return {
            "status": "unavailable",
            "distinct": False,
            "reason": "source dimensions and at least two speakers are required",
            "rectangles": [],
        }
    rectangles = []
    for index, speaker in enumerate(speakers):
        cx, cy, zoom, mode = resolve_speaker(f"speaker_{index}", width, height, crop)
        if mode is None:
            rectangle = (0, 0, width, height)
        else:
            rectangle = compute_crop(width, height, cx, cy, zoom, mode)
        x, y, crop_width, crop_height = rectangle
        rectangles.append(
            {
                "speaker": f"speaker_{index}",
                "person": speaker.get("label"),
                "x": x,
                "y": y,
                "width": crop_width,
                "height": crop_height,
            }
        )
    overlaps = []
    for left_index, left in enumerate(rectangles):
        for right in rectangles[left_index + 1 :]:
            intersection_width = max(
                0,
                min(left["x"] + left["width"], right["x"] + right["width"])
                - max(left["x"], right["x"]),
            )
            intersection_height = max(
                0,
                min(left["y"] + left["height"], right["y"] + right["height"])
                - max(left["y"], right["y"]),
            )
            intersection = intersection_width * intersection_height
            union = (
                left["width"] * left["height"]
                + right["width"] * right["height"]
                - intersection
            )
            overlaps.append(intersection / union if union else 1.0)
    maximum_overlap = max(overlaps, default=1.0)
    distinct = maximum_overlap < 0.85
    return {
        "status": "pass" if distinct else "fail",
        "distinct": distinct,
        "maximum_pair_iou": round(maximum_overlap, 4),
        "threshold": 0.85,
        "rectangles": rectangles,
    }


class SpeakerCutAgent(BaseAgent):
    name = "speaker_cut"

    def execute(self) -> dict:
        episode = self.load_json("episode.json")
        audio_data = self.load_json("audio_analysis.json")
        total_duration = self.load_json("stitch.json")["duration_seconds"]
        assignments = self._recorder_assignments(episode)
        if not assignments:
            expected_audio = audio_analysis_fingerprint(
                self.episode_dir, episode, self.config
            )
            if audio_data.get("fingerprint") != expected_audio:
                self.logger.info("Refreshing stale camera-channel analysis")
                audio_data = AudioAnalysisAgent(self.episode_dir, self.config).run()

        fingerprint = speaker_cut_fingerprint(
            self.episode_dir, episode, audio_data, self.config
        )
        track_mapping = self._track_mapping(episode, assignments)
        crop_validation = validate_speaker_crops(episode)
        if not assignments and strict_bool(audio_data.get("audio_channels_identical")):
            self.logger.info("Channels identical — single BOTH segment")
            segment = {
                "start": 0.0,
                "end": total_duration,
                "duration": total_duration,
                "speaker": "BOTH",
            }
            result = {
                "segments": [segment],
                "segment_count": 1,
                "duration_seconds": total_duration,
                "channels_identical": True,
                "clock": "source",
                "fingerprint": fingerprint,
                "algorithm_version": SPEAKER_CUT_VERSION,
                "track_mapping": track_mapping,
                "crop_validation": crop_validation,
            }
            self.save_json("segments.json", result)
            return result

        proc = self.config.get("processing", {})
        cut_cfg = episode.get("speaker_cut_config", {})
        frame_sec = cut_cfg.get("frame_seconds", proc.get("frame_seconds", 0.1))
        margin = cut_cfg.get("speech_db_margin", proc.get("speech_db_margin", 6))
        min_seg = cut_cfg.get(
            "min_segment_seconds", proc.get("min_segment_seconds", 2.0)
        )

        tracks, mode = self._load_tracks(
            episode, audio_data, total_duration, assignments
        )
        n_spk = len(tracks)
        sr = ANALYSIS_SAMPLE_RATE
        fsz = int(sr * frame_sec)
        if fsz <= 0:
            raise ValueError("speaker_cut frame_seconds must be positive")
        n_frames = min(len(t) for t in tracks) // fsz
        if n_frames == 0:
            raise RuntimeError("Speaker analysis tracks contain no complete frames")

        mean_rms = [
            np.sqrt(np.mean(track[: n_frames * fsz].astype(np.float64) ** 2)) + 1e-10
            for track in tracks
        ]
        active_rms = [r for r in mean_rms if 20 * np.log10(r) > -60]
        if len(active_rms) >= 2:
            geo = np.exp(np.mean(np.log(active_rms)))
            gains = [geo / rms if 20 * np.log10(rms) > -60 else 1.0 for rms in mean_rms]
        else:
            gains = [1.0] * n_spk

        smooth_w = max(1, int(0.3 / frame_sec))
        kernel = np.ones(smooth_w) / smooth_w
        smoothed = []
        for index, track in enumerate(tracks):
            frames = track[: n_frames * fsz].reshape(n_frames, fsz).astype(np.float64)
            db = 20 * np.log10(
                np.sqrt(np.mean((frames * gains[index]) ** 2, axis=1)) + 1e-10
            )
            smoothed.append(np.convolve(db, kernel, mode="same"))
            self.logger.info(
                "Speaker %d: mean=%.1fdB gain=%.2fx",
                index,
                20 * np.log10(mean_rms[index]),
                gains[index],
            )

        thresholds = [np.percentile(s, 10) + margin for s in smoothed]

        raw = []
        for frame in range(n_frames):
            active = [
                (index, smoothed[index][frame])
                for index in range(n_spk)
                if smoothed[index][frame] > thresholds[index]
            ]
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

        hold = max(1, int(0.5 / frame_sec))
        labels = list(raw)
        cur = labels[0] if labels[0] != "NONE" else "BOTH"
        pend, pend_n = None, 0
        for frame in range(n_frames):
            if raw[frame] == "NONE" or raw[frame] == cur:
                labels[frame] = cur
                pend = None
                pend_n = 0
            elif raw[frame] == pend:
                pend_n += 1
                if pend_n >= hold:
                    cur = pend
                    labels[frame] = cur
                    pend = None
                    pend_n = 0
                else:
                    labels[frame] = cur
            else:
                pend = raw[frame]
                pend_n = 1
                labels[frame] = cur

        segments = self._finalize_segments(labels, frame_sec, n_frames, min_seg)
        anticipate = 0.3
        for index in range(1, len(segments)):
            boundary = max(
                segments[index - 1]["start"],
                segments[index]["start"] - anticipate,
            )
            segments[index - 1]["end"] = boundary
            segments[index]["start"] = boundary
        for seg in segments:
            seg["duration"] = round(seg["end"] - seg["start"], 3)

        self.logger.info(
            "%s cut: %d segments, %d frames, %d speakers",
            mode,
            len(segments),
            n_frames,
            n_spk,
        )
        work = self.episode_dir / "work"
        for index, values in enumerate(smoothed):
            np.save(work / f"speaker_{index}_rms_db.npy", values)
        self.save_json(
            "work/rms_meta.json",
            {
                "frame_seconds": frame_sec,
                "n_frames": int(n_frames),
                "clock": "source",
                "fingerprint": fingerprint,
            },
        )

        result = {
            "segments": segments,
            "segment_count": len(segments),
            "duration_seconds": total_duration,
            "n_speakers": n_spk,
            "frame_count": n_frames,
            "mode": mode,
            "clock": "source",
            "fingerprint": fingerprint,
            "algorithm_version": SPEAKER_CUT_VERSION,
            "track_mapping": track_mapping,
            "crop_validation": crop_validation,
        }
        self.save_json("segments.json", result)
        return result

    def _recorder_assignments(self, episode: dict) -> list[dict]:
        speakers = episode.get("crop_config", {}).get("speakers", [])
        if len(speakers) < 2 or not all(
            isinstance(speaker.get("track"), int) for speaker in speakers
        ):
            return []
        groups = logical_track_groups(self.episode_dir, episode, recorder_only=True)
        if not groups:
            return []
        missing = [
            speaker["track"] for speaker in speakers if speaker["track"] not in groups
        ]
        if missing:
            raise FileNotFoundError(
                f"Assigned recorder track(s) unavailable: {sorted(set(missing))}"
            )
        return [
            {
                "index": index,
                "speaker": speaker,
                "track_number": speaker["track"],
                "tracks": groups[speaker["track"]],
            }
            for index, speaker in enumerate(speakers)
        ]

    def _track_mapping(self, episode: dict, assignments: list[dict]) -> list[dict]:
        if assignments:
            return [
                {
                    "speaker": f"speaker_{assignment['index']}",
                    "person": assignment["speaker"].get("label"),
                    "logical_track": assignment["track_number"],
                    "source_files": [
                        track.get("filename") for track in assignment["tracks"]
                    ],
                    "source_clock": True,
                }
                for assignment in assignments
            ]
        speakers = episode.get("crop_config", {}).get("speakers", [])
        return [
            {
                "speaker": f"speaker_{index}",
                "person": (
                    speakers[index].get("label") if index < len(speakers) else None
                ),
                "camera_channel": channel,
                "source_clock": True,
            }
            for index, channel in enumerate(("left", "right"))
        ]

    def _load_tracks(
        self,
        episode: dict,
        audio_data: dict,
        total_duration: float,
        assignments: list[dict] | None = None,
    ) -> tuple[list[np.ndarray], str]:
        assignments = (
            self._recorder_assignments(episode) if assignments is None else assignments
        )
        if assignments:
            return (
                self._load_recorder_tracks(episode, assignments, total_duration),
                "n_speaker",
            )

        expected = audio_analysis_fingerprint(self.episode_dir, episode, self.config)
        if audio_data.get("fingerprint") != expected:
            raise RuntimeError("Camera channel cache is stale; rerun audio_analysis")
        cached = AudioAnalysisAgent._load_cached_channels(
            self.episode_dir / "work", expected
        )
        if cached is None:
            raise RuntimeError("Camera channel cache is missing; rerun audio_analysis")
        return list(cached), "lr"

    def _load_recorder_tracks(
        self, episode: dict, assignments: list[dict], total_duration: float
    ) -> list[np.ndarray]:
        work = self.episode_dir / "work"
        work.mkdir(exist_ok=True)
        metadata_path = work / "speaker_track_cache.json"
        try:
            old_metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            old_metadata = {}
        old_entries = old_metadata.get("entries", {})
        new_entries = {}
        arrays = []
        sync = episode.get("audio_sync", {})
        offset = float(sync.get("offset_seconds", 0))
        tempo = (
            float(sync.get("tempo_factor", 1.0))
            if float(sync.get("r_squared", 0)) > 0.5
            else 1.0
        )
        for assignment in assignments:
            index = assignment["index"]
            paths = [Path(track["dest_path"]) for track in assignment["tracks"]]
            fingerprint = self._recorder_cache_fingerprint(paths, sync, total_duration)
            destination = work / f"speaker_{index}_channel.npy"
            entry = old_entries.get(str(index), {})
            if entry.get("fingerprint") == fingerprint and destination.exists():
                data = np.load(destination)
            else:
                data = self._extract_track(paths, offset, tempo, total_duration)
                temp = work / f"speaker_{index}_channel.tmp.npy"
                np.save(temp, data.astype(np.float32, copy=False))
                os.replace(temp, destination)
            arrays.append(data)
            new_entries[str(index)] = {
                "fingerprint": fingerprint,
                "logical_track": assignment["track_number"],
                "source_files": [path.name for path in paths],
                "sample_count": len(data),
            }
        atomic_write_json(
            metadata_path,
            {
                "version": SPEAKER_CUT_VERSION,
                "clock": "source",
                "sample_rate": ANALYSIS_SAMPLE_RATE,
                "entries": new_entries,
            },
        )
        return arrays

    @staticmethod
    def _recorder_cache_fingerprint(
        paths: list[Path], sync: dict, total_duration: float
    ) -> str:
        sources = []
        for path in paths:
            stat = path.stat()
            sources.append(
                {
                    "path": str(path.resolve()),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        payload = {
            "version": SPEAKER_CUT_VERSION,
            "sample_rate": ANALYSIS_SAMPLE_RATE,
            "audio_sync": sync,
            "duration": total_duration,
            "sources": sources,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _extract_track(
        self,
        paths: list[Path],
        offset: float,
        tempo: float,
        total_duration: float,
    ) -> np.ndarray:
        inputs = []
        filters = []
        labels = []
        for index, path in enumerate(paths):
            inputs.extend(["-i", str(path)])
            filters.append(f"[{index}:a]aformat=channel_layouts=mono[p{index}]")
            labels.append(f"[p{index}]")
        if len(labels) > 1:
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[joined]")
        else:
            filters.append(f"{labels[0]}anull[joined]")
        chain = "[joined]asetpts=PTS-STARTPTS"
        if offset > 0:
            chain += f",atrim=start={offset},asetpts=PTS-STARTPTS"
        elif offset < 0:
            chain += f",adelay={round(abs(offset) * 1000)}"
        if abs(tempo - 1.0) > 1e-7:
            chain += f",atempo={tempo:.8f}"
        chain += f",aresample={ANALYSIS_SAMPLE_RATE}[out]"
        filters.append(chain)
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-t",
            str(total_duration),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"Track extraction failed: {result.stderr.decode()[-500:]}"
            )
        data = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
        expected = round(total_duration * ANALYSIS_SAMPLE_RATE)
        if len(data) < expected:
            data = np.pad(data, (0, expected - len(data)))
        return data[:expected]

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
                segs.append(
                    {
                        "start": round(start * frame_sec, 3),
                        "end": round(i * frame_sec, 3),
                        "speaker": cur,
                    }
                )
                cur, start = labels[i], i
        segs.append(
            {
                "start": round(start * frame_sec, 3),
                "end": round(n_frames * frame_sec, 3),
                "speaker": cur,
            }
        )
        # Absorb short segments into neighbors, re-merge consecutive same-speaker
        changed = True
        while changed:
            changed = False
            new = []
            for i, seg in enumerate(segs):
                if seg["end"] - seg["start"] < min_dur:
                    if new:
                        # Merge into predecessor
                        new[-1]["end"] = seg["end"]
                        changed = True
                    elif i + 1 < len(segs):
                        # First segment is short — merge into next by extending next's start
                        segs[i + 1]["start"] = seg["start"]
                        changed = True
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
