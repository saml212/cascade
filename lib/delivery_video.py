"""Compact, continuous-shot video export for local delivery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from lib.atomic_write import atomic_write_json
from lib.crop import compute_crop
from lib.encoding import get_color_metadata_args, get_lut_filter, has_videotoolbox
from lib.ffprobe import probe
from lib.timeline import Timeline, build_keep_intervals, quantize_timestamp

ProgressCallback = Callable[[float, str], None]
RENDER_MANIFEST_NAME = "render_manifest.json"
_manifest_lock = threading.Lock()


@lru_cache(maxsize=1)
def ffmpeg_executable() -> str:
    """Prefer the libass-enabled ffmpeg build used by production renders."""
    configured = os.getenv("CASCADE_FFMPEG")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
        Path(shutil.which("ffmpeg-full") or ""),
        Path(shutil.which("ffmpeg") or ""),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("ffmpeg is required for video rendering")


def _video_filter(
    src_w: int, src_h: int, crop_config: dict, config: dict
) -> tuple[str, int, int]:
    zoom = float(crop_config.get("wide_zoom", crop_config.get("zoom", 1.0)))
    cx = int(crop_config.get("wide_center_x", src_w // 2))
    cy = int(crop_config.get("wide_center_y", src_h // 2))
    if zoom > 1.0:
        x, y, crop_w, crop_h = compute_crop(src_w, src_h, cx, cy, zoom, "wide")
    else:
        crop_w = min(src_w, int(src_h * 16 / 9))
        crop_h = min(src_h, int(src_w * 9 / 16))
        x = max(0, (src_w - crop_w) // 2)
        y = max(0, (src_h - crop_h) // 2)
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    out_w = min(1920, src_w)
    out_h = min(1080, src_h)
    out_w -= out_w % 2
    out_h -= out_h % 2
    filters = [
        f"crop={crop_w}:{crop_h}:{x}:{y}",
        f"scale={out_w}:{out_h}:flags=lanczos",
    ]
    lut = get_lut_filter(config)
    if lut:
        filters.append(lut)
    filters.append("format=yuv420p")
    return ",".join(filters), out_w, out_h


def _filter_graph(intervals: list[tuple[float, float]], video_filter: str) -> str:
    chains = []
    labels = []
    if len(intervals) > 1:
        video_inputs = [f"[vin{i}]" for i in range(len(intervals))]
        audio_inputs = [f"[ain{i}]" for i in range(len(intervals))]
        chains.append(f"[0:v]split={len(intervals)}{''.join(video_inputs)}")
        chains.append(f"[1:a]asplit={len(intervals)}{''.join(audio_inputs)}")
    else:
        video_inputs = ["[0:v]"]
        audio_inputs = ["[1:a]"]
    for index, (start, end) in enumerate(intervals):
        chains.append(
            f"{video_inputs[index]}trim=start={start}:end={end},"
            f"setpts=PTS-STARTPTS,{video_filter}[v{index}]"
        )
        chains.append(
            f"{audio_inputs[index]}atrim=start={start}:end={end},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        labels.append(f"[v{index}][a{index}]")
    chains.append(f"{''.join(labels)}concat=n={len(intervals)}:v=1:a=1[v][a]")
    return ";".join(chains)


def build_render_segments(
    timeline: Timeline,
    speaker_segments: list[dict],
    *,
    default_speaker: str = "BOTH",
    minimum_duration: float = 0.05,
    frame_rate: str | float | Fraction | None = None,
) -> list[dict]:
    """Cover a timeline with source-aligned speaker crop decisions.

    Gaps in acoustic speaker detection use the wide crop. Overlapping records
    are resolved chronologically so every source instant is rendered once.
    Returned ``start``/``end`` values use the output clock while
    ``source_start``/``source_end`` remain suitable for ffmpeg input trims.
    """
    candidates = sorted(
        (
            {
                "source_start": (
                    quantize_timestamp(float(segment["start"]), frame_rate)
                    if frame_rate is not None
                    else float(segment["start"])
                ),
                "source_end": (
                    quantize_timestamp(float(segment["end"]), frame_rate)
                    if frame_rate is not None
                    else float(segment["end"])
                ),
                "speaker": segment.get("speaker", default_speaker),
            }
            for segment in speaker_segments
            if float(segment["end"]) > float(segment["start"])
        ),
        key=lambda segment: (segment["source_start"], segment["source_end"]),
    )
    result = []
    for span in timeline.spans:
        cursor = span.source_start
        for segment in candidates:
            source_start = max(cursor, span.source_start, segment["source_start"])
            source_end = min(span.source_end, segment["source_end"])
            if source_end <= source_start:
                continue
            if source_start > cursor:
                result.append(
                    _render_segment_record(span, cursor, source_start, default_speaker)
                )
            result.append(
                _render_segment_record(
                    span, source_start, source_end, segment["speaker"]
                )
            )
            cursor = source_end
            if cursor >= span.source_end:
                break
        if cursor < span.source_end:
            result.append(
                _render_segment_record(span, cursor, span.source_end, default_speaker)
            )

    for index in range(len(result) - 1, -1, -1):
        segment = result[index]
        if segment["duration"] >= minimum_duration:
            continue
        if (
            index > 0
            and abs(segment["source_start"] - result[index - 1]["source_end"]) < 1e-6
        ):
            result[index - 1]["end"] = segment["end"]
            result[index - 1]["source_end"] = segment["source_end"]
            result[index - 1]["duration"] = (
                result[index - 1]["end"] - result[index - 1]["start"]
            )
            result.pop(index)
        elif (
            index + 1 < len(result)
            and abs(segment["source_end"] - result[index + 1]["source_start"]) < 1e-6
        ):
            result[index + 1]["start"] = segment["start"]
            result[index + 1]["source_start"] = segment["source_start"]
            result[index + 1]["duration"] = (
                result[index + 1]["end"] - result[index + 1]["start"]
            )
    merged = []
    for segment in result:
        if (
            merged
            and segment["speaker"] == merged[-1]["speaker"]
            and abs(segment["source_start"] - merged[-1]["source_end"]) < 1e-6
        ):
            merged[-1]["end"] = segment["end"]
            merged[-1]["source_end"] = segment["source_end"]
            merged[-1]["duration"] = merged[-1]["end"] - merged[-1]["start"]
        else:
            merged.append(segment)
    return merged


def _render_segment_record(
    span, source_start: float, source_end: float, speaker: str
) -> dict:
    start = span.output_start + source_start - span.source_start
    end = span.output_start + source_end - span.source_start
    return {
        "start": start,
        "end": end,
        "duration": end - start,
        "source_start": source_start,
        "source_end": source_end,
        "speaker": speaker,
    }


def _audio_filter_graph(
    intervals: list[tuple[float, float]], input_index: int = 1
) -> str:
    """Build a sample-accurate audio edit graph for retained source ranges."""
    if not intervals:
        raise ValueError("At least one audio interval is required")
    if len(intervals) == 1:
        start, end = intervals[0]
        return f"[{input_index}:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a]"

    inputs = [f"[ain{index}]" for index in range(len(intervals))]
    chains = [f"[{input_index}:a]asplit={len(intervals)}{''.join(inputs)}"]
    labels = []
    for index, (start, end) in enumerate(intervals):
        chains.append(
            f"{inputs[index]}atrim=start={start}:end={end},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        labels.append(f"[a{index}]")
    chains.append(f"{''.join(labels)}concat=n={len(intervals)}:v=0:a=1[a]")
    return ";".join(chains)


def source_fps(video_stream: dict, episode: dict) -> str:
    """Return the exact rational source rate reported by ffprobe."""
    numerator, _, denominator = video_stream.get("r_frame_rate", "30/1").partition("/")
    try:
        rate = Fraction(int(numerator), int(denominator or 1))
    except (ValueError, ZeroDivisionError):
        configured = episode.get("source_properties", {}).get("fps", 30)
        rate = Fraction(str(configured)).limit_denominator(1_000_000)
    if rate <= 0:
        raise ValueError("Source frame rate must be positive")
    return f"{rate.numerator}/{rate.denominator}"


def render_video_segment(
    source: Path,
    output: Path,
    *,
    source_start: float,
    source_end: float,
    video_filter: str,
    encoder_args: list[str],
    fps: int | str,
    runner: Callable = subprocess.run,
) -> None:
    """Encode one frame-accurate, video-only source interval."""
    coarse_seek = max(0.0, source_start - 5.0)
    trim_start = source_start - coarse_seek
    trim_end = source_end - coarse_seek
    rate = Fraction(str(fps))
    gop = max(1, round(float(rate)))
    cmd = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(coarse_seek),
        "-i",
        str(source),
        "-an",
        "-vf",
        f"trim=start={trim_start}:end={trim_end},setpts=PTS-STARTPTS,{video_filter}",
        *encoder_args,
        *get_color_metadata_args(),
        "-r",
        str(fps),
        "-g",
        str(gop),
        "-bf",
        "0",
        "-fps_mode",
        "cfr",
        "-video_track_timescale",
        str(rate.numerator),
        "-use_editlist",
        "0",
        "-movflags",
        "+faststart",
        str(output),
    ]
    runner(cmd, capture_output=True, text=True, check=True)
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError(f"Video render produced an empty file: {output}")


def concat_video_segments(
    segment_paths: list[Path], output: Path, *, runner: Callable = subprocess.run
) -> None:
    """Join identically encoded video-only segments without another encode."""
    if not segment_paths:
        raise ValueError("At least one video segment is required")
    concat_list = output.with_suffix(".concat.txt")
    lines = []
    for path in segment_paths:
        safe_path = str(path).replace("'", "'\\''")
        lines.append(f"file '{safe_path}'")
    concat_list.write_text("\n".join(lines) + "\n")
    try:
        runner(
            [
                ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        concat_list.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError(f"Video concat produced an empty file: {output}")


def mux_timeline_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    timeline: Timeline,
    *,
    audio_bitrate: str = "192k",
    runner: Callable = subprocess.run,
) -> dict:
    """Mux canonical audio using the exact source intervals used by video."""
    audio_duration = float(probe(audio_path)["format"]["duration"])
    required_end = max(span.source_end for span in timeline.spans)
    if audio_duration + 0.1 < required_end:
        raise RuntimeError(
            f"Canonical audio ends at {audio_duration:.3f}s but the selected video "
            f"requires audio through {required_end:.3f}s"
        )

    temp = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
    temp.unlink(missing_ok=True)
    try:
        runner(
            [
                ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-i",
                str(audio_path),
                "-filter_complex",
                _audio_filter_graph(list(timeline.keep_intervals)),
                "-map",
                "0:v",
                "-map",
                "[a]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-ar",
                "48000",
                "-shortest",
                "-use_editlist",
                "0",
                "-movflags",
                "+faststart",
                str(temp),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        media = validate_av_output(temp, timeline.duration)
        if output_path.name == "upload_video.mp4":
            _preserve_previous_wide_render(output_path)
        os.replace(temp, output_path)
        return media
    finally:
        temp.unlink(missing_ok=True)


def validate_av_output(path: Path, expected_duration: float) -> dict:
    """Validate that a render has playable H.264 video and AAC audio in sync."""
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Rendered media is empty: {path}")
    result = probe(path)
    streams = result["streams"]
    video = next(
        (stream for stream in streams if stream["codec_type"] == "video"), None
    )
    audio = next(
        (stream for stream in streams if stream["codec_type"] == "audio"), None
    )
    if video is None or audio is None:
        raise RuntimeError("Rendered media must contain video and audio")
    if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
        raise RuntimeError("Rendered media codec validation failed")

    container_duration = float(result["format"]["duration"])
    video_duration = float(video.get("duration", container_duration))
    audio_duration = float(audio.get("duration", container_duration))
    tolerance = 0.1
    if abs(container_duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"Rendered duration is {container_duration:.3f}s; "
            f"expected {expected_duration:.3f}s"
        )
    if abs(video_duration - audio_duration) > tolerance:
        raise RuntimeError(
            f"Audio/video duration mismatch: {audio_duration:.3f}s audio, "
            f"{video_duration:.3f}s video"
        )
    return {
        "duration_seconds": round(container_duration, 3),
        "audio_duration_seconds": round(audio_duration, 3),
        "video_duration_seconds": round(video_duration, 3),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "video_codec": "h264",
        "audio_codec": "aac",
    }


def _preserve_previous_wide_render(output_path: Path) -> None:
    """Keep the pre-speaker-cut delivery artifact through an atomic replacement."""
    if not output_path.exists():
        return
    prior_mode = (
        read_render_manifest(output_path.parent).get("longform", {}).get("render_mode")
    )
    if prior_mode == "speaker_cut":
        return
    backup = output_path.with_name("upload_video.wide.mp4")
    if not backup.exists():
        os.link(output_path, backup)


def render_fingerprint(paths: list[Path], state: dict) -> str:
    """Fingerprint media identities and every source-clock render decision."""
    files = []
    for path in paths:
        stat = path.stat()
        files.append(
            {
                "path": str(path.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    payload = json.dumps(
        {"files": files, "state": state}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def longform_render_fingerprint(
    episode_dir: Path,
    episode: dict,
    config: dict,
    audio_path: Path,
    segments: list[dict],
    *,
    render_mode: str = "speaker_cut",
) -> str:
    """Fingerprint every input that can change the canonical longform pixels."""
    transcript_path = episode_dir / "diarized_transcript.json"
    paths = [episode_dir / "source_merged.mp4", audio_path]
    if transcript_path.exists():
        paths.append(transcript_path)
    processing = config.get("processing", {})
    state = {
        "clock": "source",
        "render_mode": render_mode,
        "edits": episode.get("longform_edits", []),
        "crop_config": episode.get("crop_config", {}),
        "source_properties": episode.get("source_properties", {}),
        "segments": segments,
        "processing": {
            key: processing.get(key)
            for key in (
                "audio_bitrate",
                "encode_preset",
                "lut_interpolation",
                "lut_path",
                "video_contrast",
                "video_crf",
                "video_denoise",
                "video_gamma",
                "video_polish",
                "video_saturation",
                "video_sharpen",
                "video_sharpen_strength",
                "videotoolbox_quality",
            )
        },
    }
    return render_fingerprint(paths, state)


def short_render_fingerprint(
    episode_dir: Path,
    episode: dict,
    config: dict,
    audio_path: Path,
    segments: list[dict],
    clip: dict,
) -> str:
    """Fingerprint a source-clock clip and its shared render inputs."""
    processing = config.get("processing", {})
    state = {
        "clock": "source",
        "render_mode": "speaker_cut_short",
        "edits": episode.get("longform_edits", []),
        "crop_config": episode.get("crop_config", {}),
        "source_properties": episode.get("source_properties", {}),
        "segments": segments,
        "clip_bounds": {
            "start_seconds": clip.get("start_seconds"),
            "end_seconds": clip.get("end_seconds"),
        },
        "processing": {
            key: processing.get(key)
            for key in (
                "encode_preset",
                "lut_interpolation",
                "lut_path",
                "shorts_audio_bitrate",
                "shorts_crf",
                "video_contrast",
                "video_denoise",
                "video_gamma",
                "video_polish",
                "video_saturation",
                "video_sharpen",
                "video_sharpen_strength",
                "videotoolbox_quality",
            )
        },
    }
    transcript_path = episode_dir / "diarized_transcript.json"
    paths = [episode_dir / "source_merged.mp4", audio_path]
    if transcript_path.exists():
        paths.append(transcript_path)
    return render_fingerprint(paths, state)


def read_render_manifest(episode_dir: Path) -> dict:
    """Read the shared render manifest, returning an empty schema if absent."""
    path = episode_dir / RENDER_MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        manifest = {}
    if manifest.get("version") != 1:
        return {"version": 1, "clock": "source", "shorts": {}}
    manifest.setdefault("clock", "source")
    manifest.setdefault("shorts", {})
    return manifest


def record_longform_render(
    episode_dir: Path,
    *,
    fingerprint: str,
    render_mode: str,
    timeline: Timeline,
    media: dict,
) -> dict:
    """Record a validated canonical longform render for API and release gates."""
    output = episode_dir / "upload_video.mp4"
    stat = output.stat()
    record = {
        "path": output.name,
        "render_mode": render_mode,
        "fingerprint": fingerprint,
        "keep_intervals": [list(interval) for interval in timeline.keep_intervals],
        "output_duration_seconds": round(timeline.duration, 3),
        "output": {
            **media,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    with _manifest_lock:
        manifest = read_render_manifest(episode_dir)
        manifest["longform"] = record
        atomic_write_json(episode_dir / RENDER_MANIFEST_NAME, manifest)
    return record


def record_short_render(
    episode_dir: Path,
    clip_id: str,
    *,
    fingerprint: str,
    timeline: Timeline,
    media: dict,
) -> dict:
    """Record one validated short without changing other manifest entries."""
    output = episode_dir / "shorts" / f"{clip_id}.mp4"
    stat = output.stat()
    record = {
        "path": str(output.relative_to(episode_dir)),
        "render_mode": "speaker_cut_short",
        "fingerprint": fingerprint,
        "clip_source_intervals": [
            list(interval) for interval in timeline.keep_intervals
        ],
        "output_duration_seconds": round(timeline.duration, 3),
        "output": {
            **media,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    with _manifest_lock:
        manifest = read_render_manifest(episode_dir)
        manifest["shorts"][clip_id] = record
        atomic_write_json(episode_dir / RENDER_MANIFEST_NAME, manifest)
    return record


def current_longform_render(
    episode_dir: Path,
    episode: dict,
    config: dict,
    audio_path: Path,
    segments: list[dict],
) -> dict | None:
    """Return the speaker-cut manifest record only when inputs and output match."""
    output = episode_dir / "upload_video.mp4"
    if not output.exists():
        return None
    record = read_render_manifest(episode_dir).get("longform", {})
    if record.get("render_mode") != "speaker_cut":
        return None
    expected = longform_render_fingerprint(
        episode_dir, episode, config, audio_path, segments
    )
    stat = output.stat()
    recorded_output = record.get("output", {})
    if (
        record.get("fingerprint") != expected
        or recorded_output.get("size_bytes") != stat.st_size
        or recorded_output.get("mtime_ns") != stat.st_mtime_ns
    ):
        return None
    return record


def current_episode_longform_render(
    episode_dir: Path, episode: dict, config: dict, audio_path: Path
) -> dict | None:
    """Validate the canonical longform using the episode's source segments."""
    try:
        segments = json.loads((episode_dir / "segments.json").read_text()).get(
            "segments", []
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not segments:
        return None
    return current_longform_render(episode_dir, episode, config, audio_path, segments)


def current_short_render(
    episode_dir: Path,
    episode: dict,
    config: dict,
    audio_path: Path,
    segments: list[dict],
    clip: dict,
) -> dict | None:
    """Return a short manifest record only while all source inputs match."""
    clip_id = clip.get("id")
    if not clip_id:
        return None
    output = episode_dir / "shorts" / f"{clip_id}.mp4"
    if not output.exists():
        return None
    record = read_render_manifest(episode_dir).get("shorts", {}).get(clip_id, {})
    expected = short_render_fingerprint(
        episode_dir, episode, config, audio_path, segments, clip
    )
    stat = output.stat()
    recorded_output = record.get("output", {})
    if (
        record.get("render_mode") != "speaker_cut_short"
        or record.get("fingerprint") != expected
        or recorded_output.get("size_bytes") != stat.st_size
        or recorded_output.get("mtime_ns") != stat.st_mtime_ns
    ):
        return None
    return record


@contextmanager
def render_scratch_dir(label: str, estimated_bytes: int):
    """Use bounded internal scratch while preserving 10 GB of free space."""
    root = Path.home() / "Library" / "Caches" / "cascade" / "renders"
    root.mkdir(parents=True, exist_ok=True)
    reserve = 10_000_000_000
    free = shutil.disk_usage(root).free
    if free < estimated_bytes + reserve:
        raise RuntimeError(
            f"Not enough render scratch space: need about {estimated_bytes / 1e9:.1f} "
            f"GB plus 10 GB reserve; {free / 1e9:.1f} GB available"
        )
    with tempfile.TemporaryDirectory(prefix=f"{label}-", dir=root) as directory:
        yield Path(directory)


def require_output_space(path: Path, estimated_bytes: int) -> None:
    """Fail before encoding if the destination cannot retain a 1 GB reserve."""
    free = shutil.disk_usage(path).free
    if free < estimated_bytes + 1_000_000_000:
        raise RuntimeError(
            f"Not enough output space: need about {estimated_bytes / 1e9:.1f} GB plus "
            f"1 GB reserve; {free / 1e9:.1f} GB available"
        )


def estimate_output_bytes(duration: float, video_mbps: float = 8.0) -> int:
    return int(duration * ((video_mbps * 1_000_000) + 192_000) / 8 * 1.2)


def render_delivery_video(
    episode_dir: Path,
    episode: dict,
    config: dict,
    audio_path: Path,
    progress: ProgressCallback | None = None,
) -> dict:
    source = episode_dir / "source_merged.mp4"
    if not source.exists():
        raise FileNotFoundError("source_merged.mp4 is required for video delivery")
    if not audio_path.exists():
        raise FileNotFoundError(
            "Prepared mastered audio is required for video delivery"
        )

    source_probe = probe(source)
    video_stream = next(
        s for s in source_probe["streams"] if s["codec_type"] == "video"
    )
    source_duration = float(source_probe["format"]["duration"])
    intervals = build_keep_intervals(source_duration, episode.get("longform_edits", []))
    output_duration = sum(end - start for start, end in intervals)
    audio_duration = float(probe(audio_path)["format"]["duration"])
    required_audio_end = max(end for _, end in intervals)
    if audio_duration + 0.1 < required_audio_end:
        raise RuntimeError(
            f"Canonical audio ends at {audio_duration:.3f}s but the selected video "
            f"requires audio through {required_audio_end:.3f}s"
        )
    required = estimate_output_bytes(output_duration)
    free = shutil.disk_usage(episode_dir).free
    if free < required + 1_000_000_000:
        raise RuntimeError(
            f"Not enough free space: need about {required / 1e9:.1f} GB plus 1 GB reserve; "
            f"{free / 1e9:.1f} GB available"
        )

    vf, _, _ = _video_filter(
        int(video_stream["width"]),
        int(video_stream["height"]),
        episode.get("crop_config", {}),
        config,
    )
    output = episode_dir / "upload_video.mp4"
    temp = episode_dir / "upload_video.tmp.mp4"
    filter_graph = _filter_graph(intervals, vf)
    encoder = "h264_videotoolbox" if has_videotoolbox() else "libx264"
    decoder_args = (
        ["-hwaccel", "videotoolbox"] if encoder == "h264_videotoolbox" else []
    )
    encoder_args = ["-c:v", encoder, "-b:v", "7M", "-maxrate", "8M", "-bufsize", "14M"]
    if encoder == "libx264":
        encoder_args += ["-preset", "medium"]
    cmd = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *decoder_args,
        "-i",
        str(source),
        "-i",
        str(audio_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        *encoder_args,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        *get_color_metadata_args(),
        "-movflags",
        "+faststart",
        "-use_editlist",
        "0",
        "-progress",
        "pipe:1",
        "-nostats",
        str(temp),
    ]
    temp.unlink(missing_ok=True)
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key in {"out_time_us", "out_time_ms"}:
            try:
                seconds = float(value) / 1_000_000
            except ValueError:
                continue
            if progress:
                progress(min(99.0, seconds / output_duration * 100), "Encoding video")
    _, stderr = process.communicate()
    if process.returncode != 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Video export failed: {stderr[-500:]}")
    # Validate the temporary artifact before publishing it so a failed export
    # cannot replace a previously verified delivery file.
    result_probe = probe(temp)
    streams = result_probe["streams"]
    out_video = next(s for s in streams if s["codec_type"] == "video")
    out_audio = next(s for s in streams if s["codec_type"] == "audio")
    actual_duration = float(result_probe["format"]["duration"])
    video_duration = float(out_video.get("duration", actual_duration))
    audio_duration = float(out_audio.get("duration", 0))
    if out_video.get("codec_name") != "h264" or out_audio.get("codec_name") != "aac":
        raise RuntimeError("Video export codec validation failed")
    if int(out_video["width"]) > 1920 or int(out_video["height"]) > 1080:
        raise RuntimeError("Video export exceeds 1080p")
    if abs(actual_duration - output_duration) > 1.0:
        raise RuntimeError(
            f"Video duration is {actual_duration:.3f}s; expected {output_duration:.3f}s"
        )
    if abs(video_duration - output_duration) > 1.0:
        raise RuntimeError(
            f"Video stream duration is {video_duration:.3f}s; expected {output_duration:.3f}s"
        )
    if audio_duration <= 0 or abs(audio_duration - output_duration) > 1.0:
        raise RuntimeError(
            f"Audio stream duration is {audio_duration:.3f}s; expected {output_duration:.3f}s"
        )
    if temp.stat().st_size <= 0:
        raise RuntimeError("Video export is empty")
    os.replace(temp, output)
    if progress:
        progress(100.0, "Video verified")
    return {
        "path": str(output),
        "filename": output.name,
        "size_bytes": output.stat().st_size,
        "duration_seconds": round(actual_duration, 3),
        "audio_duration_seconds": round(audio_duration, 3),
        "video_duration_seconds": round(video_duration, 3),
        "expected_duration_seconds": round(output_duration, 3),
        "width": int(out_video["width"]),
        "height": int(out_video["height"]),
        "video_codec": "h264",
        "audio_codec": "aac",
        "encoder": encoder,
        "edit_count": len(episode.get("longform_edits", [])),
    }
