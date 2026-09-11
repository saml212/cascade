import json
from unittest.mock import patch

import numpy as np
import pytest

from agents.audio_analysis import audio_analysis_fingerprint
from agents.speaker_cut import (
    SpeakerCutAgent,
    align_speaker_segments_to_transcript,
    current_speaker_segments,
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


def _write_alignment_transcript(ep_dir, left_words=3, suffix=""):
    def words(speaker, start, count):
        return [
            {
                "word": f"word{index}{suffix}",
                "start": start + index * 0.3,
                "end": start + index * 0.3 + 0.25,
                "speaker": speaker,
                "suspect": False,
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
            {"speaker": 20, "words": words(20, 7.7, 4)},
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


def test_current_segments_rejects_changed_transcript(tmp_episode_dir, sample_config):
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
    align_speaker_segments_to_transcript(tmp_episode_dir)

    assert current_speaker_segments(tmp_episode_dir, episode, sample_config)
    _write_alignment_transcript(tmp_episode_dir, suffix="changed")
    assert current_speaker_segments(tmp_episode_dir, episode, sample_config) is None
