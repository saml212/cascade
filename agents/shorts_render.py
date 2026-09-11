"""Render source-clock 9:16 clips with dynamic speaker crops and ASS captions."""

from __future__ import annotations

import os
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
    current_short_render,
    mux_timeline_audio,
    record_short_render,
    render_config_for_episode,
    render_output_lock,
    render_scratch_dir,
    render_space_budget,
    render_video_segment,
    require_render_space,
    short_render_fingerprint,
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
from lib.srt import escape_srt_path, generate_srt_from_diarized
from lib.timeline import Timeline, rebase_diarized


class ShortsRenderAgent(BaseAgent):
    name = "shorts_render"

    def execute(self) -> dict:
        clips = self.load_json("clips.json").get("clips", [])
        return self._render_clips(
            [
                clip
                for clip in clips
                if clip.get("selection_status") != "rejected"
                and clip.get("status") != "rejected"
            ]
        )

    def render_clip(self, clip_id: str) -> dict:
        """Render one stored candidate without mutating clip selection state."""
        clips = self.load_json("clips.json").get("clips", [])
        clip = next((item for item in clips if item.get("id") == clip_id), None)
        if clip is None:
            raise KeyError(f"Unknown clip: {clip_id}")
        result = self._render_clips([clip])
        return {
            "clip_id": clip_id,
            "output_path": str(self.episode_dir / "shorts" / f"{clip_id}.mp4"),
            "caption_path": str(self.episode_dir / "subtitles" / f"{clip_id}.ass"),
            "reused": bool(result["renders"][clip_id].get("reused")),
            "render": result["renders"][clip_id],
        }

    def _render_clips(self, clips: list[dict]) -> dict:
        shorts_dir = self.episode_dir / "shorts"
        if not clips:
            shorts_dir.mkdir(exist_ok=True)
            return {
                "rendered_clips": [],
                "count": 0,
                "shorts_dir": str(shorts_dir),
                "render_mode": "speaker_cut_short",
                "clock": "source",
                "renders": {},
            }
        episode = self.load_json("episode.json")
        self.config = render_config_for_episode(episode, self.config)
        crop_config = episode.get("crop_config")
        if not crop_config:
            raise ValueError("Complete crop setup before rendering")

        source = self.episode_dir / "source_merged.mp4"
        if not source.exists():
            raise FileNotFoundError("source_merged.mp4 is required for shorts render")
        segment_document = current_speaker_segments(
            self.episode_dir, episode, self.config
        )
        segments = segment_document.get("segments", []) if segment_document else []
        if not segments:
            raise ValueError(
                "Current source-clock speaker segments are required for shorts render"
            )
        diarized = current_diarized_transcript(self.episode_dir, episode, self.config)
        if not diarized:
            raise ValueError(
                "Current source-clock transcript is required for shorts captions"
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
        episode_timeline = Timeline.from_edits(
            float(source_probe["format"]["duration"]),
            episode.get("longform_edits", []),
        )
        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])
        fps = source_fps(video_stream, episode)
        episode_timeline = episode_timeline.quantize(fps)
        encoding = get_video_encoding_policy(self.config, "shorts")
        encoder_args = get_video_encoder_args(self.config, "shorts")
        lut_filter = get_lut_filter(self.config)

        subtitles_dir = self.episode_dir / "subtitles"
        shorts_dir.mkdir(exist_ok=True)
        subtitles_dir.mkdir(exist_ok=True)

        rendered = []
        records = {}
        jobs = []
        for clip in clips:
            clip_id = clip["id"]
            clip_timeline = episode_timeline.slice(
                float(clip["start_seconds"]), float(clip["end_seconds"])
            ).quantize(fps)
            if clip_timeline.duration < 0.1:
                raise ValueError(f"{clip_id} contains no retained source material")
            fingerprint = short_render_fingerprint(
                self.episode_dir,
                episode,
                self.config,
                audio,
                segments,
                clip,
            )
            current = current_short_render(
                self.episode_dir,
                episode,
                self.config,
                audio,
                segments,
                clip,
            )
            if current:
                records[clip_id] = {**current, "reused": True}
                rendered.append(clip_id)
                continue
            jobs.append((clip, clip_timeline, fingerprint))

        workers = min(max((os.cpu_count() or 2) // 4, 1), 2, max(1, len(jobs)))
        budgets = [
            render_space_budget(timeline.duration, encoding) for _, timeline, _ in jobs
        ]
        if budgets:
            require_render_space(
                shorts_dir,
                {
                    "output_bytes": sum(budget["output_bytes"] for budget in budgets),
                    "scratch_bytes": sum(
                        sorted(
                            (budget["scratch_bytes"] for budget in budgets),
                            reverse=True,
                        )[:workers]
                    ),
                },
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._render_short,
                    source,
                    shorts_dir / f"{clip['id']}.mp4",
                    subtitles_dir / f"{clip['id']}.ass",
                    float(clip["start_seconds"]),
                    float(clip["end_seconds"]),
                    segments,
                    src_w,
                    src_h,
                    encoding["audio_bitrate"],
                    crop_config,
                    encoder_args,
                    lut_filter,
                    audio,
                    fps,
                    timeline=clip_timeline,
                    diarized=diarized,
                    fingerprint=fingerprint,
                    episode=episode,
                    clip=clip,
                    encoding=encoding,
                ): clip["id"]
                for clip, clip_timeline, fingerprint in jobs
            }
            for future in as_completed(futures):
                clip_id = futures[future]
                records[clip_id] = future.result()
                rendered.append(clip_id)
                self.report_progress(len(rendered), len(clips), f"Rendered {clip_id}")

        return {
            "rendered_clips": sorted(rendered),
            "count": len(rendered),
            "shorts_dir": str(shorts_dir),
            "render_mode": "speaker_cut_short",
            "clock": "source",
            "renders": records,
        }

    def _render_short(self, source, output, *args, **kwargs) -> dict:
        with render_output_lock(Path(output)):
            return self._render_short_unlocked(source, output, *args, **kwargs)

    def _render_short_unlocked(
        self,
        source,
        output,
        caption_path,
        start,
        end,
        segments,
        src_w,
        src_h,
        audio_bitrate,
        crop_config,
        encoder_args,
        lut_filter="",
        audio_mix_path=None,
        fps=30,
        *,
        timeline=None,
        diarized=None,
        fingerprint=None,
        episode=None,
        clip=None,
        encoding=None,
    ) -> dict:
        """Render one clip; positional arguments remain compatible with chat actions."""
        episode = episode or self.load_json("episode.json")
        diarized = diarized or self.load_json("diarized_transcript.json")
        if not audio_mix_path or not Path(audio_mix_path).exists():
            raise RuntimeError("Canonical work/audio_mix.wav is required")
        if timeline is None:
            source_probe = ffprobe(source)
            video_stream = next(
                stream
                for stream in source_probe["streams"]
                if stream["codec_type"] == "video"
            )
            fps = source_fps(video_stream, episode)
            duration = float(source_probe["format"]["duration"])
            timeline = (
                Timeline.from_edits(duration, episode.get("longform_edits", []))
                .slice(float(start), float(end))
                .quantize(fps)
            )
        if timeline.duration < 0.1:
            raise ValueError("Clip contains no retained source material")

        render_segments = build_render_segments(timeline, segments, frame_rate=fps)
        render_segments = self._apply_overlap_policy(render_segments)
        captions = rebase_diarized(diarized, timeline)
        style = CaptionStyle()
        caption_path = Path(caption_path).with_suffix(".ass")
        caption_path.parent.mkdir(parents=True, exist_ok=True)
        generate_ass_from_diarized(captions, 0, timeline.duration, caption_path, style)

        encoding = encoding or get_video_encoding_policy(self.config, "shorts")
        budget = render_space_budget(timeline.duration, encoding)
        require_render_space(Path(output).parent, budget)
        with render_scratch_dir(
            f"short-{Path(output).stem}", budget["scratch_bytes"]
        ) as scratch:
            paths = []
            for index, segment in enumerate(render_segments):
                segment_ass = scratch / f"segment_{index:03d}.ass"
                segment_timeline = Timeline(
                    timeline.duration, [(segment["start"], segment["end"])]
                )
                generate_ass_from_diarized(
                    rebase_diarized(captions, segment_timeline),
                    0,
                    segment["duration"],
                    segment_ass,
                    style,
                )
                filters = []
                if lut_filter:
                    filters.append(lut_filter)
                filters.extend(
                    [
                        self._get_short_crop_filter_no_subs(
                            segment["speaker"], src_w, src_h, crop_config
                        ),
                    ]
                )
                if "Dialogue:" in segment_ass.read_text():
                    filters.append(f"subtitles='{escape_srt_path(segment_ass)}'")
                segment_path = scratch / f"segment_{index:03d}.mp4"
                render_video_segment(
                    Path(source),
                    segment_path,
                    source_start=segment["source_start"],
                    source_end=segment["source_end"],
                    video_filter=",".join(filters),
                    encoder_args=encoder_args,
                    fps=fps,
                    runner=self._run_ffmpeg,
                )
                paths.append(segment_path)
            video_only = scratch / "short_video.mp4"
            concat_video_segments(paths, video_only, runner=self._run_ffmpeg)
            media = mux_timeline_audio(
                video_only,
                Path(audio_mix_path),
                Path(output),
                timeline,
                audio_bitrate=audio_bitrate,
                runner=self._run_ffmpeg,
            )

        clip = clip or {
            "id": Path(output).stem,
            "start_seconds": start,
            "end_seconds": end,
        }
        fingerprint = fingerprint or short_render_fingerprint(
            self.episode_dir,
            episode,
            self.config,
            Path(audio_mix_path),
            segments,
            clip,
        )
        return record_short_render(
            self.episode_dir,
            clip["id"],
            fingerprint=fingerprint,
            timeline=timeline,
            media=media,
            captions={
                "path": str(caption_path.relative_to(self.episode_dir)),
                "format": "ass",
                "burned_in": True,
            },
            provenance={
                "color_grade": (
                    "lut" if episode.get("delivery_apply_lut", False) else "source"
                ),
                "overlap_policy": "hold_neighbor_up_to_threshold_else_fit_wide",
                "overlap_hold_seconds": self.config.get("processing", {}).get(
                    "shorts_hold_wide_seconds", 3.0
                ),
                "encoding": {**encoding, "encoder": encoder_args[1]},
            },
        )

    def _apply_overlap_policy(self, segments: list[dict]) -> list[dict]:
        """Hold a nearby speaker through brief BOTH spans; keep long spans wide."""
        threshold = float(
            self.config.get("processing", {}).get("shorts_hold_wide_seconds", 3.0)
        )
        resolved = []
        for index, segment in enumerate(segments):
            updated = dict(segment)
            if (
                segment["speaker"] in {"BOTH", "NONE"}
                and segment["duration"] <= threshold
            ):
                neighbors = [
                    resolved[-1]["speaker"] if resolved else None,
                    next(
                        (
                            item["speaker"]
                            for item in segments[index + 1 :]
                            if item["speaker"] not in {"BOTH", "NONE"}
                        ),
                        None,
                    ),
                ]
                updated["speaker"] = next(
                    (speaker for speaker in neighbors if speaker is not None),
                    segment["speaker"],
                )
            if (
                resolved
                and resolved[-1]["speaker"] == updated["speaker"]
                and abs(resolved[-1]["source_end"] - updated["source_start"]) < 1e-6
            ):
                resolved[-1]["end"] = updated["end"]
                resolved[-1]["source_end"] = updated["source_end"]
                resolved[-1]["duration"] = resolved[-1]["end"] - resolved[-1]["start"]
            else:
                resolved.append(updated)
        return resolved

    def _get_clip_segments(self, segments, clip_start, clip_end):
        timeline = Timeline(float(clip_end), [(float(clip_start), float(clip_end))])
        return [
            {
                "start": segment["source_start"],
                "end": segment["source_end"],
                "speaker": segment["speaker"],
            }
            for segment in build_render_segments(timeline, segments)
        ]

    def _get_short_crop_region(self, speaker, src_w, src_h, crop_config):
        cx, cy, zoom, _ = resolve_speaker(
            speaker, src_w, src_h, crop_config, for_shorts=True
        )
        x, y, crop_w, crop_h = compute_crop(src_w, src_h, cx, cy, zoom, "short")
        return crop_w, crop_h, x, y

    def _get_short_crop_filter_no_subs(self, speaker, src_w, src_h, crop_config):
        if speaker in {"BOTH", "NONE"}:
            chain = (
                "scale=1080:1920:force_original_aspect_ratio=decrease:"
                "flags=lanczos+accurate_rnd+full_chroma_int:"
                "sws_dither=ed:param0=5,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p"
            )
            polish = get_video_polish_filters(self.config)
            return f"{chain},{polish}" if polish else chain
        crop_w, crop_h, x, y = self._get_short_crop_region(
            speaker, src_w, src_h, crop_config
        )
        chain = (
            f"crop={crop_w}:{crop_h}:{x}:{y},"
            f"{get_scale_filter(1080, 1920)},format=yuv420p"
        )
        polish = get_video_polish_filters(self.config)
        return f"{chain},{polish}" if polish else chain

    def _get_short_crop_filter(self, speaker, src_w, src_h, caption_path, crop_config):
        chain = self._get_short_crop_filter_no_subs(speaker, src_w, src_h, crop_config)
        if caption_path and Path(caption_path).exists():
            chain += f",subtitles='{escape_srt_path(Path(caption_path))}'"
        return chain

    def _generate_clip_srt(self, diarized, start, end, srt_path):
        """Compatibility helper for chat clients that still request SRT."""
        generate_srt_from_diarized(diarized, start, end, Path(srt_path))

    def _run_ffmpeg(self, cmd, **kwargs):
        return timed_ffmpeg(cmd, agent_logger=self.logger, **kwargs)


def render_single_clip(episode_dir: Path, config: dict, clip_id: str) -> dict:
    """Public API adapter for an atomic, manifest-backed single-clip render."""
    return ShortsRenderAgent(episode_dir, config).render_clip(clip_id)
