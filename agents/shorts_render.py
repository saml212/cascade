"""Render source-clock 9:16 clips with dynamic speaker crops and ASS captions."""

from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

from agents.base import BaseAgent, timed_ffmpeg
from agents.speaker_cut import current_speaker_segments
from agents.transcribe import current_diarized_transcript
from lib.ass import (
    CaptionPlacement,
    CaptionStyle,
    generate_ass_from_diarized,
    resolve_caption_speaker_targets,
)
from lib.audio_mix import generate_audio_mix
from lib.crop import compute_crop, resolve_speaker
from lib.delivery_video import (
    audio_packet_signature,
    audio_remaster_provenance,
    build_render_segments,
    concat_video_segments,
    current_short_render,
    ffmpeg_executable,
    mux_timeline_audio,
    preserve_reviewed_output,
    record_short_render,
    render_config_for_episode,
    render_output_lock,
    render_scratch_dir,
    render_space_budget,
    render_video_segment,
    require_render_space,
    short_render_fingerprint,
    source_fps,
    staged_render_output,
    validate_av_output,
)
from lib.encoding import (
    get_color_metadata_args,
    get_lut_filter,
    get_scale_filter,
    get_video_encoder_args,
    get_video_encoding_policy,
    get_video_polish_filters,
)
from lib.ffprobe import probe as ffprobe
from lib.ffprobe import scan_identity
from lib.loudness import (
    delivery_loudness_policy,
    loudness_status,
    measure_loudness,
    require_delivery_loudness,
)
from lib.short_variants import (
    BACKGROUND_VARIANT_ID,
    CONTAIN_BLUR_FIT_MODE,
    GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION,
    GAMEPLAY_SURROUND_RENDER_PLAN,
    GAMEPLAY_SURROUND_VARIANT_ID,
    GAMEPLAY_VARIANT_IDS,
    SPEAKER_PANEL_VARIANT_IDS,
    SPEAKER_PANELS_RENDER_PLAN,
    SPEAKER_PANELS_VARIANT_ID,
    background_variant_fingerprint,
    background_variant_output,
    background_variant_state,
    default_background_asset_id,
    file_content_identity,
    load_background_asset,
    load_background_variant_asset,
    record_background_variant,
    require_background_variant,
    require_background_variant_asset,
    require_gameplay_caption_context_revision,
    require_speaker_panel_caption_context_revision,
    resolve_gameplay_variant_playback,
    speaker_panel_caption_context_revision,
)
from lib.srt import escape_srt_path
from lib.timeline import Timeline, rebase_diarized

