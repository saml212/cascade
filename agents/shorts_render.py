"""Shorts render agent — render 9:16 vertical clips with burned-in subtitles."""

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.base import BaseAgent


class ShortsRenderAgent(BaseAgent):
    name = "shorts_render"

    def execute(self) -> dict:
        clips_data = self.load_json("clips.json")
        segments_data = self.load_json("segments.json")
        diarized = self.load_json("diarized_transcript.json")
        merged_path = self.episode_dir / "source_merged.mp4"

        # Load crop config from episode.json
        episode_data = self.load_json("episode.json")
        crop_config = episode_data.get("crop_config")
        if not crop_config:
            raise ValueError(
                "crop_config not found in episode.json. "
                "Complete crop setup before rendering."
            )

        clips = clips_data.get("clips", [])
        segments = segments_data.get("segments", [])

        # Get source dimensions
        probe = self._ffprobe(merged_path)
        video_stream = next(
            s for s in probe["streams"] if s["codec_type"] == "video"
        )
        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])

        shorts_dir = self.episode_dir / "shorts"
        shorts_dir.mkdir(exist_ok=True)
        subtitles_dir = self.episode_dir / "subtitles"
        subtitles_dir.mkdir(exist_ok=True)

        crf = self.config.get("processing", {}).get("shorts_crf", 20)
        audio_bitrate = self.config.get("processing", {}).get("shorts_audio_bitrate", "128k")

        # Generate per-clip SRT and render
        rendered = []
        self.logger.info(f"Rendering {len(clips)} shorts...")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}
            for clip in clips:
                clip_id = clip["id"]
                start = clip["start_seconds"]
                end = clip["end_seconds"]

                # Generate per-clip SRT
                srt_path = subtitles_dir / f"{clip_id}.srt"
                self._generate_clip_srt(diarized, start, end, srt_path)

                output_path = shorts_dir / f"{clip_id}.mp4"
                future = executor.submit(
                    self._render_short,
                    merged_path, output_path, srt_path,
                    start, end, segments,
                    src_w, src_h, crf, audio_bitrate,
                    crop_config,
                )
                futures[future] = clip_id

            for future in as_completed(futures):
                clip_id = futures[future]
                future.result()
                rendered.append(clip_id)
                self.report_progress(len(rendered), len(clips),
                    f"Rendered {clip_id}")
                self.logger.info(f"  {clip_id} rendered")

        return {
            "rendered_clips": sorted(rendered),
            "count": len(rendered),
            "shorts_dir": str(shorts_dir),
        }

    def _get_clip_segments(self, segments, clip_start, clip_end):
        """Get speaker segments overlapping the clip time range, clipped to bounds."""
        clip_segs = []
        for seg in segments:
            seg_start = seg["start"]
            seg_end = seg["end"]
            # Skip non-overlapping
            if seg_end <= clip_start or seg_start >= clip_end:
                continue
            # Clip to bounds
            s = max(seg_start, clip_start)
            e = min(seg_end, clip_end)
            if e - s < 0.05:
                continue
            clip_segs.append({
                "start": s,
                "end": e,
                "speaker": seg["speaker"],
            })
        # Fallback: if no segments found, use BOTH for entire clip
        if not clip_segs:
            clip_segs = [{"start": clip_start, "end": clip_end, "speaker": "BOTH"}]
            return clip_segs

        # Merge very short segments (< 0.5s) into neighbors to avoid ffmpeg failures
        merged = []
        for seg in clip_segs:
            if merged and (seg["end"] - seg["start"]) < 0.5:
                # Absorb into previous segment
                merged[-1]["end"] = seg["end"]
            elif not merged and (seg["end"] - seg["start"]) < 0.5 and len(clip_segs) > 1:
                # Skip — will be absorbed by the next segment's start
                continue
            else:
                merged.append(seg)
        # Fix gaps from skipped leading segments
        if merged and merged[0]["start"] > clip_start:
            merged[0]["start"] = clip_start
        return merged if merged else clip_segs

    def _render_short(
        self,
        source, output, srt_path,
        start, end, segments,
        src_w, src_h, crf, audio_bitrate,
        crop_config,
    ):
        """Render a 9:16 short with per-segment dynamic speaker crops."""
        clip_segs = self._get_clip_segments(segments, start, end)

        # If only one segment (or all same speaker), render directly
        speakers = set(s["speaker"] for s in clip_segs)
        if len(clip_segs) == 1 or len(speakers) == 1:
            speaker = clip_segs[0]["speaker"]
            duration = end - start
            vf = self._get_short_crop_filter(speaker, src_w, src_h, srt_path, crop_config)
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(source),
                "-t", str(duration),
                "-vf", vf,
                "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
                "-r", "30", "-g", "30", "-bf", "0",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", audio_bitrate,
                "-movflags", "+faststart",
                str(output),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return

        # Multiple segments with different speakers — render each, then concat
        # Use episode work dir (not system temp) so ffmpeg/libass can reliably access SRT files
        clip_name = output.stem
        work_dir = output.parent.parent / "work" / f"shorts_{clip_name}"
        work_dir.mkdir(parents=True, exist_ok=True)
        seg_files = []

        try:
            for idx, seg in enumerate(clip_segs):
                seg_start = seg["start"]
                seg_end = seg["end"]
                seg_duration = seg_end - seg_start
                speaker = seg["speaker"]

                # Generate per-segment SRT (time-offset to segment-relative)
                seg_srt_path = work_dir / f"seg_{idx}.srt"
                self._generate_segment_srt(srt_path, seg_start - start, seg_end - start, seg_srt_path)

                seg_output = work_dir / f"seg_{idx}.mp4"

                # Use crop filter with or without subtitles depending on SRT content
                has_subs = seg_srt_path.stat().st_size > 0
                if has_subs:
                    vf = self._get_short_crop_filter(speaker, src_w, src_h, seg_srt_path, crop_config)
                else:
                    vf = self._get_short_crop_filter_no_subs(speaker, src_w, src_h, crop_config)

                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(seg_start),
                    "-i", str(source),
                    "-t", str(seg_duration),
                    "-vf", vf,
                    "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
                    "-r", "30", "-g", "30", "-bf", "0",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", audio_bitrate,
                    str(seg_output),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"ffmpeg failed (exit {result.returncode}) for segment {idx} "
                        f"(speaker={speaker}, dur={seg_duration:.2f}s):\n{result.stderr[-1000:]}"
                    )
                seg_files.append(seg_output)

            # Concat all segments
            concat_list = work_dir / "concat.txt"
            with open(concat_list, "w") as f:
                for sf in seg_files:
                    f.write(f"file '{sf}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                "-movflags", "+faststart",
                str(output),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        finally:
            # Clean up work files
            for f in work_dir.glob("*"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            try:
                os.rmdir(work_dir)
            except OSError:
                pass

    def _generate_segment_srt(self, clip_srt_path, seg_start_rel, seg_end_rel, out_path):
        """Extract subtitle entries from the clip SRT that fall within segment bounds.

        Times in clip SRT are clip-relative. We re-offset them to be segment-relative.
        """
        entries = self._parse_srt(clip_srt_path)
        seg_entries = []
        idx = 1
        for entry in entries:
            e_start = entry["start"]
            e_end = entry["end"]
            # Keep entries that overlap this segment
            if e_end <= seg_start_rel or e_start >= seg_end_rel:
                continue
            # Clip to segment bounds and re-offset to segment-relative
            new_start = max(0.0, e_start - seg_start_rel)
            new_end = min(seg_end_rel - seg_start_rel, e_end - seg_start_rel)
            if new_end - new_start < 0.01:
                continue
            seg_entries.append(
                f"{idx}\n"
                f"{self._fmt(new_start)} --> {self._fmt(new_end)}\n"
                f"{entry['text']}\n"
            )
            idx += 1

        with open(out_path, "w") as f:
            f.write("\n".join(seg_entries))

    def _parse_srt(self, srt_path):
        """Parse an SRT file into a list of {start, end, text} dicts."""
        entries = []
        try:
            with open(srt_path, "r") as f:
                content = f.read()
        except FileNotFoundError:
            return entries

        blocks = content.strip().split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) < 3:
                continue
            # lines[0] = index, lines[1] = timecodes, lines[2:] = text
            timecode = lines[1]
            parts = timecode.split(" --> ")
            if len(parts) != 2:
                continue
            start = self._parse_srt_time(parts[0].strip())
            end = self._parse_srt_time(parts[1].strip())
            text = " ".join(lines[2:])
            entries.append({"start": start, "end": end, "text": text})
        return entries

    @staticmethod
    def _parse_srt_time(ts):
        """Parse SRT timestamp (HH:MM:SS,mmm) to seconds."""
        ts = ts.replace(",", ".")
        parts = ts.split(":")
        if len(parts) != 3:
            return 0.0
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

    def _get_short_crop_x(self, speaker, crop_w, src_w, crop_config):
        """Compute x offset for 9:16 crop centered on speaker point, clamped to bounds.

        BOTH defaults to L speaker — centering between speakers shows empty space.
        """
        if speaker == "R":
            cx = crop_config["speaker_r_center_x"]
        else:
            # L and BOTH both use L speaker position
            cx = crop_config["speaker_l_center_x"]
        return max(0, min(cx - crop_w // 2, src_w - crop_w))

    def _get_short_crop_filter_no_subs(self, speaker, src_w, src_h, crop_config):
        """Build 9:16 crop filter without subtitle burn-in."""
        crop_w = int(src_h * 9 / 16)  # 607 for 1080p
        x_offset = self._get_short_crop_x(speaker, crop_w, src_w, crop_config)
        return f"crop={crop_w}:{src_h}:{x_offset}:0,scale=1080:1920"

    def _get_short_crop_filter(
        self, speaker, src_w, src_h, srt_path, crop_config
    ):
        """Build 9:16 crop filter chain with subtitle burn-in.

        Centers a 9:16 crop (607x1080 for 1080p) on the speaker's configured
        center point, clamped to frame bounds.
        BOTH: center crop.
        """
        crop_w = int(src_h * 9 / 16)  # 607 for 1080p
        x_offset = self._get_short_crop_x(speaker, crop_w, src_w, crop_config)

        # Escape the SRT path for ffmpeg filter (colons and backslashes)
        srt_escaped = str(srt_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

        # Crop -> scale -> burn subtitles
        return (
            f"crop={crop_w}:{src_h}:{x_offset}:0,"
            f"scale=1080:1920,"
            f"subtitles='{srt_escaped}':force_style="
            f"'FontSize=12,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=3,Outline=1,Shadow=0,MarginV=80'"
        )

    def _generate_clip_srt(
        self, diarized, start, end, srt_path
    ):
        """Slice word-level transcript to clip range and write SRT."""
        words = []
        for utt in diarized.get("utterances", []):
            for w in utt.get("words", []):
                w_start = w.get("start", 0)
                w_end = w.get("end", 0)
                if w_start >= start and w_end <= end:
                    words.append(w)

        # Group into ~4-word subtitle blocks, offset times to clip-relative
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
                f"{self._fmt(t_start)} --> {self._fmt(t_end)}\n"
                f"{text}\n"
            )
            idx += 1
            i += 4

        with open(srt_path, "w") as f:
            f.write("\n".join(srt_lines))

    @staticmethod
    def _fmt(seconds):
        seconds = max(0, seconds)
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _ffprobe(self, path):
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
