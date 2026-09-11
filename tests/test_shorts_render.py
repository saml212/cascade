"""Focused tests for source-clock shorts rendering."""

import json
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from agents.shorts_render import ShortsRenderAgent, render_single_clip
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


def test_brief_both_span_holds_previous_crop_and_long_span_stays_wide(
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
    assert (
        "force_original_aspect_ratio=decrease"
        in agent._get_short_crop_filter_no_subs("BOTH", 3840, 2160, {"wide_zoom": 1})
    )


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
        }

    with (
        patch("agents.shorts_render.require_output_space"),
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
