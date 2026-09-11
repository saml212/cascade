"""Render the canonical 16:9 episode from the shared source timeline."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.base import BaseAgent, timed_ffmpeg
from agents.speaker_cut import current_speaker_segments
from agents.transcribe import current_diarized_transcript
from lib.ass import CaptionStyle, generate_ass_from_diarized
from lib.audio_mix import generate_audio_mix
from lib.crop import compute_crop, resolve_speaker
from lib.delivery_video import (
    build_render_segments,
    concat_video_segments,
    current_longform_render,
    longform_render_fingerprint,
    mux_timeline_audio,
    record_longform_render,
    render_config_for_episode,
    render_output_lock,
    render_scratch_dir,
    render_space_budget,
    render_video_segment,
    require_render_space,
    source_fps,
)
from lib.encoding import (
    get_lut_filter,
    get_scale_filter,
    get_video_encoder_args,
    get_video_encoding_policy,
    get_video_polish_filters,
)
from lib.ffprobe import probe as ffprobe
from lib.loudness import measure_loudness
from lib.srt import escape_srt_path
from lib.timeline import Timeline, rebase_diarized


class LongformRenderAgent(BaseAgent):
    name = "longform_render"

    def __init__(
        self,
        episode_dir: Path,
        config: dict,
        progress: Callable[[float, str], None] | None = None,
    ):
        super().__init__(episode_dir, config)
        self._delivery_progress = progress

    def execute(self) -> dict:
        with render_output_lock(self.episode_dir / "upload_video.mp4"):
            return self._execute_locked()

    def _execute_locked(self) -> dict:
        episode = self.load_json("episode.json")
        self.config = render_config_for_episode(episode, self.config)
        crop_config = episode.get("crop_config")
        if not crop_config:
            raise ValueError("Complete crop setup before rendering")

        source = self.episode_dir / "source_merged.mp4"
        if not source.exists():
            raise FileNotFoundError("source_merged.mp4 is required for longform render")
        segment_document = current_speaker_segments(
            self.episode_dir, episode, self.config
        )
        segments = segment_document.get("segments", []) if segment_document else []
        if not segments:
            raise ValueError(
                "Current source-clock speaker segments are required for longform render"
            )
        diarized = current_diarized_transcript(self.episode_dir, episode, self.config)
        if not diarized:
            raise ValueError(
                "Current source-clock transcript is required for longform captions"
            )

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
        render_segments = build_render_segments(timeline, segments, frame_rate=fps)
        if not render_segments:
            raise ValueError("No retained speaker segments remain after edits")

        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])
        out_w, out_h = self._output_dimensions(src_w, src_h)
        captions = rebase_diarized(diarized, timeline)
        style = CaptionStyle(
            font_size=max(36, round(out_h * 0.045)),
            margin_v=max(48, round(out_h * 0.07)),
            words_per_phrase=4,
            play_res_x=out_w,
            play_res_y=out_h,
        )
        caption_path = self.episode_dir / "subtitles" / "longform.ass"
        caption_path.parent.mkdir(parents=True, exist_ok=True)
        generate_ass_from_diarized(captions, 0, timeline.duration, caption_path, style)
        burn_captions = bool(
            self.config.get("processing", {}).get("longform_burn_captions", False)
        )
        caption_record = {
            "path": str(caption_path.relative_to(self.episode_dir)),
            "format": "ass",
            "burned_in": burn_captions,
        }
        current = current_longform_render(
            self.episode_dir, episode, self.config, audio, segments
        )
        if current:
            self.logger.info("Canonical speaker-cut longform is already current")
            return self._result(current, caption_path, reused=True)

        encoding = get_video_encoding_policy(self.config, "longform")
        encoder_args = get_video_encoder_args(self.config, "longform")
        lut_filter = get_lut_filter(self.config)
        budget = render_space_budget(timeline.duration, encoding)
        require_render_space(self.episode_dir, budget)
        output = self.episode_dir / "upload_video.mp4"

        with render_scratch_dir(
            f"longform-{self.episode_dir.name}", budget["scratch_bytes"]
        ) as scratch:
            segment_paths = self._render_segments(
                source,
                scratch,
                render_segments,
                captions if burn_captions else None,
                style if burn_captions else None,
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
                audio_bitrate=encoding["audio_bitrate"],
                runner=self._run_ffmpeg,
            )

        media.update(
            encoder=encoder_args[1],
            edit_count=len(episode.get("longform_edits", [])),
            segment_count=len(render_segments),
            expected_duration_seconds=round(timeline.duration, 3),
        )
        loudness = measure_loudness(output)
        if loudness:
            media["audio_loudness"] = loudness
        record = record_longform_render(
            self.episode_dir,
            fingerprint=fingerprint,
            render_mode="speaker_cut",
            timeline=timeline,
            media=media,
            captions=caption_record,
            provenance={
                "color_grade": (
                    "lut" if episode.get("delivery_apply_lut", False) else "source"
                ),
                "encoding": {**encoding, "encoder": encoder_args[1]},
            },
        )
        return self._result(record, caption_path, reused=False)

    def _result(self, record: dict, caption_path: Path, *, reused: bool) -> dict:
        output = self.episode_dir / "upload_video.mp4"
        return {
            "path": str(output),
            "output_path": str(output),
            "filename": output.name,
            "render_mode": record["render_mode"],
            "render_fingerprint": record["fingerprint"],
            "reused": reused,
            "source_clock": True,
            "keep_intervals": record["keep_intervals"],
            "caption_path": str(caption_path),
            "captions_burned_in": record["captions"]["burned_in"],
            **record["output"],
            "manifest": record,
        }

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
                ass_path = None
                if captions is not None and style is not None:
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

    def report_progress(self, current: int, total: int, detail: str = ""):
        super().report_progress(current, total, detail)
        if self._delivery_progress:
            percent = current / total * 100 if total else 0.0
            self._delivery_progress(percent, detail)

    def _output_dimensions(self, src_w: int, src_h: int) -> tuple[int, int]:
        value = self.config.get("processing", {}).get("output_resolution", "1920x1080")
        try:
            target_w, target_h = (int(part) for part in value.lower().split("x", 1))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid output_resolution: {value!r}") from exc
        if target_w <= 0 or target_h <= 0:
            raise ValueError(f"Invalid output_resolution: {value!r}")
        scale = min(1.0, src_w / target_w, src_h / target_h)
        width = int(target_w * scale) // 2 * 2
        height = int(target_h * scale) // 2 * 2
        return width, height


def render_longform(
    episode_dir: Path,
    config: dict,
    *,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Render or reuse the canonical manifest-backed longform artifact."""
    return LongformRenderAgent(episode_dir, config, progress=progress).execute()
