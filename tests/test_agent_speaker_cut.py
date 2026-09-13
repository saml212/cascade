import json
from unittest.mock import patch

import numpy as np
import pytest

from agents.audio_analysis import audio_analysis_fingerprint
from agents.speaker_cut import (
    SpeakerCutAgent,
    _corrected_ownership_intervals,
    _hold_same_speaker_wide_gaps,
    align_speaker_segments_to_transcript,
    current_speaker_segments,
    rebind_visual_crop_segments,
    speaker_cut_fingerprint,
    strict_bool,
    transcript_alignment_fingerprint,
    validate_speaker_crops,
)


def _write(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def _agent(ep_dir, cfg, identical=False, sr=1000, dur=20.0):
    if not (ep_dir / "episode.json").exists():
        _write(ep_dir / "episode.json", {})
    episode = json.loads((ep_dir / "episode.json").read_text())
    (ep_dir / "source_merged.mp4").write_bytes(b"source")
    _write(
        ep_dir / "audio_analysis.json",
        {
            "audio_channels_identical": identical,
            "channels": 2,
            "sample_rate": sr,
            "extracted_sample_rate": 1000,
            "fingerprint": audio_analysis_fingerprint(ep_dir, episode, cfg),
        },
    )
    _write(ep_dir / "stitch.json", {"duration_seconds": dur})
    return SpeakerCutAgent(ep_dir, cfg)


def _tracks(n_speakers, n_samples, active_ranges):
    """Synthetic tracks: low noise floor with loud speech in active_ranges."""
    out = []
    for i in range(n_speakers):
        rng = np.random.RandomState(i + 100)
        data = rng.normal(0, 5, n_samples).astype(np.float32)
        for s_frac, e_frac in active_ranges[i] if i < len(active_ranges) else []:
            s, e = int(s_frac * n_samples), int(e_frac * n_samples)
            data[s:e] = rng.normal(0, 5000, e - s).astype(np.float32)
        out.append(data)
    return out


def test_visual_crop_rebind_preserves_verified_segments_and_rms(
    tmp_episode_dir, sample_config
):
    old_episode = {
        "source_properties": {"width": 1920, "height": 1080},
        "crop_config": {
            "speakers": [
                {"label": "Host", "track": 1, "center_x": 400, "center_y": 500},
                {"label": "Guest", "track": 2, "center_x": 1400, "center_y": 500},
            ]
        },
    }
    new_episode = json.loads(json.dumps(old_episode))
    new_episode["crop_config"]["speakers"][0]["longform_center_y"] = 600
    current = {
        "clock": "source",
        "algorithm_version": "source-clock-v3",
        "fingerprint": "old-fingerprint",
        "segments": [{"start": 0, "end": 2, "speaker": "speaker_0"}],
        "crop_validation": {"distinct": True},
    }
    _write(tmp_episode_dir / "audio_analysis.json", {"fingerprint": "analysis"})
    work = tmp_episode_dir / "work"
    work.mkdir(exist_ok=True)
    _write(work / "rms_meta.json", {"fingerprint": "old-fingerprint"})

    with (
        patch("agents.speaker_cut.current_speaker_segments", return_value=current),
        patch(
            "agents.speaker_cut.speaker_cut_fingerprint",
            return_value="new-fingerprint",
        ),
    ):
        rebound = rebind_visual_crop_segments(
            tmp_episode_dir, old_episode, new_episode, sample_config
        )

    assert rebound["segments"] == current["segments"]
    assert rebound["fingerprint"] == "new-fingerprint"
    assert rebound["crop_validation"]["distinct"] is True
    assert json.loads((work / "rms_meta.json").read_text())["fingerprint"] == (
        "new-fingerprint"
    )


def test_visual_crop_rebind_rejects_speaker_assignment_change(
    tmp_episode_dir, sample_config
):
    old_episode = {"crop_config": {"speakers": [{"label": "Host", "track": 1}]}}
    new_episode = {"crop_config": {"speakers": [{"label": "Host", "track": 2}]}}

    assert (
        rebind_visual_crop_segments(
            tmp_episode_dir, old_episode, new_episode, sample_config
        )
        is None
    )


class TestIdenticalChannels:
    def test_single_both_segment(self, tmp_episode_dir, sample_config):
        result = _agent(
            tmp_episode_dir, sample_config, identical=True, dur=60.0
        ).execute()
        assert result["segment_count"] == 1
        seg = result["segments"][0]
        assert seg["speaker"] == "BOTH" and seg["start"] == 0.0 and seg["end"] == 60.0


@pytest.mark.parametrize(
    "active,expected_in,expected_not_in",
    [
        ([[(0.1, 0.5)], []], {"speaker_0"}, {"speaker_1"}),
        ([[(0.2, 0.8)], [(0.2, 0.8)]], {"BOTH"}, set()),
    ],
)
def test_classification(
    tmp_episode_dir, sample_config, active, expected_in, expected_not_in
):
    tracks = _tracks(2, 20000, active)
    _write(tmp_episode_dir / "episode.json", {})
    agent = _agent(tmp_episode_dir, sample_config)
    with patch.object(agent, "_load_tracks", return_value=(tracks, "lr")):
        result = agent.execute()
    speakers = {s["speaker"] for s in result["segments"]}
    assert expected_in <= speakers
    assert not (expected_not_in & speakers)


def test_three_speakers(tmp_episode_dir, sample_config):
    tracks = _tracks(3, 60000, [[(0.05, 0.30)], [(0.35, 0.60)], [(0.65, 0.95)]])
    _write(tmp_episode_dir / "episode.json", {})
    agent = _agent(tmp_episode_dir, sample_config, dur=60.0)
    with patch.object(agent, "_load_tracks", return_value=(tracks, "n_speaker")):
        result = agent.execute()
    assert {s["speaker"] for s in result["segments"]} >= {
        "speaker_0",
        "speaker_1",
        "speaker_2",
    }


def test_hysteresis_suppresses_blip(tmp_episode_dir, sample_config):
    """A 200ms blip from speaker_1 mid speaker_0 should not cause a switch."""
    n = 20000
    rng = np.random.RandomState(42)
    tracks = [rng.normal(0, 5, n).astype(np.float32) for _ in range(2)]
    tracks[0][int(0.1 * n) : int(0.9 * n)] = rng.normal(0, 5000, int(0.8 * n)).astype(
        np.float32
    )
    tracks[1][int(0.5 * n) : int(0.5 * n) + 200] = rng.normal(0, 8000, 200).astype(
        np.float32
    )
    _write(tmp_episode_dir / "episode.json", {})
    agent = _agent(tmp_episode_dir, sample_config)
    with patch.object(agent, "_load_tracks", return_value=(tracks, "lr")):
        result = agent.execute()
    assert all(s["speaker"] != "speaker_1" for s in result["segments"])


def test_smoothing_filters_spike(tmp_episode_dir, sample_config):
    """A 100ms spike should not survive smoothing + hysteresis."""
    n = 20000
    rng = np.random.RandomState(0)
    tracks = [rng.normal(0, 5, n).astype(np.float32) for _ in range(2)]
    tracks[0][int(0.1 * n) : int(0.9 * n)] = rng.normal(0, 5000, int(0.8 * n)).astype(
        np.float32
    )
    tracks[1][int(0.5 * n) : int(0.5 * n) + 100] = rng.normal(0, 10000, 100).astype(
        np.float32
    )
    _write(tmp_episode_dir / "episode.json", {})
    agent = _agent(tmp_episode_dir, sample_config)
    with patch.object(agent, "_load_tracks", return_value=(tracks, "lr")):
        result = agent.execute()
    assert all(s["speaker"] != "speaker_1" for s in result["segments"])


def test_absorb_and_merge(tmp_episode_dir, sample_config):
    """Short segments get absorbed, then consecutive same-speaker segments merge."""
    agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
    labels = ["speaker_0"] * 50 + ["speaker_1"] * 3 + ["speaker_0"] * 47
    segs = agent._finalize_segments(labels, 0.1, 100, 2.0)
    assert len(segs) == 1
    assert segs[0]["speaker"] == "speaker_0" and segs[0]["end"] == 10.0


def test_long_segments_kept(tmp_episode_dir, sample_config):
    agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
    labels = ["speaker_0"] * 50 + ["speaker_1"] * 50
    segs = agent._finalize_segments(labels, 0.1, 100, 2.0)
    assert len(segs) == 2


def test_segments_json_saved_with_fields(tmp_episode_dir, sample_config):
    result = _agent(tmp_episode_dir, sample_config, identical=True).execute()
    assert (tmp_episode_dir / "segments.json").exists()
    for seg in result["segments"]:
        assert all(k in seg for k in ("start", "end", "speaker", "duration"))


def test_episode_gap_hold_preserves_default_fingerprint_and_requires_rerun(
    tmp_episode_dir, sample_config
):
    episode = {
        "source_properties": {"width": 1920, "height": 1080},
        "crop_config": {
            "speakers": [
                {"label": "Host", "center_x": 400, "center_y": 500, "zoom": 1.2},
                {
                    "label": "Guest",
                    "center_x": 1400,
                    "center_y": 500,
                    "zoom": 1.2,
                },
            ]
        },
    }
    _write(tmp_episode_dir / "episode.json", episode)
    first = _agent(tmp_episode_dir, sample_config, identical=True).execute()
    audio_analysis = json.loads((tmp_episode_dir / "audio_analysis.json").read_text())
    absent = speaker_cut_fingerprint(
        tmp_episode_dir, episode, audio_analysis, sample_config
    )
    zero = speaker_cut_fingerprint(
        tmp_episode_dir,
        {**episode, "speaker_cut_config": {"same_speaker_gap_hold_seconds": 0}},
        audio_analysis,
        sample_config,
    )
    assert absent == zero == first["fingerprint"]
    assert current_speaker_segments(tmp_episode_dir, episode, sample_config) is not None

    opted_in = {
        **episode,
        "speaker_cut_config": {"same_speaker_gap_hold_seconds": 1.0},
    }
    _write(tmp_episode_dir / "episode.json", opted_in)
    assert current_speaker_segments(tmp_episode_dir, opted_in, sample_config) is None

    rerun = _agent(tmp_episode_dir, sample_config, identical=True).execute()
    assert rerun["same_speaker_gap_hold_seconds"] == 1.0
    assert rerun["fingerprint"] != absent
    assert (
        current_speaker_segments(tmp_episode_dir, opted_in, sample_config) is not None
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("True", True), ("False", False), ("junk", False)],
)
def test_strict_bool_does_not_treat_false_strings_as_true(value, expected):
    assert strict_bool(value) is expected


