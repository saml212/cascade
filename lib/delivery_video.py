"""Compact, continuous-shot video export for local delivery."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from lib.crop import compute_crop
from lib.encoding import get_color_metadata_args, get_lut_filter, has_videotoolbox
from lib.ffprobe import probe

ProgressCallback = Callable[[float, str], None]


def build_keep_intervals(
    duration: float, edits: list[dict]
) -> list[tuple[float, float]]:
    """Return source intervals left after validated trims and cuts."""
    start, end = 0.0, float(duration)
    cuts: list[tuple[float, float]] = []
    for edit in edits:
        kind = edit.get("type")
        if kind == "trim_start":
            start = max(start, float(edit["seconds"]))
        elif kind == "trim_end":
            end = min(end, float(edit["seconds"]))
        elif kind == "cut":
            cuts.append((float(edit["start_seconds"]), float(edit["end_seconds"])))
        else:
            raise ValueError(f"Unsupported longform edit type: {kind!r}")
    if not 0 <= start < end <= duration + 0.01:
        raise ValueError(f"Invalid trim range {start:.3f}-{end:.3f}s")

    intervals = [(start, end)]
    for cut_start, cut_end in sorted(cuts):
        if cut_end <= cut_start or cut_start < 0 or cut_end > duration + 0.01:
            raise ValueError(f"Invalid cut range {cut_start:.3f}-{cut_end:.3f}s")
        next_intervals = []
        for left, right in intervals:
            if cut_end <= left or cut_start >= right:
                next_intervals.append((left, right))
            else:
                if left < cut_start:
                    next_intervals.append((left, cut_start))
                if cut_end < right:
                    next_intervals.append((cut_end, right))
        intervals = next_intervals
    intervals = [(a, b) for a, b in intervals if b - a >= 0.1]
    if not intervals:
        raise ValueError("Longform edits remove the entire episode")
    return intervals


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
        "ffmpeg",
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
