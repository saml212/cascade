"""Reviewed word-level speaker attribution used only by short captions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from lib.ass import resolve_caption_speaker_targets
from lib.ffprobe import file_fingerprint

CAPTION_SPEAKER_OVERRIDES_SCHEMA = "cascade.short-caption-speaker-overrides/v1"
CAPTION_SPEAKER_OVERRIDES_DIR = "caption_speaker_overrides"

_CLIP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CROP_SPEAKER = re.compile(r"^speaker_[0-9]+$")


def _json_revision(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return round(number, 6)


def _speaker_id(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"{field} must be a non-negative integer")
    return value


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} must be nonempty text")
    return value.strip()


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
        "word": _nonempty_text(value.get("word"), "expected_words.word"),
        "punctuated_word": _nonempty_text(
            value.get("punctuated_word"), "expected_words.punctuated_word"
        ),
        "start": start,
        "end": end,
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
    source_speaker = _nonempty_text(value.get("source_speaker"), "source_speaker")
    target_crop = _nonempty_text(value.get("target_crop"), "target_crop")
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
        "id": _nonempty_text(value.get("id"), "override.id"),
        "start": start,
        "end": end,
        "from_asr_speaker": source,
        "to_asr_speaker": target,
        "source_speaker": source_speaker,
        "target_crop": target_crop,
        "reason": _nonempty_text(value.get("reason"), "override.reason"),
        "expected_words": expected_words,
    }


def normalize_caption_speaker_override_document(value: object, clip_id: str) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("Caption speaker override document must be a mapping")
    allowed = {
        "schema",
        "clock",
        "clip_id",
        "transcript_revision",
        "overrides",
        "actor",
        "reason",
        "updated_at",
    }
    if set(value) != allowed:
        raise ValueError("Caption speaker override document fields do not match schema")
    if value.get("schema") != CAPTION_SPEAKER_OVERRIDES_SCHEMA:
        raise ValueError("Unsupported caption speaker override schema")
    if value.get("clock") != "source":
        raise ValueError("Caption speaker overrides must use the source clock")
    if value.get("clip_id") != clip_id:
        raise ValueError("Caption speaker override document names another clip")
    transcript_revision = str(value.get("transcript_revision") or "")
    if not _SHA256.fullmatch(transcript_revision):
        raise ValueError("Caption speaker overrides need a transcript SHA-256")
    overrides = value.get("overrides")
    if not isinstance(overrides, list) or not overrides:
        raise ValueError("Stored caption speaker overrides cannot be empty")
    normalized = sorted(
        (_normalize_override(override) for override in overrides),
        key=lambda override: (override["start"], override["end"], override["id"]),
    )
    ids = [override["id"] for override in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("Caption speaker override ids must be unique")
    return {
        "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
        "clock": "source",
        "clip_id": clip_id,
        "transcript_revision": transcript_revision,
        "overrides": normalized,
        "actor": _nonempty_text(value.get("actor"), "actor"),
        "reason": _nonempty_text(value.get("reason"), "reason"),
        "updated_at": _nonempty_text(value.get("updated_at"), "updated_at"),
    }


def caption_speaker_override_revision(clip_id: str, document: dict | None) -> str:
    state = {
        "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
        "clock": "source",
        "clip_id": clip_id,
        "transcript_revision": (
            document.get("transcript_revision") if document is not None else None
        ),
        "overrides": document.get("overrides", []) if document is not None else [],
    }
    return _json_revision(state)


def _word_identity(word: Mapping[str, object]) -> dict:
    start = _finite_number(word.get("start"), "transcript word start")
    end = _finite_number(word.get("end"), "transcript word end")
    if end <= start:
        raise ValueError("Transcript word end must be after its start")
    return {
        "word": _nonempty_text(word.get("word"), "transcript word"),
        "punctuated_word": _nonempty_text(
            word.get("punctuated_word"), "punctuated transcript word"
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


def _apply_document(
    diarized: dict,
    document: dict,
    speaker_targets: Mapping[int, str],
    clip: Mapping[str, object],
) -> tuple[dict, int]:
    reviewed = deepcopy(diarized)
    clip_start, clip_end = _clip_bounds(clip)
    selected: list[tuple[int, int, int]] = []
    used_words: set[tuple[int, int]] = set()
    utterances = reviewed.get("utterances")
    if not isinstance(utterances, list):
        raise TypeError("Current transcript has no utterance list")

    for override in document["overrides"]:
        if override["start"] < clip_start or override["end"] > clip_end:
            raise ValueError(
                f"Caption speaker override {override['id']} is outside its clip"
            )
        source = override["from_asr_speaker"]
        target = override["to_asr_speaker"]
        if speaker_targets.get(source) != override["source_speaker"]:
            raise ValueError(
                f"ASR speaker {source} no longer maps to {override['source_speaker']}"
            )
        if speaker_targets.get(target) != override["target_crop"]:
            raise ValueError(
                f"ASR speaker {target} no longer maps to {override['target_crop']}"
            )
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
                    override["start"] <= midpoint < override["end"]
                    and speaker == source
                ):
                    matches.append((utterance_index, word_index, identity))
        observed = [match[2] for match in matches]
        if observed != override["expected_words"]:
            raise ValueError(
                f"Caption speaker override {override['id']} no longer matches "
                "the reviewed source words"
            )
        for utterance_index, word_index, _identity in matches:
            key = (utterance_index, word_index)
            if key in used_words:
                raise ValueError("Caption speaker overrides select the same word twice")
            used_words.add(key)
            selected.append((utterance_index, word_index, target))

    for utterance_index, word_index, target in selected:
        reviewed["utterances"][utterance_index]["words"][word_index]["speaker"] = target
    return reviewed, len(selected)


def _read_document(path: Path) -> tuple[dict | None, str | None, str | None]:
    if not path.exists():
        return None, None, None
    try:
        stored_revision = file_fingerprint(path)["id"]
    except OSError as exc:
        return None, f"Caption speaker override file cannot be read: {exc}", None
    try:
        value = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return (
            None,
            f"Caption speaker override file is invalid ({stored_revision}): {exc}",
            stored_revision,
        )
    return value, None, stored_revision


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
    _clip_bounds(clip)
    transcript_path = Path(episode_dir) / "diarized_transcript.json"
    transcript_revision = _bound_transcript_revision(transcript_path, diarized)
    speaker_targets = resolve_caption_speaker_targets(
        diarized, segment_document, crop_config
    )
    path = caption_speaker_overrides_path(episode_dir, clip_id)
    raw, read_error, stored_revision = _read_document(path)
    document = None
    error = read_error
    if raw is not None:
        try:
            document = normalize_caption_speaker_override_document(raw, clip_id)
        except (TypeError, ValueError) as exc:
            error = str(exc)

    overrides_revision = caption_speaker_override_revision(clip_id, document)
    document_revision = (
        _json_revision(document)
        if document is not None
        else stored_revision
        or _json_revision(
            {
                "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
                "clip_id": clip_id,
                "document": None,
            }
        )
    )
    binding_state = [
        {"asr_speaker": source, "target_speaker": target}
        for source, target in sorted(speaker_targets.items())
    ]
    revision = _json_revision(
        {
            "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
            "clip_id": clip_id,
            "transcript_revision": transcript_revision,
            "speaker_targets": binding_state,
            "overrides_revision": overrides_revision,
            "document_revision": document_revision,
            "read_error": error,
        }
    )
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
        "schema": CAPTION_SPEAKER_OVERRIDES_SCHEMA,
        "clock": "source",
        "clip_id": clip_id,
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
