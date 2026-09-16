"""One read-only contract for reviewing current and previous media."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agents.pipeline import load_config
from agents.qa import (
    canonical_release_metadata,
    clip_review_revision,
    current_clip_boundary_evidence,
    editorial_revision,
)
from agents.speaker_cut import current_speaker_segments
from agents.transcribe import (
    build_transcript_clock_mapping,
    current_diarized_transcript,
    repair_existing_transcript,
    transcript_repair_revisions,
    validate_transcript_clock_mapping,
)
from lib.atomic_write import atomic_write_json
from lib.audio_mix import json_fingerprint, selected_audio_source
from lib.clips import clip_selection_status
from lib.delivery_video import (
    capture_transcript_render_reuse_proof,
    current_longform_render,
    current_short_render,
    longform_render_fingerprint,
    migrate_unchanged_transcript_render_fingerprints,
    read_render_manifest,
    render_artifact_state,
    short_render_fingerprint,
)
from lib.encoding import get_video_encoding_policy
from lib.ffprobe import media_fingerprint, probe
from lib.paths import get_episodes_dir
from lib.short_distribution import PLATFORM_COPY_FIELDS, SHORT_PLATFORM_SPECS
from lib.short_variants import (
    BACKGROUND_VARIANT_IDS,
    DISTRIBUTION_RELEASE_FIELD,
    SATISFYING_VARIANT_ID,
    background_variant_approval_state,
    background_variant_asset_ids,
    background_variant_label,
    background_variant_state,
    default_background_asset_id,
    require_background_variant,
    selected_short_variant_id,
)
from server.media_inspection import (
    InspectionTarget,
    file_revision,
    inspect_audio_window,
    inspect_media_window,
    resolve_target,
)
from server.routes import require_episode_dir
from server.routes.clips import (
    publication_change_lock,
    render_job_state,
    variant_render_job_id,
)

router = APIRouter(prefix="/api/episodes", tags=["review"])

_CLIP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_transcript_corrections_lock = threading.Lock()
_DERIVED_TRANSCRIPT_ARTIFACTS = (
    "transcript_corrections.json",
    "diarized_transcript.json",
    "transcript_provenance.json",
    "subtitles/transcript.srt",
    "segments.json",
    "render_manifest.json",
)


class TranscriptCorrectionsRequest(BaseModel):
    expected_revision: str
    operations: list[dict] = Field(min_length=1, max_length=100)


class TranscriptClockEvidence(BaseModel):
    method: str = Field(min_length=1, max_length=200)
    anchor_count: int = Field(ge=2, le=10000)
    summary: str = Field(min_length=1, max_length=2000)
    fit_r_squared: float | None = Field(default=None, ge=0, le=1)
    artifact_references: list[str] = Field(default_factory=list, max_length=20)


class TranscriptSpeakerBinding(BaseModel):
    asr_speaker: int = Field(ge=0)
    crop_speaker_index: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=200)
    evidence: str = Field(min_length=1, max_length=1000)


class TranscriptClockRepairRequest(BaseModel):
    expected_binding_revision: str = Field(min_length=1)
    source_seconds_per_asr_second: float = Field(ge=0.99, le=1.01)
    source_offset_seconds: float
    evidence: TranscriptClockEvidence
    speaker_bindings: list[TranscriptSpeakerBinding] | None = Field(
        default=None, min_length=1, max_length=100
    )


class _TranscriptRevisionConflict(RuntimeError):
    pass


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _transcript_corrections_document(episode_dir: Path) -> dict:
    raw_revision = file_revision(episode_dir / "transcript.json").removeprefix(
        "sha256:"
    )
    path = episode_dir / "transcript_corrections.json"
    if path.exists():
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise ValueError("Transcript corrections must be a JSON object")
    else:
        document = {}
    if document.get("version", 1) != 1 or document.get("clock", "source") != "source":
        raise ValueError("Transcript corrections must use version 1 and source clock")
    operations = document.get("operations", [])
    if not isinstance(operations, list) or not all(
        isinstance(operation, dict) for operation in operations
    ):
        raise ValueError("Transcript correction operations must be objects")
    expected_raw = document.get("raw_transcript_sha256")
    if expected_raw and expected_raw != raw_revision:
        raise _TranscriptRevisionConflict(
            "Transcript corrections target a different raw transcript"
        )
    result = dict(document)
    result.update(
        version=1,
        clock="source",
        raw_transcript_sha256=raw_revision,
        operations=operations,
    )
    return result


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _restore_transcript_artifacts(snapshot: dict[Path, bytes | None]) -> None:
    errors = []
    for path, content in snapshot.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, content)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        raise RuntimeError(
            "Could not restore transcript artifacts: " + "; ".join(errors)
        )


def _snapshot_transcript_artifacts(episode_dir: Path) -> dict[Path, bytes | None]:
    paths = [episode_dir / relative for relative in _DERIVED_TRANSCRIPT_ARTIFACTS]
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _transcript_render_inputs(
    episode_dir: Path, episode: dict, config: dict
) -> tuple[Path, list[dict], list[dict]] | None:
    try:
        audio = selected_audio_source(episode_dir, episode, config) or (
            episode_dir / "work" / "audio_mix.wav"
        )
        plan = current_speaker_segments(episode_dir, episode, config)
        segments = plan.get("segments", []) if plan else []
        clips = _stored_clips(episode_dir)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    if not audio.is_file() or not segments:
        return None
    return audio, segments, clips


def _capture_transcript_render_reuse(
    episode_dir: Path, episode: dict, config: dict
) -> dict | None:
    inputs = _transcript_render_inputs(episode_dir, episode, config)
    if inputs is None:
        return None
    return capture_transcript_render_reuse_proof(episode_dir, episode, config, *inputs)


def _migrate_transcript_render_reuse(
    episode_dir: Path, episode: dict, config: dict, proof: dict | None
) -> dict | None:
    inputs = _transcript_render_inputs(episode_dir, episode, config)
    if proof is None or inputs is None:
        return None
    return migrate_unchanged_transcript_render_fingerprints(
        episode_dir, episode, config, *inputs, proof
    )


def _transcript_media_state(episode_dir: Path) -> dict:
    before = transcript_repair_revisions(episode_dir)
    source = episode_dir / "source_merged.mp4"
    asr_input = episode_dir / before["asr_input_identity"]["relative_path"]
    source_probe = probe(source)
    duration = float(source_probe.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Source media duration is unavailable")
    source_content = media_fingerprint(source, source_probe)
    asr_content = media_fingerprint(asr_input, probe(asr_input))
    after = transcript_repair_revisions(episode_dir)
    if before != after:
        raise OSError("Transcript or source media changed during inspection")
    return {
        **after,
        "source_duration_seconds": duration,
        "source_content_fingerprint": source_content,
        "asr_input_content_fingerprint": asr_content,
    }


def _transcript_binding_revision(episode_dir: Path, state: dict) -> str:
    transcript_path = episode_dir / "diarized_transcript.json"
    segments_path = episode_dir / "segments.json"
    transcript_revision = (
        file_revision(transcript_path) if transcript_path.is_file() else None
    )
    speaker_plan_revision = (
        file_revision(segments_path) if segments_path.is_file() else None
    )
    episode_revision = file_revision(episode_dir / "episode.json")
    return json_fingerprint(
        {
            **state,
            "episode_revision": episode_revision,
            "speaker_plan_revision": speaker_plan_revision,
            "transcript_revision": transcript_revision,
        }
    )


def _transcript_repair_document(episode_dir: Path, config: dict) -> dict:
    state = _transcript_media_state(episode_dir)
    provenance = _read_json(episode_dir / "transcript_provenance.json", {})
    mapping = provenance.get("clock_mapping")
    mapping_current = False
    if mapping is not None:
        try:
            mapping = validate_transcript_clock_mapping(episode_dir, mapping)
            mapping_current = (
                mapping["source_content_fingerprint"]["id"]
                == state["source_content_fingerprint"]["id"]
                and mapping["asr_input_content_fingerprint"]["id"]
                == state["asr_input_content_fingerprint"]["id"]
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
    episode = _read_json(episode_dir / "episode.json", {})
    current = current_diarized_transcript(episode_dir, episode, config) is not None
    transcript_path = episode_dir / "diarized_transcript.json"
    return {
        "schema": "cascade.transcript-clock-repair/v1",
        "clock": "source",
        **state,
        "binding_revision": _transcript_binding_revision(episode_dir, state),
        "mapping_current": mapping_current,
        "mapping": mapping,
        "speaker_bindings": provenance.get("speaker_map_override"),
        "episode_revision": file_revision(episode_dir / "episode.json"),
        "speaker_plan_revision": (
            file_revision(episode_dir / "segments.json")
            if (episode_dir / "segments.json").is_file()
            else None
        ),
        "transcript_current": current,
        "transcript_revision": (
            file_revision(transcript_path) if transcript_path.is_file() else None
        ),
    }


def _apply_transcript_clock_repair(
    episode_dir: Path, config: dict, request: TranscriptClockRepairRequest
) -> dict:
    with _transcript_corrections_lock:
        before = _transcript_media_state(episode_dir)
        if request.expected_binding_revision != _transcript_binding_revision(
            episode_dir, before
        ):
            raise _TranscriptRevisionConflict(
                "Transcript repair inputs changed; inspect the repair state again"
            )

        episode = _read_json(episode_dir / "episode.json", {})
        render_reuse_proof = _capture_transcript_render_reuse(
            episode_dir, episode, config
        )
        if request.expected_binding_revision != _transcript_binding_revision(
            episode_dir, before
        ):
            raise _TranscriptRevisionConflict(
                "Transcript repair inputs changed during render inspection; "
                "inspect the repair state again"
            )

        mapping = build_transcript_clock_mapping(
            episode_dir,
            source_seconds_per_asr_second=request.source_seconds_per_asr_second,
            source_offset_seconds=request.source_offset_seconds,
            source_duration_seconds=before["source_duration_seconds"],
            source_content_fingerprint=before["source_content_fingerprint"],
            asr_input_content_fingerprint=before["asr_input_content_fingerprint"],
            evidence=request.evidence.model_dump(exclude_none=True),
        )
        snapshot = _snapshot_transcript_artifacts(episode_dir)
        try:
            result = repair_existing_transcript(
                episode_dir,
                config,
                clock_mapping_document=mapping,
                speaker_bindings=(
                    [binding.model_dump() for binding in request.speaker_bindings]
                    if request.speaker_bindings is not None
                    else None
                ),
            )
            after = _transcript_media_state(episode_dir)
            if json_fingerprint(after) != json_fingerprint(before):
                raise RuntimeError(
                    "Transcript or source media changed during local repair"
                )
            if current_diarized_transcript(episode_dir, episode, config) is None:
                raise RuntimeError(
                    "Repaired transcript did not pass currentness checks"
                )
            if request.speaker_bindings is not None:
                current_segments = current_speaker_segments(
                    episode_dir, episode, config
                )
                if result.get("speaker_alignment") is None or current_segments is None:
                    raise RuntimeError(
                        "Speaker bindings require a current aligned speaker plan; "
                        "configure crops and rerun speaker_cut before retrying"
                    )
                available_targets = {
                    row.get("speaker")
                    for row in current_segments.get("track_mapping", [])
                }
                missing_targets = sorted(
                    {
                        row["target_speaker"]
                        for row in result.get("speaker_map", [])
                        if row.get("target_speaker") != "BOTH"
                    }
                    - available_targets
                )
                if missing_targets:
                    raise RuntimeError(
                        "Current speaker plan is missing reviewed crop targets: "
                        + ", ".join(missing_targets)
                    )
            render_reuse = _migrate_transcript_render_reuse(
                episode_dir, episode, config, render_reuse_proof
            )
        except BaseException:
            try:
                inputs_unchanged = json_fingerprint(
                    _transcript_media_state(episode_dir)
                ) == json_fingerprint(before)
            except (
                json.JSONDecodeError,
                OSError,
                subprocess.CalledProcessError,
                TypeError,
                ValueError,
            ):
                inputs_unchanged = False
            if inputs_unchanged:
                _restore_transcript_artifacts(snapshot)
            raise

        return {
            "mapping": mapping,
            "transcript_revision": file_revision(
                episode_dir / "diarized_transcript.json"
            ),
            "speaker_alignment": result.get("speaker_alignment"),
            "speaker_map": result.get("speaker_map", []),
            "render_reuse": render_reuse,
            "raw_transcript_unchanged": True,
            "source_media_unchanged": True,
        }


def _apply_transcript_corrections(
    episode_dir: Path,
    config: dict,
    expected_revision: str,
    requested_operations: list[dict],
) -> dict:
    if not expected_revision:
        raise ValueError("expected_revision is required")
    if not requested_operations:
        raise ValueError("At least one transcript correction operation is required")

    with _transcript_corrections_lock:
        episode = _read_json(episode_dir / "episode.json", {})
        transcript_path = episode_dir / "diarized_transcript.json"
        if current_diarized_transcript(episode_dir, episode, config) is None:
            raise _TranscriptRevisionConflict(
                "Current canonical transcript is unavailable; repair it before applying corrections"
            )
        current_revision = file_revision(transcript_path)
        if current_revision != expected_revision:
            raise _TranscriptRevisionConflict(
                f"Transcript revision changed; expected {expected_revision}, current {current_revision}"
            )
        require_current_alignment = (
            current_speaker_segments(episode_dir, episode, config) is not None
        )

        corrections = _transcript_corrections_document(episode_dir)
        operations = [dict(operation) for operation in corrections["operations"]]
        positions = {}
        for index, operation in enumerate(operations):
            operation_id = operation.get("id")
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise ValueError("Every stored transcript correction must have an id")
            if operation_id in positions:
                raise ValueError(
                    f"Duplicate stored transcript correction id: {operation_id}"
                )
            positions[operation_id] = index

        request_ids = []
        added_ids = []
        updated_ids = []
        unchanged_ids = []
        for requested in requested_operations:
            operation = dict(requested)
            operation_id = operation.get("id")
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise ValueError(
                    "Every transcript correction operation must have an id"
                )
            if operation_id in request_ids:
                raise ValueError(
                    f"Duplicate requested transcript correction id: {operation_id}"
                )
            request_ids.append(operation_id)
            if operation_id not in positions:
                positions[operation_id] = len(operations)
                operations.append(operation)
                added_ids.append(operation_id)
            elif operations[positions[operation_id]] == operation:
                unchanged_ids.append(operation_id)
            else:
                operations[positions[operation_id]] = operation
                updated_ids.append(operation_id)

        if not added_ids and not updated_ids:
            return {
                "revision": current_revision,
                "correction_count": len(operations),
                "added_ids": [],
                "updated_ids": [],
                "unchanged_ids": unchanged_ids,
                "speaker_alignment": None,
                "render_reuse": None,
            }

        corrections = {**corrections, "operations": operations}
        render_reuse_proof = _capture_transcript_render_reuse(
            episode_dir, episode, config
        )
        if file_revision(transcript_path) != current_revision:
            raise _TranscriptRevisionConflict(
                "Transcript changed during render inspection; inspect it again"
            )
        snapshot = _snapshot_transcript_artifacts(episode_dir)
        try:
            atomic_write_json(episode_dir / "transcript_corrections.json", corrections)
            result = repair_existing_transcript(episode_dir, config)
            if (
                file_revision(episode_dir / "transcript.json").removeprefix("sha256:")
                != corrections["raw_transcript_sha256"]
            ):
                raise RuntimeError(
                    "Raw transcript changed during local recanonicalization"
                )
            if current_diarized_transcript(episode_dir, episode, config) is None:
                raise RuntimeError(
                    "Recanonicalized transcript did not pass currentness checks"
                )
            if require_current_alignment and (
                current_speaker_segments(episode_dir, episode, config) is None
            ):
                raise RuntimeError(
                    "Rebuilt speaker alignment did not pass currentness checks"
                )
            render_reuse = _migrate_transcript_render_reuse(
                episode_dir, episode, config, render_reuse_proof
            )
        except BaseException:
            try:
                raw_changed = (
                    file_revision(episode_dir / "transcript.json").removeprefix(
                        "sha256:"
                    )
                    != corrections["raw_transcript_sha256"]
                )
            except OSError:
                raw_changed = True
            if raw_changed:
                correction_path = episode_dir / "transcript_corrections.json"
                _restore_transcript_artifacts(
                    {correction_path: snapshot[correction_path]}
                )
            else:
                _restore_transcript_artifacts(snapshot)
            raise

        return {
            "revision": file_revision(transcript_path),
            "correction_count": len(operations),
            "added_ids": added_ids,
            "updated_ids": updated_ids,
            "unchanged_ids": unchanged_ids,
            "speaker_alignment": result.get("speaker_alignment"),
            "render_reuse": render_reuse,
        }


def _with_media_url(state: dict, episode_id: str) -> dict:
    state = dict(state)
    if state["playable"]:
        path = "/".join(quote(part, safe="") for part in state["path"].split("/"))
        revision = f"{int(state['size_bytes']):x}-{int(state['mtime_ns']):x}"
        state["media_revision"] = revision
        state["url"] = (
            f"/media/episodes/{quote(episode_id, safe='')}/{path}?v={revision}"
        )
        state["download_url"] = state["url"]
    else:
        state["url"] = None
        state["download_url"] = None
    return state


def _enabled_destinations(config: dict) -> list[dict]:
    configured = config.get("platforms", {})
    return [
        {
            "key": key,
            "label": SHORT_PLATFORM_SPECS[key]["label"],
            "required_fields": list(fields),
        }
        for key, fields in PLATFORM_COPY_FIELDS.items()
        if configured.get(key, {}).get("enabled") is True
    ]


def _has_copy(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _metadata_state(copy: dict, destinations: list[dict]) -> dict:
    states = []
    for destination in destinations:
        platform_copy = copy.get(destination["key"], {})
        if not isinstance(platform_copy, dict):
            platform_copy = {}
        missing = [
            field
            for field in destination["required_fields"]
            if not _has_copy(platform_copy.get(field))
        ]
        states.append(
            {
                **destination,
                "complete": not missing,
                "missing_fields": missing,
            }
        )
    complete_count = sum(item["complete"] for item in states)
    return {
        "complete": complete_count == len(states),
        "enabled_destination_count": len(states),
        "complete_destination_count": complete_count,
        "destinations": states,
    }


def _approval_state(
    clip: dict,
    render: dict,
    render_record: dict,
    metadata_entry: dict | None,
) -> dict:
    revision = clip_review_revision(clip, render_record, metadata_entry)
    state = {"revision": revision}
    if clip.get("status") == "rejected":
        return {"status": "rejected", "current": False, **state}
    if clip.get("status") != "approved":
        return {"status": "unapproved", "current": False, **state}
    if not render["current"]:
        return {"status": "stale", "current": False, **state}
    current = (
        clip.get("approved_render_fingerprint") == render["recorded_fingerprint"]
        and clip.get("approved_revision") == revision
    )
    return {
        "status": "current" if current else "stale",
        "current": current,
        **state,
    }


def _expected_fingerprints(
    episode_dir: Path, episode: dict, clips: list[dict], config: dict
) -> tuple[str | None, dict[str, str | None]]:
    audio = None
    try:
        audio = selected_audio_source(episode_dir, episode, config) or (
            episode_dir / "work" / "audio_mix.wav"
        )
        current_segments = current_speaker_segments(episode_dir, episode, config)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        current_segments = None
    segments = (
        current_segments.get("segments", [])
        if isinstance(current_segments, dict)
        else []
    )
    if audio is None or not audio.is_file() or not segments:
        return None, {str(clip.get("id")): None for clip in clips}
    try:
        current_longform = current_longform_render(
            episode_dir, episode, config, audio, segments
        )
        longform = (
            current_longform["fingerprint"]
            if current_longform
            else longform_render_fingerprint(
                episode_dir, episode, config, audio, segments
            )
        )
        shorts = {}
        for clip in clips:
            if not clip.get("id"):
                continue
            current_short = current_short_render(
                episode_dir, episode, config, audio, segments, clip
            )
            shorts[str(clip["id"])] = (
                current_short["fingerprint"]
                if current_short
                else short_render_fingerprint(
                    episode_dir, episode, config, audio, segments, clip
                )
            )
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None, {str(clip.get("id")): None for clip in clips}
    return longform, shorts


def _boundary_evidence_with_inspection(
    episode_id: str,
    episode_dir: Path,
    episode: dict,
    clips: list[dict],
    config: dict,
) -> dict:
    evidence = current_clip_boundary_evidence(episode_dir, episode, config, clips)
    endpoint = f"/api/episodes/{quote(episode_id, safe='')}/inspection/preview"
    for clip in evidence["clips"]:
        for finding in clip["findings"]:
            finding["inspection_request"] = {
                "method": "GET",
                "endpoint": endpoint,
                "query": {
                    "target": "source",
                    "clock": "source",
                    "seconds": round(
                        max(0.0, float(finding["boundary_seconds"]) - 2.0), 3
                    ),
                    "duration_seconds": 4.0,
                },
            }
    return evidence


def episode_review_state(episode_dir: Path) -> dict:
    """Build the canonical current review state for routes and local agents."""
    episode_dir = Path(episode_dir)
    episode_id = episode_dir.name
    episode = _read_json(episode_dir / "episode.json", {})
    clips_data = _read_json(episode_dir / "clips.json", {"clips": []})
    clips = clips_data.get("clips", []) if isinstance(clips_data, dict) else clips_data
    clips = [clip for clip in clips if isinstance(clip, dict) and clip.get("id")]
    config = load_config()
    destinations = _enabled_destinations(config)
    metadata = canonical_release_metadata(episode_dir, episode, clips)
    metadata_by_id = {
        str(item["id"]): item
        for item in metadata.get("clips", [])
        if isinstance(item, dict) and item.get("id")
    }
    approval_metadata = _read_json(
        episode_dir / "metadata" / "metadata.json", {"clips": []}
    )
    approval_metadata_by_id = {
        str(item["id"]): item
        for item in approval_metadata.get("clips", [])
        if isinstance(item, dict) and item.get("id")
    }
    manifest = read_render_manifest(episode_dir)
    expected_longform, expected_shorts = _expected_fingerprints(
        episode_dir, episode, clips, config
    )
    canonical = _with_media_url(
        render_artifact_state(
            episode_dir,
            Path("upload_video.mp4"),
            manifest.get("longform"),
            expected_fingerprint=expected_longform,
            expected_mode="speaker_cut",
        ),
        episode_id,
    )
    legacy = _with_media_url(
        render_artifact_state(
            episode_dir,
            Path("longform.mp4"),
            None,
            expected_fingerprint=None,
            expected_mode=None,
        ),
        episode_id,
    )
    preferred = canonical if canonical["playable"] else legacy
    approval_revision = editorial_revision(episode_dir, episode)
    approval = episode.get("editorial_approval")
    approval_current = (
        canonical["current"]
        and isinstance(approval, dict)
        and approval.get("revision") == approval_revision
    )

    reviewed_clips = []
    short_records = manifest.get("shorts", {})
    variant_encoding = get_video_encoding_policy(config, "shorts")
    for clip in clips:
        clip_id = str(clip["id"])
        copy = metadata_by_id.get(clip_id, {"id": clip_id})
        render_record = short_records.get(clip_id, {})
        render = _with_media_url(
            render_artifact_state(
                episode_dir,
                Path("shorts") / f"{clip_id}.mp4",
                render_record,
                expected_fingerprint=expected_shorts.get(clip_id),
                expected_mode="speaker_cut_short",
            ),
            episode_id,
        )
        base_approval = _approval_state(
            clip,
            render,
            render_record,
            approval_metadata_by_id.get(clip_id),
        )
        variants = {}
        variant_states = {}
        for variant_id in BACKGROUND_VARIANT_IDS:
            variant_record, variant_render = background_variant_state(
                episode_dir,
                clip_id,
                base_record=render_record if render["current"] else None,
                encoding=variant_encoding,
                variant_id=variant_id,
            )
            variant_render = _with_media_url(variant_render, episode_id)
            variant_revision = clip_review_revision(
                clip, variant_record, approval_metadata_by_id.get(clip_id)
            )
            variant_approval = background_variant_approval_state(
                variant_record, variant_render, variant_revision
            )
            variant_states[variant_id] = (variant_render, variant_approval)
            if variant_id == SATISFYING_VARIANT_ID and not variant_record:
                continue
            variant_asset = variant_record.get("asset")
            variant_asset_id = (
                variant_asset.get("asset_id")
                if isinstance(variant_asset, dict)
                else None
            )
            if not isinstance(variant_asset_id, str):
                variant_asset_id = default_background_asset_id(variant_id)
            variant_asset_ids = (
                [
                    item.get("asset_id")
                    for item in variant_asset.get("assets", [])
                    if isinstance(item, dict) and isinstance(item.get("asset_id"), str)
                ]
                if isinstance(variant_asset, dict)
                else []
            )
            if not variant_asset_ids:
                variant_asset_ids = list(background_variant_asset_ids(variant_id))
            variants[variant_id] = {
                "id": variant_id,
                "label": background_variant_label(variant_id),
                "asset_id": variant_asset_id,
                "asset_ids": variant_asset_ids,
                "render": variant_render,
                "approval": variant_approval,
                "render_job": render_job_state(
                    episode_dir,
                    variant_render_job_id(clip_id, variant_id),
                ),
            }
        raw_release_request = clip.get(DISTRIBUTION_RELEASE_FIELD)
        release_request = (
            raw_release_request if isinstance(raw_release_request, dict) else None
        )
        change_lock = (
            publication_change_lock(episode_dir, clip_id, raw_release_request)
            if DISTRIBUTION_RELEASE_FIELD in clip
            else publication_change_lock(episode_dir, clip_id)
        )
        try:
            selected_variant_id = selected_short_variant_id(clip)
        except KeyError:
            distribution = {
                "version": "invalid",
                "variant_id": None,
                "label": "Invalid selection",
                "current": False,
                "approval_current": False,
                "revision": base_approval["revision"],
                "re_release_request": release_request,
                **change_lock,
            }
        else:
            selected_render, selected_approval = (
                variant_states[selected_variant_id]
                if selected_variant_id
                else (render, base_approval)
            )
            distribution = {
                "version": selected_variant_id or "base",
                "variant_id": selected_variant_id,
                "label": (
                    background_variant_label(selected_variant_id)
                    if selected_variant_id
                    else "Base"
                ),
                "current": selected_render["current"],
                "approval_current": selected_approval["current"],
                "revision": selected_approval["revision"],
                "re_release_request": release_request,
                **change_lock,
            }
        reviewed_clips.append(
            {
                **clip,
                "metadata": {key: value for key, value in copy.items() if key != "id"},
                "review": {
                    "selection": {"status": clip_selection_status(clip)},
                    "render": render,
                    "approval": base_approval,
                    "distribution": distribution,
                    "metadata": _metadata_state(copy, destinations),
                    "render_job": render_job_state(episode_dir, clip_id),
                    "variants": variants,
                },
            }
        )

    selection_counts = {
        status: sum(clip_selection_status(clip) == status for clip in clips)
        for status in ("selected", "unselected", "rejected")
    }
    return {
        "schema": "cascade.review/v1",
        "episode_id": episode_id,
        "clock": "source",
        "enabled_destinations": destinations,
        "clip_summary": {
            "candidate_count": len(clips),
            **{f"{status}_count": count for status, count in selection_counts.items()},
        },
        "longform": {
            "render": preferred,
            "canonical_render": canonical,
            "legacy_render": legacy,
            "approval": {
                "status": "current"
                if approval_current
                else "stale"
                if approval
                else "unapproved",
                "current": approval_current,
                "revision": approval_revision,
            },
            "source_preview_url": f"/api/episodes/{quote(episode_id, safe='')}/video-preview",
        },
        "clips": reviewed_clips,
    }


@router.get("/{episode_id}/review")
async def review_state(episode_id: str) -> dict:
    """Return reviewable files, their freshness, copy, and approval state."""
    return await asyncio.to_thread(
        episode_review_state, require_episode_dir(get_episodes_dir(), episode_id)
    )


def _stored_clips(episode_dir: Path) -> list[dict]:
    payload = _read_json(episode_dir / "clips.json", {"clips": []})
    clips = payload.get("clips", []) if isinstance(payload, dict) else payload
    return [clip for clip in clips if isinstance(clip, dict) and clip.get("id")]


async def _inspection_target(
    episode_id: str,
    target: Literal["source", "longform", "short", "short_variant"],
    clip_id: str | None,
    variant_id: str | None,
) -> tuple[Path, InspectionTarget]:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    clip = None
    if target in {"short", "short_variant"}:
        if not clip_id or not _CLIP_ID.fullmatch(clip_id):
            raise HTTPException(status_code=422, detail="A valid clip_id is required")
        clip = next(
            (item for item in _stored_clips(episode_dir) if item["id"] == clip_id),
            None,
        )
        if clip is None:
            raise HTTPException(status_code=404, detail=f"Unknown clip: {clip_id}")
        if target == "short_variant":
            try:
                require_background_variant(str(variant_id))
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail=str(exc).strip("'")
                ) from exc
        elif variant_id is not None:
            raise HTTPException(
                status_code=422,
                detail="variant_id is only valid for a short_variant target",
            )
    elif clip_id is not None or variant_id is not None:
        raise HTTPException(
            status_code=422,
            detail="clip_id and variant_id are only valid for short targets",
        )
    try:
        resolved = await asyncio.to_thread(
            resolve_target,
            episode_dir,
            episode,
            load_config(),
            target,
            clip=clip,
            variant_id=variant_id,
        )
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return episode_dir, resolved


async def _create_inspection_asset(
    episode_id: str,
    target: Literal["source", "longform", "short", "short_variant"],
    clip_id: str | None,
    variant_id: str | None,
    clock: Literal["source", "output"],
    seconds: float,
    *,
    kind: Literal["frame", "preview"],
    duration: float = 0.0,
) -> dict:
    episode_dir, resolved = await _inspection_target(
        episode_id, target, clip_id, variant_id
    )
    try:
        result = await asyncio.to_thread(
            inspect_media_window,
            resolved,
            episode_dir / "work" / "media_inspection",
            kind=kind,
            clock=clock,
            seconds=seconds,
            duration=duration,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(
            status_code=502, detail="Could not generate inspection media"
        ) from exc
    return {
        "schema": "cascade.media-inspection/v1",
        "episode_id": episode_id,
        "target": target,
        "clip_id": clip_id,
        "variant_id": variant_id,
        **result.payload,
        "asset": {
            **result.payload["asset"],
            "url": _inspection_url(episode_id, result.path),
        },
    }


@router.get("/{episode_id}/inspection/frame")
async def inspection_frame(
    episode_id: str,
    target: Literal["source", "longform", "short", "short_variant"] = "source",
    clock: Literal["source", "output"] = "source",
    seconds: float = 0.0,
    clip_id: str | None = None,
    variant_id: str | None = None,
) -> dict:
    return await _create_inspection_asset(
        episode_id, target, clip_id, variant_id, clock, seconds, kind="frame"
    )


@router.get("/{episode_id}/inspection/preview")
async def inspection_preview(
    episode_id: str,
    target: Literal["source", "longform", "short", "short_variant"] = "source",
    clock: Literal["source", "output"] = "source",
    seconds: float = 0.0,
    duration_seconds: float = 10.0,
    clip_id: str | None = None,
    variant_id: str | None = None,
) -> dict:
    return await _create_inspection_asset(
        episode_id,
        target,
        clip_id,
        variant_id,
        clock,
        seconds,
        kind="preview",
        duration=duration_seconds,
    )


@router.get("/{episode_id}/inspection/audio-preview")
async def inspection_audio_preview(
    episode_id: str,
    source_kind: Literal["recorder", "camera"],
    start_seconds: float,
    duration_seconds: float = 10.0,
    logical_track: int | None = None,
    channel: Literal["left", "right"] | None = None,
) -> dict:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    try:
        result = await asyncio.to_thread(
            inspect_audio_window,
            episode_dir,
            episode,
            load_config(),
            source_kind=source_kind,
            start=start_seconds,
            duration=duration_seconds,
            logical_track=logical_track,
            channel=channel,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=502, detail="Could not export audio window"
        ) from exc
    return {
        "schema": "cascade.audio-inspection/v1",
        "episode_id": episode_id,
        **result.payload,
        "asset": {
            **result.payload["asset"],
            "url": _inspection_url(episode_id, result.path),
        },
    }


@router.get("/{episode_id}/inspection/transcript")
async def inspection_transcript(episode_id: str) -> dict:
    return await _current_inspection_document(
        episode_id,
        "diarized_transcript.json",
        "cascade.transcript/v1",
        "transcript",
        current_diarized_transcript,
    )


@router.get("/{episode_id}/inspection/transcript/repair")
async def inspection_transcript_repair(episode_id: str) -> dict:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    try:
        document = await asyncio.to_thread(
            _transcript_repair_document, episode_dir, load_config()
        )
    except (
        FileNotFoundError,
        OSError,
        subprocess.CalledProcessError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**document, "episode_id": episode_id}


@router.post("/{episode_id}/inspection/transcript/repair")
async def apply_inspection_transcript_repair(
    episode_id: str, request: TranscriptClockRepairRequest
) -> dict:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    try:
        result = await asyncio.to_thread(
            _apply_transcript_clock_repair,
            episode_dir,
            load_config(),
            request,
        )
    except _TranscriptRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "schema": "cascade.transcript-clock-repair/v1",
        "episode_id": episode_id,
        "clock": "source",
        "current": True,
        **result,
    }


@router.get("/{episode_id}/inspection/transcript/corrections")
async def inspection_transcript_corrections(episode_id: str) -> dict:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    config = load_config()
    try:
        transcript = await asyncio.to_thread(
            current_diarized_transcript, episode_dir, episode, config
        )
        if transcript is None:
            raise _TranscriptRevisionConflict(
                "Current canonical transcript is unavailable"
            )
        corrections = await asyncio.to_thread(
            _transcript_corrections_document, episode_dir
        )
        revision = await asyncio.to_thread(
            file_revision, episode_dir / "diarized_transcript.json"
        )
    except _TranscriptRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "schema": "cascade.transcript-corrections/v1",
        "episode_id": episode_id,
        "clock": "source",
        "current": True,
        "transcript_revision": revision,
        "correction_count": len(corrections["operations"]),
        "corrections": corrections,
    }


@router.post("/{episode_id}/inspection/transcript/corrections")
async def apply_inspection_transcript_corrections(
    episode_id: str, request: TranscriptCorrectionsRequest
) -> dict:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    try:
        result = await asyncio.to_thread(
            _apply_transcript_corrections,
            episode_dir,
            load_config(),
            request.expected_revision,
            request.operations,
        )
    except _TranscriptRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "schema": "cascade.transcript-corrections/v1",
        "episode_id": episode_id,
        "clock": "source",
        "current": True,
        **result,
    }


@router.get("/{episode_id}/inspection/shot-plan")
async def inspection_shot_plan(episode_id: str) -> dict:
    return await _current_inspection_document(
        episode_id,
        "segments.json",
        "cascade.shot-plan/v1",
        "shot_plan",
        current_speaker_segments,
    )


@router.get("/{episode_id}/inspection/clip-boundaries")
async def inspection_clip_boundaries(episode_id: str) -> dict:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    clips = _stored_clips(episode_dir)
    return _boundary_evidence_with_inspection(
        episode_id, episode_dir, episode, clips, load_config()
    )


async def _current_inspection_document(
    episode_id: str,
    filename: str,
    schema: str,
    key: str,
    loader,
) -> dict:
    episode_dir = require_episode_dir(get_episodes_dir(), episode_id)
    episode = _read_json(episode_dir / "episode.json", {})
    document = await asyncio.to_thread(loader, episode_dir, episode, load_config())
    if document is None:
        label = key.replace("_", " ")
        raise HTTPException(status_code=409, detail=f"Current {label} is unavailable")
    return {
        "schema": schema,
        "episode_id": episode_id,
        "clock": "source",
        "current": True,
        "revision": await asyncio.to_thread(file_revision, episode_dir / filename),
        key: document,
    }


def _inspection_url(episode_id: str, path: Path) -> str:
    return (
        f"/media/episodes/{quote(episode_id, safe='')}/work/media_inspection/"
        f"{quote(path.name, safe='')}"
    )
