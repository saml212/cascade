"""Tests for the shared source-clock timeline contract."""

import pytest

from lib.timeline import Timeline, build_keep_intervals, rebase_diarized


def test_keep_intervals_are_order_independent_and_merge_overlapping_cuts():
    edits = [
        {"type": "cut", "start_seconds": 40, "end_seconds": 60},
        {"type": "trim_end", "seconds": 100},
        {"type": "cut", "start_seconds": 30, "end_seconds": 50},
        {"type": "trim_start", "seconds": 10},
    ]

    assert build_keep_intervals(120, edits) == [(10.0, 30.0), (60.0, 100.0)]


def test_source_and_output_clock_round_trip_across_interior_cut():
    timeline = Timeline.from_edits(
        100, [{"type": "cut", "start_seconds": 20, "end_seconds": 40}]
    )

    assert timeline.duration == 80
    assert timeline.source_to_output(10) == 10
    assert timeline.source_to_output(30) is None
    assert timeline.source_to_output(50) == 30
    assert timeline.output_to_source(20) == 40
    assert timeline.output_to_source(30) == 50


def test_slice_intersects_clip_with_edits_and_rebases_to_zero():
    timeline = Timeline.from_edits(
        100, [{"type": "cut", "start_seconds": 20, "end_seconds": 40}]
    ).slice(10, 50)

    assert timeline.keep_intervals == ((10.0, 20.0), (40.0, 50.0))
    assert timeline.duration == 20
    assert timeline.source_to_output(10) == 0
    assert timeline.source_to_output(40) == 10


def test_project_splits_speaker_segment_and_preserves_source_bounds():
    timeline = Timeline.from_edits(
        100, [{"type": "cut", "start_seconds": 20, "end_seconds": 40}]
    )
    segments = [{"start": 10, "end": 50, "duration": 40, "speaker": "guest"}]

    assert timeline.project(segments, output_clock=True) == [
        {
            "start": 10.0,
            "end": 20.0,
            "duration": 10.0,
            "speaker": "guest",
            "source_start": 10.0,
            "source_end": 20.0,
        },
        {
            "start": 20.0,
            "end": 30.0,
            "duration": 10.0,
            "speaker": "guest",
            "source_start": 40.0,
            "source_end": 50.0,
        },
    ]


def test_rebase_diarized_excludes_cut_words_and_moves_later_captions():
    diarized = {
        "utterances": [
            {
                "speaker": 0,
                "start": 18,
                "end": 43,
                "words": [
                    {"word": "keep", "start": 18, "end": 19, "speaker": 0},
                    {"word": "remove", "start": 25, "end": 26, "speaker": 0},
                    {"word": "later", "start": 42, "end": 43, "speaker": 0},
                ],
            }
        ]
    }
    timeline = Timeline.from_edits(
        60, [{"type": "cut", "start_seconds": 20, "end_seconds": 40}]
    )

    rebased = rebase_diarized(diarized, timeline)

    words = rebased["utterances"][0]["words"]
    assert [word["word"] for word in words] == ["keep", "later"]
    assert [(word["start"], word["end"]) for word in words] == [
        (18.0, 19.0),
        (22.0, 23.0),
    ]
    assert words[1]["source_start"] == 42


def test_rebase_diarized_drops_word_when_only_tiny_fragment_is_retained():
    diarized = {
        "utterances": [
            {"words": [{"word": "truncated", "start": 9.9, "end": 10.8, "speaker": 0}]}
        ]
    }
    timeline = Timeline.from_edits(
        20, [{"type": "cut", "start_seconds": 10, "end_seconds": 15}]
    )

    assert rebase_diarized(diarized, timeline)["utterances"] == []


@pytest.mark.parametrize(
    "edits, message",
    [
        ([{"type": "fade"}], "Unsupported"),
        ([{"type": "cut", "start_seconds": 5, "end_seconds": 4}], "Invalid cut"),
        ([{"type": "trim_start", "seconds": 100}], "Invalid trim"),
        ([{"type": "trim_start", "seconds": -1}], "Invalid trim_start"),
        ([{"type": "trim_end", "seconds": 101}], "Invalid trim_end"),
        ([{"type": "cut", "start_seconds": 5, "end_seconds": 101}], "Invalid cut"),
    ],
)
def test_invalid_edits_fail_before_render(edits, message):
    with pytest.raises(ValueError, match=message):
        Timeline.from_edits(100, edits)


def test_project_drops_removed_and_tiny_records_without_mutating_input():
    items = [
        {"start": 1.0, "end": 2.0, "speaker": "A"},
        {"start": 3.0, "end": 3.05, "speaker": "B"},
    ]
    timeline = Timeline.from_edits(
        5, [{"type": "cut", "start_seconds": 1, "end_seconds": 2}]
    )

    assert timeline.project(items, minimum_duration=0.1) == []
    assert items[0]["start"] == 1.0


def test_project_does_not_emit_records_that_only_touch_a_boundary():
    timeline = Timeline(10, [(2, 4)])

    assert timeline.project([{"start": 0, "end": 2}]) == []
    assert timeline.project([{"start": 4, "end": 6}]) == []
