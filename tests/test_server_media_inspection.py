"""Tests for bounded machine-readable server media inspection."""

import ast
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from agents.transcribe import export_logical_track_window
from lib.delivery_video import ffmpeg_executable
from lib.ffprobe import probe
from lib.timeline import Timeline
from server.media_inspection import (
    InspectionTarget,
    map_timestamp,
    render_cached_inspection,
    resolve_target,
    source_ranges,
)


def test_library_layer_does_not_import_agent_orchestration():
    violations = []
    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "lib").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if any(
                module and module.split(".", 1)[0] == "agents" for module in modules
            ):
                violations.append(path.relative_to(root))

    assert violations == []


def test_output_window_maps_across_source_cut():
    timeline = Timeline.from_edits(
        30, [{"type": "cut", "start_seconds": 10, "end_seconds": 20}]
    )

    assert map_timestamp(timeline, "source", 25) == (25, 15)
    assert map_timestamp(timeline, "output", 15) == (25, 15)
    assert source_ranges(timeline, 5, 15) == [
        {"start_seconds": 5.0, "end_seconds": 10.0},
        {"start_seconds": 20.0, "end_seconds": 25.0},
    ]
    with pytest.raises(ValueError, match="outside retained"):
        map_timestamp(timeline, "source", 15)
    with pytest.raises(ValueError, match="finite"):
        map_timestamp(timeline, "source", math.nan)


def test_resolve_longform_uses_revision_selected_audio(tmp_path, monkeypatch):
    source = tmp_path / "source_merged.mp4"
    output = tmp_path / "upload_video.mp4"
    selected = tmp_path / "work" / "audio_repair_selected.wav"
    selected.parent.mkdir()
    for path in (source, output, selected):
        path.write_bytes(path.name.encode())
    episode = {
        "longform_edits": [{"type": "cut", "start_seconds": 10, "end_seconds": 20}]
    }
    segments = [{"start": 0, "end": 100, "speaker": "speaker_0"}]

    import server.media_inspection as inspection

    monkeypatch.setattr(
        inspection,
        "_video",
        lambda path, _episode: (90.0 if path == output else 100.0, "30/1"),
    )
    monkeypatch.setattr(inspection, "selected_audio_source", lambda *_args: selected)
    monkeypatch.setattr(
        inspection,
        "current_speaker_segments",
        lambda *_args: {"segments": segments},
    )
    monkeypatch.setattr(
        inspection, "current_diarized_transcript", lambda *_args: {"utterances": []}
    )

    def current_render(_dir, _episode, _config, audio, actual_segments):
        assert audio == selected
        assert actual_segments == segments
        return {"fingerprint": "selected-audio-render"}

    monkeypatch.setattr(inspection, "current_longform_render", current_render)

    target = resolve_target(tmp_path, episode, {}, "longform")

    assert target.path == output
    assert target.timeline.duration == 90
    assert target.fingerprint == "selected-audio-render"
    assert target.preserve_audio_timestamps is False


def test_resolve_render_rejects_stale_selected_audio(tmp_path, monkeypatch):
    source = tmp_path / "source_merged.mp4"
    source.write_bytes(b"source")

    import server.media_inspection as inspection

    monkeypatch.setattr(inspection, "_video", lambda *_args: (10.0, "30/1"))
    monkeypatch.setattr(
        inspection,
        "selected_audio_source",
        lambda *_args: (_ for _ in ()).throw(ValueError("Selected audio is stale")),
    )

    with pytest.raises(ValueError, match="Selected audio is stale"):
        resolve_target(tmp_path, {}, {}, "longform")


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="ffmpeg is required",
)
def test_source_preview_materializes_real_aac_timestamp_gap(tmp_path):
    ffmpeg = ffmpeg_executable()
    source = tmp_path / "source_merged.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=160x90:rate=30:duration=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=12",
            "-filter_complex",
            "[1:a]aformat=channel_layouts=stereo,aselect='not(between(t,8,9))'[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    target = InspectionTarget(
        path=source,
        timeline=Timeline.from_edits(12),
        source_duration=12,
        media_duration=12,
        fingerprint="gap-fixture",
        preserve_audio_timestamps=True,
    )

    preview, cached = render_cached_inspection(
        target,
        tmp_path / "cache",
        kind="preview",
        start=7,
        duration=4,
    )

    assert cached is False
    details = probe(preview)
    streams = {stream["codec_type"]: stream for stream in details["streams"]}
    assert float(details["format"]["duration"]) == pytest.approx(4, abs=0.1)
    assert float(streams["audio"]["duration"]) == pytest.approx(4, abs=0.1)

    channel = tmp_path / "right.flac"
    channel_manifest = export_logical_track_window(
        tmp_path,
        {"duration_seconds": 12},
        None,
        7,
        11,
        channel,
        source_kind="camera",
        channel="right",
    )
    assert channel_manifest["source"] == {"kind": "camera", "channel": "right"}
    for media in (preview, channel):
        decoded = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(media),
                "-map",
                "0:a:0",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        samples = np.frombuffer(decoded, dtype=np.int16).astype(np.float64)
        assert len(samples) == pytest.approx(64_000, abs=2_000)
        assert np.sqrt(np.mean(samples[20_000:24_000] ** 2)) < 10
        assert np.sqrt(np.mean(samples[40_000:44_000] ** 2)) > 500
