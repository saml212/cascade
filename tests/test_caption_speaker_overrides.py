"""Caption-only word speaker overrides stay guarded and noncanonical."""

import copy
import json

import pytest

from lib.caption_speaker_overrides import (
    CAPTION_SPEAKER_OVERRIDES_SCHEMA,
    caption_speaker_override_state,
    caption_speaker_overrides_path,
    validate_caption_speaker_override_document,
)
from lib.ffprobe import file_fingerprint


def _word(word, punctuated_word, start, end, speaker=1):
    return {
        "word": word,
        "punctuated_word": punctuated_word,
        "start": start,
        "end": end,
        "speaker": speaker,
        "confidence": 0.99,
    }


def _review_inputs(tmp_path):
    diarized = {
        "clock": "source",
        "speaker_map": [
            {"index": 0, "target_speaker": "speaker_0"},
            {"index": 1, "target_speaker": "speaker_1"},
            {"index": 2, "target_speaker": "speaker_2"},
        ],
        "utterances": [
            {
                "speaker": 1,
                "words": [
                    _word("just", "Just", 4092.32, 4092.64),
                    _word("keep", "keep", 4092.64, 4092.88),
                    _word("the", "the", 4092.88, 4093.04),
                    _word("education", "education", 4093.04, 4093.44),
                    _word("going", "going.", 4093.44, 4094.08),
                    _word("yeah", "Yeah.", 4094.08, 4094.4),
                    _word("sure", "Sure.", 4114.015, 4114.654999),
                    _word("yeah", "Yeah.", 4124.71, 4125.27),
                    _word("yeah", "Yeah.", 4125.27, 4125.59),
                ],
            }
        ],
    }
    path = tmp_path / "diarized_transcript.json"
    path.write_text(json.dumps(diarized, indent=2))
    clip = {
        "id": "clip_04",
        "start_seconds": 4092.25,
        "end_seconds": 4128.92,
    }
    segments = {"clock": "source", "track_mapping": []}
    crop_config = {"speakers": [{}, {}, {}]}
    return diarized, clip, segments, crop_config, path


def _reviewed_overrides():
    return [
        {
            "id": "review_ty04_garrett_education_prompt",
            "start": 4092.32,
            "end": 4094.08,
            "from_asr_speaker": 1,
            "to_asr_speaker": 2,
            "source_speaker": "speaker_1",
            "target_crop": "speaker_2",
            "reason": "Reviewed close-mic and picture evidence.",
            "expected_words": [
                {key: word[key] for key in ("word", "punctuated_word", "start", "end")}
                for word in [
                    _word("just", "Just", 4092.32, 4092.64),
                    _word("keep", "keep", 4092.64, 4092.88),
                    _word("the", "the", 4092.88, 4093.04),
                    _word("education", "education", 4093.04, 4093.44),
                    _word("going", "going.", 4093.44, 4094.08),
                ]
            ],
        },
        {
            "id": "review_ty04_garrett_sure",
            "start": 4114.015,
            "end": 4114.654999,
            "from_asr_speaker": 1,
            "to_asr_speaker": 2,
            "source_speaker": "speaker_1",
            "target_crop": "speaker_2",
            "reason": "Reviewed close-mic and picture evidence.",
            "expected_words": [
                {
                    "word": "sure",
                    "punctuated_word": "Sure.",
                    "start": 4114.015,
                    "end": 4114.654999,
                }
            ],
        },
        {
            "id": "review_ty04_garrett_backchannel_yeah",
            "start": 4124.71,
            "end": 4125.27,
            "from_asr_speaker": 1,
            "to_asr_speaker": 2,
            "source_speaker": "speaker_1",
            "target_crop": "speaker_2",
            "reason": "Reviewed close-mic and picture evidence.",
            "expected_words": [
                {
                    "word": "yeah",
                    "punctuated_word": "Yeah.",
                    "start": 4124.71,
                    "end": 4125.27,
                }
            ],
        },
    ]


def _document(clip_id, transcript_revision):
    return {
        "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
        "clock": "source",
        "clip_id": clip_id,
        "transcript_revision": transcript_revision,
        "overrides": _reviewed_overrides(),
        "actor": "reviewer",
        "reason": "Apply seven reviewed words to short captions only.",
        "updated_at": "2026-09-16T12:00:00+00:00",
    }


