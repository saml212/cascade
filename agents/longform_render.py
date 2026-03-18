"""Longform render agent — render full episode with speaker-appropriate crops.

Inputs:
    - segments.json, diarized_transcript.json, episode.json (crop_config)
    - source_merged.mp4
Outputs:
    - longform.mp4 (final 16:9 render with speaker crops + subtitles)
Dependencies:
    - ffmpeg (render + concat), ffprobe (dimensions + validation)
Config:
    - processing.video_crf, processing.audio_bitrate
"""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.base import BaseAgent, timed_ffmpeg
from lib.audio_mix import generate_audio_mix
from lib.crop import compute_crop, resolve_speaker
from lib.encoding import get_video_encoder_args, get_lut_filter
from lib.ffprobe import probe as ffprobe
from lib.srt import fmt_timecode, escape_srt_path


class LongformRenderAgent(BaseAgent):
    name = "longform_render"

    def execute(self) -> dict:
        segments_data = self.load_json("segments.json")
        diarized = self.load_json("diarized_transcript.json")
        segments = segments_data["segments"]

        # Apply longform edits (cuts/trims) if present
        episode_data = self.load_json("episode.json")
        edits = episode_data.get("longform_edits", [])
        if edits:
            segments = self._apply_edits(segments, edits)
            self.logger.info(f"Applied {len(edits)} edits, {len(segments)} segments remaining")

        merged_path = self.episode_dir / "source_merged.mp4"
        work_dir = self.episode_dir / "work"
        work_dir.mkdir(exist_ok=True)
        srt_dir = work_dir / "longform_srt"
        srt_dir.mkdir(exist_ok=True)

        crop_config = episode_data.get("crop_config")
        if not crop_config:
            raise ValueError(
                "crop_config not found in episode.json. "
                "Complete crop setup before rendering."
            )

        # Resolve audio source: always regenerate audio_mix.wav to ensure
        # it uses the current sync offset/tempo from episode.json
        audio_mix_path = work_dir / "audio_mix.wav"
        has_h6e_tracks = bool(episode_data.get("audio_tracks"))
        if has_h6e_tracks:
            self.logger.info("Generating audio_mix.wav from H6E tracks with current sync data...")
            mix_result = generate_audio_mix(self.episode_dir, episode_data)
            if mix_result and mix_result.exists():
                audio_mix_path = mix_result
                self.logger.info("Generated audio_mix.wav from H6E tracks")
            else:
                self.logger.warning("Failed to generate audio_mix.wav, falling back to camera audio")
                audio_mix_path = None
        else:
            self.logger.info("No H6E audio tracks — using camera audio from source_merged.mp4")
            audio_mix_path = None

        # Get source video dimensions
        probe = ffprobe(merged_path)
        video_stream = next(
            s for s in probe["streams"] if s["codec_type"] == "video"
        )
        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])

        audio_bitrate = self.config.get("processing", {}).get("audio_bitrate", "192k")
        encoder_args = get_video_encoder_args(self.config)
        lut_filter = get_lut_filter(self.config)
        if lut_filter:
            self.logger.info(f"LUT enabled: {self.config['processing'].get('lut_path')}")

        # Pre-generate per-segment SRT files
        self.logger.info("Generating per-segment subtitles...")
        for i, seg in enumerate(segments):
            srt_path = srt_dir / f"seg_{i:04d}.srt"
            self._generate_segment_srt(diarized, seg["start"], seg["end"], srt_path)

        # Render each segment with appropriate crop + subtitles
        segment_files = []
        self.logger.info(f"Rendering {len(segments)} segments with speaker crops...")

        # Resume support: skip segments that are already rendered (non-zero size)
        skipped = 0
        to_render = []
        for i, seg in enumerate(segments):
            seg_path = work_dir / f"longform_seg_{i:04d}.mp4"
            if seg_path.exists() and seg_path.stat().st_size > 0:
                segment_files.append((i, seg_path))
                skipped += 1
            else:
                to_render.append((i, seg))
        if skipped:
            self.logger.info(f"Resuming: {skipped} segments already rendered, {len(to_render)} remaining")

        with ThreadPoolExecutor(max_workers=min(os.cpu_count() // 2, 6)) as executor:
            futures = {}
            for i, seg in to_render:
                seg_path = work_dir / f"longform_seg_{i:04d}.mp4"
                srt_path = srt_dir / f"seg_{i:04d}.srt"
                future = executor.submit(
                    self._render_segment,
                    merged_path, seg_path, seg, src_w, src_h, audio_bitrate,
                    crop_config, srt_path, encoder_args, lut_filter,
                    audio_mix_path,
                )
                futures[future] = (i, seg_path)

            for future in as_completed(futures):
                i, seg_path = futures[future]
                future.result()  # Raise any exception
                segment_files.append((i, seg_path))
                self.report_progress(len(segment_files), len(segments),
                    f"Rendered segment {i}")
                self.logger.info(f"  Segment {i} rendered")

        # Sort by index
        segment_files.sort(key=lambda x: x[0])

        # Concat all segments
        concat_list = work_dir / "longform_concat.txt"
        with open(concat_list, "w") as f:
            for _, seg_path in segment_files:
                safe_path = str(seg_path).replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

        output_path = self.episode_dir / "longform.mp4"
        raw_concat = work_dir / "longform_raw.mp4"
        self.logger.info("Concatenating segments into longform.mp4...")

        # Step 1: Concat all segments (stream-copy, fast)
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(raw_concat),
        ]
        timed_ffmpeg(concat_cmd, agent_logger=self.logger, capture_output=True, text=True, check=True)

        # Step 2: Mux video with audio_mix.wav directly.
        # Per-segment audio encoding is skipped entirely — each AAC segment
        # adds ~21ms of padding (1024 samples at 48kHz) which accumulates
        # to seconds of drift over hundreds of segments. Using audio_mix.wav
        # as a single audio source eliminates this completely.
        audio_source = audio_mix_path if (audio_mix_path and audio_mix_path.exists()) else merged_path
        mux_cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_concat),
            "-i", str(audio_source),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-shortest",
            "-use_editlist", "0",
            "-movflags", "+faststart",
            str(output_path),
        ]
        timed_ffmpeg(mux_cmd, agent_logger=self.logger, capture_output=True, text=True, check=True)
        raw_concat.unlink(missing_ok=True)

        # Validate
        probe = ffprobe(output_path)
        output_duration = float(probe["format"]["duration"])

        return {
            "output_path": str(output_path),
            "duration_seconds": round(output_duration, 3),
            "segment_count": len(segments),
            "file_size_mb": round(output_path.stat().st_size / 1e6, 1),
        }

    def _render_segment(
        self,
        source: Path,
        output: Path,
        segment: dict,
        src_w: int,
        src_h: int,
        audio_bitrate: str,
        crop_config: dict,
        srt_path: Path = None,
        encoder_args: list = None,
        lut_filter: str = "",
        audio_mix_path: Path = None,
    ):
        """Render a single segment with speaker-appropriate crop and subtitles."""
        start = segment["start"]
        duration = segment["end"] - segment["start"]
        speaker = segment["speaker"]

        # Build video filter: LUT (color grade) → crop → scale
        vf_parts = []
        if lut_filter:
            vf_parts.append(lut_filter)
        vf_parts.append(self._get_crop_filter(speaker, src_w, src_h, crop_config))
        vf = ",".join(vf_parts)

        # Append subtitle burn-in if SRT has content
        if srt_path and srt_path.exists() and srt_path.stat().st_size > 0:
            srt_escaped = escape_srt_path(srt_path)
            vf += (
                f",subtitles='{srt_escaped}':force_style="
                f"'FontSize=14,FontName=Arial,Bold=1,"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                f"BackColour=&H80000000,BorderStyle=4,Outline=2,"
                f"Shadow=1,ShadowColour=&HA0000000,MarginV=30,"
                f"Alignment=2'"
            )

        # Render VIDEO ONLY — no per-segment audio encoding.
        # Audio is muxed once at the end from audio_mix.wav to avoid
        # AAC frame padding accumulation (21ms per segment = seconds of drift).
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start), "-i", str(source),
            "-t", str(duration),
            "-an",  # no audio
            "-vf", vf,
            *encoder_args,
            "-r", "30", "-g", "30", "-bf", "0",
            "-vsync", "cfr",
            "-pix_fmt", "p010le",
            "-video_track_timescale", "30000",
            "-use_editlist", "0",
            "-movflags", "+faststart",
            str(output),
        ]
        timed_ffmpeg(cmd, agent_logger=self.logger, capture_output=True, text=True, check=True)

    def _get_crop_filter(self, speaker, src_w, src_h, crop_config):
        """Get ffmpeg crop+scale filter. Crop math in lib/crop.py."""
        cx, cy, zoom, mode = resolve_speaker(speaker, src_w, src_h, crop_config)
        if mode is None:
            return "scale=1920:1080"
        x, y, crop_w, crop_h = compute_crop(src_w, src_h, cx, cy, zoom, mode)
        return f"crop={crop_w}:{crop_h}:{x}:{y},scale=1920:1080"

    def _generate_segment_srt(self, diarized, start, end, srt_path):
        """Generate an SRT file for a segment, with times offset to segment-relative."""
        words = []
        last_end = -1.0
        for utt in diarized.get("utterances", []):
            for w in utt.get("words", []):
                w_start = w.get("start", 0)
                w_end = w.get("end", 0)
                if w_start >= start and w_end <= end:
                    # Skip overlapping words from other channels (multichannel bleed)
                    if w_start < last_end - 0.05:
                        continue
                    words.append(w)
                    last_end = w_end

        srt_lines = []
        idx = 1
        i = 0
        while i < len(words):
            chunk = words[i : i + 4]
            t_start = chunk[0]["start"] - start
            t_end = chunk[-1]["end"] - start
            text = " ".join(w.get("word", "") for w in chunk)

            srt_lines.append(
                f"{idx}\n"
                f"{fmt_timecode(t_start)} --> {fmt_timecode(t_end)}\n"
                f"{text}\n"
            )
            idx += 1
            i += 4

        with open(srt_path, "w") as f:
            f.write("\n".join(srt_lines))

    def _apply_edits(self, segments: list, edits: list) -> list:
        """Apply longform edits (cuts/trims) to the segment list.

        Edit types:
          - cut: Remove time range [start, end] from the video
          - trim_start: Set global start time (remove everything before)
          - trim_end: Set global end time (remove everything after)
        """
        result = [dict(s) for s in segments]  # deep copy

        for edit in edits:
            edit_type = edit.get("type")
            if edit_type == "trim_start":
                trim_at = edit["seconds"]
                result = [s for s in result if s["end"] > trim_at]
                if result and result[0]["start"] < trim_at:
                    result[0] = dict(result[0])
                    result[0]["start"] = trim_at
                    result[0]["duration"] = result[0]["end"] - result[0]["start"]

            elif edit_type == "trim_end":
                trim_at = edit["seconds"]
                result = [s for s in result if s["start"] < trim_at]
                if result and result[-1]["end"] > trim_at:
                    result[-1] = dict(result[-1])
                    result[-1]["end"] = trim_at
                    result[-1]["duration"] = result[-1]["end"] - result[-1]["start"]

            elif edit_type == "cut":
                cut_start = edit["start_seconds"]
                cut_end = edit["end_seconds"]
                new_result = []
                for s in result:
                    if s["end"] <= cut_start or s["start"] >= cut_end:
                        # Segment is entirely outside the cut — keep
                        new_result.append(s)
                    elif s["start"] >= cut_start and s["end"] <= cut_end:
                        # Segment is entirely within the cut — remove
                        continue
                    elif s["start"] < cut_start and s["end"] > cut_end:
                        # Cut is in the middle of this segment — split into two
                        left = dict(s)
                        left["end"] = cut_start
                        left["duration"] = left["end"] - left["start"]
                        right = dict(s)
                        right["start"] = cut_end
                        right["duration"] = right["end"] - right["start"]
                        new_result.extend([left, right])
                    elif s["start"] < cut_start:
                        # Segment starts before cut — trim end
                        trimmed = dict(s)
                        trimmed["end"] = cut_start
                        trimmed["duration"] = trimmed["end"] - trimmed["start"]
                        new_result.append(trimmed)
                    else:
                        # Segment ends after cut — trim start
                        trimmed = dict(s)
                        trimmed["start"] = cut_end
                        trimmed["duration"] = trimmed["end"] - trimmed["start"]
                        new_result.append(trimmed)
                result = new_result

        # Filter out tiny segments (< 0.1s)
        result = [s for s in result if s.get("duration", s["end"] - s["start"]) >= 0.1]
        return result

