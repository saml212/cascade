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
from lib.ass import (
    DEFAULT_MARGIN_V,
    build_ass,
    generate_ass_from_diarized,
    resolve_caption_speaker_targets,
)
from lib.delivery_video import (
    audio_packet_signature,
    ffmpeg_executable,
    render_space_budget,
)
from lib.encoding import get_video_encoding_policy
from lib.ffprobe import probe as ffprobe_probe
from lib.short_variants import (
    CONTAIN_BLUR_FIT_MODE,
    GAMEPLAY_SURROUND_VARIANT_ID,
    SPEAKER_PANELS_RENDER_PLAN,
    SPEAKER_PANELS_VARIANT_ID,
)
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


def test_background_overlap_panels_use_landscape_crop_settings(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {
                "center_x": 500,
                "center_y": 465,
                "zoom": 1.4,
                "longform_center_x": 520,
                "longform_center_y": 400,
                "longform_zoom": 1,
            },
            {
                "center_x": 1420,
                "center_y": 520,
                "zoom": 1.4,
                "longform_center_x": 1400,
                "longform_center_y": 460,
                "longform_zoom": 1,
            },
        ]
    }

    video_filter = agent._get_background_crop_filter_no_subs(
        "BOTH", 1920, 1080, crop_config
    )

    assert "[motion0]crop=960:568:40:68" in video_filter
    assert "[motion1]crop=960:568:920:128" in video_filter
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
    assert graph.count("enable='not(between(t,2.000000,4.000000))'") == 2


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
        [(0.2, 0.4)],
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


def test_gameplay_surround_podcast_column_keeps_every_configured_speaker(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {
                "center_x": 450,
                "center_y": 500,
                "zoom": 1.4,
                "longform_center_x": 480,
                "longform_center_y": 480,
                "longform_zoom": 1,
            },
            {
                "center_x": 1470,
                "center_y": 520,
                "zoom": 1.4,
                "longform_center_x": 1440,
                "longform_center_y": 500,
                "longform_zoom": 1,
            },
        ]
    }

    video_filter = agent._get_gameplay_surround_podcast_filter_no_subs(
        1920, 1080, crop_config
    )

    assert video_filter.startswith("split=2[surround0][surround1]")
    assert video_filter.count("scale=540:572") == 2
    assert "[row0][row1]vstack=inputs=2" in video_filter
    assert "drawbox=x=0:y=569:w=540:h=6" in video_filter


@pytest.mark.parametrize(
    ("center_xs", "expected"),
    [
        ([900], {"speaker_0": 1080}),
        ([1200, 500], {"speaker_1": 508, "speaker_0": 1080}),
        (
            [1210, 825, 565],
            {"speaker_2": 357, "speaker_1": 737, "speaker_0": 1120},
        ),
    ],
)
def test_gameplay_caption_positions_follow_exact_spatial_panel_rows(
    tmp_episode_dir, sample_config, center_xs, expected
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {
                "center_x": center_x,
                "center_y": 540,
                "longform_center_x": center_x,
                "longform_center_y": 540,
                "zoom": 1,
                "longform_zoom": 1,
            }
            for center_x in center_xs
        ]
    }

    placements, fallback = agent._gameplay_surround_caption_placements(
        1920, 1080, crop_config
    )

    assert {speaker: placement.y for speaker, placement in placements.items()} == (
        expected
    )
    assert all(placement.x == 540 for placement in placements.values())
    assert fallback.x == 540
    assert fallback.y == 36
    assert fallback.font_size == 24
    assert fallback.background_box == (270, 0, 540, 72)


def test_gameplay_surround_requires_reviewed_layout_above_three_speakers(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {"center_x": 100 + index * 100, "center_y": 90, "zoom": 1}
            for index in range(4)
        ]
    }

    with pytest.raises(ValueError, match="reviewed layout for more than 3"):
        agent._get_gameplay_surround_podcast_filter_no_subs(640, 360, crop_config)