def test_exact_seven_words_apply_to_copy_and_leave_canonical_bytes(tmp_path):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    transcript_revision = file_fingerprint(transcript_path)["id"]
    original = copy.deepcopy(diarized)
    original_bytes = transcript_path.read_bytes()
    document = _document(clip["id"], transcript_revision)

    effective, count = validate_caption_speaker_override_document(
        document,
        clip=clip,
        diarized=diarized,
        segment_document=segments,
        crop_config=crop_config,
        transcript_revision=transcript_revision,
    )

    words = effective["utterances"][0]["words"]
    assert count == 7
    assert [word["speaker"] for word in words] == [2, 2, 2, 2, 2, 1, 2, 2, 1]
    assert diarized == original
    assert transcript_path.read_bytes() == original_bytes
    for before, after in zip(original["utterances"][0]["words"], words, strict=True):
        assert {key: value for key, value in before.items() if key != "speaker"} == {
            key: value for key, value in after.items() if key != "speaker"
        }


def test_stale_or_mismatched_document_fails_closed(tmp_path):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    transcript_revision = file_fingerprint(transcript_path)["id"]
    document = _document(clip["id"], "sha256:" + "0" * 64)
    caption_speaker_overrides_path(tmp_path, clip["id"]).parent.mkdir()
    caption_speaker_overrides_path(tmp_path, clip["id"]).write_text(
        json.dumps(document)
    )

    state, effective = caption_speaker_override_state(
        tmp_path, clip, diarized, segments, crop_config
    )

    assert state["current"] is False
    assert "another transcript" in state["error"]
    assert state["applied_word_count"] == 0
    assert effective == diarized

    current_document = _document(clip["id"], transcript_revision)
    current_document["overrides"][0]["expected_words"][0]["word"] = "changed"
    with pytest.raises(ValueError, match="no longer matches"):
        validate_caption_speaker_override_document(
            current_document,
            clip=clip,
            diarized=diarized,
            segment_document=segments,
            crop_config=crop_config,
            transcript_revision=transcript_revision,
        )


def test_wrong_source_binding_or_out_of_clip_window_is_rejected(tmp_path):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    transcript_revision = file_fingerprint(transcript_path)["id"]
    wrong_binding = _document(clip["id"], transcript_revision)
    wrong_binding["overrides"][0]["source_speaker"] = "speaker_0"
    with pytest.raises(ValueError, match="no longer maps"):
        validate_caption_speaker_override_document(
            wrong_binding,
            clip=clip,
            diarized=diarized,
            segment_document=segments,
            crop_config=crop_config,
            transcript_revision=transcript_revision,
        )

    outside = _document(clip["id"], transcript_revision)
    outside["overrides"][0]["start"] = clip["start_seconds"] - 0.01
    with pytest.raises(ValueError, match="outside its clip"):
        validate_caption_speaker_override_document(
            outside,
            clip=clip,
            diarized=diarized,
            segment_document=segments,
            crop_config=crop_config,
            transcript_revision=transcript_revision,
        )


def test_malformed_sidecar_is_inspectable_but_never_applied(tmp_path):
    diarized, clip, segments, crop_config, _transcript_path = _review_inputs(tmp_path)
    path = caption_speaker_overrides_path(tmp_path, clip["id"])
    path.parent.mkdir()
    path.write_text("{not-json")

    state, effective = caption_speaker_override_state(
        tmp_path, clip, diarized, segments, crop_config
    )

    assert state["current"] is False
    assert state["document_revision"].startswith("sha256:")
    assert state["applied_word_count"] == 0
    assert effective == diarized


def test_state_never_binds_file_revision_to_another_transcript_object(tmp_path):
    diarized, clip, segments, crop_config, _transcript_path = _review_inputs(tmp_path)
    stale_object = copy.deepcopy(diarized)
    stale_object["utterances"][0]["words"][0]["punctuated_word"] = "Changed"

    with pytest.raises(ValueError, match="changed while it was inspected"):
        caption_speaker_override_state(
            tmp_path, clip, stale_object, segments, crop_config
        )
