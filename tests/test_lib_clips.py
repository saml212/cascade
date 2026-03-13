"""Tests for lib.clips module."""

import json
import pytest
from pathlib import Path

from lib.clips import normalize_clip, load_clips, save_clips


class TestNormalizeClip:
    def test_start_seconds_to_start(self):
        clip = {"start_seconds": 10.0, "end_seconds": 20.0}
        result = normalize_clip(clip)
        assert result["start"] == 10.0
        assert result["end"] == 20.0

    def test_start_to_start_seconds(self):
        clip = {"start": 10.0, "end": 20.0}
        result = normalize_clip(clip)
        assert result["start_seconds"] == 10.0
        assert result["end_seconds"] == 20.0

    def test_both_present_no_change(self):
        clip = {"start": 10.0, "end": 20.0, "start_seconds": 10.0, "end_seconds": 20.0}
        result = normalize_clip(clip)
        assert result["start"] == 10.0
        assert result["start_seconds"] == 10.0

    def test_idempotent(self):
        clip = {"start_seconds": 5.0, "end_seconds": 15.0}
        first = normalize_clip(clip)
        second = normalize_clip(first)
        assert first == second

    def test_missing_keys_no_error(self):
        clip = {"id": "clip_01", "title": "Test"}
        result = normalize_clip(clip)
        assert "start" not in result
        assert "start_seconds" not in result

    def test_preserves_other_fields(self):
        clip = {"start_seconds": 10.0, "end_seconds": 20.0, "title": "My Clip", "score": 8}
        result = normalize_clip(clip)
        assert result["title"] == "My Clip"
        assert result["score"] == 8

    def test_only_start_seconds_no_end(self):
        """If only start_seconds is present (no end), only start should be filled."""
        clip = {"start_seconds": 10.0}
        result = normalize_clip(clip)
        assert result["start"] == 10.0
        assert "end" not in result
        assert "end_seconds" not in result

    def test_only_start_no_end(self):
        clip = {"start": 10.0}
        result = normalize_clip(clip)
        assert result["start_seconds"] == 10.0
        assert "end" not in result

    def test_zero_values(self):
        clip = {"start_seconds": 0.0, "end_seconds": 0.0}
        result = normalize_clip(clip)
        assert result["start"] == 0.0
        assert result["end"] == 0.0

    def test_float_precision_preserved(self):
        clip = {"start_seconds": 10.123456, "end_seconds": 20.654321}
        result = normalize_clip(clip)
        assert result["start"] == 10.123456
        assert result["end"] == 20.654321

    def test_mixed_sources(self):
        """start from one source, end_seconds from another."""
        clip = {"start": 10.0, "end_seconds": 20.0}
        result = normalize_clip(clip)
        assert result["start_seconds"] == 10.0
        assert result["end"] == 20.0

    def test_mutates_input(self):
        """normalize_clip mutates the input dict (not a copy)."""
        clip = {"start_seconds": 5.0, "end_seconds": 15.0}
        result = normalize_clip(clip)
        assert result is clip  # Same object


class TestLoadSaveClips:
    def test_load_clips_existing(self, tmp_path):
        clips = [{"id": "clip_01", "start_seconds": 10.0, "end_seconds": 20.0}]
        with open(tmp_path / "clips.json", "w") as f:
            json.dump({"clips": clips}, f)
        loaded = load_clips(tmp_path)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "clip_01"
        # Should be normalized
        assert loaded[0]["start"] == 10.0

    def test_load_clips_missing_file(self, tmp_path):
        loaded = load_clips(tmp_path)
        assert loaded == []

    def test_save_and_load_roundtrip(self, tmp_path):
        clips = [
            {"id": "clip_01", "start_seconds": 10.0, "end_seconds": 20.0, "title": "A"},
            {"id": "clip_02", "start_seconds": 30.0, "end_seconds": 40.0, "title": "B"},
        ]
        save_clips(tmp_path, clips)
        loaded = load_clips(tmp_path)
        assert len(loaded) == 2
        assert loaded[0]["title"] == "A"
        assert loaded[1]["title"] == "B"

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        save_clips(nested, [{"id": "clip_01"}])
        assert (nested / "clips.json").exists()

    def test_load_clips_list_format(self, tmp_path):
        """clips.json as a bare list (not wrapped in {"clips": [...]})."""
        clips = [{"id": "clip_01", "start_seconds": 10.0, "end_seconds": 20.0}]
        with open(tmp_path / "clips.json", "w") as f:
            json.dump(clips, f)
        loaded = load_clips(tmp_path)
        assert len(loaded) == 1

    def test_save_wraps_in_clips_key(self, tmp_path):
        save_clips(tmp_path, [{"id": "clip_01"}])
        with open(tmp_path / "clips.json") as f:
            data = json.load(f)
        assert "clips" in data

    def test_load_empty_clips_list(self, tmp_path):
        with open(tmp_path / "clips.json", "w") as f:
            json.dump({"clips": []}, f)
        loaded = load_clips(tmp_path)
        assert loaded == []
