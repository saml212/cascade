"""Caption-only word speaker overrides stay guarded and noncanonical."""

import copy
import json

import pytest

from lib.ass import CaptionPlacement, generate_ass_from_diarized
from lib.caption_speaker_overrides import (
    CAPTION_SPEAKER_OVERRIDES_SCHEMA,
    CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA,
    apply_current_caption_speaker_overrides,
    caption_speaker_override_document_revision,
    caption_speaker_override_revision,
    caption_speaker_override_state,
    caption_speaker_overrides_path,
    normalize_caption_speaker_override_document,
    validate_caption_speaker_override_document,
)
from lib.ffprobe import file_fingerprint
from lib.short_variants import speaker_panel_caption_context_revision
from lib.timeline import Timeline, rebase_diarized


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
                "words": _review_inputs_words(),
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


def _text_replacement_document(clip_id, transcript_revision):
    expected = [
        {key: word[key] for key in ("word", "punctuated_word", "start", "end")}
        for word in _review_inputs_words()[:5]
    ]
    return {
        "schema": CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA,
        "clock": "source",
        "clip_id": clip_id,
        "transcript_revision": transcript_revision,
        "overrides": [],
        "text_replacements": [
            {
                "id": "review_parallel_ty_host",
                "start": 4092.32,
                "end": 4094.08,
                "from_asr_speaker": 1,
                "source_speaker": "speaker_1",
                "reason": "Reviewed close microphones and source picture.",
                "expected_words": expected,
                "display_phrases": [
                    {
                        "id": "ty_lead",
                        "to_asr_speaker": 1,
                        "target_crop": "speaker_1",
                        "words": [
                            {
                                "word": "yeah",
                                "punctuated_word": "Yeah.",
                                "start": 4092.32,
                                "end": 4092.55,
                            },
                            {
                                "word": "and",
                                "punctuated_word": "And",
                                "start": 4092.56,
                                "end": 4092.75,
                            },
                            {
                                "word": "then",
                                "punctuated_word": "then,",
                                "start": 4092.75,
                                "end": 4093.0,
                            },
                            {
                                "word": "so",
                                "punctuated_word": "so—",
                                "start": 4093.0,
                                "end": 4093.2,
                            },
                        ],
                    },
                    {
                        "id": "host_overlap",
                        "to_asr_speaker": 2,
                        "target_crop": "speaker_2",
                        "words": [
                            {
                                "word": "the",
                                "punctuated_word": "The",
                                "start": 4092.8,
                                "end": 4092.95,
                            },
                            {
                                "word": "crawling",
                                "punctuated_word": "crawling",
                                "start": 4092.95,
                                "end": 4093.3,
                            },
                            {
                                "word": "slash",
                                "punctuated_word": "slash",
                                "start": 4093.3,
                                "end": 4093.55,
                            },
                            {
                                "word": "longboarding",
                                "punctuated_word": "longboarding.",
                                "start": 4093.55,
                                "end": 4094.08,
                            },
                        ],
                    },
                ],
            }
        ],
        "actor": "reviewer",
        "reason": "Replace conflated panel captions only.",
        "updated_at": "2026-09-16T12:00:00+00:00",
    }


def _review_inputs_words():
    return [
        _word("just", "Just", 4092.32, 4092.64),
        _word("keep", "keep", 4092.64, 4092.88),
        _word("the", "the", 4092.88, 4093.04),
        _word("education", "education", 4093.04, 4093.44),
        _word("going", "going.", 4093.44, 4094.08),
        _word("yeah", "Yeah.", 4094.08, 4094.4),
        _word("sure", "Sure.", 4114.015, 4114.654999),
        _word("yeah", "Yeah.", 4124.71, 4125.27),
        _word("yeah", "Yeah.", 4125.27, 4125.59),
    ]


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


def test_review_metadata_changes_only_the_document_cas_revision(tmp_path):
    _diarized, clip, _segments, _crop_config, transcript_path = _review_inputs(tmp_path)
    document = normalize_caption_speaker_override_document(
        _document(clip["id"], file_fingerprint(transcript_path)["id"]), clip["id"]
    )
    changed = copy.deepcopy(document)
    changed["actor"] = "second-reviewer"
    changed["reason"] = "Keep the same pixel result with updated review context."
    changed["updated_at"] = "2026-09-16T13:00:00+00:00"
    assert caption_speaker_override_revision(
        clip["id"], changed
    ) == caption_speaker_override_revision(clip["id"], document)
    assert caption_speaker_override_document_revision(
        clip["id"], changed
    ) != caption_speaker_override_document_revision(clip["id"], document)


