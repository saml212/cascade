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

from agents.base import BaseAgent
from lib.encoding import get_video_encoder_args
from lib.ffprobe import probe as ffprobe
from lib.srt import fmt_timecode, escape_srt_path


class LongformRenderAgent(BaseAgent):
    name = "longform_render"

    def execute(self) -> dict:
        segments_data = self.load_json("segments.json")
        diarized = self.load_json("diarized_transcript.json")
        segments = segments_data["segments"]
        merged_path = self.episode_dir / "source_merged.mp4"
        work_dir = self.episode_dir / "work"
        work_dir.mkdir(exist_ok=True)
        srt_dir = work_dir / "longform_srt"
        srt_dir.mkdir(exist_ok=True)

        # Load crop config from episode.json
        episode_data = self.load_json("episode.json")
        crop_config = episode_data.get("crop_config")
        if not crop_config:
            raise ValueError(
                "crop_config not found in episode.json. "
                "Complete crop setup before rendering."
            )

        # Get source video dimensions
        probe = ffprobe(merged_path)
        video_stream = next(
            s for s in probe["streams"] if s["codec_type"] == "video"
        )
        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])

        audio_bitrate = self.config.get("processing", {}).get("audio_bitrate", "192k")
        encoder_args = get_video_encoder_args(self.config)

        # Pre-generate per-segment SRT files
        self.logger.info("Generating per-segment subtitles...")
        for i, seg in enumerate(segments):
            srt_path = srt_dir / f"seg_{i:04d}.srt"
            self._generate_segment_srt(diarized, seg["start"], seg["end"], srt_path)

        # Render each segment with appropriate crop + subtitles
        segment_files = []
        self.logger.info(f"Rendering {len(segments)} segments with speaker crops...")

        with ThreadPoolExecutor(max_workers=min(os.cpu_count() // 2, 6)) as executor:
            futures = {}
            for i, seg in enumerate(segments):
                seg_path = work_dir / f"longform_seg_{i:04d}.mp4"
                srt_path = srt_dir / f"seg_{i:04d}.srt"
                future = executor.submit(
                    self._render_segment,
                    merged_path, seg_path, seg, src_w, src_h, audio_bitrate,
                    crop_config, srt_path, encoder_args,
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
        subprocess.run(concat_cmd, capture_output=True, text=True, check=True)

        # Step 2: Get exact video track duration
        raw_probe = ffprobe(raw_concat)
        video_dur = None
        for s in raw_probe["streams"]:
            if s["codec_type"] == "video" and "duration" in s:
                video_dur = float(s["duration"])
                break

        # Step 3: Re-mux with -t to hard-stop both tracks at the video
        # duration. This prevents audio from being longer than video
        # (segment concat can introduce timing gaps that inflate audio).
        # Re-encode audio to flush any accumulated timestamp drift;
        # video is stream-copied (already encoded with correct settings).
        mux_cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_concat),
        ]
        if video_dur:
            mux_cmd += ["-t", str(video_dur)]
        mux_cmd += [
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-use_editlist", "0",
            "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(mux_cmd, capture_output=True, text=True, check=True)
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
    ):
        """Render a single segment with speaker-appropriate crop and subtitles."""
        start = segment["start"]
        duration = segment["end"] - segment["start"]
        speaker = segment["speaker"]

        # Build video filter based on speaker
        vf = self._get_crop_filter(speaker, src_w, src_h, crop_config)

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

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(source),
            "-t", str(duration),
            "-vf", vf,
            "-af", "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1",
            *encoder_args,
            "-r", "30", "-g", "30", "-bf", "0",
            "-vsync", "cfr",
            "-pix_fmt", "yuv420p",
            "-video_track_timescale", "30000",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-use_editlist", "0",
            "-movflags", "+faststart",
            str(output),
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)

    def _get_crop_filter(self, speaker, src_w, src_h, crop_config):
        """Get ffmpeg crop filter for speaker type using crop_config center points.

        Centers a 16:9 crop (half-width x corresponding height) on the speaker's
        configured center point, clamped to frame bounds.
        BOTH: passthrough at 1920x1080.
        """
        if speaker not in ("L", "R"):
            return "scale=1920:1080"

        # Crop dimensions: half the source width, 16:9 aspect
        crop_w = src_w // 2  # 960 for 1920
        crop_h = int(crop_w * 9 / 16)  # 540 for 960

        if speaker == "L":
            cx = crop_config["speaker_l_center_x"]
            cy = crop_config["speaker_l_center_y"]
        else:
            cx = crop_config["speaker_r_center_x"]
            cy = crop_config["speaker_r_center_y"]

        # Clamp crop origin to frame bounds
        x = max(0, min(cx - crop_w // 2, src_w - crop_w))
        y = max(0, min(cy - crop_h // 2, src_h - crop_h))

        return f"crop={crop_w}:{crop_h}:{x}:{y},scale=1920:1080"

    def _generate_segment_srt(self, diarized, start, end, srt_path):
        """Generate an SRT file for a segment, with times offset to segment-relative."""
        words = []
        for utt in diarized.get("utterances", []):
            for w in utt.get("words", []):
                w_start = w.get("start", 0)
                w_end = w.get("end", 0)
                if w_start >= start and w_end <= end:
                    words.append(w)

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