def test_assigned_recorder_tracks_override_legacy_identical_camera_flag(
    tmp_episode_dir, sample_config
):
    tracks = []
    for session in ("a", "b"):
        for number in (1, 2):
            path = tmp_episode_dir / f"{session}_Tr{number}.wav"
            path.write_bytes(b"track")
            tracks.append(
                {
                    "filename": path.name,
                    "dest_path": str(path),
                    "track_number": number,
                    "track_type": "input",
                }
            )
    episode = {
        "crop_config": {
            "speakers": [
                {"label": "Host", "track": 1},
                {"label": "Guest", "track": 2},
            ]
        },
        "audio_tracks": tracks,
    }
    _write(tmp_episode_dir / "episode.json", episode)
    agent = _agent(tmp_episode_dir, sample_config, identical="True")
    synthetic = _tracks(2, 20000, [[(0.1, 0.5)], [(0.55, 0.9)]])

    with patch.object(agent, "_load_recorder_tracks", return_value=synthetic):
        result = agent.execute()

    assert result["mode"] == "n_speaker"
    assert [item["source_files"] for item in result["track_mapping"]] == [
        ["a_Tr1.wav", "b_Tr1.wav"],
        ["a_Tr2.wav", "b_Tr2.wav"],
    ]
    assert {segment["speaker"] for segment in result["segments"]} != {"BOTH"}


