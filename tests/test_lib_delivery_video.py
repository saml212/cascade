"""Tests for the compact delivery video renderer."""

import shutil
import subprocess
from unittest.mock import patch

import numpy as np
import pytest

from lib.ass import CaptionStyle, generate_ass_from_diarized
from lib.delivery_video import (
    _audio_filter_graph,
    _filter_graph,
    _video_filter,
    build_keep_intervals,
    build_render_segments,
    concat_video_segments,
    current_longform_render,
    estimate_output_bytes,
    ffmpeg_executable,
    longform_render_fingerprint,
    mux_timeline_audio,
    record_longform_render,
    render_delivery_video,
    render_video_segment,
    source_fps,
)
from lib.srt import escape_srt_path
from lib.timeline import Timeline, rebase_diarized


def test_keep_intervals_applies_trims_and_cuts():
    edits = [
        {"type": "trim_start", "seconds": 10},
        {"type": "trim_end", "seconds": 100},
        {"type": "cut", "start_seconds": 30, "end_seconds": 40},
    ]
    assert build_keep_intervals(120, edits) == [(10.0, 30.0), (40.0, 100.0)]


def test_keep_intervals_rejects_unknown_edit():
    with pytest.raises(ValueError, match="Unsupported"):
        build_keep_intervals(100, [{"type": "fade"}])


def test_filter_graph_splits_inputs_for_multiple_ranges():
    graph = _filter_graph([(0, 10), (20, 30)], "scale=1920:1080")
    assert "[0:v]split=2[vin0][vin1]" in graph
    assert "[1:a]asplit=2[ain0][ain1]" in graph
    assert "concat=n=2:v=1:a=1[v][a]" in graph


def test_audio_filter_graph_applies_the_same_interior_cut_ranges():
    graph = _audio_filter_graph([(1, 2), (3, 4)])

    assert "[1:a]asplit=2[ain0][ain1]" in graph
    assert "[ain0]atrim=start=1:end=2" in graph
    assert "[ain1]atrim=start=3:end=4" in graph
    assert "[a0][a1]concat=n=2:v=0:a=1[a]" in graph


def test_source_fps_preserves_fractional_ffprobe_rate():
    assert source_fps({"r_frame_rate": "30000/1001"}, {}) == "30000/1001"


def test_render_segments_cover_edits_and_switch_speakers_without_overlap():
    timeline = Timeline.from_edits(
        10, [{"type": "cut", "start_seconds": 4, "end_seconds": 6}]
    )
    segments = [
        {"start": 0, "end": 2, "speaker": "A"},
        {"start": 2, "end": 8, "speaker": "B"},
        {"start": 8, "end": 10, "speaker": "A"},
    ]

    rendered = build_render_segments(timeline, segments)

    assert [segment["speaker"] for segment in rendered] == ["A", "B", "B", "A"]
    assert [
        (segment["source_start"], segment["source_end"]) for segment in rendered
    ] == [(0, 2.0), (2.0, 4.0), (6.0, 8.0), (8.0, 10)]
    assert sum(segment["duration"] for segment in rendered) == timeline.duration


def test_render_segments_fill_detection_gaps_with_wide_crop():
    timeline = Timeline.from_edits(5)

    rendered = build_render_segments(timeline, [{"start": 1, "end": 4, "speaker": "A"}])

    assert [segment["speaker"] for segment in rendered] == ["BOTH", "A", "BOTH"]
    assert sum(segment["duration"] for segment in rendered) == 5


def test_video_filter_caps_4k_at_1080p_even_without_crop():
    vf, width, height = _video_filter(3840, 2160, {}, {})
    assert (width, height) == (1920, 1080)
    assert "scale=1920:1080" in vf


def test_video_filter_uses_wide_crop():
    vf, width, height = _video_filter(
        3840,
        2160,
        {"wide_zoom": 1.5, "wide_center_x": 2000, "wide_center_y": 1080},
        {},
    )
    assert "crop=2560:1440" in vf
    assert (width, height) == (1920, 1080)


def test_disk_estimate_includes_video_audio_and_margin():
    assert estimate_output_bytes(3600) > 3_600_000_000


