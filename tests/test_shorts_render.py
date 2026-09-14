"""Focused tests for source-clock shorts rendering."""

import json
import shutil
import subprocess
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from agents.shorts_render import (
    BACKGROUND_CAPTION_MARGIN_V,
    THREE_PERSON_STACK_CAPTION_MARGIN_V,
    ShortsRenderAgent,
    render_single_clip,
    repair_single_clip_audio,
)
from lib.ass import DEFAULT_MARGIN_V
from lib.delivery_video import (
    audio_packet_signature,
    ffmpeg_executable,
    render_space_budget,
)
from lib.encoding import get_video_encoding_policy
from lib.timeline import Timeline


def test_batch_render_skips_rejected_candidates(tmp_episode_dir, sample_config):
    clips = [
        {"id": "pending", "status": "pending"},
        {"id": "selected", "selection_status": "selected"},
        {"id": "rejected-selection", "selection_status": "rejected"},
        {"id": "rejected-legacy", "status": "rejected"},
    ]
    (tmp_episode_dir / "clips.json").write_text(json.dumps({"clips": clips}))
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)

    with patch.object(agent, "_render_clips", return_value={}) as render:
        agent.execute()

    assert render.call_args.args == ([clips[0], clips[1]],)


def test_clip_segments_switch_crop_dynamically_and_fill_gaps(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    segments = [
        {"start": 10, "end": 13, "speaker": "A"},
        {"start": 15, "end": 20, "speaker": "B"},
    ]
    assert agent._get_clip_segments(segments, 10, 20) == [
        {"start": 10.0, "end": 13.0, "speaker": "A"},
        {"start": 13.0, "end": 15.0, "speaker": "BOTH"},
        {"start": 15.0, "end": 20.0, "speaker": "B"},
    ]


def test_brief_both_span_holds_previous_crop_and_long_span_stays_both(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    segments = [
        {
            "start": 0,
            "end": 2,
            "duration": 2,
            "source_start": 0,
            "source_end": 2,
            "speaker": "speaker_0",
        },
        {
            "start": 2,
            "end": 4,
            "duration": 2,
            "source_start": 2,
            "source_end": 4,
            "speaker": "BOTH",
        },
        {
            "start": 4,
            "end": 8,
            "duration": 4,
            "source_start": 4,
            "source_end": 8,
            "speaker": "BOTH",
        },
    ]

    resolved = agent._apply_overlap_policy(segments)

    assert [(item["speaker"], item["duration"]) for item in resolved] == [
        ("speaker_0", 4),
        ("BOTH", 4),
    ]


def test_two_person_both_span_stacks_close_crops(tmp_episode_dir, sample_config):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {"center_x": 80, "center_y": 90, "zoom": 1},
            {"center_x": 240, "center_y": 90, "zoom": 1},
        ]
    }

    video_filter = agent._get_short_crop_filter_no_subs("BOTH", 320, 180, crop_config)

    assert video_filter.startswith("split=2[stack0][stack1]")
    assert "[stack0]crop=100:88:30:22" in video_filter
    assert "[stack1]crop=100:88:190:22" in video_filter
    assert video_filter.count("scale=1080:960") == 2
    assert "[top][bottom]vstack=inputs=2" in video_filter


def test_background_overlap_panels_center_on_configured_faces(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {"center_x": 500, "center_y": 465, "zoom": 1},
            {"center_x": 1420, "center_y": 520, "zoom": 1},
        ]
    }

    video_filter = agent._get_background_crop_filter_no_subs(
        "BOTH", 1920, 1080, crop_config
    )

    assert "[motion0]crop=606:358:196:286" in video_filter
    assert "[motion1]crop=606:358:1116:340" in video_filter
    assert video_filter.count("scale=1080:640") == 2
    assert "pad=1080:1920:0:0:black" in video_filter