def test_recorder_extraction_concatenates_sessions_before_sync(
    tmp_episode_dir, sample_config
):
    agent = SpeakerCutAgent(tmp_episode_dir, sample_config)
    paths = [tmp_episode_dir / "first.wav", tmp_episode_dir / "second.wav"]
    completed = type(
        "Result",
        (),
        {
            "returncode": 0,
            "stdout": np.arange(2000, dtype=np.int16).tobytes(),
            "stderr": b"",
        },
    )()

    with patch("agents.speaker_cut.subprocess.run", return_value=completed) as run:
        data = agent._extract_track(paths, 1.25, 0.9999, 2.0)

    cmd = run.call_args.args[0]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert str(paths[0]) in cmd and str(paths[1]) in cmd
    assert "[p0][p1]concat=n=2:v=0:a=1[joined]" in graph
    assert graph.index("concat=n=2") < graph.index("atrim=start=1.25")
    assert "atempo=0.99990000" in graph
    assert len(data) == 2000


def test_crop_validation_rejects_nearly_identical_longform_rectangles():
    episode = {
        "source_properties": {"width": 1920, "height": 1080},
        "crop_config": {
            "speakers": [
                {
                    "label": "Host",
                    "center_x": 500,
                    "center_y": 500,
                    "longform_center_x": 904,
                    "longform_center_y": 343,
                    "longform_zoom": 0.75,
                },
                {
                    "label": "Guest",
                    "center_x": 1000,
                    "center_y": 500,
                    "longform_center_x": 913,
                    "longform_center_y": 334,
                    "longform_zoom": 0.75,
                },
            ]
        },
    }

    assert validate_speaker_crops(episode)["status"] == "fail"

    episode["crop_config"]["speakers"][0].update(
        {"longform_center_x": 570, "longform_center_y": 339, "longform_zoom": 1.2}
    )
    episode["crop_config"]["speakers"][1].update(
        {"longform_center_x": 1312, "longform_center_y": 380, "longform_zoom": 1.2}
    )
    assert validate_speaker_crops(episode)["status"] == "pass"


