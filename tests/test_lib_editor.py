"""Tests for lib.editor — edit list management."""

import json

import pytest

from lib.editor import (
    add_cut,
    add_trim_start,
    add_trim_end,
    clear_edits,
    list_edits,
    load_edits,
    remove_edit,
    save_edits,
    total_time_removed,
    find_and_propose_cut,
)


@pytest.fixture
def episode_dir(tmp_path):
    """Create a minimal episode directory with episode.json."""
    ep_file = tmp_path / "episode.json"
    ep_file.write_text(json.dumps({
        "episode_id": "test_ep",
        "duration_seconds": 3600.0,
    }))
    return tmp_path


class TestEditList:
    def test_load_empty(self, episode_dir):
        assert load_edits(episode_dir) == []

    def test_add_cut(self, episode_dir):
        edit = add_cut(episode_dir, 100.0, 150.0, reason="test cut")
        assert edit["type"] == "cut"
        assert edit["start_seconds"] == 100.0
        assert edit["end_seconds"] == 150.0
        assert edit["duration_removed"] == 50.0
        assert edit["reason"] == "test cut"

    def test_load_after_save(self, episode_dir):
        add_cut(episode_dir, 100.0, 150.0)
        edits = load_edits(episode_dir)
        assert len(edits) == 1
        assert edits[0]["type"] == "cut"

    def test_add_invalid_cut(self, episode_dir):
        with pytest.raises(ValueError):
            add_cut(episode_dir, 200.0, 100.0)  # end before start

    def test_add_trim_start_replaces(self, episode_dir):
        add_trim_start(episode_dir, 30.0)
        add_trim_start(episode_dir, 45.0)
        edits = load_edits(episode_dir)
        trims = [e for e in edits if e["type"] == "trim_start"]
        assert len(trims) == 1
        assert trims[0]["seconds"] == 45.0

    def test_add_trim_end_replaces(self, episode_dir):
        add_trim_end(episode_dir, 3500.0)
        add_trim_end(episode_dir, 3550.0)
        edits = load_edits(episode_dir)
        trims = [e for e in edits if e["type"] == "trim_end"]
        assert len(trims) == 1
        assert trims[0]["seconds"] == 3550.0

    def test_remove_edit(self, episode_dir):
        add_cut(episode_dir, 100.0, 150.0)
        add_cut(episode_dir, 200.0, 250.0)
        removed = remove_edit(episode_dir, 0)
        assert removed["start_seconds"] == 100.0
        assert len(load_edits(episode_dir)) == 1

    def test_remove_invalid_index(self, episode_dir):
        add_cut(episode_dir, 100.0, 150.0)
        assert remove_edit(episode_dir, 99) is None

    def test_clear_edits(self, episode_dir):
        add_cut(episode_dir, 100.0, 150.0)
        add_cut(episode_dir, 200.0, 250.0)
        n = clear_edits(episode_dir)
        assert n == 2
        assert load_edits(episode_dir) == []

    def test_total_time_removed(self):
        edits = [
            {"type": "cut", "start_seconds": 100, "end_seconds": 150, "duration_removed": 50},
            {"type": "cut", "start_seconds": 200, "end_seconds": 230, "duration_removed": 30},
            {"type": "trim_start", "seconds": 10},
        ]
        assert total_time_removed(edits) == 80


class TestFindAndProposeCut:
    def test_no_transcript(self, episode_dir):
        with pytest.raises(FileNotFoundError):
            find_and_propose_cut(episode_dir, "anything")

    def test_finds_phrase(self, tmp_path):
        ep = tmp_path / "ep"
        ep.mkdir()
        (ep / "episode.json").write_text('{"episode_id":"test"}')
        (ep / "diarized_transcript.json").write_text(json.dumps({
            "utterances": [{
                "speaker": 0,
                "start": 0.0,
                "end": 5.0,
                "text": "let me tell you about credit cards in strippers it was crazy",
                "words": [
                    {"word": "let", "start": 0.0, "end": 0.2, "speaker": 0},
                    {"word": "me", "start": 0.3, "end": 0.4, "speaker": 0},
                    {"word": "tell", "start": 0.5, "end": 0.7, "speaker": 0},
                    {"word": "you", "start": 0.8, "end": 1.0, "speaker": 0},
                    {"word": "about", "start": 1.1, "end": 1.4, "speaker": 0},
                    {"word": "credit", "start": 1.5, "end": 1.9, "speaker": 0},
                    {"word": "cards", "start": 2.0, "end": 2.4, "speaker": 0},
                    {"word": "in", "start": 2.5, "end": 2.6, "speaker": 0},
                    {"word": "strippers", "start": 2.7, "end": 3.3, "speaker": 0},
                    {"word": "it", "start": 3.4, "end": 3.5, "speaker": 0},
                    {"word": "was", "start": 3.6, "end": 3.8, "speaker": 0},
                    {"word": "crazy", "start": 3.9, "end": 4.3, "speaker": 0},
                ],
            }]
        }))

        proposals = find_and_propose_cut(ep, "credit cards")
        assert len(proposals) >= 1
        first = proposals[0]
        assert "credit" in first["matched_text"]
        assert "cards" in first["matched_text"]
        assert first["start_seconds"] < first["end_seconds"]
