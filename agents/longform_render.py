"""Render the canonical 16:9 episode from the shared source timeline."""

from __future__ import annotations

import copy
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
    longform_trim_reuse_fingerprint,
    longform_trim_reuse_proof,
    mux_timeline_audio,
    preserve_reviewed_output,
    record_longform_render,
    render_config_for_episode,
    render_output_lock,
    render_scratch_dir,
    render_space_budget,
    render_video_segment,
    require_render_space,
    reusable_terminal_trim_render,
    source_fps,
    staged_render_output,
)
from lib.encoding import (
    get_lut_filter,
    get_scale_filter,
    get_video_encoder_args,
    get_video_encoding_policy,
    get_video_polish_filters,
)
from lib.ffprobe import probe as ffprobe
from lib.loudness import delivery_loudness_policy, loudness_status
from lib.srt import escape_srt_path
from lib.timeline import Timeline, rebase_diarized

_POST_RENDER_PHASES = 3


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

    def repair_audio(self) -> dict:
        """Remaster a current longform's audio without re-encoding its video."""
        with render_output_lock(self.episode_dir / "upload_video.mp4"):
            return self._execute_locked(repair_only=True)

    def _execute_locked(self, *, repair_only: bool = False) -> dict:
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
            raise RuntimeError("Canonical audio source is required")

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
        encoding = get_video_encoding_policy(self.config, "longform")
        loudness_policy = delivery_loudness_policy(self.config, "longform")
        encoder_args = get_video_encoder_args(self.config, "longform")
        current = current_longform_render(
            self.episode_dir, episode, self.config, audio, segments
        )
        if current:
            audio_state = loudness_status(
                current.get("output", {}).get("audio_loudness"), loudness_policy
            )
            if audio_state["safe"]:
                self.logger.info("Canonical speaker-cut longform is already current")
                return self._result(current, caption_path, reused=True)
            return self._remaster_current_audio(
                episode,
                audio,
                segments,
                timeline,
                fingerprint,
                caption_path,
                caption_record,
                render_segments,
                encoding,
                encoder_args,
                current,
            )
        if repair_only:
            raise RuntimeError(
                "Audio repair requires a current manifest-backed longform render"
            )

        reuse_record = reusable_terminal_trim_render(
            self.episode_dir,
            episode,
            self.config,
            audio,
            segments,
            timeline,
        )
        if reuse_record is not None:
            return self._reuse_terminal_prefix(
                episode,
                audio,
                segments,
                timeline,
                fingerprint,
                caption_path,
                caption_record,
                render_segments,
                encoding,
                encoder_args,
                reuse_record,
            )

        lut_filter = get_lut_filter(self.config)
        budget = render_space_budget(timeline.duration, encoding)
        require_render_space(self.episode_dir, budget)
        output = self.episode_dir / "upload_video.mp4"

        with staged_render_output(output) as staged:
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
                progress_total = len(render_segments) + _POST_RENDER_PHASES
                self.report_progress(
                    len(render_segments), progress_total, "Joining rendered segments"
                )
                concat_video_segments(
                    segment_paths, video_only, runner=self._run_ffmpeg
                )
                self.report_progress(
                    len(render_segments) + 1,
                    progress_total,
                    "Muxing canonical audio",
                )
                media = mux_timeline_audio(
                    video_only,
                    audio,
                    staged,
                    timeline,
                    audio_bitrate=encoding["audio_bitrate"],
                    loudness_policy=loudness_policy,
                    runner=self._run_ffmpeg,
                )

            media.update(
                encoder=encoder_args[1],
                edit_count=len(episode.get("longform_edits", [])),
                segment_count=len(render_segments),
                expected_duration_seconds=round(timeline.duration, 3),
            )
            self.report_progress(
                len(render_segments) + 2,
                progress_total,
                "Output audio verified",
            )
            if "audio_loudness" not in media:
                raise RuntimeError("Rendered longform has no verified audio loudness")
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
                "terminal_trim_reuse": longform_trim_reuse_proof(
                    fingerprint,
                    longform_trim_reuse_fingerprint(
                        self.episode_dir,
                        episode,
                        self.config,
                        audio,
                        segments,
                    ),
                    timeline,
                ),
            },
        )
        self.report_progress(progress_total, progress_total, "Longform render complete")
        return self._result(record, caption_path, reused=False)

    def _remaster_current_audio(
        self,
        episode: dict,
        audio: Path,
        segments: list[dict],
        timeline: Timeline,
        fingerprint: str,
        caption_path: Path,
        caption_record: dict,
        render_segments: list[dict],
        encoding: dict,
        encoder_args: list[str],
        current: dict,
    ) -> dict:
        """Copy verified video packets while replacing and checking only audio."""
        output = self.episode_dir / "upload_video.mp4"
        output_bytes = int(current.get("output", {}).get("size_bytes", 0))
        if output_bytes <= 0:
            raise RuntimeError("Current longform has no recorded output size")
        require_render_space(
            self.episode_dir, {"output_bytes": output_bytes, "scratch_bytes": 0}
        )
        policy = delivery_loudness_policy(self.config, "longform")
        self.report_progress(0, 2, "Remastering canonical audio; preserving video")
        with preserve_reviewed_output(output):
            with staged_render_output(output) as staged:
                media = mux_timeline_audio(
                    output,
                    audio,
                    staged,
                    timeline,
                    audio_bitrate=encoding["audio_bitrate"],
                    loudness_policy=policy,
                    verify_video_copy=True,
                    runner=self._run_ffmpeg,
                )
                if "audio_loudness" not in media:
                    raise RuntimeError(
                        "Repaired longform has no verified audio loudness"
                    )
                media.update(
                    encoder=current.get("output", {}).get("encoder", encoder_args[1]),
                    edit_count=len(episode.get("longform_edits", [])),
                    segment_count=len(render_segments),
                    expected_duration_seconds=round(timeline.duration, 3),
                )
            provenance = copy.deepcopy(current.get("provenance", {}))
            provenance["audio_remaster"] = {
                "method": "copy-video-remux-canonical-audio/v1",
                "source_render_fingerprint": current["fingerprint"],
                "video_reencoded": False,
                "video_copy_verification": copy.deepcopy(
                    media["video_copy_verification"]
                ),
                "policy": policy,
            }
            self.report_progress(1, 2, "Recording verified audio repair")
            record = record_longform_render(
                self.episode_dir,
                fingerprint=fingerprint,
                render_mode="speaker_cut",
                timeline=timeline,
                media=media,
                captions=caption_record,
                provenance=provenance,
            )
        self.report_progress(2, 2, "Longform audio repair complete")
        result = self._result(record, caption_path, reused=True)
        result["audio_repaired"] = True
        result["video_reencoded"] = False
        return result

    def _reuse_terminal_prefix(
        self,
        episode: dict,
        audio: Path,
        segments: list[dict],
        timeline: Timeline,
        fingerprint: str,
        caption_path: Path,
        caption_record: dict,
        render_segments: list[dict],
        encoding: dict,
        encoder_args: list[str],
        reuse_record: dict,
    ) -> dict:
        output = self.episode_dir / "upload_video.mp4"
        output_bytes = int(reuse_record.get("output", {}).get("size_bytes", 0))
        if output_bytes <= 0:
            raise RuntimeError("Verified prefix render has no recorded output size")
        require_render_space(
            self.episode_dir,
            {"output_bytes": output_bytes, "scratch_bytes": 0},
        )
        progress_total = 3
        self.report_progress(
            0,
            progress_total,
            "Reusing verified video pixels and muxing canonical audio",
        )
        with staged_render_output(output) as staged:
            media = mux_timeline_audio(
                output,
                audio,
                staged,
                timeline,
                audio_bitrate=encoding["audio_bitrate"],
                loudness_policy=delivery_loudness_policy(self.config, "longform"),
                runner=self._run_ffmpeg,
            )
            media.update(
                encoder=encoder_args[1],
                edit_count=len(episode.get("longform_edits", [])),
                segment_count=len(render_segments),
                expected_duration_seconds=round(timeline.duration, 3),
            )
            self.report_progress(1, progress_total, "Output audio verified")
            if "audio_loudness" not in media:
                raise RuntimeError("Rendered longform has no verified audio loudness")
        input_fingerprint = longform_trim_reuse_fingerprint(
            self.episode_dir,
            episode,
            self.config,
            audio,
            segments,
        )
        self.report_progress(2, progress_total, "Recording verified render manifest")
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
                "terminal_trim_reuse": longform_trim_reuse_proof(
                    fingerprint, input_fingerprint, timeline
                ),
                "video_reuse": {
                    "mode": "verified_terminal_prefix",
                    "source_render_fingerprint": reuse_record["fingerprint"],
                    "source_output_size_bytes": output_bytes,
                },
            },
        )
        self.report_progress(progress_total, progress_total, "Longform render complete")
        return self._result(record, caption_path, reused=True)

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
                    len(paths) + _POST_RENDER_PHASES,
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


def repair_longform_audio(
    episode_dir: Path,
    config: dict,
    *,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Repair current longform audio while preserving its encoded video packets."""
    return LongformRenderAgent(episode_dir, config, progress=progress).repair_audio()