def test_v1_normalization_and_revisions_remain_bit_for_bit_compatible(tmp_path):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    raw = _document(clip["id"], file_fingerprint(transcript_path)["id"])
    document = normalize_caption_speaker_override_document(raw, clip["id"])

    assert document == raw
    assert caption_speaker_override_revision(clip["id"], document) == (
        "sha256:5f4ea7624596c77a9b390581075a37e8b41b465e78758514b78a973639aa9ac9"
    )
    assert caption_speaker_override_document_revision(clip["id"], document) == (
        "sha256:c8e90532e3c8155aae5fbcd2e0d7f88c737604630f7fa9d6d48ad8c070c006a1"
    )
    path = caption_speaker_overrides_path(tmp_path, clip["id"])
    path.parent.mkdir()
    path.write_text(json.dumps(document))
    state, effective = caption_speaker_override_state(
        tmp_path, clip, diarized, segments, crop_config
    )
    assert state["revision"] == (
        "sha256:870b8345b7c36f116de9aaef46a08d01129ecea82d04ddc1b21ed8a513abe1c0"
    )
    assert "text_replacements" not in state
    assert effective["utterances"][0]["words"][0]["speaker"] == 2

    changed = copy.deepcopy(document)
    changed["overrides"][0]["reason"] = (
        "Updated evidence summary without changing the selected words."
    )
    assert caption_speaker_override_revision(
        clip["id"], changed
    ) == caption_speaker_override_revision(clip["id"], document)
    assert caption_speaker_override_document_revision(
        clip["id"], changed
    ) != caption_speaker_override_document_revision(clip["id"], document)


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


def test_state_revision_binds_clip_bounds_and_stored_schema_is_strict(tmp_path):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    first, _ = caption_speaker_override_state(
        tmp_path, clip, diarized, segments, crop_config
    )
    moved = {**clip, "start_seconds": clip["start_seconds"] + 0.1}
    second, _ = caption_speaker_override_state(
        tmp_path, moved, diarized, segments, crop_config
    )
    assert second["revision"] != first["revision"]

    transcript_revision = file_fingerprint(transcript_path)["id"]
    invalid = _document(clip["id"], transcript_revision)
    invalid["overrides"][0]["id"] = "../../unstable"
    with pytest.raises(ValueError, match="id is invalid"):
        normalize_caption_speaker_override_document(invalid, clip["id"])
    invalid = _document(clip["id"], transcript_revision)
    invalid["overrides"][0]["start"] = "4092.32"
    with pytest.raises(TypeError, match="finite number"):
        normalize_caption_speaker_override_document(invalid, clip["id"])


def test_effective_variant_helper_binds_valid_sidecar_and_fails_closed(tmp_path):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    document = _document(clip["id"], file_fingerprint(transcript_path)["id"])
    path = caption_speaker_overrides_path(tmp_path, clip["id"])
    path.parent.mkdir()
    path.write_text(json.dumps(document))

    effective, binding = apply_current_caption_speaker_overrides(
        tmp_path, clip, diarized, segments, crop_config
    )

    assert binding == {
        "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
        "clock": "source",
        "revision": binding["revision"],
        "operation_ids": [override["id"] for override in _reviewed_overrides()],
        "override_count": 3,
        "applied_word_count": 7,
    }
    assert effective["utterances"][0]["words"][0]["speaker"] == 2

    path.write_text("{not-json")
    with pytest.raises(ValueError, match="file is invalid"):
        apply_current_caption_speaker_overrides(
            tmp_path, clip, diarized, segments, crop_config
        )

    path.write_text("null")
    with pytest.raises(ValueError, match="must be a mapping"):
        apply_current_caption_speaker_overrides(
            tmp_path, clip, diarized, segments, crop_config
        )


def test_text_replacement_is_caption_only_and_preserves_parallel_panel_events(
    tmp_path,
):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    transcript_revision = file_fingerprint(transcript_path)["id"]
    original = copy.deepcopy(diarized)
    original_bytes = transcript_path.read_bytes()
    document = _text_replacement_document(clip["id"], transcript_revision)

    effective, count = validate_caption_speaker_override_document(
        document,
        clip=clip,
        diarized=diarized,
        segment_document=segments,
        crop_config=crop_config,
        transcript_revision=transcript_revision,
    )

    assert count == 5
    assert diarized == original
    assert transcript_path.read_bytes() == original_bytes
    ordinary_words = effective["utterances"][0]["words"]
    assert [word["word"] for word in ordinary_words[:2]] == ["yeah", "sure"]
    rebased = rebase_diarized(
        effective, Timeline(5000, [(clip["start_seconds"], clip["end_seconds"])])
    )
    ass_path = tmp_path / "parallel.ass"
    generate_ass_from_diarized(
        rebased,
        0,
        clip["end_seconds"] - clip["start_seconds"],
        ass_path,
        speaker_targets={1: "speaker_1", 2: "speaker_2"},
        speaker_placements={
            "speaker_1": CaptionPlacement(540, 640),
            "speaker_2": CaptionPlacement(540, 1280),
        },
    )
    dialogue = [
        line
        for line in ass_path.read_text().splitlines()
        if line.startswith("Dialogue:")
    ]
    ty = next(line for line in dialogue if "Yeah. And then, so—" in line)
    host = next(line for line in dialogue if "The crawling slash longboarding." in line)
    ty_fields = ty.split(",", 9)
    host_fields = host.split(",", 9)
    assert ty_fields[1] == "0:00:00.07"
    assert host_fields[1] == "0:00:00.55"
    assert ty_fields[2] > host_fields[1]
    assert r"\pos(540,640)" in ty
    assert r"\pos(540,1280)" in host

    path = caption_speaker_overrides_path(tmp_path, clip["id"])
    path.parent.mkdir()
    path.write_text(json.dumps(document))
    applied_effective, binding = apply_current_caption_speaker_overrides(
        tmp_path, clip, diarized, segments, crop_config
    )
    assert binding == {
        "schema": CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA,
        "clock": "source",
        "revision": binding["revision"],
        "operation_ids": ["review_parallel_ty_host"],
        "override_count": 0,
        "applied_word_count": 5,
        "text_replacement_count": 1,
    }
    original_context = speaker_panel_caption_context_revision(
        tmp_path,
        episode={"crop_config": crop_config},
        diarized=diarized,
        segment_document=segments,
    )
    replacement_context = speaker_panel_caption_context_revision(
        tmp_path,
        episode={"crop_config": crop_config},
        diarized=applied_effective,
        segment_document=segments,
        caption_speaker_overrides=binding,
    )
    assert replacement_context != original_context