def test_crop_validation_uses_dimensions_saved_with_crop_config():
    episode = {
        "crop_config": {
            "source_width": 1920,
            "source_height": 1080,
            "speakers": [
                {
                    "label": "Left",
                    "center_x": 710,
                    "center_y": 630,
                    "longform_center_x": 680,
                    "longform_center_y": 560,
                    "longform_zoom": 1.5,
                },
                {
                    "label": "Center",
                    "center_x": 960,
                    "center_y": 630,
                    "longform_center_x": 1020,
                    "longform_center_y": 560,
                    "longform_zoom": 1.5,
                },
                {
                    "label": "Right",
                    "center_x": 1370,
                    "center_y": 635,
                    "longform_center_x": 1370,
                    "longform_center_y": 545,
                    "longform_zoom": 1.25,
                },
            ],
        }
    }

    result = validate_speaker_crops(episode)

    assert result["status"] == "pass"
    assert result["distinct"] is True
    assert len(result["rectangles"]) == 3


def test_current_segments_invalidates_when_crop_changes(tmp_episode_dir, sample_config):
    episode = {
        "source_properties": {"width": 1920, "height": 1080},
        "crop_config": {
            "speakers": [
                {
                    "label": "Host",
                    "center_x": 500,
                    "center_y": 500,
                    "longform_center_x": 400,
                    "longform_zoom": 1.2,
                },
                {
                    "label": "Guest",
                    "center_x": 1400,
                    "center_y": 500,
                    "longform_center_x": 1400,
                    "longform_zoom": 1.2,
                },
            ]
        },
    }
    _write(tmp_episode_dir / "episode.json", episode)
    with patch.object(
        SpeakerCutAgent,
        "_load_tracks",
        return_value=(_tracks(2, 20000, [[(0.1, 0.5)], [(0.5, 0.9)]]), "lr"),
    ):
        result = _agent(tmp_episode_dir, sample_config).execute()

    assert result["crop_validation"]["distinct"] is True
    assert current_speaker_segments(tmp_episode_dir, episode, sample_config)
    episode["crop_config"]["speakers"][0]["longform_center_x"] = 1000
    assert current_speaker_segments(tmp_episode_dir, episode, sample_config) is None


def _write_alignment_transcript(
    ep_dir,
    left_words=3,
    right_words=4,
    right_start=7.7,
    suffix="",
    corrected=False,
):
    def words(speaker, start, count):
        return [
            {
                "word": f"word{index}{suffix}",
                "start": start + index * 0.3,
                "end": start + index * 0.3 + 0.25,
                "speaker": speaker,
                "suspect": False,
                "corrected": corrected,
                "correction_id": "reviewed-window" if corrected else None,
            }
            for index in range(count)
        ]

    transcript = {
        "clock": "source",
        "speaker_map": [
            {"index": 10, "logical_track": 1},
            {"index": 20, "logical_track": 2},
        ],
        "utterances": [
            {"speaker": 10, "words": words(10, 7.0, left_words)},
            {"speaker": 20, "words": words(20, right_start, right_words)},
        ],
    }
    _write(ep_dir / "diarized_transcript.json", transcript)
    _write(ep_dir / "transcript_provenance.json", {"clock": "source"})