def test_background_composition_copies_complete_base_audio(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    commands = []
    agent._run_ffmpeg = lambda command, **_kwargs: commands.append(command)

    agent._compose_background_variant(
        tmp_episode_dir / "podcast.mp4",
        tmp_episode_dir / "motion.mp4",
        tmp_episode_dir / "base.mp4",
        tmp_episode_dir / "variant.mp4",
        "30/1",
        [(2.0, 4.0)],
        ["-c:v", "libx264"],
    )

    command = commands[0]
    assert command[command.index("-c:a") + 1] == "copy"
    assert "-shortest" in command
    assert "-t" not in command
    graph = command[command.index("-filter_complex") + 1]
    assert "tpad=stop_mode=clone:stop_duration=0.25" in graph
    assert "enable='not(between(t,2.000000,4.000000))'" in graph


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_background_composition_keeps_trailing_aac_packet(
    tmp_episode_dir, sample_config
):
    ffmpeg = ffmpeg_executable()
    podcast = tmp_episode_dir / "podcast.mp4"
    motion = tmp_episode_dir / "motion.mp4"
    base = tmp_episode_dir / "base.mp4"
    output = tmp_episode_dir / "variant.mp4"
    for path, source in (
        (podcast, "color=blue:size=1080x1920:rate=30:duration=1"),
        (motion, "testsrc2=size=160x90:rate=30:duration=0.5"),
    ):
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                source,
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(path),
            ],
            check=True,
        )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=160x284:rate=30:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.05",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-y",
            str(base),
        ],
        check=True,
    )
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    agent._compose_background_variant(
        podcast,
        motion,
        base,
        output,
        "30/1",
        [],
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            "500k",
            "-maxrate",
            "1M",
            "-bufsize",
            "2M",
        ],
    )

    assert audio_packet_signature(output) == audio_packet_signature(base)


def test_background_caption_margin_stays_above_motion_panel():
    assert BACKGROUND_CAPTION_MARGIN_V == 840


@pytest.mark.parametrize("overlap", ["BOTH", "NONE"])
def test_three_person_overlap_stacks_all_crops_in_spatial_order(
    tmp_episode_dir, sample_config, overlap
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {"center_x": 240, "center_y": 90, "zoom": 1},
            {"center_x": 160, "center_y": 90, "zoom": 1},
            {"center_x": 80, "center_y": 90, "zoom": 1},
        ]
    }

    video_filter = agent._get_short_crop_filter_no_subs(
        overlap, 320, 180, crop_config, three_person_stack=True
    )

    assert video_filter.startswith("split=3[stack0][stack1][stack2]")
    assert "[stack2]crop=100:58:30:20" in video_filter
    assert "[stack1]crop=100:58:110:20" in video_filter
    assert "[stack0]crop=100:58:190:20" in video_filter
    assert video_filter.index("[stack2]crop") < video_filter.index("[stack1]crop")
    assert video_filter.index("[stack1]crop") < video_filter.index("[stack0]crop")
    assert video_filter.count("scale=1080:640") == 3
    assert "[row0][row1][row2]vstack=inputs=3" in video_filter
    assert "drawbox=x=0:y=637:w=1080:h=6" in video_filter
    assert "drawbox=x=0:y=1277:w=1080:h=6" in video_filter


@pytest.mark.parametrize(
    ("overlap", "speakers"),
    [
        ("BOTH", [{}]),
        ("NONE", [{}, {}]),
        ("BOTH", [{}, {}, {}]),
        ("BOTH", [{}, {}, {}, {}]),
    ],
)
def test_overlap_without_supported_stack_fits_wide(
    tmp_episode_dir, sample_config, overlap, speakers
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)

    video_filter = agent._get_short_crop_filter_no_subs(
        overlap, 3840, 2160, {"speakers": speakers, "wide_zoom": 1}
    )

    assert "force_original_aspect_ratio=decrease" in video_filter
    assert "vstack" not in video_filter