def test_gameplay_surround_graph_uses_manifest_playback_and_focus(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    commands = []
    agent._run_ffmpeg = lambda command, **_kwargs: commands.append(command)
    assets = [
        {
            "role": "subway",
            "path": tmp_episode_dir / "subway.mp4",
            "playback_start_seconds": 7.25,
            "focus_x": 0.4,
            "focus_y": 0.5,
            "fit_mode": "stretch",
        },
        {
            "role": "gta",
            "path": tmp_episode_dir / "gta.mp4",
            "playback_start_seconds": 11.5,
            "focus_x": 0.6,
            "focus_y": 0.5,
        },
        {
            "role": "minecraft",
            "path": tmp_episode_dir / "minecraft.mp4",
            "playback_start_seconds": 18,
            "focus_x": 0.55,
            "focus_y": 0.45,
        },
    ]

    agent._compose_gameplay_surround_variant(
        tmp_episode_dir / "podcast.mp4",
        assets,
        tmp_episode_dir / "base.mp4",
        tmp_episode_dir / "captions.ass",
        tmp_episode_dir / "variant.mp4",
        "30/1",
        ["-c:v", "libx264"],
    )

    graph = commands[0][commands[0].index("-filter_complex") + 1]
    assert "[1:v]trim=start=7.250000,setpts=PTS-STARTPTS" in graph
    assert "[2:v]trim=start=11.500000,setpts=PTS-STARTPTS" in graph
    assert "[3:v]trim=start=18.000000,setpts=PTS-STARTPTS" in graph
    assert "[1:v]trim=start=7.250000,setpts=PTS-STARTPTS,scale=270:1216" in graph
    assert "crop=270:1216:(iw-ow)*0.400000:(ih-oh)*0.500000" not in graph
    assert "crop=270:1216:(iw-ow)*0.600000:(ih-oh)*0.500000" in graph
    assert "crop=1080:704:(iw-ow)*0.550000:(ih-oh)*0.450000" in graph
    assert (
        "drawtext=text='thelocalpod.link':fontcolor=white:fontsize=36:"
        "x=(w-text_w)/2:y=1286" in graph
    )


def test_gameplay_surround_captions_stay_inside_center_column(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    style = agent._caption_style({"variant_id": GAMEPLAY_SURROUND_VARIANT_ID})

    assert style.margin_l == 300
    assert style.margin_r == 300
    assert style.font_size == 52
    ass = build_ass([], style)
    style_line = next(line for line in ass.splitlines() if line.startswith("Style:"))
    assert ",2,300,300,840,1" in style_line


@pytest.mark.parametrize(
    ("center_xs", "expected_order", "expected_y", "row_height"),
    (
        ([900], [0], None, 1776),
        ([1400, 400], [1, 0], {"speaker_1": 824, "speaker_0": 1712}, 888),
        (
            [400, 900, 1500],
            [0, 1, 2],
            {"speaker_0": 528, "speaker_1": 1120, "speaker_2": 1712},
            592,
        ),
    ),
)
def test_clean_speaker_panels_use_full_width_physical_rows(
    tmp_episode_dir,
    sample_config,
    center_xs,
    expected_order,
    expected_y,
    row_height,
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {
                "label": label,
                "center_x": center_x,
                "center_y": 540,
                "longform_center_x": center_x,
                "longform_center_y": 540,
                "zoom": 1,
                "longform_zoom": 1,
            }
            for label, center_x in zip(
                ("Host", "Ty", "Garrett"), center_xs, strict=False
            )
        ]
    }

    rows = agent._speaker_panel_rows(
        1920,
        1080,
        crop_config,
        SPEAKER_PANELS_RENDER_PLAN["panel_width"],
        SPEAKER_PANELS_RENDER_PLAN["panels_height"],
    )
    placements, fallback = agent._speaker_panel_placements(
        1920,
        1080,
        crop_config,
        variant_id=SPEAKER_PANELS_VARIANT_ID,
    )
    video_filter = agent._get_speaker_panels_filter_no_subs(1920, 1080, crop_config)

    assert [row[0] for row in rows] == expected_order
    assert all(row[1] == row_height for row in rows)
    assert all(placement.x == 540 for placement in placements.values())
    if expected_y is None:
        picture_top = 72 + (1776 - 608) // 2
        picture_bottom = picture_top + 608
        assert picture_top < placements["speaker_0"].y < picture_bottom
    else:
        assert {
            speaker: placement.y for speaker, placement in placements.items()
        } == expected_y
    assert fallback.background_box == (0, 0, 1080, 72)
    assert f"scale=1080:{row_height}" in video_filter
    assert "scale=540" not in video_filter
    assert "SUBWAY" not in video_filter
    assert "MINECRAFT" not in video_filter


def test_clean_speaker_panel_bindings_keep_host_ty_garrett_rows(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {"label": "Host", "track": 1, "longform_center_x": 400},
            {"label": "Ty", "track": 2, "longform_center_x": 900},
            {"label": "Garrett", "track": 3, "longform_center_x": 1500},
        ]
    }
    diarized = {
        "clock": "source",
        "speaker_map": [
            {"index": 0, "target_speaker": "speaker_0"},
            {"index": 1, "target_speaker": "speaker_1"},
            {"index": 3, "target_speaker": "speaker_1"},
            {"index": 2, "target_speaker": "speaker_2"},
        ],
    }
    segment_document = {"clock": "source", "track_mapping": []}

    targets = resolve_caption_speaker_targets(diarized, segment_document, crop_config)
    placements, _ = agent._speaker_panel_placements(
        1920,
        1080,
        crop_config,
        variant_id=SPEAKER_PANELS_VARIANT_ID,
    )

    assert targets == {
        0: "speaker_0",
        1: "speaker_1",
        2: "speaker_2",
        3: "speaker_1",
    }
    assert placements[targets[0]].y == 528
    assert placements[targets[1]].y == placements[targets[3]].y == 1120
    assert placements[targets[2]].y == 1712


