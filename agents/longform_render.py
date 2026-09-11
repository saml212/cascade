"""Render the canonical 16:9 episode from the shared source timeline."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.base import BaseAgent, timed_ffmpeg
from lib.ass import CaptionStyle, generate_ass_from_diarized
from lib.audio_mix import generate_audio_mix
from lib.crop import compute_crop, resolve_speaker
from lib.delivery_video import (
    build_render_segments,
    concat_video_segments,
    current_longform_render,
    estimate_output_bytes,
    longform_render_fingerprint,
    mux_timeline_audio,
    record_longform_render,
    render_scratch_dir,
    render_video_segment,
    require_output_space,
    source_fps,
)
from lib.encoding import (
    get_lut_filter,
    get_scale_filter,
    get_video_encoder_args,
    get_video_polish_filters,
)
from lib.ffprobe import probe as ffprobe
from lib.loudness import measure_loudness
from lib.srt import escape_srt_path
from lib.timeline import Timeline, rebase_diarized


class LongformRenderAgent(BaseAgent):
    name = "longform_render"

    def execute(self) -> dict:
        episode = self.load_json("episode.json")
        crop_config = episode.get("crop_config")
        if not crop_config:
            raise ValueError("Complete crop setup before rendering")

        source = self.episode_dir / "source_merged.mp4"
        if not source.exists():
            raise FileNotFoundError("source_merged.mp4 is required for longform render")
        segments = self.load_json("segments.json").get("segments", [])
        if not segments:
            raise ValueError("Speaker segments are required for speaker-cut longform")
        diarized = self.load_json("diarized_transcript.json")

        audio = generate_audio_mix(self.episode_dir, episode, self.config)
        if not audio or not audio.exists():
            raise RuntimeError("Canonical work/audio_mix.wav is required")

        source_probe = ffprobe(source)
        video_stream = next(
            stream
            for stream in source_probe["streams"]
            if stream["codec_type"] == "video"
        )
        source_duration = float(source_probe["format"]["duration"])
        fps = source_fps(video_stream, episode)
        timeline = Timeline.from_edits(
            source_duration, episode.get("longform_edits", [])
        ).quantize(fps)
        fingerprint = longform_render_fingerprint(
            self.episode_dir, episode, self.config, audio, segments
        )
        current = current_longform_render(
            self.episode_dir, episode, self.config, audio, segments
        )
        if current:
            self.logger.info("Canonical speaker-cut longform is already current")
            return {
                "output_path": str(self.episode_dir / "upload_video.mp4"),
                "render_mode": "speaker_cut",
                "render_fingerprint": fingerprint,
                "reused": True,
                **current["output"],
            }

        render_segments = build_render_segments(timeline, segments, frame_rate=fps)
        if not render_segments:
            raise ValueError("No retained speaker segments remain after edits")
        captions = rebase_diarized(diarized, timeline)

        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])
        out_w, out_h = self._output_dimensions(src_w, src_h)
        encoder_args = get_video_encoder_args(self.config)
        lut_filter = get_lut_filter(self.config)
        audio_bitrate = self.config.get("processing", {}).get("audio_bitrate", "192k")
        estimate = estimate_output_bytes(timeline.duration)
        require_output_space(self.episode_dir, estimate)
        output = self.episode_dir / "upload_video.mp4"

        style = CaptionStyle(
            font_size=max(36, round(out_h * 0.045)),
            margin_v=max(48, round(out_h * 0.07)),
            words_per_phrase=4,
            play_res_x=out_w,
            play_res_y=out_h,
        )
        with render_scratch_dir(
            f"longform-{self.episode_dir.name}", round(estimate * 2.2)
        ) as scratch:
            segment_paths = self._render_segments(
                source,
                scratch,
                render_segments,
                captions,
                style,
                src_w,
                src_h,
                crop_config,
                encoder_args,
                lut_filter,
                fps,
                out_w,
                out_h,
            )
            video_only = scratch / "longform_video.mp4"
            concat_video_segments(segment_paths, video_only, runner=self._run_ffmpeg)
            media = mux_timeline_audio(
                video_only,
                audio,
                output,
                timeline,
                audio_bitrate=audio_bitrate,
                runner=self._run_ffmpeg,
            )

        record = record_longform_render(
            self.episode_dir,
            fingerprint=fingerprint,
            render_mode="speaker_cut",
            timeline=timeline,
            media=media,
        )
        loudness = measure_loudness(output)
        result = {
            "output_path": str(output),
            "render_mode": "speaker_cut",
            "render_fingerprint": fingerprint,
            "segment_count": len(render_segments),
            "source_clock": True,
            "keep_intervals": [list(value) for value in timeline.keep_intervals],
            "file_size_mb": round(output.stat().st_size / 1e6, 1),
            **media,
        }
        if loudness:
            result["audio_loudness"] = loudness
        result["manifest"] = record
        return result

    def _render_segments(
        self,
        source,
        scratch,
        segments,
        captions,
        style,
        src_w,
        src_h,
        crop_config,
        encoder_args,
        lut_filter,
        fps,
        out_w,
        out_h,
    ) -> list[Path]:
        paths: list[Path | None] = [None] * len(segments)
        workers = min(max((os.cpu_count() or 2) // 2, 1), 4, len(segments))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for index, segment in enumerate(segments):
                ass_path = scratch / f"segment_{index:04d}.ass"
                segment_timeline = Timeline(
                    segments[-1]["end"], [(segment["start"], segment["end"])]
                )
                generate_ass_from_diarized(
                    rebase_diarized(captions, segment_timeline),
                    0,
                    segment["duration"],
                    ass_path,
                    style,
                )
                output = scratch / f"segment_{index:04d}.mp4"
                future = executor.submit(
                    self._render_segment,
                    source,
                    output,
                    segment,
                    src_w,
                    src_h,
                    crop_config,
                    ass_path,
                    encoder_args,
                    lut_filter,
                    fps,
                    out_w,
                    out_h,
                )
                futures[future] = (index, output)
            for future in as_completed(futures):
                index, output = futures[future]
                future.result()
                paths[index] = output
                self.report_progress(
                    sum(path is not None for path in paths),
                    len(paths),
                    f"Rendered speaker segment {index + 1}",
                )
        return [path for path in paths if path is not None]

    def _render_segment(
        self,
        source: Path,
        output: Path,
        segment: dict,
        src_w: int,
        src_h: int,
        crop_config: dict,
        ass_path: Path | None = None,
        encoder_args: list | None = None,
        lut_filter: str = "",
        fps: int | str = 30,
        out_w: int = 1920,
        out_h: int = 1080,
    ) -> None:
        filters = []
        if lut_filter:
            filters.append(lut_filter)
        filters.extend(
            [
                self._get_crop_filter(
                    segment["speaker"], src_w, src_h, crop_config, out_w, out_h
                ),
                "format=yuv420p",
            ]
        )
        polish = get_video_polish_filters(self.config)
        if polish:
            filters.append(polish)
        if ass_path and ass_path.exists() and "Dialogue:" in ass_path.read_text():
            filters.append(f"subtitles='{escape_srt_path(ass_path)}'")
        render_video_segment(
            source,
            output,
            source_start=segment.get("source_start", segment["start"]),
            source_end=segment.get("source_end", segment["end"]),
            video_filter=",".join(filters),
            encoder_args=encoder_args or get_video_encoder_args(self.config),
            fps=fps,
            runner=self._run_ffmpeg,
        )

    def _get_crop_filter(
        self, speaker, src_w, src_h, crop_config, out_w=1920, out_h=1080
    ):
        scale = get_scale_filter(out_w, out_h)
        cx, cy, zoom, mode = resolve_speaker(speaker, src_w, src_h, crop_config)
        if mode is None:
            return scale
        x, y, crop_w, crop_h = compute_crop(src_w, src_h, cx, cy, zoom, mode)
        return f"crop={crop_w}:{crop_h}:{x}:{y},{scale}"

    def _apply_edits(self, segments: list, edits: list) -> list:
        """Compatibility wrapper; production render uses the probed source duration."""
        if not segments:
            return []
        duration = max(float(segment["end"]) for segment in segments)
        for edit in edits:
            if edit.get("type") == "cut":
                duration = max(duration, float(edit["end_seconds"]))
            elif edit.get("type") in {"trim_start", "trim_end"}:
                duration = max(duration, float(edit["seconds"]))
        try:
            timeline = Timeline.from_edits(duration, edits)
        except ValueError as exc:
            if "remove the entire episode" in str(exc):
                return []
            raise
        return timeline.project(segments, minimum_duration=0.1)

    def _run_ffmpeg(self, cmd, **kwargs):
        return timed_ffmpeg(cmd, agent_logger=self.logger, **kwargs)

    @staticmethod
    def _output_dimensions(src_w: int, src_h: int) -> tuple[int, int]:
        scale = min(1.0, 1920 / src_w, 1080 / src_h)
        width = int(src_w * scale) // 2 * 2
        height = int(src_h * scale) // 2 * 2
        return width, height