def test_render_short_uses_each_retained_source_range_and_rebases_ass(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    (tmp_episode_dir / "episode.json").write_text(
        json.dumps({"crop_config": {"speakers": []}})
    )
    diarized = {
        "utterances": [
            {
                "speaker": 0,
                "words": [
                    {"word": "before", "start": 0.2, "end": 0.5, "speaker": 0},
                    {"word": "removed", "start": 3.0, "end": 3.3, "speaker": 0},
                    {"word": "after", "start": 4.2, "end": 4.5, "speaker": 0},
                ],
            }
        ]
    }
    segments = [
        {"start": 0, "end": 3, "speaker": "A"},
        {"start": 3, "end": 6, "speaker": "B"},
    ]
    timeline = Timeline(6, [(0, 2), (4, 6)])
    scratch = tmp_episode_dir / "scratch"
    scratch.mkdir()
    output = tmp_episode_dir / "shorts" / "clip_01.mp4"
    caption_path = tmp_episode_dir / "subtitles" / "clip_01.ass"

    def fake_segment(*args, **kwargs):
        args[1].write_bytes(b"video")

    def fake_concat(_paths, destination, **_kwargs):
        destination.write_bytes(b"joined")

    def fake_mux(_video, _audio, destination, _timeline, **_kwargs):
        destination.write_bytes(b"muxed")
        return {
            "duration_seconds": 4,
            "audio_duration_seconds": 4,
            "video_duration_seconds": 4,
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_loudness": {
                "integrated_lufs": -16.0,
                "true_peak_dbfs": -1.5,
            },
        }

    with (
        patch("agents.shorts_render.require_render_space"),
        patch(
            "agents.shorts_render.render_scratch_dir",
            return_value=nullcontext(scratch),
        ),
        patch(
            "agents.shorts_render.render_video_segment", side_effect=fake_segment
        ) as render,
        patch("agents.shorts_render.concat_video_segments", side_effect=fake_concat),
        patch("agents.shorts_render.mux_timeline_audio", side_effect=fake_mux),
        patch(
            "agents.shorts_render.record_short_render",
            return_value={"render_mode": "speaker_cut_short"},
        ),
    ):
        agent._render_short(
            source,
            output,
            caption_path,
            0,
            6,
            segments,
            320,
            180,
            "128k",
            {
                "speakers": [
                    {"center_x": 80, "center_y": 90, "zoom": 1},
                    {"center_x": 240, "center_y": 90, "zoom": 1},
                ]
            },
            ["-c:v", "libx264"],
            audio_mix_path=audio,
            timeline=timeline,
            diarized=diarized,
            fingerprint="fingerprint",
            clip={"id": "clip_01", "start_seconds": 0, "end_seconds": 6},
        )

    rendered_ranges = [
        (call.kwargs["source_start"], call.kwargs["source_end"])
        for call in render.call_args_list
    ]
    assert rendered_ranges == [(0, 2), (4, 6)]
    assert "removed" not in caption_path.read_text()
    assert "after" in (scratch / "segment_001.ass").read_text()
    assert "0:00:00.20" in (scratch / "segment_001.ass").read_text()