def test_clean_speaker_panel_compositor_has_no_asset_inputs_or_labels(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    commands = []
    agent._run_ffmpeg = lambda command, **_kwargs: commands.append(command)

    agent._compose_speaker_panels_variant(
        tmp_episode_dir / "podcast.mp4",
        tmp_episode_dir / "base.mp4",
        tmp_episode_dir / "captions.ass",
        tmp_episode_dir / "variant.mp4",
        "30/1",
        ["-c:v", "libx264"],
    )

    command = commands[0]
    graph = command[command.index("-filter_complex") + 1]
    assert command.count("-i") == 2
    assert "-stream_loop" not in command
    assert command[command.index("-map") + 1] == "[variant]"
    assert command[command.index("-c:a") + 1] == "copy"
    assert "THE LOCAL PODCAST" in graph
    assert "thelocalpod.link" in graph
    assert "SUBWAY SURFERS" not in graph
    assert "GTA DRIVING" not in graph
    assert "MINECRAFT PARKOUR" not in graph


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_clean_speaker_panel_pixels_follow_speaker_and_neutral_header(
    tmp_episode_dir, sample_config
):
    ffmpeg = ffmpeg_executable()
    podcast = tmp_episode_dir / "speaker-rows.mp4"
    base = tmp_episode_dir / "base-panels.mp4"
    captions = tmp_episode_dir / "speaker-panels.ass"
    output = tmp_episode_dir / "speaker-panels.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=1080x592:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=1080x592:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "color=green:size=1080x592:rate=30:duration=3",
            "-filter_complex",
            "[0:v][1:v][2:v]vstack=inputs=3,format=yuv420p[rows]",
            "-map",
            "[rows]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-y",
            str(podcast),
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
            "color=black:size=1080x1920:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3.05",
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

    crop_config = {
        "speakers": [
            {"label": "Host", "longform_center_x": 400},
            {"label": "Ty", "longform_center_x": 900},
            {"label": "Garrett", "longform_center_x": 1500},
        ]
    }
    diarized = {
        "clock": "source",
        "speaker_map": [
            {"index": 0, "target_speaker": "speaker_0"},
            {"index": 1, "target_speaker": "speaker_1"},
            {"index": 9, "target_speaker": "BOTH"},
        ],
        "utterances": [
            {
                "speaker": 0,
                "words": [{"word": "HOST", "start": 0.1, "end": 0.5, "speaker": 0}],
            },
            {
                "speaker": 1,
                "words": [{"word": "TY", "start": 1.0, "end": 1.4, "speaker": 1}],
            },
            {
                "speaker": 9,
                "words": [
                    {
                        "word": "UNKNOWN",
                        "start": 2.0,
                        "end": 2.4,
                        "speaker": 9,
                    }
                ],
            },
        ],
    }
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    placements, fallback = agent._speaker_panel_placements(
        1920,
        1080,
        crop_config,
        variant_id=SPEAKER_PANELS_VARIANT_ID,
    )
    generate_ass_from_diarized(
        diarized,
        0,
        3,
        captions,
        agent._caption_style({"variant_id": SPEAKER_PANELS_VARIANT_ID}),
        speaker_targets=resolve_caption_speaker_targets(
            diarized, {"clock": "source", "track_mapping": []}, crop_config
        ),
        speaker_placements=placements,
        fallback_placement=fallback,
    )
    agent._compose_speaker_panels_variant(
        podcast,
        base,
        captions,
        output,
        "30/1",
        ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "0"],
    )

    stream = next(
        item
        for item in ffprobe_probe(output)["streams"]
        if item["codec_type"] == "video"
    )
    assert (stream["width"], stream["height"]) == (1080, 1920)
    assert audio_packet_signature(output) == audio_packet_signature(base)
    top = _sample_rgb(ffmpeg, output, 100, 300)
    middle = _sample_rgb(ffmpeg, output, 100, 900)
    bottom = _sample_rgb(ffmpeg, output, 100, 1500)
    assert top[2] > top[0] + 80 and top[2] > top[1] + 80
    assert middle[0] > middle[1] + 80 and middle[0] > middle[2] + 80
    assert bottom[1] > bottom[0] + 40 and bottom[1] > bottom[2] + 40

    def region(timestamp, crop):
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                str(timestamp),
                "-i",
                str(output),
                "-vf",
                f"crop={crop},format=rgb24",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
        )
        return result.stdout

    header_at_host = region(0.3, "1080:72:0:0")
    assert header_at_host == region(1.2, "1080:72:0:0")
    assert header_at_host != region(2.2, "1080:72:0:0")
    assert region(0.3, "1080:592:0:72") != region(1.2, "1080:592:0:72")
    assert region(0.3, "1080:592:0:664") != region(1.2, "1080:592:0:664")
    assert region(0.3, "1080:592:0:1256") == region(1.2, "1080:592:0:1256")


