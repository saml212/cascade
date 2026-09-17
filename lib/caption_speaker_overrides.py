"""Reviewed word-level speaker attribution used only by short captions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from lib.ass import (
    MAX_PHRASE_DURATION,
    MIN_PHRASE_DURATION,
    resolve_caption_speaker_targets,
)
from lib.ffprobe import file_fingerprint

CAPTION_SPEAKER_OVERRIDES_SCHEMA = "cascade.short-caption-speaker-overrides/v1"
CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA = "cascade.short-caption-speaker-overrides/v2"
CAPTION_SPEAKER_OVERRIDES_DIR = "caption_speaker_overrides"

# These keys exist only on the effective caption copy.  They let the ASS
# compositor preserve an explicitly reviewed cross-panel overlap without
# changing the legacy overlap policy for any other caption.
CAPTION_DISPLAY_PHRASE_ID = "_caption_display_phrase_id"
CAPTION_TEXT_REPLACEMENTS_APPLIED = "_caption_text_replacements_applied"

_CLIP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CROP_SPEAKER = re.compile(r"^speaker_[0-9]+$")


def _json_revision(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _finite_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return round(number, 6)


def _speaker_id(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"{field} must be a non-negative integer")
    return value


def _bounded_text(
    value: object, field: str, *, min_length: int = 1, max_length: int
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be nonempty text")
    text = value.strip()
    if not min_length <= len(text) <= max_length:
        raise ValueError(f"{field} has an invalid length")
    return text


def _review_timestamp(value: object) -> str:
    text = _bounded_text(value, "updated_at", max_length=100)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("updated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("updated_at must include a timezone")
    return text


def caption_speaker_overrides_path(episode_dir: Path, clip_id: str) -> Path:
    if not _CLIP_ID.fullmatch(str(clip_id)):
        raise ValueError("Invalid clip id for caption speaker overrides")
    return Path(episode_dir) / CAPTION_SPEAKER_OVERRIDES_DIR / f"{clip_id}.json"


def _normalize_expected_word(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("Each expected caption word must be a mapping")
    allowed = {"word", "punctuated_word", "start", "end"}
    if set(value) != allowed:
        raise ValueError("Expected caption words require exact word and timing fields")
    start = _finite_number(value.get("start"), "expected_words.start")
    end = _finite_number(value.get("end"), "expected_words.end")
    if end <= start:
        raise ValueError("Expected caption word end must be after its start")
    return {
        "word": _bounded_text(value.get("word"), "expected_words.word", max_length=200),
        "punctuated_word": _bounded_text(
            value.get("punctuated_word"),
            "expected_words.punctuated_word",
            max_length=200,
        ),
        "start": start,
        "end": end,
    }


def _normalize_display_word(value: object) -> dict:
    word = _normalize_expected_word(value)
    for field in ("word", "punctuated_word"):
        text = word[field]
        if not text.isprintable():
            raise ValueError(f"Display caption {field} cannot contain control text")
        if any(character in text for character in "{}\\"):
            raise ValueError(f"Display caption {field} cannot contain ASS syntax")
    return word


def _normalize_display_phrase(
    value: object, replacement_start: float, replacement_end: float
) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("Each display caption phrase must be a mapping")
    allowed = {"id", "to_asr_speaker", "target_crop", "words"}
    if set(value) != allowed:
        raise ValueError("Display caption phrase fields do not match the schema")
    phrase_id = _bounded_text(value.get("id"), "display_phrases.id", max_length=128)
    if not _CLIP_ID.fullmatch(phrase_id):
        raise ValueError("Display caption phrase id is invalid")
    target = _speaker_id(value.get("to_asr_speaker"), "to_asr_speaker")
    target_crop = _bounded_text(value.get("target_crop"), "target_crop", max_length=64)
    if not _CROP_SPEAKER.fullmatch(target_crop):
        raise ValueError("target_crop must identify one reviewed crop speaker")
    words = value.get("words")
    if not isinstance(words, list) or not words:
        raise ValueError("Display caption phrase needs words")
    if len(words) > 12:
        raise ValueError("Display caption phrase has too many words")
    normalized_words = [_normalize_display_word(word) for word in words]
    if normalized_words != sorted(
        normalized_words,
        key=lambda word: (word["start"], word["end"], word["word"]),
    ):
        raise ValueError("Display caption phrase words must be in source-time order")
    if any(
        word["start"] < replacement_start or word["end"] > replacement_end
        for word in normalized_words
    ):
        raise ValueError("Every display word must be inside its text replacement")
    phrase = {
        "id": phrase_id,
        "to_asr_speaker": target,
        "target_crop": target_crop,
        "words": normalized_words,
    }
    phrase_start, phrase_end = _phrase_bounds(phrase)
    if phrase_end - phrase_start > MAX_PHRASE_DURATION:
        raise ValueError("Display caption phrase exceeds the maximum display duration")
    return phrase


def _phrase_bounds(phrase: Mapping[str, object]) -> tuple[float, float]:
    words = phrase["words"]
    return (
        min(word["start"] for word in words),
        max(word["end"] for word in words),
    )


def _normalize_expected_words(
    value: object, start: float, end: float, *, label: str
) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} needs expected words")
    if len(value) > 100:
        raise ValueError(f"{label} has too many expected words")
    words = sorted(
        (_normalize_expected_word(word) for word in value),
        key=lambda word: (word["start"], word["end"], word["word"]),
    )
    if any(not (start <= (word["start"] + word["end"]) / 2 < end) for word in words):
        raise ValueError(f"Every expected word midpoint must be inside its {label}")
    return words


def _normalize_display_phrases(value: object, start: float, end: float) -> list[dict]:
    if not isinstance(value, list):
        raise TypeError("Caption text replacement display_phrases must be a list")
    if len(value) > 20:
        raise ValueError("Caption text replacement has too many display phrases")
    phrases = sorted(
        (_normalize_display_phrase(phrase, start, end) for phrase in value),
        key=lambda phrase: (*_phrase_bounds(phrase), phrase["id"]),
    )
    phrase_ids = [phrase["id"] for phrase in phrases]
    if len(phrase_ids) != len(set(phrase_ids)):
        raise ValueError("Display caption phrase ids must be unique per replacement")
    return phrases


def _reject_same_panel_overlaps(phrases: list[dict]) -> None:
    for index, phrase in enumerate(phrases):
        phrase_start, phrase_end = _phrase_bounds(phrase)
        phrase_end = max(phrase_end, phrase_start + MIN_PHRASE_DURATION)
        for other in phrases[index + 1 :]:
            other_start, other_end = _phrase_bounds(other)
            other_end = max(other_end, other_start + MIN_PHRASE_DURATION)
            overlaps = other_start < phrase_end and phrase_start < other_end
            if overlaps and phrase["target_crop"] == other["target_crop"]:
                raise ValueError(
                    "Overlapping display phrases must target different speaker panels"
                )


def _normalize_text_replacement(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("Each caption text replacement must be a mapping")
    allowed = {
        "id",
        "start",
        "end",
        "from_asr_speaker",
        "source_speaker",
        "reason",
        "expected_words",
        "display_phrases",
    }
    if set(value) != allowed:
        raise ValueError("Caption text replacement fields do not match the schema")
    start = _finite_number(value.get("start"), "text_replacement.start")
    end = _finite_number(value.get("end"), "text_replacement.end")
    if end <= start:
        raise ValueError("Caption text replacement end must be after its start")
    replacement_id = _bounded_text(
        value.get("id"), "text_replacement.id", max_length=128
    )
    if not _CLIP_ID.fullmatch(replacement_id):
        raise ValueError("Caption text replacement id is invalid")
    source = _speaker_id(value.get("from_asr_speaker"), "from_asr_speaker")
    source_speaker = _bounded_text(
        value.get("source_speaker"), "source_speaker", max_length=64
    )
    if not _CROP_SPEAKER.fullmatch(source_speaker):
        raise ValueError("source_speaker must identify one reviewed crop speaker")
    expected_words = _normalize_expected_words(
        value.get("expected_words"), start, end, label="text replacement"
    )
    display_phrases = _normalize_display_phrases(
        value.get("display_phrases"), start, end
    )
    _reject_same_panel_overlaps(display_phrases)
    return {
        "id": replacement_id,
        "start": start,
        "end": end,
        "from_asr_speaker": source,
        "source_speaker": source_speaker,
        "reason": _bounded_text(
            value.get("reason"),
            "text_replacement.reason",
            min_length=3,
            max_length=1000,
        ),
        "expected_words": expected_words,
        "display_phrases": display_phrases,
    }


def _normalize_override(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("Each caption speaker override must be a mapping")
    allowed = {
        "id",
        "start",
        "end",
        "from_asr_speaker",
        "to_asr_speaker",
        "source_speaker",
        "target_crop",
        "reason",
        "expected_words",
    }
    if set(value) != allowed:
        raise ValueError("Caption speaker override fields do not match the schema")
    start = _finite_number(value.get("start"), "override.start")
    end = _finite_number(value.get("end"), "override.end")
    if end <= start:
        raise ValueError("Caption speaker override end must be after its start")
    source = _speaker_id(value.get("from_asr_speaker"), "from_asr_speaker")
    target = _speaker_id(value.get("to_asr_speaker"), "to_asr_speaker")
    if source == target:
        raise ValueError("Caption speaker override must change the speaker")
    operation_id = _bounded_text(value.get("id"), "override.id", max_length=128)
    if not _CLIP_ID.fullmatch(operation_id):
        raise ValueError("Caption speaker override id is invalid")
    source_speaker = _bounded_text(
        value.get("source_speaker"), "source_speaker", max_length=64
    )
    target_crop = _bounded_text(value.get("target_crop"), "target_crop", max_length=64)
    if not _CROP_SPEAKER.fullmatch(source_speaker):
        raise ValueError("source_speaker must identify one reviewed crop speaker")
    if not _CROP_SPEAKER.fullmatch(target_crop):
        raise ValueError("target_crop must identify one reviewed crop speaker")
    if source_speaker == target_crop:
        raise ValueError("Caption speaker override must change its crop target")
    words = value.get("expected_words")
    if not isinstance(words, list) or not words:
        raise ValueError("Caption speaker override needs expected words")
    expected_words = sorted(
        (_normalize_expected_word(word) for word in words),
        key=lambda word: (word["start"], word["end"], word["word"]),
    )
    if any(
        not (start <= (word["start"] + word["end"]) / 2 < end)
        for word in expected_words
    ):
        raise ValueError("Every expected word midpoint must be inside its override")
    return {
        "id": operation_id,
        "start": start,
        "end": end,
        "from_asr_speaker": source,
        "to_asr_speaker": target,
        "source_speaker": source_speaker,
        "target_crop": target_crop,
        "reason": _bounded_text(
            value.get("reason"), "override.reason", min_length=3, max_length=1000
        ),
        "expected_words": expected_words,
    }


def _normalize_text_replacements(value: object) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("Version 2 caption overrides need text replacements")
    if len(value) > 100:
        raise ValueError("Caption override document has too many text replacements")
    replacements = sorted(
        (_normalize_text_replacement(item) for item in value),
        key=lambda item: (item["start"], item["end"], item["id"]),
    )
    _reject_same_panel_overlaps(
        [
            phrase
            for replacement in replacements
            for phrase in replacement["display_phrases"]
        ]
    )
    return replacements


def normalize_caption_speaker_override_document(value: object, clip_id: str) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("Caption speaker override document must be a mapping")
    schema = value.get("schema")
    if schema not in {
        CAPTION_SPEAKER_OVERRIDES_SCHEMA,
        CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA,
    }:
        raise ValueError("Unsupported caption speaker override schema")
    v1_fields = {
        "schema",
        "clock",
        "clip_id",
        "transcript_revision",
        "overrides",
        "actor",
        "reason",
        "updated_at",
    }
    allowed = (
        v1_fields | {"text_replacements"}
        if schema == CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA
        else v1_fields
    )
    if set(value) != allowed:
        raise ValueError("Caption speaker override document fields do not match schema")
    if value.get("clock") != "source":
        raise ValueError("Caption speaker overrides must use the source clock")
    if value.get("clip_id") != clip_id:
        raise ValueError("Caption speaker override document names another clip")
    transcript_revision = str(value.get("transcript_revision") or "")
    if not _SHA256.fullmatch(transcript_revision):
        raise ValueError("Caption speaker overrides need a transcript SHA-256")
    overrides = value.get("overrides")
    if not isinstance(overrides, list):
        raise TypeError("Stored caption speaker overrides must be a list")
    if schema == CAPTION_SPEAKER_OVERRIDES_SCHEMA and not overrides:
        raise ValueError("Stored caption speaker overrides cannot be empty")
    normalized = sorted(
        (_normalize_override(override) for override in overrides),
        key=lambda override: (override["start"], override["end"], override["id"]),
    )
    text_replacements = (
        _normalize_text_replacements(value.get("text_replacements"))
        if schema == CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA
        else []
    )
    ids = [override["id"] for override in normalized] + [
        replacement["id"] for replacement in text_replacements
    ]
    if len(ids) != len(set(ids)):
        raise ValueError("Caption override operation ids must be unique")
    document = {
        "schema": schema,
        "clock": "source",
        "clip_id": clip_id,
        "transcript_revision": transcript_revision,
        "overrides": normalized,
        "actor": _bounded_text(value.get("actor"), "actor", max_length=120),
        "reason": _bounded_text(
            value.get("reason"), "reason", min_length=3, max_length=1000
        ),
        "updated_at": _review_timestamp(value.get("updated_at")),
    }
    if schema == CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA:
        document["text_replacements"] = text_replacements
    return document


def caption_speaker_override_revision(clip_id: str, document: dict | None) -> str:
    """Return only the caption-pixel identity, excluding review metadata."""
    state = {
        "schema": (
            document.get("schema")
            if document is not None
            else CAPTION_SPEAKER_OVERRIDES_SCHEMA
        ),
        "clock": "source",
        "clip_id": clip_id,
        "transcript_revision": (
            document.get("transcript_revision") if document is not None else None
        ),
        "overrides": (
            [
                {key: value for key, value in override.items() if key != "reason"}
                for override in document.get("overrides", [])
            ]
            if document is not None
            else []
        ),
    }
    if (
        document is not None
        and document.get("schema") == CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA
    ):
        state["text_replacements"] = [
            {key: value for key, value in replacement.items() if key != "reason"}
            for replacement in document.get("text_replacements", [])
        ]
    return _json_revision(state)


def caption_speaker_override_document_revision(
    clip_id: str, document: dict | None
) -> str:
    """Return the mutation CAS identity, including review metadata."""
    caption_speaker_overrides_path(Path("."), clip_id)
    return _json_revision(
        document
        if document is not None
        else {
            "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
            "clip_id": clip_id,
            "document": None,
        }
    )


def _word_identity(word: Mapping[str, object]) -> dict:
    start = _finite_number(word.get("start"), "transcript word start")
    end = _finite_number(word.get("end"), "transcript word end")
    if end <= start:
        raise ValueError("Transcript word end must be after its start")
    return {
        "word": _bounded_text(word.get("word"), "transcript word", max_length=200),
        "punctuated_word": _bounded_text(
            word.get("punctuated_word"),
            "punctuated transcript word",
            max_length=200,
        ),
        "start": start,
        "end": end,
    }


def _clip_bounds(clip: Mapping[str, object]) -> tuple[float, float]:
    start = _finite_number(clip.get("start_seconds", clip.get("start")), "clip start")
    end = _finite_number(clip.get("end_seconds", clip.get("end")), "clip end")
    if end <= start:
        raise ValueError("Clip end must be after its start")
    return start, end


def _source_word_matches(
    utterances: list, operation: Mapping[str, object]
) -> list[tuple[int, int, dict]]:
    matches = []
    for utterance_index, utterance in enumerate(utterances):
        if not isinstance(utterance, Mapping):
            continue
        utterance_speaker = utterance.get("speaker")
        words = utterance.get("words")
        if not isinstance(words, list):
            continue
        for word_index, word in enumerate(words):
            if not isinstance(word, Mapping):
                continue
            identity = _word_identity(word)
            midpoint = (identity["start"] + identity["end"]) / 2
            speaker = word.get("speaker", utterance_speaker)
            if (
                operation["start"] <= midpoint < operation["end"]
                and speaker == operation["from_asr_speaker"]
            ):
                matches.append((utterance_index, word_index, identity))
    return matches


def _select_operation_words(
    utterances: list,
    operation: Mapping[str, object],
    label: str,
    speaker_targets: Mapping[int, str],
    clip_bounds: tuple[float, float],
    used_words: set[tuple[int, int]],
) -> list[tuple[int, int, dict]]:
    clip_start, clip_end = clip_bounds
    if operation["start"] < clip_start or operation["end"] > clip_end:
        raise ValueError(f"{label} {operation['id']} is outside its clip")
    source = operation["from_asr_speaker"]
    if speaker_targets.get(source) != operation["source_speaker"]:
        raise ValueError(
            f"ASR speaker {source} no longer maps to {operation['source_speaker']}"
        )
    matches = _source_word_matches(utterances, operation)
    if [match[2] for match in matches] != operation["expected_words"]:
        raise ValueError(
            f"{label} {operation['id']} no longer matches the reviewed source words"
        )
    selected = {
        (utterance_index, word_index) for utterance_index, word_index, _ in matches
    }
    if selected & used_words:
        raise ValueError("Caption speaker overrides select the same word twice")
    used_words.update(selected)
    return matches


def _replacement_utterances(
    replacement: Mapping[str, object], speaker_targets: Mapping[int, str]
) -> list[dict]:
    utterances = []
    for phrase in replacement["display_phrases"]:
        target = phrase["to_asr_speaker"]
        if speaker_targets.get(target) != phrase["target_crop"]:
            raise ValueError(
                f"ASR speaker {target} no longer maps to {phrase['target_crop']}"
            )
        words = [
            {
                **word,
                "speaker": target,
                CAPTION_DISPLAY_PHRASE_ID: f"{replacement['id']}:{phrase['id']}",
            }
            for word in phrase["words"]
        ]
        phrase_start, phrase_end = _phrase_bounds(phrase)
        utterances.append(
            {
                "speaker": target,
                "start": phrase_start,
                "end": phrase_end,
                "transcript": " ".join(
                    word["punctuated_word"] for word in phrase["words"]
                ),
                "words": words,
            }
        )
    return utterances


def _remove_replaced_words(
    utterances: list, replaced_words: set[tuple[int, int]]
) -> None:
    for utterance_index, utterance in enumerate(utterances):
        words = utterance.get("words") if isinstance(utterance, Mapping) else None
        if isinstance(words, list):
            utterance["words"] = [
                word
                for word_index, word in enumerate(words)
                if (utterance_index, word_index) not in replaced_words
            ]


def _apply_document(
    diarized: dict,
    document: dict,
    speaker_targets: Mapping[int, str],
    clip: Mapping[str, object],
) -> tuple[dict, int]:
    reviewed = deepcopy(diarized)
    clip_bounds = _clip_bounds(clip)
    selected: list[tuple[int, int, int]] = []
    replaced_words: set[tuple[int, int]] = set()
    replacement_utterances: list[dict] = []
    used_words: set[tuple[int, int]] = set()
    utterances = reviewed.get("utterances")
    if not isinstance(utterances, list):
        raise TypeError("Current transcript has no utterance list")

    for override in document["overrides"]:
        target = override["to_asr_speaker"]
        if speaker_targets.get(target) != override["target_crop"]:
            raise ValueError(
                f"ASR speaker {target} no longer maps to {override['target_crop']}"
            )
        matches = _select_operation_words(
            utterances,
            override,
            "Caption speaker override",
            speaker_targets,
            clip_bounds,
            used_words,
        )
        for utterance_index, word_index, _identity in matches:
            selected.append((utterance_index, word_index, target))

    for replacement in document.get("text_replacements", []):
        matches = _select_operation_words(
            utterances,
            replacement,
            "Caption text replacement",
            speaker_targets,
            clip_bounds,
            used_words,
        )
        replaced_words.update(
            (utterance_index, word_index)
            for utterance_index, word_index, _identity in matches
        )
        replacement_utterances.extend(
            _replacement_utterances(replacement, speaker_targets)
        )

    for utterance_index, word_index, target in selected:
        reviewed["utterances"][utterance_index]["words"][word_index]["speaker"] = target
    if replaced_words:
        _remove_replaced_words(reviewed["utterances"], replaced_words)
        reviewed["utterances"].extend(replacement_utterances)
        reviewed[CAPTION_TEXT_REPLACEMENTS_APPLIED] = (
            CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA
        )
    return reviewed, len(used_words)


def _read_document(
    path: Path,
) -> tuple[object | None, str | None, str | None, bool]:
    if not path.exists():
        return None, None, None, False
    try:
        stored_revision = file_fingerprint(path)["id"]
    except OSError as exc:
        return (
            None,
            f"Caption speaker override file cannot be read: {exc}",
            None,
            True,
        )
    try:
        value = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return (
            None,
            f"Caption speaker override file is invalid ({stored_revision}): {exc}",
            stored_revision,
            True,
        )
    return value, None, stored_revision, True


def _bound_transcript_revision(path: Path, diarized: dict) -> str:
    """Bind the supplied canonical object to one stable on-disk revision."""
    before = file_fingerprint(path)["id"]
    try:
        stored = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("Canonical transcript cannot be inspected") from exc
    after = file_fingerprint(path)["id"]
    if before != after or stored != diarized:
        raise ValueError("Canonical transcript changed while it was inspected")
    return before


def validate_caption_speaker_override_document(
    document: dict,
    *,
    clip: Mapping[str, object],
    diarized: dict,
    segment_document: dict,
    crop_config: dict,
    transcript_revision: str,
) -> tuple[dict, int]:
    """Validate and apply one proposed document without touching canonical state."""
    clip_id = str(clip.get("id") or "")
    normalized = normalize_caption_speaker_override_document(document, clip_id)
    if normalized["transcript_revision"] != transcript_revision:
        raise ValueError(
            "Caption speaker overrides were reviewed against another transcript"
        )
    speaker_targets = resolve_caption_speaker_targets(
        diarized, segment_document, crop_config
    )
    return _apply_document(diarized, normalized, speaker_targets, clip)


def caption_speaker_override_state(
    episode_dir: Path,
    clip: Mapping[str, object],
    diarized: dict,
    segment_document: dict,
    crop_config: dict,
) -> tuple[dict, dict]:
    """Return inspectable state plus the caption-only effective transcript."""
    clip_id = str(clip.get("id") or "")
    caption_speaker_overrides_path(episode_dir, clip_id)
    clip_bounds = _clip_bounds(clip)
    transcript_path = Path(episode_dir) / "diarized_transcript.json"
    transcript_revision = _bound_transcript_revision(transcript_path, diarized)
    speaker_targets = resolve_caption_speaker_targets(
        diarized, segment_document, crop_config
    )
    path = caption_speaker_overrides_path(episode_dir, clip_id)
    raw, read_error, stored_revision, document_present = _read_document(path)
    document = None
    error = read_error
    if document_present and error is None:
        try:
            document = normalize_caption_speaker_override_document(raw, clip_id)
        except (TypeError, ValueError) as exc:
            error = str(exc)

    overrides_revision = caption_speaker_override_revision(clip_id, document)
    document_revision = (
        caption_speaker_override_document_revision(clip_id, document)
        if document is not None
        else stored_revision
        or caption_speaker_override_document_revision(clip_id, None)
    )
    binding_state = [
        {"asr_speaker": source, "target_speaker": target}
        for source, target in sorted(speaker_targets.items())
    ]
    schema = (
        document.get("schema")
        if document is not None
        else CAPTION_SPEAKER_OVERRIDES_SCHEMA
    )
    revision_state = {
        "schema": schema,
        "clip_id": clip_id,
        "clip_source_window": list(clip_bounds),
        "transcript_revision": transcript_revision,
        "speaker_targets": binding_state,
        "overrides_revision": overrides_revision,
        "document_revision": document_revision,
        "read_error": error,
    }
    revision = _json_revision(revision_state)
    effective = deepcopy(diarized)
    applied_word_count = 0
    if document is not None and error is None:
        if document["transcript_revision"] != transcript_revision:
            error = "Caption speaker overrides were reviewed against another transcript"
        else:
            try:
                effective, applied_word_count = _apply_document(
                    diarized, document, speaker_targets, clip
                )
            except (TypeError, ValueError) as exc:
                error = str(exc)

    override_count = len(document["overrides"]) if document is not None else 0
    state = {
        "schema": schema,
        "clock": "source",
        "clip_id": clip_id,
        "clip_source_window": list(clip_bounds),
        "current": error is None,
        "revision": revision,
        "transcript_revision": transcript_revision,
        "overrides_revision": overrides_revision,
        "document_revision": document_revision,
        "override_count": override_count,
        "applied_word_count": applied_word_count,
        "overrides": document["overrides"] if document is not None else [],
        "speaker_targets": binding_state,
    }
    if schema == CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA:
        replacements = document.get("text_replacements", []) if document else []
        state["text_replacements"] = replacements
    if document is not None:
        state["review"] = {
            key: document[key] for key in ("actor", "reason", "updated_at")
        }
    if error is not None:
        state["error"] = error
    return state, effective


def require_current_caption_speaker_overrides(
    episode_dir: Path,
    clip: Mapping[str, object],
    diarized: dict,
    segment_document: dict,
    crop_config: dict,
) -> tuple[dict, dict]:
    state, effective = caption_speaker_override_state(
        episode_dir, clip, diarized, segment_document, crop_config
    )
    if not state["current"]:
        raise ValueError(state.get("error") or "Caption speaker overrides are stale")
    return effective, state


def caption_speaker_overrides_binding(state: Mapping[str, object]) -> dict | None:
    """Return only pixel-affecting override identity for a variant manifest."""
    if state.get("current") is not True:
        raise ValueError("Caption speaker overrides are not current")
    count = state.get("override_count")
    applied = state.get("applied_word_count")
    overrides = state.get("overrides")
    if not isinstance(count, int) or not isinstance(applied, int):
        raise TypeError("Caption speaker override counts are invalid")
    if not isinstance(overrides, list) or len(overrides) != count:
        raise TypeError("Caption speaker override state is malformed")
    has_text_replacements = (
        state.get("schema") == CAPTION_SPEAKER_TEXT_REPLACEMENTS_SCHEMA
    )
    if count == 0 and not has_text_replacements:
        if applied != 0:
            raise TypeError("Empty caption speaker overrides changed words")
        return None
    revision = str(state.get("overrides_revision") or "")
    if not _SHA256.fullmatch(revision) or applied <= 0:
        raise TypeError("Caption speaker override identity is invalid")
    operation_ids = [
        override.get("id") if isinstance(override, Mapping) else None
        for override in overrides
    ]
    if any(not isinstance(operation_id, str) for operation_id in operation_ids):
        raise TypeError("Caption speaker override operation ids are invalid")
    replacements = state.get("text_replacements") if has_text_replacements else []
    if not isinstance(replacements, list) or (
        has_text_replacements and not replacements
    ):
        raise TypeError("Caption text replacement identity is invalid")
    replacement_ids = [
        replacement.get("id") if isinstance(replacement, Mapping) else None
        for replacement in replacements
    ]
    if any(not isinstance(operation_id, str) for operation_id in replacement_ids):
        raise TypeError("Caption text replacement ids are invalid")
    operation_ids.extend(replacement_ids)
    binding = {
        "schema": state.get("schema"),
        "clock": "source",
        "revision": revision,
        "operation_ids": operation_ids,
        "override_count": count,
        "applied_word_count": applied,
    }
    if has_text_replacements:
        binding["text_replacement_count"] = len(replacements)
    return binding


def apply_current_caption_speaker_overrides(
    episode_dir: Path,
    clip: Mapping[str, object],
    diarized: dict,
    segment_document: dict,
    crop_config: dict,
) -> tuple[dict, dict | None]:
    """Return effective panel-caption words and their optional manifest binding."""
    clip_id = str(clip.get("id") or "")
    if not caption_speaker_overrides_path(episode_dir, clip_id).exists():
        return diarized, None
    effective, state = require_current_caption_speaker_overrides(
        episode_dir, clip, diarized, segment_document, crop_config
    )
    return effective, caption_speaker_overrides_binding(state)