def test_text_replacement_can_suppress_exact_duplicate_without_inventing_text(
    tmp_path,
):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    document = _text_replacement_document(
        clip["id"], file_fingerprint(transcript_path)["id"]
    )
    replacement = document["text_replacements"][0]
    replacement["start"] = 4093.44
    replacement["expected_words"] = replacement["expected_words"][-1:]
    replacement["display_phrases"] = []

    effective, count = validate_caption_speaker_override_document(
        document,
        clip=clip,
        diarized=diarized,
        segment_document=segments,
        crop_config=crop_config,
        transcript_revision=document["transcript_revision"],
    )

    assert count == 1
    assert "going" not in [
        word["word"]
        for utterance in effective["utterances"]
        for word in utterance.get("words", [])
    ]
    path = caption_speaker_overrides_path(tmp_path, clip["id"])
    path.parent.mkdir()
    path.write_text(json.dumps(document))
    _effective, binding = apply_current_caption_speaker_overrides(
        tmp_path, clip, diarized, segments, crop_config
    )
    assert binding["text_replacement_count"] == 1


def test_text_replacement_rejects_stale_duplicate_selection_and_unsafe_phrases(
    tmp_path,
):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    revision = file_fingerprint(transcript_path)["id"]

    stale = _text_replacement_document(clip["id"], revision)
    stale["text_replacements"][0]["expected_words"][0]["word"] = "changed"
    with pytest.raises(ValueError, match="no longer matches"):
        validate_caption_speaker_override_document(
            stale,
            clip=clip,
            diarized=diarized,
            segment_document=segments,
            crop_config=crop_config,
            transcript_revision=revision,
        )

    same_panel = _text_replacement_document(clip["id"], revision)
    same_panel["text_replacements"][0]["display_phrases"][1].update(
        {"to_asr_speaker": 1, "target_crop": "speaker_1"}
    )
    with pytest.raises(ValueError, match="different speaker panels"):
        normalize_caption_speaker_override_document(same_panel, clip["id"])

    injected = _text_replacement_document(clip["id"], revision)
    injected["text_replacements"][0]["display_phrases"][0]["words"][0][
        "punctuated_word"
    ] = r"{\pos(1,1)}Yeah."
    with pytest.raises(ValueError, match="ASS syntax"):
        normalize_caption_speaker_override_document(injected, clip["id"])

    extra = _text_replacement_document(clip["id"], revision)
    extra["text_replacements"][0]["display_phrases"][0]["unreviewed"] = True
    with pytest.raises(ValueError, match="fields do not match"):
        normalize_caption_speaker_override_document(extra, clip["id"])


def test_text_replacement_rejects_duplicate_source_selection_and_stale_crop(
    tmp_path,
):
    diarized, clip, segments, crop_config, transcript_path = _review_inputs(tmp_path)
    revision = file_fingerprint(transcript_path)["id"]
    document = _text_replacement_document(clip["id"], revision)
    document["overrides"] = [_reviewed_overrides()[0]]
    with pytest.raises(ValueError, match="same word twice"):
        validate_caption_speaker_override_document(
            document,
            clip=clip,
            diarized=diarized,
            segment_document=segments,
            crop_config=crop_config,
            transcript_revision=revision,
        )

    stale_crop = _text_replacement_document(clip["id"], revision)
    stale_crop["text_replacements"][0]["display_phrases"][1]["target_crop"] = (
        "speaker_0"
    )
    with pytest.raises(ValueError, match="no longer maps"):
        validate_caption_speaker_override_document(
            stale_crop,
            clip=clip,
            diarized=diarized,
            segment_document=segments,
            crop_config=crop_config,
            transcript_revision=revision,
        )