@pytest.mark.parametrize(
    ("speaker", "speaker_count", "stack_enabled", "expected_margin"),
    [
        ("BOTH", 3, False, DEFAULT_MARGIN_V),
        ("BOTH", 3, True, THREE_PERSON_STACK_CAPTION_MARGIN_V),
        ("NONE", 3, True, THREE_PERSON_STACK_CAPTION_MARGIN_V),
        ("BOTH", 2, True, DEFAULT_MARGIN_V),
        ("speaker_0", 3, True, DEFAULT_MARGIN_V),
    ],
)
def test_short_caption_style_changes_only_for_opted_in_three_person_stack(
    tmp_episode_dir,
    sample_config,
    speaker,
    speaker_count,
    stack_enabled,
    expected_margin,
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {"speakers": [{} for _ in range(speaker_count)]}

    style = agent._short_caption_style(speaker, crop_config, stack_enabled)

    assert style.margin_v == expected_margin


def test_empty_clip_set_can_render_for_review_without_youtube_url(
    tmp_episode_dir, sample_config
):
    episode = {
        "crop_config": {"speakers": [{"center_x": 160, "center_y": 90}]},
        "longform_edits": [],
    }
    for name, data in (
        ("episode.json", episode),
        ("clips.json", {"clips": []}),
        ("segments.json", {"segments": [{"start": 0, "end": 5, "speaker": "A"}]}),
        ("diarized_transcript.json", {"utterances": []}),
    ):
        (tmp_episode_dir / name).write_text(json.dumps(data))
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    probe = {
        "format": {"duration": "5"},
        "streams": [
            {
                "codec_type": "video",
                "width": 320,
                "height": 180,
                "r_frame_rate": "30/1",
            }
        ],
    }

    with (
        patch("agents.shorts_render.generate_audio_mix", return_value=audio),
        patch("agents.shorts_render.ffprobe", return_value=probe),
    ):
        result = ShortsRenderAgent(tmp_episode_dir, sample_config).execute()

    assert result["count"] == 0
    assert result["render_mode"] == "speaker_cut_short"


def test_batch_preflights_all_outputs_and_two_concurrent_scratch_sets(
    tmp_episode_dir, sample_config
):
    episode = {"crop_config": {"speakers": [{}]}, "longform_edits": []}
    (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
    (tmp_episode_dir / "diarized_transcript.json").write_text('{"utterances": []}')
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    source.write_bytes(b"source")
    audio.write_bytes(b"audio")
    clips = [
        {"id": "clip_01", "start_seconds": 0, "end_seconds": 5},
        {"id": "clip_02", "start_seconds": 10, "end_seconds": 15},
    ]
    probe = {
        "format": {"duration": "20"},
        "streams": [
            {
                "codec_type": "video",
                "width": 320,
                "height": 180,
                "r_frame_rate": "30/1",
            }
        ],
    }
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)

    with (
        patch(
            "agents.shorts_render.current_speaker_segments",
            return_value={"segments": [{"start": 0, "end": 20, "speaker": "A"}]},
        ),
        patch(
            "agents.shorts_render.current_diarized_transcript",
            return_value={"utterances": []},
        ),
        patch("agents.shorts_render.generate_audio_mix", return_value=audio),
        patch("agents.shorts_render.ffprobe", return_value=probe),
        patch("agents.shorts_render.current_short_render", return_value=None),
        patch("agents.shorts_render.short_render_fingerprint", return_value="fp"),
        patch("agents.shorts_render.require_render_space") as render_space,
        patch.object(
            agent,
            "_render_short",
            side_effect=lambda *_args, **_kwargs: {"fingerprint": "fp"},
        ),
    ):
        result = agent._render_clips(clips)

    one = render_space_budget(5, get_video_encoding_policy(sample_config, "shorts"))
    render_space.assert_called_once_with(
        tmp_episode_dir / "shorts",
        {
            "output_bytes": one["output_bytes"] * 2,
            "scratch_bytes": one["scratch_bytes"] * 2,
        },
    )
    assert result["rendered_clips"] == ["clip_01", "clip_02"]


def test_public_single_clip_adapter_does_not_mutate_candidate_selection(
    tmp_episode_dir, sample_config
):
    clips_path = tmp_episode_dir / "clips.json"
    clips_path.write_text(
        json.dumps(
            {
                "clips": [
                    {"id": "clip_01", "start_seconds": 1, "end_seconds": 4},
                    {"id": "clip_02", "start_seconds": 5, "end_seconds": 8},
                ]
            },
            indent=2,
        )
    )
    original = clips_path.read_bytes()
    render = {
        "path": "shorts/clip_02.mp4",
        "render_mode": "speaker_cut_short",
        "reused": False,
    }

    with patch.object(
        ShortsRenderAgent,
        "_render_clips",
        return_value={"renders": {"clip_02": render}},
    ) as render_clips:
        result = render_single_clip(tmp_episode_dir, sample_config, "clip_02")

    assert render_clips.call_args.args == (
        [{"id": "clip_02", "start_seconds": 5, "end_seconds": 8}],
    )
    assert clips_path.read_bytes() == original
    assert result == {
        "clip_id": "clip_02",
        "output_path": str(tmp_episode_dir / "shorts" / "clip_02.mp4"),
        "caption_path": str(tmp_episode_dir / "subtitles" / "clip_02.ass"),
        "reused": False,
        "render": render,
    }


def test_public_single_clip_adapter_rejects_unknown_clip(
    tmp_episode_dir, sample_config
):
    (tmp_episode_dir / "clips.json").write_text('{"clips": []}')

    with pytest.raises(KeyError, match="Unknown clip: missing"):
        render_single_clip(tmp_episode_dir, sample_config, "missing")


@pytest.mark.parametrize("record_failure", [False, True])
def test_repair_single_clip_audio_is_atomic_with_manifest_record(
    tmp_episode_dir, sample_config, record_failure
):
    clip = {"id": "clip_02", "start_seconds": 1, "end_seconds": 3}
    episode = {"crop_config": {"speakers": [{}]}, "longform_edits": []}
    (tmp_episode_dir / "episode.json").write_text(json.dumps(episode))
    (tmp_episode_dir / "clips.json").write_text(json.dumps({"clips": [clip]}))
    source = tmp_episode_dir / "source_merged.mp4"
    audio = tmp_episode_dir / "work" / "audio_mix.wav"
    output = tmp_episode_dir / "shorts" / "clip_02.mp4"
    caption = tmp_episode_dir / "subtitles" / "clip_02.ass"
    source.write_bytes(b"source")
    audio.parent.mkdir(exist_ok=True)
    audio.write_bytes(b"audio")
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"old audio, reviewed video")
    manifest_path = tmp_episode_dir / "render_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "clock": "source",
                "shorts": {"clip_02": {"fingerprint": "prior-record"}},
            }
        )
    )
    original_output = output.read_bytes()
    original_manifest = manifest_path.read_bytes()
    caption.parent.mkdir(exist_ok=True)
    caption.write_text("captions")
    current = {
        "fingerprint": "current-short",
        "captions": {"path": "subtitles/clip_02.ass", "burned_in": True},
        "provenance": {"overlap_policy": "verified"},
        "output": {
            "size_bytes": output.stat().st_size,
            "audio_loudness": {
                "integrated_lufs": -19.7,
                "true_peak_dbfs": -1.4,
            },
        },
    }
    source_probe = {
        "format": {"duration": "4"},
        "streams": [
            {
                "codec_type": "video",
                "width": 320,
                "height": 180,
                "r_frame_rate": "30/1",
            }
        ],
    }

    def fake_mux(video, _audio, destination, timeline, **kwargs):
        assert video == output
        assert timeline.keep_intervals == ((1.0, 3.0),)
        assert kwargs["loudness_policy"]["profile"] == "shorts"
        assert kwargs["verify_video_copy"] is True
        replacement = destination.with_name(f".{destination.name}.replacement")
        replacement.write_bytes(b"same video, normalized audio")
        replacement.replace(destination)
        return {
            "duration_seconds": 2,
            "audio_duration_seconds": 2,
            "video_duration_seconds": 2,
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_loudness": {
                "integrated_lufs": -16.0,
                "true_peak_dbfs": -1.4,
            },
            "video_copy_verification": {
                "status": "pass",
                "input": {"sha256": "same", "packet_count": 60},
                "output": {"sha256": "same", "packet_count": 60},
            },
        }

    record = (
        patch(
            "agents.shorts_render.record_short_render",
            side_effect=RuntimeError("manifest write failed"),
        )
        if record_failure
        else nullcontext()
    )
    with (
        patch(
            "agents.shorts_render.current_speaker_segments",
            return_value={"segments": [{"start": 0, "end": 4, "speaker": "speaker_0"}]},
        ),
        patch("agents.shorts_render.generate_audio_mix", return_value=audio),
        patch("agents.shorts_render.ffprobe", return_value=source_probe),
        patch("agents.shorts_render.current_short_render", return_value=current),
        patch("agents.shorts_render.require_render_space"),
        patch("agents.shorts_render.mux_timeline_audio", side_effect=fake_mux),
        record,
    ):
        if record_failure:
            with pytest.raises(RuntimeError, match="manifest write failed"):
                repair_single_clip_audio(tmp_episode_dir, sample_config, "clip_02")
        else:
            result = repair_single_clip_audio(tmp_episode_dir, sample_config, "clip_02")

    if record_failure:
        assert output.read_bytes() == original_output
        assert manifest_path.read_bytes() == original_manifest
        return

    assert output.read_bytes() == b"same video, normalized audio"
    assert result["audio_repaired"] is True
    assert result["video_reencoded"] is False
    assert result["render"]["fingerprint"] == "current-short"
    assert result["render"]["provenance"]["audio_remaster"]["video_reencoded"] is False
    assert (
        result["render"]["provenance"]["audio_remaster"]["video_copy_verification"][
            "status"
        ]
        == "pass"
    )