def test_longform_manifest_rejects_changed_segments(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio_mix.wav"
    output = tmp_path / "upload_video.mp4"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    output.write_bytes(b"render")
    episode = {"crop_config": {"wide_zoom": 1}, "longform_edits": []}
    config = {"processing": {"video_crf": 22}}
    segments = [{"start": 0, "end": 5, "speaker": "A"}]
    fingerprint = longform_render_fingerprint(
        tmp_path, episode, config, audio, segments
    )
    timeline = Timeline.from_edits(5)

    record_longform_render(
        tmp_path,
        fingerprint=fingerprint,
        render_mode="speaker_cut",
        timeline=timeline,
        media={"duration_seconds": 5},
    )

    assert current_longform_render(tmp_path, episode, config, audio, segments)
    changed = [{"start": 0, "end": 5, "speaker": "B"}]
    assert current_longform_render(tmp_path, episode, config, audio, changed) is None


def test_export_rejects_canonical_audio_that_does_not_cover_selected_end(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio_mix.wav"
    source.write_bytes(b"video")
    audio.write_bytes(b"audio")
    source_info = {
        "format": {"duration": "100"},
        "streams": [{"codec_type": "video", "width": 320, "height": 180}],
    }
    audio_info = {"format": {"duration": "94.5"}, "streams": []}
    episode = {
        "longform_edits": [
            {"type": "trim_start", "seconds": 10},
            {"type": "trim_end", "seconds": 95},
        ]
    }
    with (
        patch("lib.delivery_video.probe", side_effect=[source_info, audio_info]),
        patch("lib.delivery_video.subprocess.Popen") as popen,
        pytest.raises(RuntimeError, match="requires audio through 95.000s"),
    ):
        render_delivery_video(tmp_path, episode, {}, audio)
    popen.assert_not_called()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_real_export_applies_audio_trim_once_from_canonical_wav(tmp_path):
    source = tmp_path / "source_merged.mp4"
    audio = tmp_path / "audio_mix.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    inputs = []
    for frequency in (440, 880, 1320, 1760):
        inputs += [
            "-f",
            "lavfi",
            "-i",
            f"sine={frequency}:sample_rate=48000:duration=1",
        ]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            "[0:a][1:a][2:a][3:a]concat=n=4:v=0:a=1[a]",
            "-map",
            "[a]",
            "-c:a",
            "pcm_s24le",
            str(audio),
        ],
        check=True,
    )
    episode = {
        "longform_edits": [
            {"type": "trim_start", "seconds": 1},
            {"type": "trim_end", "seconds": 4},
        ]
    }
    with patch("lib.delivery_video.has_videotoolbox", return_value=False):
        result = render_delivery_video(
            tmp_path, episode, {"processing": {"lut_path": ""}}, audio
        )
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-t",
            "0.5",
            "-i",
            result["path"],
            "-vn",
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    samples = np.frombuffer(decoded, dtype="<f4")
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    frequencies = np.fft.rfftfreq(len(samples), 1 / 8000)
    dominant = frequencies[int(np.argmax(spectrum))]
    assert dominant == pytest.approx(880, abs=10)
    assert result["audio_duration_seconds"] == pytest.approx(3, abs=0.1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_segmented_render_keeps_audio_video_crops_and_captions_on_one_timeline(
    tmp_path,
):
    source = tmp_path / "source.mp4"
    audio = tmp_path / "audio.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=160x180:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=160x180:rate=30:duration=6",
            "-filter_complex",
            "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ],
        check=True,
    )
    inputs = []
    for frequency in (440, 550, 660, 770, 880, 990):
        inputs.extend(
            ["-f", "lavfi", "-i", f"sine={frequency}:sample_rate=48000:duration=1"]
        )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            "[0:a][1:a][2:a][3:a][4:a][5:a]concat=n=6:v=0:a=1[a]",
            "-map",
            "[a]",
            "-c:a",
            "pcm_s24le",
            str(audio),
        ],
        check=True,
    )

    timeline = Timeline.from_edits(
        6,
        [
            {"type": "trim_start", "seconds": 1},
            {"type": "cut", "start_seconds": 3, "end_seconds": 4},
        ],
    )
    speakers = [
        {"start": 0, "end": 3, "speaker": "left"},
        {"start": 3, "end": 6, "speaker": "right"},
    ]
    render_segments = build_render_segments(timeline, speakers)
    captions = rebase_diarized(
        {
            "utterances": [
                {
                    "speaker": 0,
                    "words": [
                        {"word": "left", "start": 1.2, "end": 1.5},
                        {"word": "removed", "start": 3.2, "end": 3.5},
                        {"word": "right", "start": 4.2, "end": 4.5},
                    ],
                }
            ]
        },
        timeline,
    )
    segment_paths = []
    for index, segment in enumerate(render_segments):
        segment_timeline = Timeline(
            timeline.duration, [(segment["start"], segment["end"])]
        )
        ass = tmp_path / f"segment_{index}.ass"
        generate_ass_from_diarized(
            rebase_diarized(captions, segment_timeline),
            0,
            segment["duration"],
            ass,
            CaptionStyle(font_size=24, margin_v=20, play_res_x=160, play_res_y=180),
        )
        crop_x = 0 if segment["speaker"] == "left" else 160
        segment_path = tmp_path / f"segment_{index}.mp4"
        render_video_segment(
            source,
            segment_path,
            source_start=segment["source_start"],
            source_end=segment["source_end"],
            video_filter=(
                f"crop=160:180:{crop_x}:0,format=yuv420p,"
                f"subtitles='{escape_srt_path(ass)}'"
            ),
            encoder_args=["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20"],
            fps=30,
        )
        segment_paths.append(segment_path)

    video_only = tmp_path / "video_only.mp4"
    output = tmp_path / "output.mp4"
    concat_video_segments(segment_paths, video_only)
    media = mux_timeline_audio(video_only, audio, output, timeline)

    def pixel_at(timestamp):
        return np.frombuffer(
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(output),
                    "-vf",
                    "crop=16:16:8:8,scale=1:1",
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-",
                ],
                capture_output=True,
                check=True,
            ).stdout,
            dtype=np.uint8,
        )

    def frequency_at(timestamp):
        samples = np.frombuffer(
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(timestamp),
                    "-t",
                    "0.4",
                    "-i",
                    str(output),
                    "-vn",
                    "-f",
                    "f32le",
                    "-ac",
                    "1",
                    "-ar",
                    "8000",
                    "-",
                ],
                capture_output=True,
                check=True,
            ).stdout,
            dtype="<f4",
        )
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        frequencies = np.fft.rfftfreq(len(samples), 1 / 8000)
        return frequencies[int(np.argmax(spectrum))]

    assert pixel_at(0.5)[0] > pixel_at(0.5)[2]
    assert pixel_at(2.5)[2] > pixel_at(2.5)[0]
    assert frequency_at(0.25) == pytest.approx(550, abs=10)
    assert frequency_at(2.25) == pytest.approx(880, abs=10)
    assert "removed" not in "".join(path.read_text() for path in tmp_path.glob("*.ass"))
    assert media["duration_seconds"] == pytest.approx(4, abs=0.1)
    assert media["audio_duration_seconds"] == pytest.approx(
        media["video_duration_seconds"], abs=0.1
    )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_fractional_fps_hundred_cut_fixture_has_no_cumulative_av_drift(tmp_path):
    rate = "30000/1001"
    frame_duration = 1001 / 30000
    source_duration = 200 * frame_duration
    source = tmp_path / "fractional_source.mp4"
    audio = tmp_path / "fractional_audio.wav"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=green:size=64x36:rate={rate}:duration={source_duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=990:sample_rate=48000:duration={source_duration - 6}",
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "[a]",
            "-c:a",
            "pcm_s24le",
            str(audio),
        ],
        check=True,
    )

    cuts = [
        {
            "type": "cut",
            "start_seconds": (frame_index + 1) * frame_duration,
            "end_seconds": (frame_index + 2) * frame_duration,
        }
        for frame_index in range(0, 200, 2)
    ]
    timeline = Timeline.from_edits(source_duration, cuts).quantize(rate)
    render_segments = build_render_segments(
        timeline,
        [
            {
                "start": start,
                "end": end,
                "speaker": "A" if index % 2 == 0 else "B",
            }
            for index, (start, end) in enumerate(timeline.keep_intervals)
        ],
        frame_rate=rate,
    )
    assert len(render_segments) == 100

    paths = []
    for index, segment in enumerate(render_segments):
        path = tmp_path / f"fractional_{index:03d}.mp4"
        render_video_segment(
            source,
            path,
            source_start=segment["source_start"],
            source_end=segment["source_end"],
            video_filter="format=yuv420p",
            encoder_args=["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"],
            fps=rate,
        )
        paths.append(path)

    video_only = tmp_path / "fractional_video.mp4"
    output = tmp_path / "fractional_output.mp4"
    concat_video_segments(paths, video_only)
    media = mux_timeline_audio(video_only, audio, output, timeline)

    samples = np.frombuffer(
        subprocess.run(
            [
                ffmpeg_executable(),
                "-v",
                "error",
                "-ss",
                "3.1",
                "-t",
                "0.2",
                "-i",
                str(output),
                "-vn",
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout,
        dtype="<f4",
    )
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    frequencies = np.fft.rfftfreq(len(samples), 1 / 8000)
    dominant = frequencies[int(np.argmax(spectrum))]

    expected_duration = 100 * frame_duration
    assert timeline.duration == pytest.approx(expected_duration, abs=1e-9)
    assert media["video_duration_seconds"] == pytest.approx(expected_duration, abs=0.05)
    assert media["audio_duration_seconds"] == pytest.approx(
        media["video_duration_seconds"], abs=0.05
    )
    assert dominant == pytest.approx(990, abs=15)