THREE_PERSON_STACK_CAPTION_MARGIN_V = 600
BACKGROUND_CAPTION_MARGIN_V = 840
BACKGROUND_PANEL_HEADROOM_FRACTION = 1 / 12


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

    def repair_clip_audio(self, clip_id: str) -> dict:
        """Normalize one current short while preserving its encoded video packets."""
        output = self.episode_dir / "shorts" / f"{clip_id}.mp4"
        with render_output_lock(output):
            return self._repair_clip_audio_locked(clip_id, output)

    def render_background_variant(
        self,
        clip_id: str,
        asset_id: str | None = None,
        variant_id: str = BACKGROUND_VARIANT_ID,
    ) -> dict:
        """Compose one optional motion variant from a current canonical short."""
        selected_asset_id = (
            default_background_asset_id(variant_id) if asset_id is None else asset_id
        )
        require_background_variant_asset(variant_id, selected_asset_id)
        clips = self.load_json("clips.json").get("clips", [])
        clip = next((item for item in clips if item.get("id") == clip_id), None)
        if clip is None:
            raise KeyError(f"Unknown clip: {clip_id}")
        output = background_variant_output(self.episode_dir, clip_id, variant_id)
        with render_output_lock(output):
            return self._render_background_variant_locked(
                clip, selected_asset_id, variant_id, output
            )

    def _render_background_variant_locked(
        self, clip: dict, asset_id: str, variant_id: str, output: Path
    ) -> dict:
        episode = self.load_json("episode.json")
        self.config = render_config_for_episode(episode, self.config)
        segment_document = current_speaker_segments(
            self.episode_dir, episode, self.config
        )
        segments = segment_document.get("segments", []) if segment_document else []
        if not segments:
            raise ValueError(
                "Current speaker segments are required for a short variant"
            )
        source = self.episode_dir / "source_merged.mp4"
        source_probe = ffprobe(source)
        video_stream = next(
            stream
            for stream in source_probe["streams"]
            if stream["codec_type"] == "video"
        )
        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])
        fps = source_fps(video_stream, episode)
        timeline = (
            Timeline.from_edits(
                float(source_probe["format"]["duration"]),
                episode.get("longform_edits", []),
            )
            .slice(float(clip["start_seconds"]), float(clip["end_seconds"]))
            .quantize(fps)
        )
        if timeline.duration < 0.1:
            raise ValueError("Clip contains no retained source material")

        from lib.audio_mix import selected_audio_source

        audio = selected_audio_source(self.episode_dir, episode, self.config) or (
            self.episode_dir / "work" / "audio_mix.wav"
        )
        if not audio.is_file():
            raise RuntimeError("Canonical audio source is required")
        base_record = current_short_render(
            self.episode_dir, episode, self.config, audio, segments, clip
        )
        if base_record is None:
            raise ValueError(
                "A current canonical short is required for a background variant"
            )

        base_path = self.episode_dir / "shorts" / f"{clip['id']}.mp4"
        base_duration = float(ffprobe(base_path)["format"]["duration"])
        base_identity = scan_identity(base_path)
        if base_identity is None:
            raise OSError("Canonical short changed while its identity was read")
        asset = (
            None
            if variant_id == SPEAKER_PANELS_VARIANT_ID
            else load_background_variant_asset(variant_id, verify_content=True)
            if variant_id == GAMEPLAY_SURROUND_VARIANT_ID
            else load_background_asset(asset_id, verify_content=True)
        )
        media_assets = asset.get("assets", [asset]) if asset else []
        source_durations = {}
        for media_asset in media_assets:
            asset_probe = ffprobe(media_asset["path"])
            if not any(
                item.get("codec_type") == "video" for item in asset_probe["streams"]
            ):
                raise ValueError("Background asset has no video stream")
            if any(
                item.get("codec_type") == "audio" for item in asset_probe["streams"]
            ):
                raise ValueError("Background assets must be silent")
            source_durations[str(media_asset["asset_id"])] = float(
                asset_probe["format"]["duration"]
            )
        if variant_id in GAMEPLAY_VARIANT_IDS:
            asset = resolve_gameplay_variant_playback(
                asset,
                episode_id=self.episode_dir.name,
                clip_id=str(clip["id"]),
                variant_id=variant_id,
                clip_duration_seconds=base_duration,
                source_durations=source_durations,
            )

        encoding = get_video_encoding_policy(self.config, "shorts")
        diarized = None
        caption_context_revision = None
        if variant_id in SPEAKER_PANEL_VARIANT_IDS:
            diarized = current_diarized_transcript(
                self.episode_dir, episode, self.config
            )
            if not diarized:
                raise ValueError("Current transcript is required for variant captions")
            caption_context_revision = speaker_panel_caption_context_revision(
                self.episode_dir,
                episode=episode,
                diarized=diarized,
                segment_document=segment_document,
            )
        fingerprint = background_variant_fingerprint(
            base_record,
            base_identity,
            asset,
            encoding,
            variant_id=variant_id,
            caption_context_revision=caption_context_revision,
        )
        current_record, current_state = background_variant_state(
            self.episode_dir,
            str(clip["id"]),
            base_record=base_record,
            encoding=encoding,
            variant_id=variant_id,
        )
        recorded_asset = current_record.get("asset")
        recorded_asset_id = (
            recorded_asset.get("asset_id") if isinstance(recorded_asset, dict) else None
        )
        if (
            current_state["current"]
            and current_record.get("fingerprint") == fingerprint
            and recorded_asset_id == asset_id
        ):
            return self._variant_result(output, current_record, reused=True)

        encoder_args = get_video_encoder_args(self.config, "shorts")
        lut_filter = get_lut_filter(self.config)
        diarized = diarized or current_diarized_transcript(
            self.episode_dir, episode, self.config
        )
        if not diarized:
            raise ValueError("Current transcript is required for variant captions")
        had_output = output.is_file()
        protection = preserve_reviewed_output(output) if had_output else nullcontext()
        try:
            with protection:
                record = self._render_short_unlocked(
                    source,
                    output,
                    self.episode_dir
                    / "subtitles"
                    / "short_variants"
                    / variant_id
                    / f"{clip['id']}.ass",
                    float(clip["start_seconds"]),
                    float(clip["end_seconds"]),
                    segments,
                    src_w,
                    src_h,
                    encoding["audio_bitrate"],
                    episode.get("crop_config", {}),
                    encoder_args,
                    lut_filter,
                    audio,
                    fps,
                    timeline=timeline,
                    diarized=diarized,
                    fingerprint=fingerprint,
                    episode=episode,
                    clip=clip,
                    encoding=encoding,
                    background={
                        "variant_id": variant_id,
                        "asset": asset,
                        "base_path": base_path,
                        "base_duration": base_duration,
                        "base_record": base_record,
                        "base_identity": base_identity,
                        "caption_context_revision": caption_context_revision,
                    },
                    segment_document=segment_document,
                )
        except BaseException:
            if not had_output:
                output.unlink(missing_ok=True)
            raise
        return self._variant_result(output, record, reused=False)

    def _compose_background_variant(
        self,
        podcast_video: Path,
        asset: Path,
        base_short: Path,
        output: Path,
        fps: str,
        suppress_motion: list[tuple[float, float]],
        encoder_args: list[str],
        playback_start_seconds: float | None = None,
        loop_source: bool = True,
    ) -> None:
        disabled = "+".join(
            f"between(t,{start:.6f},{end:.6f})" for start, end in suppress_motion
        )
        enable = f":enable='not({disabled})'" if disabled else ""
        playback_filter = (
            ""
            if playback_start_seconds is None
            else f"trim=start={playback_start_seconds:.6f},setpts=PTS-STARTPTS,"
        )
        graph = (
            "[0:v]setpts=PTS-STARTPTS[podcast];"
            f"[1:v]{playback_filter}"
            "scale=1080:640:force_original_aspect_ratio=increase,"
            f"crop=1080:640,fps={fps},setpts=PTS-STARTPTS[motion];"
            f"[podcast][motion]overlay=0:1280{enable},"
            f"drawbox=x=0:y=1276:w=1080:h=8:color=black@0.85:t=fill{enable},"
            "tpad=stop_mode=clone:stop_duration=0.25,format=yuv420p[variant]"
        )
        self._run_ffmpeg(
            [
                ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(podcast_video),
                *(["-stream_loop", "-1"] if loop_source else []),
                "-i",
                str(asset),
                "-i",
                str(base_short),
                "-filter_complex",
                graph,
                "-map",
                "[variant]",
                "-map",
                "2:a:0",
                *encoder_args,
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                "-shortest",
                *get_color_metadata_args(),
                "-use_editlist",
                "0",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )

    def _compose_gameplay_surround_variant(
        self,
        podcast_video: Path,
        assets: list[dict],
        base_short: Path,
        caption_path: Path,
        output: Path,
        fps: str,
        encoder_args: list[str],
    ) -> None:
        """Compose the approved four-panel layout without touching base audio."""
        by_role = {item["role"]: item for item in assets}
        required = {"subway", "gta", "minecraft"}
        if set(by_role) != required:
            raise ValueError("Gameplay surround requires subway, gta, and minecraft")
        plan = GAMEPLAY_SURROUND_RENDER_PLAN
        upper_h = plan["upper_height"]
        bottom_h = plan["bottom_height"]
        side_w = plan["left_width"]
        podcast_w = plan["podcast_width"]
        header_h = plan["podcast_header_height"]
        podcast_h = upper_h - header_h
        border = plan["panel_border"]
        brand = plan["brand"]
        brand_font_size = plan["brand_font_size"]
        brand_y = plan["brand_y"]
        subtitle_filter = f"subtitles='{escape_srt_path(caption_path)}'"
        subway = by_role["subway"]
        gta = by_role["gta"]
        minecraft = by_role["minecraft"]

        def source_input(asset: dict) -> list[str]:
            policy = asset.get("playback_policy")
            wraps = (
                bool(policy.get("wrap_required")) if isinstance(policy, dict) else True
            )
            return [
                *(["-stream_loop", "-1"] if wraps else []),
                "-i",
                str(asset["path"]),
            ]

        def playback_filter(asset: dict) -> str:
            start = float(asset.get("playback_start_seconds", 0.0))
            return f"trim=start={start:.6f},setpts=PTS-STARTPTS,"

        def focus_crop(asset: dict, width: int, height: int) -> str:
            focus_x = float(asset.get("focus_x", 0.5))
            focus_y = float(asset.get("focus_y", 0.5))
            return f"crop={width}:{height}:(iw-ow)*{focus_x:.6f}:(ih-oh)*{focus_y:.6f}"

        def panel_fit(
            input_index: int,
            asset: dict,
            width: int,
            height: int,
            fit_label: str,
        ) -> str:
            source = f"[{input_index}:v]{playback_filter(asset)}"
            fit_mode = asset.get("fit_mode", "crop")
            if fit_mode == "stretch":
                return f"{source}scale={width}:{height}[{fit_label}]"
            if fit_mode == CONTAIN_BLUR_FIT_MODE:
                return (
                    f"{source}split=2[{fit_label}_fill_source]"
                    f"[{fit_label}_full_source];"
                    f"[{fit_label}_fill_source]scale={width}:{height}:"
                    "force_original_aspect_ratio=increase,"
                    f"{focus_crop(asset, width, height)},gblur=sigma=20,"
                    "eq=brightness=-0.12:saturation=0.75,setsar=1"
                    f"[{fit_label}_fill];"
                    f"[{fit_label}_full_source]scale={width}:{height}:"
                    "force_original_aspect_ratio=decrease:force_divisible_by=2,"
                    f"setsar=1[{fit_label}_full];"
                    f"[{fit_label}_fill][{fit_label}_full]"
                    f"overlay=(W-w)/2:(H-h)/2[{fit_label}]"
                )
            return (
                f"{source}scale={width}:{height}:"
                "force_original_aspect_ratio=increase,"
                f"{focus_crop(asset, width, height)}[{fit_label}]"
            )

        graph = (
            f"[0:v]scale={podcast_w}:{podcast_h},setsar=1,"
            "tpad=stop_mode=clone:stop_duration=0.25[podcast];"
            f"color=c=0x10151d:s={podcast_w}x{header_h}:r={fps},"
            "drawtext=text='THE LOCAL PODCAST':fontcolor=white:fontsize=28:"
            "x=(w-text_w)/2:y=(h-text_h)/2[header];"
            "[header][podcast]vstack=inputs=2[center];"
            f"{panel_fit(1, subway, side_w, upper_h, 'subway_fit')};"
            f"[subway_fit]fps={fps},setsar=1,"
            "drawtext=text='SUBWAY SURFERS':fontcolor=white:fontsize=22:"
            "x=(w-text_w)/2:y=24:box=1:boxcolor=black@0.62:boxborderw=8[left];"
            f"{panel_fit(2, gta, side_w, upper_h, 'gta_fit')};"
            f"[gta_fit]fps={fps},setsar=1,"
            "drawtext=text='GTA DRIVING':fontcolor=white:fontsize=22:"
            "x=(w-text_w)/2:y=24:box=1:boxcolor=black@0.62:boxborderw=8[right];"
            f"{panel_fit(3, minecraft, 1080, bottom_h, 'minecraft_fit')};"
            f"[minecraft_fit]fps={fps},setsar=1,"
            "drawtext=text='MINECRAFT PARKOUR':fontcolor=white:fontsize=24:"
            "x=24:y=24:box=1:boxcolor=black@0.62:boxborderw=8[bottom];"
            "[left][center][right]hstack=inputs=3[upper];"
            "[upper][bottom]vstack=inputs=2[canvas];"
            f"[canvas]drawbox=x={side_w - border // 2}:y=0:w={border}:h={upper_h}:"
            "color=black@0.88:t=fill,"
            f"drawbox=x={side_w + podcast_w - border // 2}:y=0:w={border}:"
            f"h={upper_h}:color=black@0.88:t=fill,"
            f"drawbox=x=0:y={upper_h - border // 2}:w=1080:h={border}:"
            "color=black@0.88:t=fill,"
            f"drawtext=text='{brand}':fontcolor=white:fontsize={brand_font_size}:"
            f"x=(w-text_w)/2:y={brand_y}:box=1:boxcolor=black@0.72:"
            f"boxborderw=14,{subtitle_filter},format=yuv420p[variant]"
        )
        self._run_ffmpeg(
            [
                ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(podcast_video),
                *source_input(by_role["subway"]),
                *source_input(by_role["gta"]),
                *source_input(by_role["minecraft"]),
                "-i",
                str(base_short),
                "-filter_complex",
                graph,
                "-map",
                "[variant]",
                "-map",
                "4:a:0",
                *encoder_args,
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                "-shortest",
                *get_color_metadata_args(),
                "-use_editlist",
                "0",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )

    def _compose_speaker_panels_variant(
        self,
        podcast_video: Path,
        base_short: Path,
        caption_path: Path,
        output: Path,
        fps: str,
        encoder_args: list[str],
    ) -> None:
        plan = SPEAKER_PANELS_RENDER_PLAN
        width, _ = plan["canvas"]
        header_h = plan["header_height"]
        panels_h = plan["panels_height"]
        footer_h = plan["footer_height"]
        subtitle_filter = f"subtitles='{escape_srt_path(caption_path)}'"
        graph = (
            f"[0:v]scale={width}:{panels_h},setsar=1,"
            "tpad=stop_mode=clone:stop_duration=0.25[podcast];"
            f"color=c=0x10151d:s={width}x{header_h}:r={fps},"
            f"drawtext=text='{plan['title']}':fontcolor=white:"
            f"fontsize={plan['title_font_size']}:x=(w-text_w)/2:"
            "y=(h-text_h)/2[header];"
            f"color=c=0x10151d:s={width}x{footer_h}:r={fps},"
            f"drawtext=text='{plan['brand']}':fontcolor=white@0.9:"
            f"fontsize={plan['brand_font_size']}:x=(w-text_w)/2:"
            "y=(h-text_h)/2[footer];"
            "[header][podcast][footer]vstack=inputs=3[canvas];"
            f"[canvas]{subtitle_filter},format=yuv420p[variant]"
        )
        self._run_ffmpeg(
            [
                ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(podcast_video),
                "-i",
                str(base_short),
                "-filter_complex",
                graph,
                "-map",
                "[variant]",
                "-map",
                "1:a:0",
                *encoder_args,
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                "-shortest",
                *get_color_metadata_args(),
                "-use_editlist",
                "0",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )

    def _finish_background_variant(
        self,
        podcast_video: Path,
        output: Path,
        fps: str,
        suppress_motion: list[tuple[float, float]],
        encoder_args: list[str],
        background: dict,
        caption_path: Path,
    ) -> dict:
        base_path = background["base_path"]
        asset = background["asset"]
        base_signature = audio_packet_signature(base_path, runner=self._run_ffmpeg)
        with staged_render_output(output) as staged:
            if background["variant_id"] == GAMEPLAY_SURROUND_VARIANT_ID:
                self._compose_gameplay_surround_variant(
                    podcast_video,
                    asset["assets"],
                    base_path,
                    caption_path,
                    staged,
                    fps,
                    encoder_args,
                )
            elif background["variant_id"] == SPEAKER_PANELS_VARIANT_ID:
                self._compose_speaker_panels_variant(
                    podcast_video,
                    base_path,
                    caption_path,
                    staged,
                    fps,
                    encoder_args,
                )
            else:
                playback_policy = asset.get("playback_policy", {})
                gameplay = background["variant_id"] in GAMEPLAY_VARIANT_IDS
                self._compose_background_variant(
                    podcast_video,
                    asset["path"],
                    base_path,
                    staged,
                    fps,
                    suppress_motion,
                    encoder_args,
                    (float(asset["playback_start_seconds"]) if gameplay else None),
                    (bool(playback_policy.get("wrap_required")) if gameplay else True),
                )
            media = validate_av_output(staged, background["base_duration"])
            if (media["width"], media["height"]) != (1080, 1920):
                raise RuntimeError("Background variant must be 1080x1920")
            media["audio_loudness"] = require_delivery_loudness(
                measure_loudness(staged, ffmpeg_bin=ffmpeg_executable()),
                delivery_loudness_policy(self.config, "shorts"),
            )
            output_signature = audio_packet_signature(staged, runner=self._run_ffmpeg)
            if output_signature != base_signature:
                raise RuntimeError("Background variant changed canonical audio packets")
            media["audio_copy_verification"] = {
                "status": "pass",
                "input": base_signature,
                "output": output_signature,
            }
            if scan_identity(base_path) != background["base_identity"]:
                raise RuntimeError("Canonical short changed during variant render")
            for media_asset in asset.get("assets", [asset]) if asset else []:
                if (
                    file_content_identity(
                        media_asset["path"], media_asset["content_revision"]
                    )["scan_identity"]
                    != media_asset["scan_identity"]
                ):
                    raise RuntimeError("Background asset changed during variant render")
        return media

    @staticmethod
    def _variant_result(output: Path, record: dict, *, reused: bool) -> dict:
        recorded_asset = record.get("asset")
        return {
            "clip_id": output.stem,
            "variant_id": record.get("variant_id"),
            "asset_id": (
                recorded_asset.get("asset_id")
                if isinstance(recorded_asset, dict)
                else None
            ),
            "asset_ids": [
                item.get("asset_id")
                for item in (
                    recorded_asset.get("assets", [])
                    if isinstance(recorded_asset, dict)
                    else []
                )
                if isinstance(item, dict)
            ],
            "output_path": str(output),
            "reused": reused,
            "render": record,
        }

    def _repair_clip_audio_locked(self, clip_id: str, output: Path) -> dict:
        clips = self.load_json("clips.json").get("clips", [])
        clip = next((item for item in clips if item.get("id") == clip_id), None)
        if clip is None:
            raise KeyError(f"Unknown clip: {clip_id}")
        episode = self.load_json("episode.json")
        self.config = render_config_for_episode(episode, self.config)
        segment_document = current_speaker_segments(
            self.episode_dir, episode, self.config
        )
        segments = segment_document.get("segments", []) if segment_document else []
        if not segments:
            raise ValueError(
                "Current source-clock speaker segments are required for shorts repair"
            )
        audio = generate_audio_mix(self.episode_dir, episode, self.config)
        if not audio or not audio.exists():
            raise RuntimeError("Canonical audio source is required")
        source = self.episode_dir / "source_merged.mp4"
        source_probe = ffprobe(source)
        video_stream = next(
            stream
            for stream in source_probe["streams"]
            if stream["codec_type"] == "video"
        )
        fps = source_fps(video_stream, episode)
        timeline = (
            Timeline.from_edits(
                float(source_probe["format"]["duration"]),
                episode.get("longform_edits", []),
            )
            .slice(float(clip["start_seconds"]), float(clip["end_seconds"]))
            .quantize(fps)
        )
        current = current_short_render(
            self.episode_dir,
            episode,
            self.config,
            audio,
            segments,
            clip,
        )
        if current is None:
            raise RuntimeError(
                "Audio repair requires a current manifest-backed short render"
            )
        policy = delivery_loudness_policy(self.config, "shorts")
        audio_state = loudness_status(
            current.get("output", {}).get("audio_loudness"), policy
        )
        if audio_state["safe"]:
            return self._clip_repair_result(
                clip_id, output, {**current, "reused": True}, repaired=False
            )

        output_bytes = int(current.get("output", {}).get("size_bytes", 0))
        if output_bytes <= 0:
            raise RuntimeError("Current short has no recorded output size")
        require_render_space(
            output.parent, {"output_bytes": output_bytes, "scratch_bytes": 0}
        )
        encoding = get_video_encoding_policy(self.config, "shorts")
        with preserve_reviewed_output(output):
            media = mux_timeline_audio(
                output,
                audio,
                output,
                timeline,
                audio_bitrate=encoding["audio_bitrate"],
                loudness_policy=policy,
                verify_video_copy=True,
                runner=self._run_ffmpeg,
            )
            if "audio_loudness" not in media:
                raise RuntimeError("Repaired short has no verified audio loudness")
            provenance = audio_remaster_provenance(current, media, policy)
            record = record_short_render(
                self.episode_dir,
                clip_id,
                fingerprint=current["fingerprint"],
                timeline=timeline,
                media=media,
                captions=copy.deepcopy(current.get("captions")),
                provenance=provenance,
            )
        return self._clip_repair_result(clip_id, output, record, repaired=True)

    def _clip_repair_result(
        self, clip_id: str, output: Path, record: dict, *, repaired: bool
    ) -> dict:
        caption = str(record.get("captions", {}).get("path", ""))
        return {
            "clip_id": clip_id,
            "output_path": str(output),
            "caption_path": str(self.episode_dir / caption) if caption else None,
            "reused": True,
            "audio_repaired": repaired,
            "video_reencoded": False,
            "render": record,
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
            raise RuntimeError("Canonical audio source is required")

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
        background=None,
        segment_document=None,
    ) -> dict:
        """Render one clip; positional arguments remain compatible with chat actions."""
        speaker_panel_variant = bool(
            background and background.get("variant_id") in SPEAKER_PANEL_VARIANT_IDS
        )
        gameplay_surround = bool(
            background and background.get("variant_id") == GAMEPLAY_SURROUND_VARIANT_ID
        )
        speaker_panels = bool(
            background and background.get("variant_id") == SPEAKER_PANELS_VARIANT_ID
        )
        if gameplay_surround:
            require_gameplay_caption_context_revision(
                background.get("caption_context_revision")
            )
        elif speaker_panels:
            require_speaker_panel_caption_context_revision(
                background.get("caption_context_revision")
            )
        episode = episode or self.load_json("episode.json")
        diarized = diarized or self.load_json("diarized_transcript.json")
        if not audio_mix_path or not Path(audio_mix_path).exists():
            raise RuntimeError("Canonical audio source is required")
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
        style = self._caption_style(background)
        three_person_stack_enabled = self._three_person_stack_enabled(
            episode,
            crop_config,
            src_w,
            src_h,
            for_background=background is not None,
        )
        caption_path = Path(caption_path).with_suffix(".ass")
        caption_path.parent.mkdir(parents=True, exist_ok=True)
        caption_options = {}
        if speaker_panel_variant:
            speaker_placements, fallback_placement = self._speaker_panel_placements(
                src_w,
                src_h,
                crop_config,
                variant_id=background["variant_id"],
            )
            caption_options = {
                "speaker_targets": resolve_caption_speaker_targets(
                    captions, segment_document or {}, crop_config
                ),
                "speaker_placements": speaker_placements,
                "fallback_placement": fallback_placement,
            }
        generate_ass_from_diarized(
            captions,
            0,
            timeline.duration,
            caption_path,
            style,
            **caption_options,
        )

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
                three_person = self._uses_three_person_stack(
                    segment["speaker"], crop_config, three_person_stack_enabled
                )
                segment_style = (
                    CaptionStyle(
                        margin_v=(
                            THREE_PERSON_STACK_CAPTION_MARGIN_V
                            if three_person
                            else BACKGROUND_CAPTION_MARGIN_V
                        )
                    )
                    if background
                    else self._short_caption_style(
                        segment["speaker"], crop_config, three_person_stack_enabled
                    )
                )
                generate_ass_from_diarized(
                    rebase_diarized(captions, segment_timeline),
                    0,
                    segment["duration"],
                    segment_ass,
                    segment_style,
                )
                filters = []
                if lut_filter:
                    filters.append(lut_filter)
                if speaker_panel_variant:
                    crop_filter = (
                        self._get_gameplay_surround_podcast_filter_no_subs(
                            src_w,
                            src_h,
                            crop_config,
                        )
                        if gameplay_surround
                        else self._get_speaker_panels_filter_no_subs(
                            src_w,
                            src_h,
                            crop_config,
                        )
                    )
                elif background:
                    crop_filter = self._get_background_crop_filter_no_subs(
                        segment["speaker"],
                        src_w,
                        src_h,
                        crop_config,
                        three_person_stack=three_person,
                    )
                else:
                    crop_filter = self._get_short_crop_filter_no_subs(
                        segment["speaker"],
                        src_w,
                        src_h,
                        crop_config,
                        three_person_stack=three_person_stack_enabled,
                    )
                filters.append(crop_filter)
                if not speaker_panel_variant and "Dialogue:" in segment_ass.read_text():
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
            media = (
                self._finish_background_variant(
                    video_only,
                    Path(output),
                    fps,
                    [
                        (float(segment["start"]), float(segment["end"]))
                        for segment in render_segments
                        if self._uses_three_person_stack(
                            segment["speaker"],
                            crop_config,
                            three_person_stack_enabled,
                        )
                    ],
                    encoder_args,
                    background,
                    caption_path,
                )
                if background
                else mux_timeline_audio(
                    video_only,
                    Path(audio_mix_path),
                    Path(output),
                    timeline,
                    audio_bitrate=audio_bitrate,
                    loudness_policy=delivery_loudness_policy(self.config, "shorts"),
                    runner=self._run_ffmpeg,
                )
            )
            if "audio_loudness" not in media:
                raise RuntimeError("Rendered short has no verified audio loudness")

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
        if background:
            return record_background_variant(
                self.episode_dir,
                str(clip["id"]),
                fingerprint=fingerprint,
                timeline=timeline,
                media=media,
                base_record=background["base_record"],
                base_identity=background["base_identity"],
                asset=background["asset"],
                encoding=encoding,
                captions={
                    "path": str(caption_path.relative_to(self.episode_dir)),
                    "format": "ass",
                    "burned_in": True,
                    **(
                        {"placement_policy": (GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION)}
                        if speaker_panel_variant
                        else {}
                    ),
                    **(
                        {"context_revision": background["caption_context_revision"]}
                        if speaker_panel_variant
                        else {}
                    ),
                },
                variant_id=background["variant_id"],
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
                "overlap_policy": (
                    "hold_neighbor_up_to_threshold_else_three_person_stack"
                    if three_person_stack_enabled
                    and len(episode.get("crop_config", {}).get("speakers", [])) == 3
                    else "hold_neighbor_up_to_threshold_else_two_person_stack_or_fit_wide"
                ),
                "overlap_hold_seconds": self.config.get("processing", {}).get(
                    "shorts_hold_wide_seconds", 3.0
                ),
                "encoding": {**encoding, "encoder": encoder_args[1]},
            },
        )

    def _apply_overlap_policy(self, segments: list[dict]) -> list[dict]:
        """Hold a nearby speaker briefly; retain long overlap states for layout."""
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

    @staticmethod
    def _uses_three_person_stack(speaker, crop_config, enabled):
        return (
            enabled
            and speaker in {"BOTH", "NONE"}
            and len(crop_config.get("speakers", [])) == 3
        )

    def _three_person_stack_enabled(
        self, episode, crop_config, src_w, src_h, *, for_background
    ):
        if not for_background:
            return bool(episode.get("shorts_three_person_stack", False))
        if len(crop_config.get("speakers", [])) != 3:
            return False
        try:
            for index in range(3):
                self._get_background_panel_region(
                    f"speaker_{index}", src_w, src_h, crop_config
                )
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def _short_caption_style(self, speaker, crop_config, three_person_stack_enabled):
        if self._uses_three_person_stack(
            speaker, crop_config, three_person_stack_enabled
        ):
            return CaptionStyle(margin_v=THREE_PERSON_STACK_CAPTION_MARGIN_V)
        return CaptionStyle()

    @staticmethod
    def _caption_style(background: dict | None) -> CaptionStyle:
        if background and background.get("variant_id") == GAMEPLAY_SURROUND_VARIANT_ID:
            return CaptionStyle(
                font_size=52,
                margin_l=300,
                margin_r=300,
                margin_v=BACKGROUND_CAPTION_MARGIN_V,
            )
        if background and background.get("variant_id") == SPEAKER_PANELS_VARIANT_ID:
            return CaptionStyle(font_size=64, margin_l=60, margin_r=60)
        if background:
            return CaptionStyle(margin_v=BACKGROUND_CAPTION_MARGIN_V)
        return CaptionStyle()

    def _speaker_panel_rows(
        self,
        src_w: int,
        src_h: int,
        crop_config: dict,
        panel_width: int,
        panel_height: int,
    ) -> list[tuple[int, int, int | None, int | None, int | None, int | None]]:
        """Return speaker rows in the compositor's physical left-to-right order."""
        speakers = crop_config.get("speakers", [])
        if len(speakers) > 3:
            raise ValueError(
                "Speaker panels need a reviewed layout for more than 3 speakers"
            )
        count = len(speakers)
        if not count:
            return []
        base_height = (panel_height // count) // 2 * 2
        target_heights = [base_height] * count
        target_heights[-1] += panel_height - sum(target_heights)
        if count == 1:
            return [(0, target_heights[0], None, None, None, None)]

        anchors = []
        for index in range(count):
            center_x, center_y, zoom, _ = resolve_speaker(
                f"speaker_{index}", src_w, src_h, crop_config, for_shorts=False
            )
            anchors.append((center_x, index, center_y, zoom))

        rows = []
        for row, (center_x, index, center_y, zoom) in enumerate(sorted(anchors)):
            target_h = target_heights[row]
            _, _, anchor_w, _ = compute_crop(
                src_w, src_h, center_x, center_y, zoom, "speaker"
            )
            target_ratio = panel_width / target_h
            viewport_w = min(float(src_w), float(anchor_w))
            viewport_h = viewport_w / target_ratio
            if viewport_h > src_h:
                viewport_h = float(src_h)
                viewport_w = viewport_h * target_ratio
            viewport_w = max(2, int(viewport_w) // 2 * 2)
            viewport_h = max(2, int(viewport_h) // 2 * 2)
            x = max(0, min(round(center_x - viewport_w / 2), src_w - viewport_w))
            headroom = viewport_h * BACKGROUND_PANEL_HEADROOM_FRACTION
            y = max(
                0,
                min(round(center_y - viewport_h / 2 - headroom), src_h - viewport_h),
            )
            rows.append(
                (
                    index,
                    target_h,
                    viewport_w,
                    viewport_h,
                    x // 2 * 2,
                    y // 2 * 2,
                )
            )
        return rows

    def _gameplay_surround_panel_rows(
        self, src_w: int, src_h: int, crop_config: dict
    ) -> list[tuple[int, int, int | None, int | None, int | None, int | None]]:
        plan = GAMEPLAY_SURROUND_RENDER_PLAN
        return self._speaker_panel_rows(
            src_w,
            src_h,
            crop_config,
            plan["podcast_width"],
            plan["upper_height"] - plan["podcast_header_height"],
        )

    def _gameplay_surround_caption_placements(
        self, src_w: int, src_h: int, crop_config: dict
    ) -> tuple[dict[str, CaptionPlacement], CaptionPlacement]:
        return self._speaker_panel_placements(
            src_w,
            src_h,
            crop_config,
            variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
        )

    def _speaker_panel_placements(
        self,
        src_w: int,
        src_h: int,
        crop_config: dict,
        *,
        variant_id: str,
    ) -> tuple[dict[str, CaptionPlacement], CaptionPlacement]:
        """Place known speakers in their row and unknown speech in the header."""
        gameplay = variant_id == GAMEPLAY_SURROUND_VARIANT_ID
        plan = GAMEPLAY_SURROUND_RENDER_PLAN if gameplay else SPEAKER_PANELS_RENDER_PLAN
        width = plan["podcast_width"] if gameplay else plan["panel_width"]
        height = (
            plan["upper_height"] - plan["podcast_header_height"]
            if gameplay
            else plan["panels_height"]
        )
        header_h = plan["podcast_header_height"] if gameplay else plan["header_height"]
        left = plan["left_width"] if gameplay else 0
        x = left + width // 2
        cursor = header_h
        placements = {}
        rows = self._speaker_panel_rows(src_w, src_h, crop_config, width, height)
        for index, row_height, *_ in rows:
            visible_top = cursor
            visible_height = row_height
            if not gameplay and len(rows) == 1:
                scale = min(width / src_w, row_height / src_h)
                visible_height = min(row_height, max(2, int(src_h * scale)))
                visible_top += (row_height - visible_height) // 2
            inset = min(136, visible_height // 4)
            placements[f"speaker_{index}"] = CaptionPlacement(
                x=x,
                y=visible_top + visible_height - inset,
            )
            cursor += row_height
        fallback = CaptionPlacement(
            x=x,
            y=header_h // 2,
            alignment=5,
            font_size=24 if gameplay else 28,
            background_box=(left, 0, width, header_h),
        )
        return placements, fallback

    def _get_gameplay_surround_podcast_filter_no_subs(
        self, src_w: int, src_h: int, crop_config: dict
    ) -> str:
        plan = GAMEPLAY_SURROUND_RENDER_PLAN
        return self._speaker_panels_filter_no_subs(
            src_w,
            src_h,
            crop_config,
            plan["podcast_width"],
            plan["upper_height"] - plan["podcast_header_height"],
            plan["panel_border"],
        )

    def _get_speaker_panels_filter_no_subs(
        self, src_w: int, src_h: int, crop_config: dict
    ) -> str:
        plan = SPEAKER_PANELS_RENDER_PLAN
        return self._speaker_panels_filter_no_subs(
            src_w,
            src_h,
            crop_config,
            plan["panel_width"],
            plan["panels_height"],
            plan["panel_border"],
        )

    def _speaker_panels_filter_no_subs(
        self,
        src_w: int,
        src_h: int,
        crop_config: dict,
        panel_width: int,
        panel_height: int,
        panel_border: int,
    ) -> str:
        """Show every configured speaker in a fixed-width vertical panel stack."""
        speakers = crop_config.get("speakers", [])
        if not speakers:
            return (
                f"scale={panel_width}:{panel_height}:"
                "force_original_aspect_ratio=decrease:"
                "flags=lanczos+accurate_rnd+full_chroma_int:sws_dither=ed:param0=5,"
                f"pad={panel_width}:{panel_height}:(ow-iw)/2:(oh-ih)/2:black,"
                "format=yuv420p"
            )

        count = len(speakers)
        if count == 1:
            return (
                f"scale={panel_width}:{panel_height}:"
                "force_original_aspect_ratio=decrease:"
                "flags=lanczos+accurate_rnd+full_chroma_int:sws_dither=ed:param0=5,"
                f"pad={panel_width}:{panel_height}:(ow-iw)/2:(oh-ih)/2:black,"
                "format=yuv420p"
            )
        rows = self._speaker_panel_rows(
            src_w, src_h, crop_config, panel_width, panel_height
        )
        target_heights = [row[1] for row in rows]
        panels = []
        labels = []
        for row, (index, target_h, width, height, x, y) in enumerate(rows):
            panels.append(
                f"[surround{index}]crop={width}:{height}:{x}:{y},"
                f"{get_scale_filter(panel_width, target_h)},format=yuv420p[row{row}]"
            )
            labels.append(f"[row{row}]")
        separators = []
        cursor = 0
        for height in target_heights[:-1]:
            cursor += height
            separators.append(
                f"drawbox=x=0:y={cursor - panel_border // 2}:w={panel_width}:"
                f"h={panel_border}:"
                "color=black@0.85:t=fill"
            )
        chain = (
            f"split={count}"
            + "".join(f"[surround{index}]" for index in range(count))
            + ";"
            + ";".join(panels)
            + ";"
            + "".join(labels)
            + f"vstack=inputs={count}"
        )
        if separators:
            chain += "," + ",".join(separators)
        chain += ",format=yuv420p"
        polish = get_video_polish_filters(self.config)
        return f"{chain},{polish}" if polish else chain

    def _get_background_crop_filter_no_subs(
        self,
        speaker,
        src_w,
        src_h,
        crop_config,
        *,
        three_person_stack=False,
    ):
        if three_person_stack:
            return self._three_person_stack_filter(
                src_w, src_h, crop_config, background_anchors=True
            )
        if speaker == "BOTH" and len(crop_config.get("speakers", [])) == 2:
            panels = []
            for index, label in enumerate(("top", "bottom")):
                _, panel_w, panel_h, x, y = self._get_background_panel_region(
                    f"speaker_{index}", src_w, src_h, crop_config
                )
                panels.append(
                    f"[motion{index}]crop={panel_w}:{panel_h}:{x}:{y},"
                    f"{get_scale_filter(1080, 640)},format=yuv420p[{label}]"
                )
            chain = (
                "split=2[motion0][motion1];"
                f"{panels[0]};{panels[1]};"
                "[top][bottom]vstack=inputs=2,"
                "drawbox=x=0:y=637:w=1080:h=6:color=black@0.8:t=fill,"
                "pad=1080:1920:0:0:black,format=yuv420p"
            )
        elif speaker in {"BOTH", "NONE"}:
            chain = (
                "scale=1080:1280:force_original_aspect_ratio=decrease:"
                "flags=lanczos+accurate_rnd+full_chroma_int:sws_dither=ed:param0=5,"
                "pad=1080:1280:(ow-iw)/2:(oh-ih)/2:black,"
                "pad=1080:1920:0:0:black,format=yuv420p"
            )
        else:
            _, crop_h, _, _ = self._get_short_crop_region(
                speaker, src_w, src_h, crop_config
            )
            center_x, center_y, _, _ = resolve_speaker(
                speaker, src_w, src_h, crop_config, for_shorts=True
            )
            viewport_w = min(src_w, crop_h * 27 / 32)
            viewport_h = min(src_h, viewport_w * 32 / 27)
            viewport_w = min(src_w, viewport_h * 27 / 32)
            viewport_w = max(2, int(viewport_w) // 2 * 2)
            viewport_h = max(2, int(viewport_h) // 2 * 2)
            x = max(0, min(round(center_x - viewport_w / 2), src_w - viewport_w))
            y = max(0, min(round(center_y - viewport_h / 2), src_h - viewport_h))
            chain = (
                f"crop={viewport_w}:{viewport_h}:{x // 2 * 2}:{y // 2 * 2},"
                f"{get_scale_filter(1080, 1280)},"
                "pad=1080:1920:0:0:black,format=yuv420p"
            )
        polish = get_video_polish_filters(self.config)
        return f"{chain},{polish}" if polish else chain

    def _get_background_panel_region(self, speaker, src_w, src_h, crop_config):
        center_x, center_y, zoom, _ = resolve_speaker(
            speaker, src_w, src_h, crop_config, for_shorts=False
        )
        _, _, panel_w, _ = compute_crop(
            src_w, src_h, center_x, center_y, zoom, "speaker"
        )
        panel_h = panel_w * 16 / 27
        if panel_h > src_h:
            panel_h = src_h
            panel_w = panel_h * 27 / 16
        panel_w = max(2, int(panel_w) // 2 * 2)
        panel_h = max(2, int(panel_h) // 2 * 2)
        x = max(0, min(round(center_x - panel_w / 2), src_w - panel_w))
        panel_top = (
            center_y - panel_h / 2 - panel_h * BACKGROUND_PANEL_HEADROOM_FRACTION
        )
        y = max(0, min(round(panel_top), src_h - panel_h))
        return center_x, panel_w, panel_h, x // 2 * 2, y // 2 * 2

    def _three_person_stack_filter(
        self, src_w, src_h, crop_config, *, background_anchors=False
    ):
        regions = []
        for index in range(3):
            if background_anchors:
                center_x, panel_w, panel_h, x, y = self._get_background_panel_region(
                    f"speaker_{index}", src_w, src_h, crop_config
                )
            else:
                portrait_w, portrait_h, portrait_x, portrait_y = (
                    self._get_short_crop_region(
                        f"speaker_{index}", src_w, src_h, crop_config
                    )
                )
                panel_w = min(src_w, portrait_w)
                panel_h = panel_w * 16 / 27
                if panel_h > src_h:
                    panel_h = src_h
                    panel_w = panel_h * 27 / 16
                panel_w = max(2, int(panel_w) // 2 * 2)
                panel_h = max(2, int(panel_h) // 2 * 2)
                center_x = portrait_x + portrait_w / 2
                x = max(0, min(round(center_x - panel_w / 2), src_w - panel_w))
                upper_body_y = portrait_y + max(0, portrait_h - panel_h) / 6
                y = max(0, min(round(upper_body_y), src_h - panel_h))
                x = x // 2 * 2
                y = y // 2 * 2
            regions.append((center_x, index, panel_w, panel_h, x, y))

        panels = []
        for row, (_, index, panel_w, panel_h, x, y) in enumerate(sorted(regions)):
            panels.append(
                f"[stack{index}]crop={panel_w}:{panel_h}:{x}:{y},"
                f"{get_scale_filter(1080, 640)},format=yuv420p[row{row}]"
            )
        chain = (
            "split=3[stack0][stack1][stack2];"
            f"{';'.join(panels)};"
            "[row0][row1][row2]vstack=inputs=3,"
            "drawbox=x=0:y=637:w=1080:h=6:color=black@0.8:t=fill,"
            "drawbox=x=0:y=1277:w=1080:h=6:color=black@0.8:t=fill,"
            "format=yuv420p"
        )
        polish = get_video_polish_filters(self.config)
        return f"{chain},{polish}" if polish else chain

    def _get_short_crop_filter_no_subs(
        self,
        speaker,
        src_w,
        src_h,
        crop_config,
        *,
        three_person_stack=False,
    ):
        if speaker == "BOTH" and len(crop_config.get("speakers", [])) == 2:
            panels = []
            for index, output_label in enumerate(("top", "bottom")):
                portrait_w, portrait_h, portrait_x, portrait_y = (
                    self._get_short_crop_region(
                        f"speaker_{index}", src_w, src_h, crop_config
                    )
                )
                panel_w = min(src_w, portrait_w)
                panel_h = panel_w * 8 / 9
                if panel_h > src_h:
                    panel_h = src_h
                    panel_w = panel_h * 9 / 8
                panel_w = max(2, int(panel_w) // 2 * 2)
                panel_h = max(2, int(panel_h) // 2 * 2)
                cx = portrait_x + portrait_w / 2
                x = max(0, min(round(cx - panel_w / 2), src_w - panel_w))
                upper_body_y = portrait_y + max(0, portrait_h - panel_h) / 4
                y = max(0, min(round(upper_body_y), src_h - panel_h))
                x = x // 2 * 2
                y = y // 2 * 2
                panels.append(
                    f"[stack{index}]crop={panel_w}:{panel_h}:{x}:{y},"
                    f"{get_scale_filter(1080, 960)},format=yuv420p[{output_label}]"
                )
            chain = (
                "split=2[stack0][stack1];"
                f"{panels[0]};{panels[1]};"
                "[top][bottom]vstack=inputs=2,"
                "drawbox=x=0:y=957:w=1080:h=6:color=black@0.8:t=fill,"
                "format=yuv420p"
            )
            polish = get_video_polish_filters(self.config)
            return f"{chain},{polish}" if polish else chain
        if self._uses_three_person_stack(speaker, crop_config, three_person_stack):
            return self._three_person_stack_filter(src_w, src_h, crop_config)
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

    def _run_ffmpeg(self, cmd, **kwargs):
        return timed_ffmpeg(cmd, agent_logger=self.logger, **kwargs)


def render_single_clip(episode_dir: Path, config: dict, clip_id: str) -> dict:
    """Public API adapter for an atomic, manifest-backed single-clip render."""
    return ShortsRenderAgent(episode_dir, config).render_clip(clip_id)


def render_single_clip_variant(
    episode_dir: Path,
    config: dict,
    clip_id: str,
    variant_id: str,
    asset_id: str | None = None,
) -> dict:
    """Render one supported optional short without changing the canonical short."""
    require_background_variant(variant_id)
    selected_asset_id = (
        default_background_asset_id(variant_id) if asset_id is None else asset_id
    )
    require_background_variant_asset(variant_id, selected_asset_id)
    return ShortsRenderAgent(episode_dir, config).render_background_variant(
        clip_id, selected_asset_id, variant_id
    )


def repair_single_clip_audio(episode_dir: Path, config: dict, clip_id: str) -> dict:
    """Repair one current short's audio while preserving encoded video packets."""
    return ShortsRenderAgent(episode_dir, config).repair_clip_audio(clip_id)
