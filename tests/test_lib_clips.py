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