def test_transcript_alignment_moves_sustained_speaker_handoff(tmp_episode_dir):
    _write(
        tmp_episode_dir / "segments.json",
        {
            "clock": "source",
            "fingerprint": "microphone-analysis",
            "track_mapping": [
                {"speaker": "speaker_0", "logical_track": 1},
                {"speaker": "speaker_1", "logical_track": 2},
            ],
            "segments": [
                {"speaker": "speaker_0", "start": 0.0, "end": 10.0},
                {"speaker": "speaker_1", "start": 10.0, "end": 20.0},
            ],
        },
    )
    _write_alignment_transcript(tmp_episode_dir)

    result = align_speaker_segments_to_transcript(tmp_episode_dir)

    assert result is not None
    assert result["segments"][0]["end"] == 7.775
    assert result["segments"][1]["start"] == 7.775
    alignment = result["transcript_alignment"]
    assert alignment["adjustment_count"] == 1
    assert alignment["adjustments"][0]["shift_seconds"] == -2.225
    assert alignment["fingerprint"] == transcript_alignment_fingerprint(
        tmp_episode_dir, result
    )

    repeated = align_speaker_segments_to_transcript(tmp_episode_dir)
    assert repeated == result

    _write_alignment_transcript(tmp_episode_dir, suffix="changed")
    realigned = align_speaker_segments_to_transcript(tmp_episode_dir)
    assert realigned["segments"][0]["end"] == 7.775
    assert realigned["transcript_alignment"]["adjustment_count"] == 1
    assert realigned["transcript_alignment"]["base_segments"][0]["end"] == 10.0


def test_transcript_alignment_ignores_one_word_reaction(tmp_episode_dir):
    _write(
        tmp_episode_dir / "segments.json",
        {
            "clock": "source",
            "fingerprint": "microphone-analysis",
            "track_mapping": [
                {"speaker": "speaker_0", "logical_track": 1},
                {"speaker": "speaker_1", "logical_track": 2},
            ],
            "segments": [
                {"speaker": "speaker_0", "start": 0.0, "end": 10.0},
                {"speaker": "speaker_1", "start": 10.0, "end": 20.0},
            ],
        },
    )
    _write_alignment_transcript(tmp_episode_dir, left_words=1)

    result = align_speaker_segments_to_transcript(tmp_episode_dir)

    assert result is not None
    assert result["segments"][0]["end"] == 10.0
    assert result["segments"][1]["start"] == 10.0
    assert result["transcript_alignment"]["adjustment_count"] == 0


def test_reviewed_turn_resolves_lagging_speaker_and_ambiguous_segments(
    tmp_episode_dir,
):
    _write(
        tmp_episode_dir / "segments.json",
        {
            "clock": "source",
            "fingerprint": "microphone-analysis",
            "track_mapping": [
                {"speaker": "speaker_0", "logical_track": 1},
                {"speaker": "speaker_1", "logical_track": 2},
            ],
            "segments": [
                {"speaker": "speaker_0", "start": 0.0, "end": 10.0},
                {"speaker": "speaker_1", "start": 10.0, "end": 12.0},
                {"speaker": "BOTH", "start": 12.0, "end": 16.0},
                {"speaker": "speaker_1", "start": 16.0, "end": 20.0},
            ],
        },
    )

    _write_alignment_transcript(
        tmp_episode_dir,
        left_words=0,
        right_words=37,
        right_start=7.0,
        corrected=True,
    )
    result = align_speaker_segments_to_transcript(tmp_episode_dir)

    assert [
        (segment["speaker"], segment["start"], segment["end"])
        for segment in result["segments"]
    ] == [("speaker_0", 0.0, 7.0), ("speaker_1", 7.0, 20.0)]
    alignment = result["transcript_alignment"]
    assert alignment["version"] == "source-clock-v2"
    assert alignment["adjustment_count"] == 2
    assert {item["from_speaker"] for item in alignment["adjustments"]} == {
        "speaker_0",
        "BOTH",
    }

    _write_alignment_transcript(
        tmp_episode_dir,
        left_words=0,
        right_words=37,
        right_start=8.0,
        suffix="changed",
        corrected=True,
    )
    realigned = align_speaker_segments_to_transcript(tmp_episode_dir)

    assert [
        (segment["speaker"], segment["start"], segment["end"])
        for segment in realigned["segments"]
    ] == [("speaker_0", 0.0, 8.0), ("speaker_1", 8.0, 20.0)]
    assert len(realigned["transcript_alignment"]["base_segments"]) == 4