@pytest.mark.parametrize(
    "variant_id", (GAMEPLAY_SURROUND_VARIANT_ID, SPEAKER_PANELS_VARIANT_ID)
)
def test_speaker_panel_render_requires_caption_context_before_writing(
    tmp_episode_dir, sample_config, variant_id
):
    audio = tmp_episode_dir / "audio.wav"
    audio.write_bytes(b"audio")
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)

    with pytest.raises(ValueError, match="valid caption context revision"):
        agent._render_short_unlocked(
            tmp_episode_dir / "source.mp4",
            tmp_episode_dir / "variant.mp4",
            tmp_episode_dir / "captions.ass",
            0,
            1,
            [{"start": 0, "end": 1, "speaker": "speaker_0"}],
            320,
            180,
            "128k",
            {"speakers": [{"center_x": 160, "center_y": 90}]},
            ["-c:v", "libx264"],
            audio_mix_path=audio,
            timeline=Timeline(1, [(0, 1)]),
            diarized={"utterances": []},
            episode={},
            background={"variant_id": variant_id},
        )


def _sample_rgb(ffmpeg: str, path, x: int, y: int) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            "0.4",
            "-i",
            str(path),
            "-vf",
            f"crop=1:1:{x}:{y},format=rgb24",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return tuple(result.stdout[:3])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_gameplay_surround_composition_has_four_panels_and_exact_base_audio(
    tmp_episode_dir, sample_config
):
    ffmpeg = ffmpeg_executable()
    podcast = tmp_episode_dir / "podcast.mp4"
    subway = tmp_episode_dir / "subway.mp4"
    gta = tmp_episode_dir / "gta.mp4"
    minecraft = tmp_episode_dir / "minecraft.mp4"
    base = tmp_episode_dir / "base-surround.mp4"
    captions = tmp_episode_dir / "surround.ass"
    output = tmp_episode_dir / "surround.mp4"
    for path, color, size in (
        (podcast, "yellow", "540x1144"),
        (gta, "red", "160x284"),
        (minecraft, "green", "320x180"),
    ):
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:size={size}:rate=30:duration=1",
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
            "color=blue:size=160x284:rate=30:duration=1",
            "-vf",
            "drawbox=x=50:y=112:w=60:h=60:color=white:t=fill",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(subway),
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
            "color=black:size=1080x1920:rate=30:duration=1",
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
    generate_ass_from_diarized({"utterances": []}, 0, 1, captions)
    assets = [
        {
            "role": "subway",
            "path": subway,
            "fit_mode": CONTAIN_BLUR_FIT_MODE,
        },
        {"role": "gta", "path": gta},
        {"role": "minecraft", "path": minecraft},
    ]
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    agent._compose_gameplay_surround_variant(
        podcast,
        assets,
        base,
        captions,
        output,
        "30/1",
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            "1M",
            "-maxrate",
            "2M",
            "-bufsize",
            "4M",
        ],
    )

    stream = next(
        item
        for item in ffprobe_probe(output)["streams"]
        if item["codec_type"] == "video"
    )
    assert (stream["width"], stream["height"]) == (1080, 1920)
    assert audio_packet_signature(output) == audio_packet_signature(base)

    # A vertical stretch makes the square cover both blue checkpoints.
    subway_marker = _sample_rgb(ffmpeg, output, 135, 608)
    subway_above = _sample_rgb(ffmpeg, output, 135, 500)
    subway_below = _sample_rgb(ffmpeg, output, 135, 716)
    center = _sample_rgb(ffmpeg, output, 540, 600)
    right = _sample_rgb(ffmpeg, output, 980, 600)
    bottom = _sample_rgb(ffmpeg, output, 540, 1500)
    assert min(subway_marker) > 200
    for blue_pixel in (subway_above, subway_below):
        assert blue_pixel[2] > blue_pixel[0] + 80
        assert blue_pixel[2] > blue_pixel[1] + 80
    assert center[0] > 180 and center[1] > 180 and center[2] < 80
    assert right[0] > right[1] + 80 and right[0] > right[2] + 80
    assert bottom[1] > bottom[0] + 40 and bottom[1] > bottom[2] + 40


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


