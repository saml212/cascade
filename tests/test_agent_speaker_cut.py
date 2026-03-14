"""Tests for the speaker cut agent."""

import json
import numpy as np
import pytest
from unittest.mock import patch

from agents.speaker_cut import SpeakerCutAgent


def _write(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def _agent(ep_dir, cfg, identical=False, sr=1000, dur=20.0):
    _write(ep_dir / "audio_analysis.json",
           {"audio_channels_identical": identical, "channels": 2, "sample_rate": sr})
    _write(ep_dir / "stitch.json", {"duration_seconds": dur})
    if not (ep_dir / "episode.json").exists():
        _write(ep_dir / "episode.json", {})
    return SpeakerCutAgent(ep_dir, cfg)


def _tracks(n_speakers, n_samples, active_ranges):
    """Synthetic tracks: low noise floor with loud speech in active_ranges."""
    out = []
    for i in range(n_speakers):
        rng = np.random.RandomState(i + 100)
        data = rng.normal(0, 5, n_samples).astype(np.float32)
        for s_frac, e_frac in (active_ranges[i] if i < len(active_ranges) else []):
            s, e = int(s_frac * n_samples), int(e_frac * n_samples)
            data[s:e] = rng.normal(0, 5000, e - s).astype(np.float32)
        out.append(data)
    return out


class TestIdenticalChannels:
    def test_single_both_segment(self, tmp_episode_dir, sample_config):
        result = _agent(tmp_episode_dir, sample_config, identical=True, dur=60.0).execute()
        assert result["segment_count"] == 1
        seg = result["segments"][0]
        assert seg["speaker"] == "BOTH" and seg["start"] == 0.0 and seg["end"] == 60.0


@pytest.mark.parametrize("active,expected_in,expected_not_in", [
    ([[(0.1, 0.5)], []], {"speaker_0"}, {"speaker_1"}),
    ([[(0.2, 0.8)], [(0.2, 0.8)]], {"BOTH"}, set()),
])
def test_classification(tmp_episode_dir, sample_config, active, expected_in, expected_not_in):
    tracks = _tracks(2, 20000, active)
    _write(tmp_episode_dir / "episode.json", {})
    agent = _agent(tmp_episode_dir, sample_config)
    with patch.object(agent, "_load_tracks", return_value=(tracks, "lr")):
        result = agent.execute()
    speakers = {s["speaker"] for s in result["segments"]}
    assert expected_in <= speakers
    assert not (expected_not_in & speakers)


def test_three_speakers(tmp_episode_dir, sample_config):
    tracks = _tracks(3, 960000, [[(0.05, 0.30)], [(0.35, 0.60)], [(0.65, 0.95)]])
    _write(tmp_episode_dir / "episode.json", {})
    agent = _agent(tmp_episode_dir, sample_config, dur=60.0)
    with patch.object(agent, "_load_tracks", return_value=(tracks, "n_speaker")):
        result = agent.execute()
    assert {s["speaker"] for s in result["segments"]} >= {"speaker_0", "speaker_1", "speaker_2"}


def test_hysteresis_suppresses_blip(tmp_episode_dir, sample_config):
    """A 200ms blip from speaker_1 mid speaker_0 should not cause a switch."""
    n = 20000
    rng = np.random.RandomState(42)
    tracks = [rng.normal(0, 5, n).astype(np.float32) for _ in range(2)]
    tracks[0][int(0.1*n):int(0.9*n)] = rng.normal(0, 5000, int(0.8*n)).astype(np.float32)
    tracks[1][int(0.5*n):int(0.5*n)+200] = rng.normal(0, 8000, 200).astype(np.float32)
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
    tracks[0][int(0.1*n):int(0.9*n)] = rng.normal(0, 5000, int(0.8*n)).astype(np.float32)
    tracks[1][int(0.5*n):int(0.5*n)+100] = rng.normal(0, 10000, 100).astype(np.float32)
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
