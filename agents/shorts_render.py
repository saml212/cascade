"""Shorts render agent — render 9:16 vertical clips with burned-in subtitles.

Inputs:
    - clips.json, segments.json, diarized_transcript.json, episode.json (crop_config)
    - source_merged.mp4
Outputs:
    - shorts/<clip_id>.mp4 (9:16 vertical clips)
    - subtitles/<clip_id>.srt (per-clip SRT files)
Dependencies:
    - ffmpeg (render + concat), ffprobe (dimensions)
Config:
    - processing.shorts_crf, processing.shorts_audio_bitrate
"""

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.base import BaseAgent, timed_ffmpeg
from lib.audio_mix import generate_audio_mix
from lib.crop import compute_crop, resolve_speaker
from lib.encoding import get_video_encoder_args, get_lut_filter
from lib.ffprobe import probe as ffprobe
from lib.srt import fmt_timecode, escape_srt_path


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

        # Always regenerate audio_mix.wav to use current sync data
        audio_mix_path = self.episode_dir / "work" / "audio_mix.wav"
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

        clips = clips_data.get("clips", [])
        segments = segments_data.get("segments", [])

        # Get source dimensions
        probe = ffprobe(merged_path)
        video_stream = next(
            s for s in probe["streams"] if s["codec_type"] == "video"
        )
        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])

        shorts_dir = self.episode_dir / "shorts"
        shorts_dir.mkdir(exist_ok=True)
        subtitles_dir = self.episode_dir / "subtitles"
        subtitles_dir.mkdir(exist_ok=True)

        audio_bitrate = self.config.get("processing", {}).get("shorts_audio_bitrate", "128k")
        encoder_args = get_video_encoder_args(self.config, crf_key="shorts_crf")
        lut_filter = get_lut_filter(self.config)
        if lut_filter:
            self.logger.info(f"LUT enabled: {self.config['processing'].get('lut_path')}")

        # Generate per-clip SRT and render
        rendered = []
        self.logger.info(f"Rendering {len(clips)} shorts...")

        with ThreadPoolExecutor(max_workers=min(os.cpu_count() // 2, 6)) as executor:
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
                    src_w, src_h, audio_bitrate,
                    crop_config, encoder_args, lut_filter,
                    audio_mix_path,
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
        for i, seg in enumerate(clip_segs):
            if (seg["end"] - seg["start"]) < 0.5:
                if merged:
                    # Absorb into previous segment
                    merged[-1]["end"] = seg["end"]
                elif i + 1 < len(clip_segs):
                    # First segment is short — extend next segment's start to absorb it
                    clip_segs[i + 1]["start"] = seg["start"]
                else:
                    # Only segment — keep it regardless of duration
                    merged.append(seg)
            else:
                merged.append(seg)
        return merged if merged else clip_segs

    def _render_short(
        self,
        source, output, srt_path,
        start, end, segments,
        src_w, src_h, audio_bitrate,
        crop_config, encoder_args, lut_filter="",
        audio_mix_path=None,
    ):
        """Render a 9:16 short with per-segment dynamic speaker crops."""
        clip_segs = self._get_clip_segments(segments, start, end)

        def _audio_args(seek_time):
            """Return (extra_inputs, map_args, af_args) for audio source.

            When audio_mix_path exists, use pre-mixed H6E audio (offset already
            baked in — only seek to segment time, no additional offset).
            Otherwise fall back to camera audio with stereo-to-mono pan.
            """
            if audio_mix_path and audio_mix_path.exists():
                return (
                    ["-ss", str(seek_time), "-i", str(audio_mix_path)],
                    ["-map", "0:v", "-map", "1:a"],
                    [],
                )
            return ([], [], ["-af", "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"])

        # If only one segment (or all same speaker), render directly
        speakers = set(s["speaker"] for s in clip_segs)
        # Find the dominant speaker for this clip (most time in the clip)
        speaker_time = {}
        for seg in clip_segs:
            spk = seg["speaker"]
            speaker_time[spk] = speaker_time.get(spk, 0) + (seg["end"] - seg["start"])
        speaker = max(speaker_time, key=speaker_time.get)

        # Single-pass render with dominant speaker's crop + audio_mix.wav.
        # No per-segment splitting/concat — avoids AAC padding accumulation.
        duration = end - start
        vf = self._get_short_crop_filter(speaker, src_w, src_h, srt_path, crop_config)
        if lut_filter:
            vf = lut_filter + "," + vf
        extra_inputs, map_args, af_args = _audio_args(start)
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(source),
            *extra_inputs,
            "-t", str(duration),
            *map_args,
            "-vf", vf,
            *af_args,
            *encoder_args,
            "-r", "30", "-g", "30", "-bf", "0",
            "-vsync", "cfr",
            "-pix_fmt", "p010le",
            "-video_track_timescale", "30000",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-use_editlist", "0",
            "-movflags", "+faststart",
            str(output),
        ]
        timed_ffmpeg(cmd, agent_logger=self.logger, capture_output=True, text=True, check=True)

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
                f"{fmt_timecode(new_start)} --> {fmt_timecode(new_end)}\n"
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

    def _get_short_crop_region(self, speaker, src_w, src_h, crop_config):
        """Compute 9:16 crop region (w, h, x, y). Crop math in lib/crop.py."""
        cx, cy, zoom, _ = resolve_speaker(speaker, src_w, src_h, crop_config, for_shorts=True)
        x, y, crop_w, crop_h = compute_crop(src_w, src_h, cx, cy, zoom, "short")
        return crop_w, crop_h, x, y

    def _get_short_crop_filter_no_subs(self, speaker, src_w, src_h, crop_config):
        """Build 9:16 crop filter without subtitle burn-in."""
        crop_w, crop_h, x, y = self._get_short_crop_region(speaker, src_w, src_h, crop_config)
        return f"crop={crop_w}:{crop_h}:{x}:{y},scale=1080:1920"

    def _get_short_crop_filter(
        self, speaker, src_w, src_h, srt_path, crop_config
    ):
        """Build 9:16 crop filter chain with subtitle burn-in.

        Centers a 9:16 crop on the speaker's configured center point with zoom,
        clamped to frame bounds.
        """
        crop_w, crop_h, x, y = self._get_short_crop_region(speaker, src_w, src_h, crop_config)

        # Escape the SRT path for ffmpeg filter (colons and backslashes)
        srt_escaped = escape_srt_path(srt_path)

        # Crop -> scale -> burn subtitles
        return (
            f"crop={crop_w}:{crop_h}:{x}:{y},"
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
                f"{fmt_timecode(t_start)} --> {fmt_timecode(t_end)}\n"
                f"{text}\n"
            )
            idx += 1
            i += 4

        with open(srt_path, "w") as f:
            f.write("\n".join(srt_lines))