def test_background_three_person_stack_uses_valid_crops_without_base_opt_in(
    tmp_episode_dir, sample_config
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {"center_x": 80, "center_y": 90, "zoom": 1},
            {"center_x": 160, "center_y": 90, "zoom": 1},
            {"center_x": 240, "center_y": 90, "zoom": 1},
        ]
    }

    assert agent._three_person_stack_enabled(
        {}, crop_config, 320, 180, for_background=True
    )
    assert not agent._three_person_stack_enabled(
        {}, crop_config, 320, 180, for_background=False
    )
    assert agent._three_person_stack_enabled(
        {"shorts_three_person_stack": True},
        crop_config,
        320,
        180,
        for_background=False,
    )
    assert "[row0][row1][row2]vstack=inputs=3" in (
        agent._get_background_crop_filter_no_subs(
            "BOTH", 320, 180, crop_config, three_person_stack=True
        )
    )
    assert "[stack1]crop=160:94:80:34" in (
        agent._get_background_crop_filter_no_subs(
            "BOTH", 320, 180, crop_config, three_person_stack=True
        )
    )

    del crop_config["speakers"][1]["center_x"]
    assert not agent._three_person_stack_enabled(
        {}, crop_config, 320, 180, for_background=True
    )


@pytest.mark.parametrize(
    ("source_size", "speaker", "expected"),
    (
        (
            (1920, 1080),
            {
                "longform_center_x": 565,
                "longform_center_y": 570,
                "longform_zoom": 2,
            },
            (565, 480, 284, 324, 404),
        ),
        (
            (3840, 2160),
            {
                "longform_center_x": 2390,
                "longform_center_y": 1157,
                "longform_zoom": 1.2,
            },
            (2390, 1600, 948, 1590, 604),
        ),
        (
            (1920, 1080),
            {
                "longform_center_x": 948,
                "longform_center_y": 330,
                "longform_zoom": 1.4,
            },
            (948, 684, 404, 606, 94),
        ),
    ),
)
def test_background_panel_headroom_preserves_real_pilot_face_regions(
    tmp_episode_dir, sample_config, source_size, speaker, expected
):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    width, height = source_size

    assert (
        agent._get_background_panel_region(
            "speaker_0", width, height, {"speakers": [speaker]}
        )
        == expected
    )


def test_background_panel_headroom_clamps_at_source_top(tmp_episode_dir, sample_config):
    agent = ShortsRenderAgent(tmp_episode_dir, sample_config)
    crop_config = {
        "speakers": [
            {
                "longform_center_x": 160,
                "longform_center_y": 20,
                "longform_zoom": 1,
            }
        ]
    }

    assert (
        agent._get_background_panel_region("speaker_0", 320, 180, crop_config)[-1] == 0
    )


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
