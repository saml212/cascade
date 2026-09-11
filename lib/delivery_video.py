"""Compact, continuous-shot video export for local delivery."""

from __future__ import annotations

import copy
import fcntl
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
from lib.encoding import (
    get_color_metadata_args,
    resolve_lut_path,
)
from lib.ffprobe import probe
from lib.timeline import (  # noqa: F401
    Timeline,
    build_keep_intervals,
    quantize_timestamp,
)

RENDER_MANIFEST_NAME = "render_manifest.json"
RENDER_PIPELINE_VERSION = "source-clock/v2"
_manifest_lock = threading.Lock()
_render_locks_guard = threading.Lock()
_render_locks: dict[str, threading.Lock] = {}


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


def render_config_for_episode(episode: dict, config: dict) -> dict:
    """Apply episode-level choices to the shared render configuration."""
    resolved = copy.deepcopy(config)
    processing = resolved.setdefault("processing", {})
    if episode.get("delivery_apply_lut", False):
        if resolve_lut_path(resolved) is None:
            raise ValueError("The selected episode LUT is unavailable")
    else:
        processing["lut_path"] = ""
    if "delivery_burn_captions" in episode:
        processing["longform_burn_captions"] = bool(episode["delivery_burn_captions"])
    return resolved


@contextmanager
def _file_lock(path: Path, local_lock: threading.Lock):
    path.parent.mkdir(parents=True, exist_ok=True)
    with local_lock, path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def render_output_lock(output: Path):
    """Serialize writers of one media artifact across threads and processes."""
    key = str(output.resolve())
    with _render_locks_guard:
        local_lock = _render_locks.setdefault(key, threading.Lock())
    lock_path = output.with_name(f".{output.name}.render.lock")
    with _file_lock(lock_path, local_lock):
        yield


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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-",
        suffix=output_path.suffix,
        dir=output_path.parent,
    )
    os.close(descriptor)
    temp = Path(temp_name)
    temp.unlink()
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
        identity = {"path": str(path.resolve())}
        try:
            stat = path.stat()
        except OSError:
            identity["missing"] = True
        else:
            identity.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        files.append(identity)
    payload = json.dumps(
        {"files": files, "state": state}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _render_inputs(
    episode_dir: Path, audio_path: Path, config: dict
) -> tuple[list[Path], str | None]:
    paths = [episode_dir / "source_merged.mp4", audio_path]
    transcript = episode_dir / "diarized_transcript.json"
    if transcript.exists():
        paths.append(transcript)
    lut = resolve_lut_path(config)
    if lut is not None:
        paths.append(lut)
        digest = hashlib.sha256(lut.read_bytes()).hexdigest()
    else:
        digest = None
    return paths, digest


_VIDEO_PROCESSING_KEYS = (
    "encode_preset",
    "lut_interpolation",
    "lut_path",
    "output_resolution",
    "use_hardware_accel",
    "video_contrast",
    "video_denoise",
    "video_gamma",
    "video_polish",
    "video_saturation",
    "video_sharpen",
    "video_sharpen_strength",
    "videotoolbox_quality",
)


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
    config = render_config_for_episode(episode, config)
    paths, lut_digest = _render_inputs(episode_dir, audio_path, config)
    processing = config.get("processing", {})
    state = {
        "pipeline_version": RENDER_PIPELINE_VERSION,
        "clock": "source",
        "render_mode": render_mode,
        "color_grade": "lut" if episode.get("delivery_apply_lut", False) else "source",
        "lut_sha256": lut_digest,
        "edits": episode.get("longform_edits", []),
        "crop_config": episode.get("crop_config", {}),
        "source_properties": episode.get("source_properties", {}),
        "segments": segments,
        "processing": {
            key: processing.get(key)
            for key in (
                "audio_bitrate",
                "longform_burn_captions",
                "video_crf",
                *_VIDEO_PROCESSING_KEYS,
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
    config = render_config_for_episode(episode, config)
    processing = config.get("processing", {})
    state = {
        "pipeline_version": RENDER_PIPELINE_VERSION,
        "clock": "source",
        "render_mode": "speaker_cut_short",
        "color_grade": "lut" if episode.get("delivery_apply_lut", False) else "source",
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
                "shorts_audio_bitrate",
                "shorts_crf",
                "shorts_hold_wide_seconds",
                *_VIDEO_PROCESSING_KEYS,
            )
        },
    }
    paths, lut_digest = _render_inputs(episode_dir, audio_path, config)
    state["lut_sha256"] = lut_digest
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
    captions: dict | None = None,
    provenance: dict | None = None,
) -> dict:
    """Record a validated canonical longform render for API and release gates."""
    output = episode_dir / "upload_video.mp4"
    stat = output.stat()
    record = {
        "path": output.name,
        "render_mode": render_mode,
        "pipeline_version": RENDER_PIPELINE_VERSION,
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
    if captions is not None:
        record["captions"] = captions
    if provenance is not None:
        record["provenance"] = provenance
    with _file_lock(episode_dir / ".render_manifest.lock", _manifest_lock):
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
    captions: dict | None = None,
    provenance: dict | None = None,
) -> dict:
    """Record one validated short without changing other manifest entries."""
    output = episode_dir / "shorts" / f"{clip_id}.mp4"
    stat = output.stat()
    record = {
        "path": str(output.relative_to(episode_dir)),
        "render_mode": "speaker_cut_short",
        "pipeline_version": RENDER_PIPELINE_VERSION,
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
    if captions is not None:
        record["captions"] = captions
    if provenance is not None:
        record["provenance"] = provenance
    with _file_lock(episode_dir / ".render_manifest.lock", _manifest_lock):
        manifest = read_render_manifest(episode_dir)
        manifest["shorts"][clip_id] = record
        atomic_write_json(episode_dir / RENDER_MANIFEST_NAME, manifest)
    return record


def render_artifact_state(
    episode_dir: Path,
    output: Path,
    record: dict | None,
    *,
    expected_fingerprint: str | None,
    expected_mode: str | None,
) -> dict:
    """Describe an artifact for review without hiding an older usable file."""
    path = output if output.is_absolute() else episode_dir / output
    try:
        relative_path = str(path.relative_to(episode_dir))
    except ValueError:
        relative_path = path.name
    state = {
        "status": "missing",
        "current": False,
        "playable": False,
        "path": relative_path,
        "reason_code": "artifact_missing",
        "detail": "No rendered file exists yet.",
    }
    try:
        stat = path.stat()
    except OSError:
        return state
    if not path.is_file() or stat.st_size <= 0:
        return state

    state.update(
        playable=True,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    record = record if isinstance(record, dict) else {}
    state.update(
        render_mode=record.get("render_mode"),
        recorded_fingerprint=record.get("fingerprint"),
        fingerprint=record.get("fingerprint"),
        completed_at=record.get("completed_at"),
        output_duration_seconds=record.get("output_duration_seconds"),
    )
    if not record:
        state.update(
            status="untracked",
            reason_code="manifest_missing",
            detail="The previous render is reviewable, but has no render manifest.",
        )
        return state
    if expected_mode and record.get("render_mode") != expected_mode:
        state.update(
            status="stale",
            reason_code="render_mode_changed",
            detail="The previous render used an older layout or render mode.",
        )
        return state
    captions = record.get("captions")
    if captions is not None and (
        not isinstance(captions, dict)
        or not captions.get("path")
        or not (episode_dir / str(captions["path"])).is_file()
    ):
        state.update(
            status="stale",
            reason_code="caption_artifact_missing",
            detail="The video is reviewable, but its recorded caption sidecar is missing.",
        )
        return state
    recorded_output = record.get("output", {})
    if (
        not isinstance(recorded_output, dict)
        or recorded_output.get("size_bytes") != stat.st_size
        or recorded_output.get("mtime_ns") != stat.st_mtime_ns
    ):
        state.update(
            status="stale",
            reason_code="artifact_changed",
            detail="The rendered file changed after its manifest was recorded.",
        )
        return state
    if expected_fingerprint is None:
        state.update(
            status="stale",
            reason_code="verification_inputs_unavailable",
            detail="The previous render is reviewable, but its current inputs cannot be verified.",
        )
        return state
    if record.get("fingerprint") != expected_fingerprint:
        state.update(
            status="stale",
            reason_code="render_inputs_changed",
            detail="Render inputs changed after this file was produced.",
        )
        return state
    state.update(
        status="current",
        current=True,
        reason_code=None,
        detail="This file matches the current render inputs.",
    )
    return state


def current_longform_render(
    episode_dir: Path,
    episode: dict,
    config: dict,
    audio_path: Path,
    segments: list[dict],
) -> dict | None:
    """Return the speaker-cut manifest record only when inputs and output match."""
    output = episode_dir / "upload_video.mp4"
    record = read_render_manifest(episode_dir).get("longform", {})
    expected = longform_render_fingerprint(
        episode_dir, episode, config, audio_path, segments
    )
    state = render_artifact_state(
        episode_dir,
        output,
        record,
        expected_fingerprint=expected,
        expected_mode="speaker_cut",
    )
    return record if state["current"] else None


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
    record = read_render_manifest(episode_dir).get("shorts", {}).get(clip_id, {})
    expected = short_render_fingerprint(
        episode_dir, episode, config, audio_path, segments, clip
    )
    state = render_artifact_state(
        episode_dir,
        output,
        record,
        expected_fingerprint=expected,
        expected_mode="speaker_cut_short",
    )
    return record if state["current"] else None


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