def test_unreviewed_one_sided_turn_does_not_override_microphone_segments(
    tmp_episode_dir,
):
    original = [
        {"speaker": "speaker_0", "start": 0.0, "end": 10.0},
        {"speaker": "BOTH", "start": 10.0, "end": 14.0},
    ]
    _write(
        tmp_episode_dir / "segments.json",
        {
            "clock": "source",
            "fingerprint": "microphone-analysis",
            "track_mapping": [
                {"speaker": "speaker_0", "logical_track": 1},
                {"speaker": "speaker_1", "logical_track": 2},
            ],
            "segments": original,
        },
    )
    _write_alignment_transcript(
        tmp_episode_dir,
        left_words=0,
        right_words=37,
        right_start=7.0,
    )

    result = align_speaker_segments_to_transcript(tmp_episode_dir)

    assert result["segments"] == original
    assert result["transcript_alignment"]["adjustment_count"] == 0


def test_reviewed_overlap_does_not_assert_competing_speaker_ownership():
    turns = [
        {
            "speaker": "speaker_0",
            "start": 1.0,
            "end": 5.0,
            "fully_corrected": True,
            "correction_ids": ["host-review"],
        },
        {
            "speaker": "speaker_1",
            "start": 3.0,
            "end": 7.0,
            "fully_corrected": True,
            "correction_ids": ["guest-review"],
        },
    ]

    assert _corrected_ownership_intervals(turns) == [
        {
            "speaker": "speaker_0",
            "start": 1.0,
            "end": 3.0,
            "correction_ids": ["host-review"],
        },
        {
            "speaker": "speaker_1",
            "start": 5.0,
            "end": 7.0,
            "correction_ids": ["guest-review"],
        },
    ]


def test_same_speaker_gap_hold_keeps_competing_words_and_long_wides():
    decisions = [
        {"speaker": "speaker_0", "start": 0.0, "end": 2.0, "duration": 2.0},
        {"speaker": "BOTH", "start": 2.0, "end": 2.8, "duration": 0.8},
        {"speaker": "speaker_0", "start": 2.8, "end": 4.0, "duration": 1.2},
        {"speaker": "BOTH", "start": 4.0, "end": 4.8, "duration": 0.8},
        {"speaker": "speaker_0", "start": 4.8, "end": 6.0, "duration": 1.2},
        {"speaker": "BOTH", "start": 6.0, "end": 7.2, "duration": 1.2},
        {"speaker": "speaker_0", "start": 7.2, "end": 9.0, "duration": 1.8},
        {"speaker": "BOTH", "start": 9.0, "end": 9.8, "duration": 0.8},
        {"speaker": "speaker_0", "start": 9.8, "end": 11.0, "duration": 1.2},
    ]
    segment_document = {
        "track_mapping": [
            {"speaker": "speaker_0", "person": "Host"},
            {"speaker": "speaker_1", "person": "Guest"},
        ]
    }
    transcript = {
        "speaker_map": [
            {
                "index": 0,
                "target_speaker": "speaker_0",
                "mapping_method": "manual_review",
            },
            {
                "index": 1,
                "target_speaker": "speaker_1",
                "mapping_method": "manual_review",
            },
        ],
        "utterances": [
            {
                "speaker": 0,
                "words": [
                    {
                        "speaker": 0,
                        "start": 2.2,
                        "end": 2.5,
                        "suspect": False,
                    }
                ],
            },
            {
                "speaker": 1,
                "words": [
                    {
                        "speaker": 1,
                        "start": 4.2,
                        "end": 4.5,
                        "suspect": False,
                    }
                ],
            },
            {
                "speaker": 2,
                "words": [
                    {
                        "speaker": 2,
                        "start": 9.2,
                        "end": 9.5,
                        "suspect": False,
                    }
                ],
            },
        ],
    }

    smoothed, adjustments = _hold_same_speaker_wide_gaps(
        decisions, transcript, segment_document, 1.0
    )

    assert [(item["speaker"], item["start"], item["end"]) for item in smoothed] == [
        ("speaker_0", 0.0, 4.0),
        ("BOTH", 4.0, 4.8),
        ("speaker_0", 4.8, 6.0),
        ("BOTH", 6.0, 7.2),
        ("speaker_0", 7.2, 9.0),
        ("BOTH", 9.0, 9.8),
        ("speaker_0", 9.8, 11.0),
    ]
    assert adjustments == [
        {
            "kind": "same_speaker_gap_hold",
            "from_speaker": "BOTH",
            "to_speaker": "speaker_0",
            "start": 2.0,
            "end": 2.8,
            "maximum_seconds": 1.0,
            "reliable_word_count": 1,
        }
    ]


def test_explicit_speaker_bindings_add_third_crop_and_wide_unresolved_turn(
    tmp_episode_dir,
):
    _write(
        tmp_episode_dir / "segments.json",
        {
            "clock": "source",
            "fingerprint": "camera-analysis",
            "track_mapping": [
                {"speaker": "speaker_0", "person": "Laura"},
                {"speaker": "speaker_1", "person": "Todd"},
                {"speaker": "speaker_2", "person": "Sam"},
            ],
            "segments": [{"speaker": "speaker_0", "start": 0.0, "end": 20.0}],
        },
    )

    def utterance(speaker, start):
        return {
            "speaker": speaker,
            "words": [
                {
                    "word": f"word{index}",
                    "start": start + index,
                    "end": start + index + 0.8,
                    "speaker": speaker,
                    "suspect": False,
                }
                for index in range(3)
            ],
        }

    _write(
        tmp_episode_dir / "diarized_transcript.json",
        {
            "clock": "source",
            "speaker_map": [
                {
                    "index": 0,
                    "target_speaker": "speaker_1",
                    "mapping_method": "manual_review",
                },
                {
                    "index": 2,
                    "target_speaker": "speaker_2",
                    "mapping_method": "manual_review",
                },
                {
                    "index": 3,
                    "target_speaker": "BOTH",
                    "mapping_method": "manual_review",
                },
            ],
            "utterances": [
                utterance(0, 2.0),
                utterance(2, 6.0),
                utterance(3, 10.0),
            ],
        },
    )
    _write(tmp_episode_dir / "transcript_provenance.json", {"clock": "source"})

    result = align_speaker_segments_to_transcript(tmp_episode_dir)

    ownership = [
        (segment["speaker"], segment["start"], segment["end"])
        for segment in result["segments"]
        if segment["speaker"] != "speaker_0"
    ]
    assert ownership == [
        ("speaker_1", 2.0, 4.8),
        ("speaker_2", 6.0, 8.8),
        ("BOTH", 10.0, 12.8),
    ]
    assert {
        adjustment["to_speaker"]
        for adjustment in result["transcript_alignment"]["adjustments"]
    } == {"speaker_1", "speaker_2", "BOTH"}


@pytest.mark.parametrize("alignment_version", ["source-clock-v1", "source-clock-v2"])
def test_current_segments_rejects_changed_transcript(
    tmp_episode_dir, sample_config, alignment_version
):
    episode = {
        "source_properties": {"width": 1920, "height": 1080},
        "crop_config": {
            "speakers": [
                {
                    "label": "Host",
                    "longform_center_x": 400,
                    "longform_center_y": 500,
                    "longform_zoom": 1.2,
                },
                {
                    "label": "Guest",
                    "longform_center_x": 1400,
                    "longform_center_y": 500,
                    "longform_zoom": 1.2,
                },
            ]
        },
    }
    _write(tmp_episode_dir / "episode.json", episode)
    with patch.object(
        SpeakerCutAgent,
        "_load_tracks",
        return_value=(_tracks(2, 20000, [[(0.1, 0.5)], [(0.5, 0.9)]]), "lr"),
    ):
        _agent(tmp_episode_dir, sample_config).execute()
    _write_alignment_transcript(tmp_episode_dir)
    if alignment_version == "source-clock-v2":
        align_speaker_segments_to_transcript(tmp_episode_dir)
    else:
        segments_path = tmp_episode_dir / "segments.json"
        segments = json.loads(segments_path.read_text())
        segments["transcript_alignment"] = {
            "version": alignment_version,
            "fingerprint": transcript_alignment_fingerprint(
                tmp_episode_dir, segments, version=alignment_version
            ),
        }
        _write(segments_path, segments)

    assert current_speaker_segments(tmp_episode_dir, episode, sample_config)
    _write_alignment_transcript(tmp_episode_dir, suffix="changed")
    assert current_speaker_segments(tmp_episode_dir, episode, sample_config) is None
