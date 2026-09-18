"""Publish approved shorts and YouTube longform through Upload-Post.

The current release gate is checked before any external request. Scheduled
shorts are reserved against the Upload-Post calendar and local episode
receipts; the calendar lock serializes Cascade publishers on this filesystem.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from agents.base import BaseAgent
from agents.qa import (
    SHORT_COPY_SCHEMA,
    canonical_release_metadata,
    current_funnel_urls,
    episode_hub_url,
    longform_publication_snapshot,
    publication_identity,
    quality_snapshot,
    release_metadata_issues,
    required_short_variants,
    short_distribution_state,
    youtube_made_for_kids,
)
from lib.atomic_write import atomic_write_json
from lib.delivery_video import read_render_manifest, render_output_lock
from lib.publication_contracts import ShortDestinationExecution, ShortDestinationRequest
from lib.short_distribution import (
    EXPANSION_DESTINATIONS,
    PLATFORM_COPY_FIELDS,
    SHORT_DESTINATIONS,
    SHORT_PLATFORM_SPECS,
    configured_destination_bindings,
    upload_fields,
    valid_destination_bindings,
    validate_destination_copy,
    validate_destination_media,
)
from lib.short_variants import (
    BACKGROUND_VARIANT_IDS,
    DESTINATION_DISTRIBUTION_RELEASE_SCHEMA,
    DISTRIBUTION_RELEASE_FIELD,
    DISTRIBUTION_VARIANT_FIELD,
    MINECRAFT_SURROUND_DESTINATIONS,
    MINECRAFT_SURROUND_VARIANT_ID,
    SCHEDULED_DESTINATION_DISTRIBUTION_RELEASE_SCHEMA,
    RetiredShortVariantError,
    background_variant_output,
    destination_distribution_release_revision,
    destination_distribution_release_schema,
    distribution_release_revision,
    normalize_destination_distribution_targets,
    require_active_background_variant,
)

UPLOAD_POST_URL = "https://api.upload-post.com/api/upload"
SCHEDULE_URL = "https://api.upload-post.com/api/uploadposts/schedule"
PROFILE_URL = "https://api.upload-post.com/api/uploadposts/users"
FACEBOOK_PAGES_URL = "https://api.upload-post.com/api/uploadposts/facebook/pages"
FACEBOOK_PAGE_PIN_URL = f"{PROFILE_URL}/facebook-page"
LINKEDIN_PAGES_URL = "https://api.upload-post.com/api/uploadposts/linkedin/pages"
LINKEDIN_PAGE_PIN_URL = f"{PROFILE_URL}/linkedin-page"
PINTEREST_BOARD_URL = "https://api.upload-post.com/api/uploadposts/pinterest/boards"
SCHEDULE_CANCELLATION_SCHEMA = "cascade.schedule-cancellation/v1"
EXACT_SCHEDULE_CANCELLATION_SCHEMA = "cascade.schedule-cancellation/v2"
SHORT_DESTINATION_SCHEMA = "cascade.short-destination/v1"
EXPANDED_SHORT_DESTINATION_SCHEMA = "cascade.short-destination/v2"
ARTIFACT_SHORT_DESTINATION_SCHEMA = "cascade.short-destination/v3"
_ARTIFACT_IDENTITY_FIELDS = (
    "version",
    "variant_id",
    "render_fingerprint",
    "rerelease_request_id",
    "rerelease_authorization_revision",
)
_VERSION_IDENTITY_FIELDS = (*_ARTIFACT_IDENTITY_FIELDS[:3], "approval_revision")
RECORDED_STATES = {
    "submitted",
    "published",
    "already_submitted",
    "partial_failure",
    "unknown",
    "cancelled",
    "intent_recorded",
}
TERMINAL_DESTINATION_STATES = {"published", "failed", "cancelled"}
_UNRESOLVED_PROVIDER_STATE_MARKERS = (
    "queue",
    "pending",
    "process",
    "progress",
    "retry",
    "inbox",
    "schedule",
    "submit",
    "unknown",
    "wait",
    "upload",
)
_LEGACY_UNRESOLVED_RECEIPT_FIELDS = frozenset(
    {
        "clip_id",
        "completed_at",
        "error",
        "external_id",
        "historical_receipt",
        "idempotency_key",
        "job_id",
        "platform_failures",
        "platforms",
        "reconciliation",
        "request_id",
        "reservation_active",
        "response",
        "reused_receipt",
        "scheduled",
        "scheduled_date",
        "server_request_id",
        "status",
        "status_history",
        "timezone",
        "was_scheduled",
    }
)
SLOT_HOURS = {"morning": 9, "afternoon": 14, "evening": 18}


class ShortDestinationConflict(RuntimeError):
    """A reviewed short request conflicts with current release state."""


class AggregatePublicationRetired(RuntimeError):
    """New publication requires an exact reviewed destination request."""

    code = "aggregate_publication_retired"
    message = (
        "Aggregate publication is retired. Record approval with "
        '{"start_publication":false}, preview exact destinations with '
        "publish-shorts/preview, then execute the returned request with "
        "run-agent/publish."
    )

    def __init__(self):
        super().__init__(self.message)

    @classmethod
    def detail(cls) -> dict:
        return {"code": cls.code, "message": cls.message}


@contextmanager
def publication_lock(episodes_dir):
    """Serialize remote scheduling with receipt-bound distribution changes."""
    with (Path(episodes_dir) / ".publish-schedule.lock").open("a+") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def receipt_key(receipt: dict) -> tuple[str, str] | None:
    for field in ("external_id", "idempotency_key", "request_id", "job_id"):
        value = receipt.get(field) if isinstance(receipt, dict) else None
        if isinstance(value, str) and value:
            return field, value
    return None


def validated_short_receipts(publish: dict) -> list[dict]:
    receipts = publish.get("shorts", [])
    if not isinstance(receipts, list) or any(
        not isinstance(receipt, dict)
        or not isinstance(receipt.get("clip_id"), str)
        or not receipt["clip_id"]
        or receipt.get("status") not in RECORDED_STATES | {"failed"}
        or (
            receipt.get("status") == "intent_recorded"
            and "destination_request_id" not in receipt
        )
        or not _destination_history_receipt_valid(receipt)
        for receipt in receipts
    ):
        raise ValueError("Publication receipt history cannot be verified")
    operations = [
        validated_schedule_cancellation(receipt)
        for receipt in receipts
        if "schedule_cancellation" in receipt
    ]
    if any(operation is None for operation in operations) or any(
        len({key(operation) for operation in operations}) != len(operations)
        for key in (
            lambda value: value["operation_id"],
            lambda value: value["snapshot"]["remote_job"]["job_id"],
        )
    ):
        raise ValueError("Schedule cancellation history cannot be verified")
    return receipts


def _provider_identity(receipt: dict) -> dict[str, str]:
    values = {
        "job_id": receipt.get("job_id"),
        "request_id": receipt.get("server_request_id") or receipt.get("request_id"),
        "external_id": receipt.get("external_id"),
    }
    return {
        field: value
        for field, value in values.items()
        if isinstance(value, str) and value
    }


def _identity_evidence(expected: dict[str, str], observed: object) -> tuple[bool, bool]:
    """Return (matched, conflicting) for same-field provider identifiers."""
    if not isinstance(observed, dict):
        return False, False
    matched = False
    conflicting = False
    for field, expected_value in expected.items():
        observed_value = observed.get(field)
        if not isinstance(observed_value, str) or not observed_value:
            continue
        if observed_value == expected_value:
            matched = True
        else:
            conflicting = True
    return matched, conflicting


def _provider_result_items(
    response: dict, *, strict: bool = True
) -> list[tuple[str, dict]] | None:
    results = response.get("results")
    if not results:
        return []
    if isinstance(results, dict):
        items = list(results.items())
    elif isinstance(results, list):
        items = [
            (str(item.get("platform", "")) if isinstance(item, dict) else "", item)
            for item in results
        ]
    else:
        return None
    if strict and any(not isinstance(item, dict) for _, item in items):
        return None
    return [(str(platform), item) for platform, item in items if isinstance(item, dict)]


def status_identity_conflicts(
    receipt: dict,
    response: object,
    *,
    profile_username: str | None = None,
) -> bool:
    """Reject contradictory IDs from a receipt-specific status response."""
    identity = _provider_identity(receipt)
    if not identity or not isinstance(response, dict):
        return False
    observed = [response]
    observed.extend(
        item for _, item in _provider_result_items(response, strict=False) or []
    )
    top_matches, _ = _identity_evidence(identity, response)
    for index, item in enumerate(observed):
        matches, conflicting = _identity_evidence(identity, item)
        if conflicting:
            return True
        if not profile_username or not (matches or (index > 0 and top_matches)):
            continue
        observed_profile = item.get("profile_username")
        if (index > 0 or observed_profile is not None) and (
            observed_profile != profile_username
        ):
            return True
    return False


def validated_terminal_destinations(
    platforms: object, destinations: object
) -> dict[str, dict] | None:
    """Validate the one canonical saved terminal-destination shape."""
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(not isinstance(platform, str) or not platform for platform in platforms)
        or len(set(platforms)) != len(platforms)
        or not isinstance(destinations, dict)
        or set(destinations) != set(platforms)
    ):
        return None
    for value in destinations.values():
        if not isinstance(value, dict):
            return None
        state = value.get("state")
        url = value.get("url")
        if state == "published":
            if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                return None
        elif state in TERMINAL_DESTINATION_STATES - {"published"}:
            if url not in (None, ""):
                return None
        else:
            return None
    return destinations


def _document_revision(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _destination_external_id(
    episode_id,
    request_id,
    destinations,
    target_revision,
    clip_id,
    schema=SHORT_DESTINATION_SCHEMA,
):
    revision = _document_revision(
        {
            "schema": schema,
            "request_id": request_id,
            "destinations": destinations,
            "target_revision": target_revision,
        }
    )
    return publication_identity(episode_id, revision, "short", clip_id)


def _valid_destination_copy_shape(copy: object, platforms: list[str]) -> bool:
    if not isinstance(copy, dict) or set(copy) != set(platforms):
        return False
    for platform in platforms:
        fields = copy.get(platform)
        allowed = set(SHORT_PLATFORM_SPECS[platform]["upload_fields"])
        configured_required = set(PLATFORM_COPY_FIELDS[platform])
        required = configured_required & allowed or allowed
        if (
            not isinstance(fields, dict)
            or not required <= set(fields) <= allowed
            or any(not isinstance(value, str) for value in fields.values())
        ):
            return False
    return True


def _destination_receipt_valid(receipt: dict) -> bool:
    try:
        request_id = str(uuid.UUID(receipt["destination_request_id"]))
        platforms = receipt["platforms"]
        deferred = receipt["deferred_platforms"]
        copy = receipt["destination_copy"]
        target = ShortDeliverySpec.target_fields(receipt["clip_id"], receipt)
        request = ShortDeliverySpec.request_fields(receipt)
        schema = receipt.get("destination_schema")
        expanded = bool(set(platforms) & EXPANSION_DESTINATIONS)
        artifact_request = schema == ARTIFACT_SHORT_DESTINATION_SCHEMA
        scheduled = receipt.get("scheduled") if artifact_request else True
        if not artifact_request or scheduled:
            _parse_time(receipt["scheduled_date"])
        return bool(
            request_id == receipt["destination_request_id"]
            and schema
            in {
                ARTIFACT_SHORT_DESTINATION_SCHEMA,
                (
                    EXPANDED_SHORT_DESTINATION_SCHEMA
                    if expanded
                    else SHORT_DESTINATION_SCHEMA
                ),
            }
            and (
                not artifact_request
                or (
                    isinstance(scheduled, bool)
                    and (scheduled or receipt.get("scheduled_date") is None)
                )
            )
            and receipt.get("copy_schema") == SHORT_COPY_SCHEMA
            and isinstance(receipt["destination_episode_id"], str)
            and bool(receipt["destination_episode_id"])
            and isinstance(receipt["destination_profile_username"], str)
            and bool(receipt["destination_profile_username"])
            and receipt["destination_actor"] == receipt["destination_actor"].strip()
            and receipt["destination_reason"] == receipt["destination_reason"].strip()
            and len(receipt["destination_reason"]) >= 3
            and platforms == sorted(platforms)
            and deferred == sorted(deferred)
            and platforms
            and len(platforms) == len(set(platforms))
            and len(deferred) == len(set(deferred))
            and set(platforms) | set(deferred) <= set(SHORT_DESTINATIONS)
            and not set(platforms) & set(deferred)
            and valid_destination_bindings(
                receipt.get("destination_bindings"), platforms
            )
            and _valid_destination_copy_shape(copy, platforms)
            and receipt["target_revision"] == _document_revision(target)
            and receipt["copy_revision"] == _document_revision(copy)
            and receipt["external_id"]
            == _destination_external_id(
                receipt["destination_episode_id"],
                request_id,
                platforms,
                receipt["target_revision"],
                receipt["clip_id"],
                schema,
            )
            and receipt["destination_request_revision"] == _document_revision(request)
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _destination_history_receipt_valid(receipt: dict) -> bool:
    """Validate destination identity through an exact cancellation wrapper."""
    if "destination_request_id" not in receipt:
        return True
    if _destination_receipt_valid(receipt):
        return True
    original = receipt.get("pre_cancellation_receipt")
    return bool(
        isinstance(original, dict)
        and _destination_receipt_valid(original)
        and validated_schedule_cancellation(receipt) is not None
    )


@dataclass(frozen=True)
class ShortDeliverySpec:
    """Immutable exact target plus the current media needed to transport it."""

    clip_id: str
    identity: str
    platforms: tuple[str, ...]
    scheduled: bool
    _snapshot: bytes

    @classmethod
    def create(
        cls,
        clip: dict,
        version: dict,
        target: dict,
    ) -> "ShortDeliverySpec":
        clip_id = str(clip.get("id", ""))
        if not (
            _destination_receipt_valid(target)
            and target["clip_id"] == clip_id
            and cls.receipt_matches_artifact(target, version)
            and cls.receipt_matches_version(target, version)
        ):
            raise ValueError("Short destination target is invalid")
        snapshot = json.dumps(
            {"clip": clip, "version": version, "target": target},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return cls(
            clip_id=clip_id,
            identity=target["external_id"],
            platforms=tuple(target["platforms"]),
            scheduled=target["scheduled"],
            _snapshot=snapshot,
        )

    def snapshot(self) -> dict:
        return json.loads(self._snapshot)

    @classmethod
    def receipt_matches_artifact(cls, receipt: dict, version: dict) -> bool:
        target = cls.target_fields(str(receipt.get("clip_id", "")), version)
        return all(
            receipt.get(field) == target[field] for field in _ARTIFACT_IDENTITY_FIELDS
        )

    def receipt(
        self,
        provider_receipt: dict,
        scheduled_at: datetime | None,
        timezone_name: str,
    ) -> dict:
        snapshot = self.snapshot()
        target = snapshot["target"]
        result = {**target, **provider_receipt}
        result.update({key: value for key, value in target.items() if key != "status"})
        result["scheduled"] = scheduled_at is not None
        if scheduled_at is not None:
            result.update(
                scheduled_date=scheduled_at.isoformat(),
                timezone=timezone_name,
            )
        return result

    @classmethod
    def receipt_matches_version(cls, receipt: dict, version: dict) -> bool:
        present = set(_VERSION_IDENTITY_FIELDS).intersection(receipt)
        if not present:
            return (
                version.get("version") == "base" and version.get("variant_id") is None
            )
        if present != set(_VERSION_IDENTITY_FIELDS):
            return False
        target = cls.target_fields(str(receipt.get("clip_id", "")), version)
        return (
            all(
                receipt.get(field) == target[field]
                for field in _VERSION_IDENTITY_FIELDS
            )
            and receipt.get("rerelease_request_id") == target["rerelease_request_id"]
        )

    @staticmethod
    def target_fields(clip_id: str, version: dict) -> dict:
        rerelease = version.get("re_release_request")
        source = rerelease if isinstance(rerelease, dict) else version
        return {
            "clip_id": clip_id,
            "version": version.get("version"),
            "variant_id": version.get("variant_id"),
            "render_fingerprint": version.get("render_fingerprint"),
            "approval_revision": version.get("revision")
            or version.get("approval_revision"),
            "rerelease_request_id": source.get("request_id")
            if source is rerelease
            else source.get("rerelease_request_id"),
            "rerelease_authorization_revision": source.get("revision")
            if source is rerelease
            else source.get("rerelease_authorization_revision"),
        }

    @staticmethod
    def receipt_identity(
        clip_id: str, version: dict, identity: str, platforms: list[str]
    ) -> dict:
        fields = {
            "clip_id": clip_id,
            "platforms": platforms,
            "request_id": identity,
            "external_id": identity,
            "idempotency_key": identity,
            "version": version["version"],
            "variant_id": version["variant_id"],
            "render_fingerprint": version["render_fingerprint"],
            "approval_revision": version["revision"],
        }
        request = version.get("re_release_request")
        if not isinstance(request, dict):
            return fields
        fields.update(
            rerelease_request_id=request.get("request_id"),
            rerelease_actor=request.get("actor"),
            rerelease_reason=request.get("reason"),
            rerelease_authorization_revision=request.get("revision"),
            parent_receipt_history_revision=request.get("receipt_history_revision"),
        )
        acknowledgement = request.get("unresolved_history_acknowledgement")
        if acknowledgement is not None:
            fields["unresolved_history_acknowledgement"] = acknowledgement
        return fields

    @staticmethod
    def request_fields(receipt: dict) -> dict:
        fields = {
            "schema": receipt["destination_schema"],
            "episode_id": receipt["destination_episode_id"],
            "profile_username": receipt["destination_profile_username"],
            "request_id": receipt["destination_request_id"],
            "actor": receipt["destination_actor"],
            "reason": receipt["destination_reason"],
            "destinations": receipt["platforms"],
            "deferred_destinations": receipt["deferred_platforms"],
            "target_revision": receipt["target_revision"],
            "scheduled_date": receipt["scheduled_date"],
            "timezone": receipt["timezone"],
            "copy_revision": receipt["copy_revision"],
            "external_id": receipt["external_id"],
        }
        if receipt["destination_schema"] == ARTIFACT_SHORT_DESTINATION_SCHEMA:
            fields["scheduled"] = receipt["scheduled"]
        if "destination_bindings" in receipt:
            fields["destination_bindings"] = receipt["destination_bindings"]
        return fields


def _clean_caption(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return "\n".join(
        line for line in text.splitlines() if "link in bio" not in line.casefold()
    ).strip()


def _append_cta(text: str, cta: str) -> str:
    return text if cta.casefold() in text.casefold() else f"{text}\n\n{cta}"


def _hashtags(text: str, values: object) -> str:
    seen = {tag.casefold() for tag in re.findall(r"(?<!\w)#([\w]+)", text)}
    result = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str):
            continue
        tag = re.sub(r"[^\w]", "", value.strip().lstrip("#"))
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            result.append(f"#{tag}")
    return " ".join(result)


def short_destination_copy(
    metadata, destinations, *, title, hub_url, youtube_url, spotify_url, channel_handle
):
    """Build exact reviewable Upload-Post copy for the requested destinations."""
    result = {}
    if "youtube" in destinations:
        source = metadata.get("youtube", {})
        description = _clean_caption(source.get("description"))
        if hub_url:
            description = _append_cta(
                description,
                "Watch the full episode on The Local Podcast channel. "
                f"Episode hub: {hub_url}",
            )
        result["youtube"] = {
            "title": str(source.get("title") or title),
            "description": description,
        }
        if youtube_url:
            result["youtube"]["first_comment"] = _build_first_comment(
                youtube_url, spotify_url, channel_handle
            )
    for destination in ("tiktok", "instagram"):
        if destination not in destinations:
            continue
        source = metadata.get(destination, {})
        caption = _clean_caption(source.get("caption")) or title
        if hub_url:
            caption = _append_cta(
                caption,
                f"Watch the full episode on The Local Podcast. Episode hub: {hub_url}",
            )
        tags = _hashtags(caption, source.get("hashtags"))
        separator = "\n\n" if destination == "instagram" else " "
        result[destination] = {
            "text": separator.join(value for value in (caption, tags) if value)
        }
    if "x" in destinations:
        source = metadata.get("x", {})
        result["x"] = {
            "text": _append_cta(
                _clean_caption(source.get("text")) or title,
                "Watch the full episode on The Local Podcast.",
            )
        }
    cta = f"Full episode: {hub_url}" if hub_url else ""
    for destination in ("facebook", "linkedin", "pinterest"):
        if destination not in destinations:
            continue
        source = metadata.get(destination, {})
        description = _clean_caption(source.get("description"))
        if cta and destination != "pinterest":
            description = _append_cta(description, cta)
        result[destination] = {
            "title": str(source.get("title") or title),
            "description": description,
            **({"link": hub_url} if destination == "pinterest" and hub_url else {}),
        }
    for destination in ("threads", "bluesky"):
        if destination not in destinations:
            continue
        text = _clean_caption(metadata.get(destination, {}).get("text")) or title
        result[destination] = {"text": _append_cta(text, cta) if cta else text}
    return result


def _has_exact_keys(value: object, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _matches_exact_receipt(receipt: dict, expected: dict) -> bool:
    return receipt == expected or receipt == {**expected, "historical_receipt": True}


def cancellation_snapshot(
    receipt: dict,
    history_revision: str,
    profile: str,
    target: dict | None,
    remote_job: dict,
) -> dict:
    snapshot = {
        "receipt_revision": _document_revision(receipt),
        "history_revision": history_revision,
        "profile_username": profile,
        "remote_job": remote_job,
    }
    if target is not None:
        snapshot["target"] = target
    return {**snapshot, "revision": _document_revision(snapshot)}


def _matching_schedule_row(receipt: dict, remote: object, profile: str) -> bool:
    try:
        platforms = receipt["platforms"]
        fields = remote.get("fields")
        return bool(
            isinstance(remote, dict)
            and remote.get("job_id") == receipt["job_id"]
            and remote.get("external_id") == receipt["external_id"]
            and remote.get("profile_username") == profile
            and isinstance(remote.get("platforms"), list)
            and len(remote["platforms"]) == len(set(remote["platforms"]))
            and set(remote["platforms"]) == set(platforms)
            and remote.get("source_filename") == f"{receipt['clip_id']}.mp4"
            and (fields is None or fields.get("external_id") == receipt["external_id"])
            and _instant(_parse_remote_schedule_time(remote["scheduled_date"]))
            == _instant(_parse_time(receipt["scheduled_date"]))
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _schedule_content_identity(
    receipt: dict, profile: str, episode_id: str
) -> str | None:
    """Identify one exact short artifact across disjoint destination jobs."""
    if (
        receipt.get("status") not in {"submitted", "already_submitted"}
        or receipt.get("destination_profile_username") != profile
        or receipt.get("destination_episode_id") != episode_id
        or not _destination_receipt_valid(receipt)
    ):
        return None
    identity = {
        "episode_id": receipt.get("destination_episode_id"),
        "clip_id": receipt.get("clip_id"),
        "version": receipt.get("version"),
        "variant_id": receipt.get("variant_id"),
        "render_fingerprint": receipt.get("render_fingerprint"),
        "rerelease_request_id": receipt.get("rerelease_request_id"),
        "rerelease_authorization_revision": receipt.get(
            "rerelease_authorization_revision"
        ),
    }
    if not all(
        isinstance(identity[key], str) and identity[key]
        for key in ("episode_id", "clip_id", "version", "render_fingerprint")
    ):
        return None
    return _document_revision(identity)


def _schedule_placement_identity(episode_id: object, clip_id: object) -> str | None:
    """Identify one episode/clip placement, independent of its media variant."""
    if not (
        isinstance(episode_id, str)
        and episode_id
        and isinstance(clip_id, str)
        and clip_id
    ):
        return None
    return _document_revision({"episode_id": episode_id, "clip_id": clip_id})


def _schedule_capacity_count(records: list[dict]) -> int:
    """Count disjoint waves for one episode/clip/time as one placement."""
    count = 0
    groups = {}
    for item in records:
        identity = item.get("_schedule_placement_identity") or item.get(
            "_schedule_content_identity"
        )
        platforms = item.get("_schedule_platforms")
        if not (
            isinstance(identity, str)
            and identity
            and isinstance(platforms, (list, tuple))
            and platforms
            and all(isinstance(platform, str) and platform for platform in platforms)
            and len(platforms) == len(set(platforms))
        ):
            count += 1
            continue
        platform_set = set(platforms)
        grouped_platforms = groups.setdefault(
            (identity, _instant(item["scheduled_at"])), []
        )
        for used in grouped_platforms:
            if used.isdisjoint(platform_set):
                used.update(platform_set)
                break
        else:
            grouped_platforms.append(platform_set)
            count += 1
    return count


_CANCELLATION_FIELDS = {
    "delete_started": set(),
    "outcome_uncertain": {"delete"},
    "delete_confirmed": {"delete"},
    "cancelled": {"delete", "post_delete", "completed_at", "terminal_event"},
}
_CANCELLATION_BASE_FIELDS = {
    "schema",
    "operation_id",
    "clip_id",
    "actor",
    "reason",
    "state",
    "snapshot",
    "pre_delete",
    "started_at",
}


def _delete_attempt_state(deletion: object, job_id: str) -> str | None:
    """Classify an exact durable DELETE result as uncertain or confirmed."""
    if not (
        isinstance(deletion, dict)
        and deletion.get("job_id") == job_id
        and isinstance(deletion.get("attempted_at"), str)
        and deletion["attempted_at"]
    ):
        return None
    if _has_exact_keys(deletion, {"job_id", "attempted_at", "error"}):
        error = deletion.get("error")
        return "uncertain" if isinstance(error, str) and error else None
    if _has_exact_keys(
        deletion,
        {"job_id", "attempted_at", "http_status", "response", "confirmed_at"},
    ):
        if (
            deletion.get("http_status") == 200
            and isinstance(deletion.get("response"), dict)
            and deletion["response"].get("success") is True
            and isinstance(deletion.get("confirmed_at"), str)
            and deletion["confirmed_at"]
        ):
            return "confirmed"
        return None
    response_fields = {"job_id", "attempted_at", "http_status"}
    if "response" in deletion:
        response_fields.add("response")
    if not _has_exact_keys(deletion, response_fields):
        return None
    status = deletion.get("http_status")
    response = deletion.get("response")
    if (
        type(status) is not int
        or ("response" in deletion and not isinstance(response, dict))
        or status == 200
        and isinstance(response, dict)
        and response.get("success") is True
    ):
        return None
    return "uncertain"


def validated_schedule_cancellation(receipt: dict) -> dict | None:
    """Validate the durable operation embedded in a scheduled receipt."""
    operation = receipt.get("schedule_cancellation")
    original = receipt.get("pre_cancellation_receipt")
    if not isinstance(operation, dict) or not isinstance(original, dict):
        return None
    state = operation.get("state")
    if not isinstance(state, str):
        return None
    state_fields = _CANCELLATION_FIELDS.get(state)
    if state_fields is None:
        return None
    expected_fields = _CANCELLATION_BASE_FIELDS | state_fields
    if state == "delete_confirmed" and "post_delete" in operation:
        expected_fields.add("post_delete")
    pre_delete = operation.get("pre_delete")
    if not (
        _has_exact_keys(operation, expected_fields)
        and _has_exact_keys(pre_delete, {"checked_at", "evidence"})
    ):
        return None

    schema = operation.get("schema")
    snapshot = operation.get("snapshot")
    target = snapshot.get("target") if isinstance(snapshot, dict) else None
    remote = snapshot.get("remote_job") if isinstance(snapshot, dict) else None
    legacy_target = schema == SCHEDULE_CANCELLATION_SCHEMA
    if schema not in {
        SCHEDULE_CANCELLATION_SCHEMA,
        EXACT_SCHEDULE_CANCELLATION_SCHEMA,
    } or not (
        _has_exact_keys(target, {"variant_id", "revision", "render_fingerprint"})
        if legacy_target
        else target is None
    ):
        return None
    if legacy_target and not (
        target["variant_id"] is None or isinstance(target["variant_id"], str)
    ):
        return None
    if legacy_target and not all(
        isinstance(target.get(key), str) and target[key]
        for key in ("revision", "render_fingerprint")
    ):
        return None

    required_text = ("operation_id", "clip_id", "actor", "reason", "started_at")
    cancelled_destinations = {
        platform: {"state": "cancelled"}
        for platform in (original.get("platforms") or [])
    }
    if not (
        all(
            isinstance(operation.get(key), str) and operation[key]
            for key in required_text
        )
        and operation["actor"] == operation["actor"].strip()
        and operation["reason"] == operation["reason"].strip()
        and len(operation["reason"]) >= 3
        and operation["clip_id"] == original.get("clip_id")
        and "schedule_cancellation" not in original
        and "pre_cancellation_receipt" not in original
        and original.get("scheduled") is True
        and validated_terminal_destinations(
            original.get("platforms"), cancelled_destinations
        )
        is not None
        and isinstance(snapshot, dict)
        and isinstance(snapshot.get("history_revision"), str)
        and isinstance(snapshot.get("profile_username"), str)
        and bool(snapshot["profile_username"])
        and snapshot
        == cancellation_snapshot(
            original,
            snapshot["history_revision"],
            snapshot["profile_username"],
            target,
            remote,
        )
        and _matching_schedule_row(original, remote, snapshot["profile_username"])
        and isinstance(pre_delete.get("checked_at"), str)
        and schedule_cancellation_provider_safe(
            original,
            pre_delete.get("evidence"),
            profile_username=snapshot["profile_username"],
            after_delete=False,
        )[0]
    ):
        return None

    pending_receipt = {
        **original,
        "pre_cancellation_receipt": original,
        "schedule_cancellation": operation,
    }
    if state != "cancelled" and not _matches_exact_receipt(receipt, pending_receipt):
        return None
    if state == "delete_started":
        return operation

    deletion_state = _delete_attempt_state(operation.get("delete"), original["job_id"])
    if state == "outcome_uncertain":
        return operation if deletion_state == "uncertain" else None
    if deletion_state != "confirmed" and not (
        state == "cancelled"
        and schema == EXACT_SCHEDULE_CANCELLATION_SCHEMA
        and deletion_state == "uncertain"
    ):
        return None

    if state == "delete_confirmed":
        if "post_delete" not in operation:
            return operation
        post = operation["post_delete"]
        if not (
            _has_exact_keys(post, {"checked_at", "calendar", "evidence"})
            and isinstance(post["checked_at"], str)
            and post["checked_at"]
            and (
                post["calendar"] is None
                or isinstance(post["calendar"], list)
                and all(isinstance(item, dict) for item in post["calendar"])
            )
            and (post["evidence"] is None or isinstance(post["evidence"], dict))
        ):
            return None
        return operation

    post = operation.get("post_delete")
    history = original.get("status_history", [])
    event = operation.get("terminal_event")
    expected_event = {
        "observed_at": operation.get("completed_at"),
        "previous_status": original.get("status"),
        "status": "cancelled",
        "provider_status": "cancelled",
        "profile_username": snapshot["profile_username"],
        "evidence_source": "schedule_cancellation",
        "terminal_destinations": cancelled_destinations,
    }
    if not (
        _has_exact_keys(post, {"checked_at", "calendar", "evidence"})
        and isinstance(post.get("checked_at"), str)
        and isinstance(post.get("calendar"), list)
        and all(isinstance(item, dict) for item in post["calendar"])
        and not any(
            item.get("job_id") == original["job_id"]
            or item.get("external_id") == original["external_id"]
            for item in post["calendar"]
        )
        and schedule_cancellation_provider_safe(
            original,
            post.get("evidence"),
            profile_username=snapshot["profile_username"],
            after_delete=True,
        )[0]
        and isinstance(history, list)
        and isinstance(operation.get("completed_at"), str)
        and event == expected_event
        and _matches_exact_receipt(
            receipt,
            {
                **original,
                "status": "cancelled",
                "scheduled": False,
                "terminal_destinations": cancelled_destinations,
                "status_history": [*history, event],
                "pre_cancellation_receipt": original,
                "schedule_cancellation": operation,
            },
        )
    ):
        return None
    return operation


def _provider_result_has_unresolved_work(value: dict) -> bool:
    provider_state = str(value.get("status") or value.get("state") or "").lower()
    return (
        any(marker in provider_state for marker in _UNRESOLVED_PROVIDER_STATE_MARKERS)
        or value.get("fallback_to_inbox") is True
        or value.get("retryable") is True
        or value.get("is_retryable") is True
    )


def status_response_has_unresolved_work(response: object) -> bool:
    """Deny history fallback while exact provider status remains uncertain."""
    if not isinstance(response, dict) or not response:
        return True
    evidence = [response]
    evidence.extend(
        item for _, item in _provider_result_items(response, strict=False) or []
    )
    return any(_provider_result_has_unresolved_work(item) for item in evidence)


def receipt_terminal_destinations(
    receipt: dict, *, profile_username: str | None = None
) -> dict[str, dict] | None:
    """Return per-destination terminal proof from a saved provider result."""
    platforms = receipt.get("platforms")
    recorded = receipt.get("terminal_destinations")
    if recorded is not None:
        recorded = validated_terminal_destinations(platforms, recorded)
        if recorded is None:
            return None
        history = receipt.get("status_history")
        if (
            isinstance(history, list)
            and history
            and isinstance(history[-1], dict)
            and history[-1].get("terminal_destinations") == recorded
            and (
                not profile_username
                or history[-1].get("profile_username") == profile_username
            )
            and history[-1].get("evidence_source")
            in {"status", "history", "schedule_cancellation"}
            and (
                history[-1].get("evidence_source") != "schedule_cancellation"
                or validated_schedule_cancellation(receipt) is not None
            )
        ):
            return recorded
        return None

    response = receipt.get("response")
    return terminal_destinations_from_status(
        receipt, response, profile_username=profile_username
    )


def terminal_destinations_from_status(
    receipt: dict,
    response: object,
    *,
    profile_username: str | None = None,
) -> dict[str, dict] | None:
    """Accept only exact, public-or-failed results for every destination."""
    platforms = receipt.get("platforms")
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(not isinstance(platform, str) or not platform for platform in platforms)
        or len(set(platforms)) != len(platforms)
    ):
        return None
    expected = set(platforms)
    if not isinstance(response, dict):
        return None
    if status_response_has_unresolved_work(response):
        return None
    identity = _provider_identity(receipt)
    results = response.get("results")
    items = _provider_result_items(response)
    if items is None:
        return None
    if isinstance(results, list):
        if response.get("status") not in {"completed", "failed", "cancelled"}:
            return None
        total = response.get("total")
        completed = response.get("completed")
        if total is not None and (not isinstance(total, int) or total != len(expected)):
            return None
        if completed is not None and (
            not isinstance(completed, int) or completed != total
        ):
            return None
    response_matches, response_conflicts = _identity_evidence(identity, response)
    if not identity or response_conflicts:
        return None
    evidence = {}
    for platform, value in items:
        item_matches, item_conflicts = _identity_evidence(identity, value)
        if (
            not platform
            or platform in evidence
            or not isinstance(value.get("success"), bool)
            or (profile_username and value.get("profile_username") != profile_username)
            or item_conflicts
            or (not response_matches and not item_matches)
        ):
            return None
        provider_state = str(value.get("status") or value.get("state") or "").lower()
        post_url = next(
            (
                value.get(field)
                for field in ("post_url", "video_url", "url")
                if isinstance(value.get(field), str) and value[field]
            ),
            None,
        )
        if value["success"] is True and not (
            isinstance(post_url, str) and post_url.startswith(("http://", "https://"))
        ):
            return None
        if (
            value["success"] is True
            and any(
                marker in provider_state
                for marker in ("fail", "cancel", "error", "skip")
            )
        ) or (
            value["success"] is False
            and (
                post_url is not None
                or any(
                    marker in provider_state
                    for marker in ("publish", "success", "live", "complete")
                )
            )
        ):
            return None
        evidence[platform] = {
            "state": (
                "published"
                if value["success"]
                else "cancelled"
                if "cancel" in provider_state
                else "failed"
            ),
            **({"url": post_url} if isinstance(post_url, str) and post_url else {}),
        }
    return validated_terminal_destinations(platforms, evidence)


def terminal_destinations_from_history(
    receipt: dict,
    response: object,
    *,
    profile_username: str | None = None,
) -> dict[str, dict] | None:
    """Extract exact receipt results from Upload-Post's broader history response."""
    if not isinstance(response, dict):
        return None
    identity = _provider_identity(receipt)
    if not identity:
        return None

    in_progress = response.get("in_progress", [])
    if not isinstance(in_progress, list) or any(
        not isinstance(item, dict) for item in in_progress
    ):
        return None
    if any(_identity_evidence(identity, item)[0] for item in in_progress):
        return None
    history = response.get("history", [])
    if not isinstance(history, list) or any(
        not isinstance(item, dict) for item in history
    ):
        return None
    identity_states = [
        (_identity_evidence(identity, item), item) for item in [*in_progress, *history]
    ]
    if any(
        (matched and conflicting)
        or (
            matched
            and profile_username
            and item.get("profile_username") != profile_username
        )
        for (matched, conflicting), item in identity_states
    ):
        return None
    matched = [
        item for item in history if _identity_evidence(identity, item) == (True, False)
    ]
    return terminal_destinations_from_status(
        receipt,
        {"status": "completed", "results": matched},
        profile_username=profile_username,
    )


def short_receipt_history_revision(
    publish: dict, clip_id: str, *, exclude_request_id: str | None = None
) -> str:
    receipts = [
        _stable_receipt(receipt)
        for receipt in validated_short_receipts(publish)
        if receipt["clip_id"] == clip_id
        and (
            exclude_request_id is None
            or receipt.get("rerelease_request_id") != exclude_request_id
        )
    ]
    return _document_revision(receipts)


def _stable_receipt(receipt: dict) -> dict:
    return {
        key: value
        for key, value in receipt.items()
        if key not in {"historical_receipt", "reused_receipt"}
    }


def _unresolved_receipt_summary(receipt: dict) -> dict:
    complete_identity = bool(
        isinstance(receipt.get("version"), str)
        and receipt["version"]
        and (
            receipt.get("variant_id") is None
            or (isinstance(receipt.get("variant_id"), str) and receipt["variant_id"])
        )
        and isinstance(receipt.get("render_fingerprint"), str)
        and receipt["render_fingerprint"]
        and isinstance(receipt.get("approval_revision"), str)
        and receipt["approval_revision"]
    )

    def identifier(field: str) -> str | None:
        value = receipt.get(field)
        return value if isinstance(value, str) and value else None

    return {
        "receipt_revision": _document_revision(_stable_receipt(receipt)),
        "status": receipt.get("status"),
        "error": (
            receipt.get("error") if isinstance(receipt.get("error"), str) else None
        ),
        "platforms": receipt.get("platforms")
        if isinstance(receipt.get("platforms"), list)
        else None,
        "request_id": identifier("request_id"),
        "job_id": identifier("job_id") or identifier("server_request_id"),
        "external_id": identifier("external_id"),
        "version": receipt.get("version") if complete_identity else None,
        "variant_id": receipt.get("variant_id") if complete_identity else None,
        "render_fingerprint": (
            receipt.get("render_fingerprint") if complete_identity else None
        ),
        "approval_revision": (
            receipt.get("approval_revision") if complete_identity else None
        ),
        "artifact_identity": "known" if complete_identity else "unknown",
    }


def _legacy_unresolved_receipt(receipt: dict) -> bool:
    """Recognize pre-schema receipts without asserting their artifact identity."""
    return set(receipt) <= _LEGACY_UNRESOLVED_RECEIPT_FIELDS


def unresolved_receipt_obligations(
    publish: dict,
    clip_id: str,
    *,
    profile_username: str | None = None,
    exclude_request_id: str | None = None,
) -> tuple[list[dict], bool]:
    receipts = [
        receipt
        for receipt in validated_short_receipts(publish)
        if receipt["clip_id"] == clip_id
        and (
            exclude_request_id is None
            or receipt.get("rerelease_request_id") != exclude_request_id
        )
        and receipt_terminal_destinations(receipt, profile_username=profile_username)
        is None
    ]
    return (
        [_unresolved_receipt_summary(receipt) for receipt in receipts],
        bool(receipts)
        and all(_legacy_unresolved_receipt(receipt) for receipt in receipts),
    )


def short_rerelease_state(
    publish: dict, clip_id: str, *, profile_username: str | None = None
) -> dict:
    """Describe whether immutable receipt history permits a new release."""
    try:
        receipts = [
            receipt
            for receipt in validated_short_receipts(publish)
            if receipt["clip_id"] == clip_id
        ]
    except ValueError as exc:
        return {"allowed": False, "reason": str(exc), "history_revision": None}
    if not receipts:
        return {
            "allowed": False,
            "reason": "No prior remote submission requires a re-release.",
            "history_revision": short_receipt_history_revision(publish, clip_id),
        }
    history_revision = short_receipt_history_revision(publish, clip_id)
    cancellation_request = None
    consumed = {receipt.get("rerelease_request_id") for receipt in receipts}
    for receipt in receipts:
        operation = validated_schedule_cancellation(receipt)
        target = operation.get("snapshot", {}).get("target") if operation else None
        if (
            operation
            and isinstance(target, dict)
            and operation["operation_id"] not in consumed
        ):
            request = {
                "request_id": operation["operation_id"],
                "actor": operation["actor"],
                "reason": operation["reason"],
                "variant_id": target["variant_id"],
                "target_revision": target["revision"],
                "render_fingerprint": target["render_fingerprint"],
            }
            if cancellation_request not in (None, request):
                return {
                    "allowed": False,
                    "reason": "Cancelled schedules bind conflicting re-release requests.",
                    "history_revision": history_revision,
                }
            cancellation_request = request
    obligations, acknowledgement_allowed = unresolved_receipt_obligations(
        publish, clip_id, profile_username=profile_username
    )
    if obligations:
        return {
            "allowed": False,
            "reason": "Prior receipts have unresolved remote destinations.",
            "history_revision": history_revision,
            "unresolved_receipt_obligations": obligations,
            "unresolved_history_acknowledgement_allowed": acknowledgement_allowed,
            "cancellation_request": cancellation_request,
        }
    return {
        "allowed": True,
        "reason": None,
        "history_revision": history_revision,
        "receipt_count": len(receipts),
        "cancellation_request": cancellation_request,
    }


def _destination_release_target(
    authorization: dict,
    version: dict,
    destinations: object,
    scheduled_date: str | None,
) -> list[dict] | None:
    expected_fields = {
        "schema",
        "request_id",
        "actor",
        "reason",
        "targets",
        "receipt_history_revision",
        "revision",
        "created_at",
    }
    if "unresolved_history_acknowledgement" in authorization:
        expected_fields.add("unresolved_history_acknowledgement")
    targets = normalize_destination_distribution_targets(authorization.get("targets"))
    if (
        set(authorization) != expected_fields
        or targets is None
        or authorization.get("schema")
        != destination_distribution_release_schema(targets)
        or not isinstance(destinations, list)
        or not destinations
        or destinations != sorted(set(destinations))
    ):
        return None
    target = next(
        (item for item in targets if item["variant_id"] == version.get("variant_id")),
        None,
    )
    if target is None or (
        target["destinations"],
        target["target_revision"],
        target["render_fingerprint"],
    ) != (
        destinations,
        version.get("revision"),
        version.get("render_fingerprint"),
    ):
        return None
    if "scheduled_date" in target and target["scheduled_date"] != scheduled_date:
        return None
    return targets


def _destination_release_receipts_valid(
    publish: dict,
    clip_id: str,
    authorization: dict,
    targets: list[dict],
    acknowledgement: object,
) -> bool:
    seen_targets = set()
    scheduled_copies = set()
    for receipt in validated_short_receipts(publish):
        if (
            receipt["clip_id"] != clip_id
            or receipt.get("rerelease_request_id") != authorization["request_id"]
        ):
            continue
        target = next(
            (
                item
                for item in targets
                if item["variant_id"] == receipt.get("variant_id")
            ),
            None,
        )
        if (
            target is None
            or target["variant_id"] in seen_targets
            or (
                receipt.get("platforms"),
                receipt.get("approval_revision"),
                receipt.get("render_fingerprint"),
                receipt.get("rerelease_authorization_revision"),
                receipt.get("parent_receipt_history_revision"),
                receipt.get("unresolved_history_acknowledgement"),
                receipt.get("rerelease_actor"),
                receipt.get("rerelease_reason"),
            )
            != (
                target["destinations"],
                target["target_revision"],
                target["render_fingerprint"],
                authorization["revision"],
                authorization["receipt_history_revision"],
                acknowledgement,
                authorization["actor"],
                authorization["reason"],
            )
        ):
            return False
        if (
            "scheduled_date" in target
            and receipt.get("scheduled_date") != target["scheduled_date"]
        ):
            return False
        if authorization.get("schema") == (
            SCHEDULED_DESTINATION_DISTRIBUTION_RELEASE_SCHEMA
        ):
            scheduled_copies.add(receipt.get("copy_revision"))
        seen_targets.add(target["variant_id"])
    return None not in scheduled_copies and len(scheduled_copies) <= 1


def valid_rerelease_authorization(
    publish: dict,
    clip: dict,
    version: dict,
    *,
    destinations: list[str] | None = None,
    scheduled_date: str | None = None,
) -> bool:
    authorization = clip.get(DISTRIBUTION_RELEASE_FIELD)
    required = (
        "request_id",
        "actor",
        "reason",
        "receipt_history_revision",
        "revision",
        "created_at",
    )
    if not isinstance(authorization, dict) or any(
        not isinstance(authorization.get(field), str) or not authorization[field]
        for field in required
    ):
        return False
    destination_release = authorization.get("schema") in {
        DESTINATION_DISTRIBUTION_RELEASE_SCHEMA,
        SCHEDULED_DESTINATION_DISTRIBUTION_RELEASE_SCHEMA,
    }
    targets = None
    if destination_release:
        targets = _destination_release_target(
            authorization, version, destinations, scheduled_date
        )
        if targets is None:
            return False
    else:
        target_revision = authorization.get("target_revision")
        render_fingerprint = authorization.get("render_fingerprint")
        if (
            not isinstance(target_revision, str)
            or not target_revision
            or not isinstance(render_fingerprint, str)
            or not render_fingerprint
            or "variant_id" not in authorization
            or authorization.get("variant_id") != version.get("variant_id")
            or target_revision != version.get("revision")
            or render_fingerprint != version.get("render_fingerprint")
        ):
            return False
    try:
        history_revision = short_receipt_history_revision(
            publish,
            str(clip.get("id", "")),
            exclude_request_id=authorization["request_id"],
        )
    except ValueError:
        return False
    acknowledgement = authorization.get("unresolved_history_acknowledgement")
    if destination_release and not _destination_release_receipts_valid(
        publish,
        str(clip.get("id", "")),
        authorization,
        targets,
        acknowledgement,
    ):
        return False
    revision_inputs = {
        "request_id": authorization["request_id"],
        "actor": authorization["actor"],
        "reason": authorization["reason"],
        "receipt_history_revision": history_revision,
        "unresolved_history_acknowledgement": acknowledgement,
    }
    expected = (
        destination_distribution_release_revision(
            **revision_inputs,
            targets=authorization["targets"],
        )
        if destination_release
        else distribution_release_revision(
            **revision_inputs,
            variant_id=authorization.get("variant_id"),
            target_revision=authorization["target_revision"],
            render_fingerprint=authorization["render_fingerprint"],
        )
    )
    obligations, acknowledgement_allowed = unresolved_receipt_obligations(
        publish,
        str(clip.get("id", "")),
        exclude_request_id=authorization["request_id"],
    )
    acknowledgement_valid = acknowledgement is None and not obligations
    if isinstance(acknowledgement, dict):
        acknowledgement_valid = bool(
            acknowledgement_allowed
            and version.get("variant_id") in BACKGROUND_VARIANT_IDS
            and acknowledgement
            == {
                "receipt_history_revision": history_revision,
                "obligations": obligations,
            }
        )
    return bool(
        authorization["receipt_history_revision"] == history_revision
        and authorization["revision"] == expected
        and acknowledgement_valid
    )


def _authorized_rerelease_version(
    publish: dict,
    clip: dict,
    version: dict,
    destinations: list[str],
    scheduled_date: str | None,
) -> tuple[dict, bool]:
    authorized = valid_rerelease_authorization(
        publish,
        clip,
        version,
        destinations=destinations,
        scheduled_date=scheduled_date,
    )
    if not authorized:
        return version, False
    return {
        **version,
        "re_release_request": clip[DISTRIBUTION_RELEASE_FIELD],
    }, True


def valid_rerelease_copy_continuation(
    publish: dict, clip: dict, version: dict, current_waves: list[dict]
) -> bool:
    """Preserve a proven re-release lineage across a copy-only reapproval."""
    authorization = clip.get(DISTRIBUTION_RELEASE_FIELD)
    if not isinstance(authorization, dict) or not current_waves:
        return False
    original_revision = authorization.get("target_revision")
    if not isinstance(original_revision, str) or not original_revision:
        return False
    original_version = {**version, "revision": original_revision}
    if not valid_rerelease_authorization(publish, clip, original_version):
        return False
    acknowledgement = authorization.get("unresolved_history_acknowledgement")
    return any(
        wave.get("approval_revision") == original_revision
        and wave.get("rerelease_request_id") == authorization.get("request_id")
        and wave.get("rerelease_authorization_revision")
        == authorization.get("revision")
        and wave.get("parent_receipt_history_revision")
        == authorization.get("receipt_history_revision")
        and wave.get("unresolved_history_acknowledgement") == acknowledgement
        for wave in current_waves
    )


def _build_first_comment(youtube_url, spotify_url="", channel_handle=""):
    lines = [f"Full episode: {youtube_url}"]
    if spotify_url:
        lines.append(f"Listen on Spotify: {spotify_url}")
    if channel_handle:
        lines.append(channel_handle)
    return "\n".join(lines)


def _parse_time(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("datetime has no UTC offset")
    return result


def _parse_remote_schedule_time(value: str) -> datetime:
    """Parse Upload-Post calendar dates, whose offsetless values are UTC."""
    value = value.strip().replace("Z", "+00:00")
    result = datetime.fromisoformat(value)
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(microsecond=0)


def cancellable_scheduled_receipt(
    publish: dict,
    clip_id: str,
    remote_schedule: object,
    *,
    profile_username: str,
    job_id: str | None = None,
    external_id: str | None = None,
    now: datetime | None = None,
) -> tuple[dict, dict, str]:
    """Resolve one future receipt, optionally by exact provider identity."""
    if (
        not profile_username
        or not isinstance(remote_schedule, list)
        or any(not isinstance(item, dict) for item in remote_schedule)
        or (job_id is None) != (external_id is None)
    ):
        raise ValueError("Upload-Post schedule or profile cannot be verified")
    receipts = [
        receipt
        for receipt in validated_short_receipts(publish)
        if receipt["clip_id"] == clip_id
    ]
    pending = [
        receipt
        for receipt in receipts
        if receipt_terminal_destinations(receipt, profile_username=profile_username)
        is None
    ]
    candidates = [
        receipt
        for receipt in pending
        if receipt.get("scheduled") is True
        and "schedule_cancellation" not in receipt
        and (job_id is None or receipt.get("job_id") == job_id)
        and (external_id is None or receipt.get("external_id") == external_id)
    ]
    if job_id is not None and len(candidates) != 1:
        raise ValueError("The exact scheduled receipt could not be resolved")
    if job_id is None and (len(candidates) != 1 or len(pending) != 1):
        raise ValueError("The clip has no single cancellable scheduled receipt")
    receipt = candidates[0]
    platforms = receipt.get("platforms")
    if (
        not isinstance(receipt.get("status_history", []), list)
        or validated_terminal_destinations(
            platforms,
            {platform: {"state": "cancelled"} for platform in platforms or []},
        )
        is None
    ):
        raise ValueError("The scheduled receipt identity cannot be verified")
    try:
        scheduled_at = _parse_time(receipt["scheduled_date"])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("The scheduled receipt date cannot be verified") from exc
    reference = now or datetime.now(timezone.utc)
    if _instant(scheduled_at) <= _instant(reference):
        raise ValueError("Only future scheduled jobs can be cancelled")
    matches = [
        item
        for item in remote_schedule
        if item.get("job_id") == receipt.get("job_id")
        or item.get("external_id") == receipt.get("external_id")
    ]
    if len(matches) != 1 or not _matching_schedule_row(
        receipt, matches[0], profile_username
    ):
        raise ValueError("The provider calendar row does not exactly match the receipt")
    return receipt, matches[0], short_receipt_history_revision(publish, clip_id)


def _provider_states(item: dict) -> set[str]:
    return {
        str(item[key]).strip().lower()
        for key in ("upload_status", "upload_overall_status", "status", "state")
        if item.get(key) not in (None, "")
    }


def _provider_row_is_queued(item: dict) -> bool:
    states = _provider_states(item)
    return (
        bool(states)
        and states <= {"queued", "scheduled"}
        and item.get("success") is None
        and not any(
            item.get(key)
            for key in (
                "fallback_to_inbox",
                "retryable",
                "is_retryable",
                "post_url",
                "video_url",
                "url",
            )
        )
    )


def _provider_status_is_queued(receipt: dict, status: dict, bound: list[dict]) -> bool:
    """Accept queued status rows, including Upload-Post's unattempted result shape."""
    if all(_provider_row_is_queued(item) for item in bound):
        return True

    identity = _provider_identity(receipt)
    platforms = receipt.get("platforms")
    results = status.get("results")
    items = _provider_result_items(status)
    if not (
        identity
        and all(status.get(field) == value for field, value in identity.items())
        and _provider_states(status) == {"queued"}
        and str(status.get("scheduler_status", "")).strip().lower() == "pending"
        and isinstance(platforms, list)
        and platforms
        and len(platforms) == len(set(platforms))
        and isinstance(results, list)
        and items is not None
        and len(items) == len(platforms)
        and all(platform for platform, _ in items)
        and len({platform for platform, _ in items}) == len(items)
        and {platform for platform, _ in items} == set(platforms)
        and type(status.get("total")) is int
        and status["total"] == len(platforms)
        and all(
            type(status.get(field)) is int and status[field] == 0
            for field in ("completed", "failed", "skipped", "retryable")
        )
    ):
        return False
    return all(
        _provider_states(item) == {"queued"}
        and type(item.get("attempts")) is int
        and item["attempts"] == 0
        and item.get("success") is False
        and not any(
            item.get(field)
            for field in (
                "fallback_to_inbox",
                "retryable",
                "is_retryable",
                "post_url",
                "video_url",
                "url",
                "error",
                "error_message",
            )
        )
        for _, item in items
    )


def _provider_status_is_inert_tombstone(receipt: dict, status: dict) -> bool:
    """Recognize Upload-Post's exact post-DELETE inactivity tombstone."""
    platforms = receipt.get("platforms")
    return bool(
        isinstance(platforms, list)
        and platforms
        and status.get("job_id") == receipt.get("job_id")
        and status.get("external_id") == receipt.get("external_id")
        and _provider_states(status) == {"failed"}
        and status.get("message")
        == "Upload appears to have failed (no activity for over 1 hour)"
        and status.get("results") == []
        and type(status.get("completed")) is int
        and status["completed"] == 0
        and type(status.get("total")) is int
        and status["total"] == len(platforms)
        and all(
            status.get(field) in (None, 0)
            for field in ("failed", "skipped", "retryable")
        )
        and status.get("success") in (None, False)
        and status.get("scheduler_status") in (None, "")
        and not any(
            status.get(field)
            for field in (
                "fallback_to_inbox",
                "is_retryable",
                "post_url",
                "video_url",
                "url",
            )
        )
    )


def schedule_cancellation_provider_safe(
    receipt: dict,
    evidence: object,
    *,
    profile_username: str,
    after_delete: bool,
) -> tuple[bool, str | None]:
    """Accept exact queued provider evidence before DELETE and absence afterward."""
    if not (
        _has_exact_keys(evidence, {"status_not_found", "status", "history"})
        and isinstance(evidence["status_not_found"], bool)
        and isinstance(evidence.get("history"), dict)
        and (evidence["status"] is None) is evidence["status_not_found"]
    ):
        return False, "Provider status/history evidence is malformed"
    identity = _provider_identity(receipt)
    status_missing = evidence.get("status_not_found") is True
    status = evidence.get("status")
    history = evidence["history"]
    status_confirmed = False
    if not identity:
        return False, "Provider receipt identity is missing"
    if not status_missing:
        if not isinstance(status, dict) or status_identity_conflicts(
            receipt,
            status,
            profile_username=profile_username if after_delete else None,
        ):
            return False, "Provider status conflicts with the scheduled receipt"
        top_match, _ = _identity_evidence(identity, status)
        items = _provider_result_items(status)
        if items is None:
            return False, "Provider status results are malformed"
        children = [item for _, item in items]
        if not after_delete and any(
            item.get("profile_username") not in (None, profile_username)
            for item in [status, *children]
        ):
            return False, "Provider status conflicts with the scheduled receipt"
        bound = [
            item
            for index, item in enumerate([status, *children])
            if _identity_evidence(identity, item)[0] or index and top_match
        ]
        if not bound:
            return False, "Provider status did not confirm the scheduled job"
        if after_delete:
            if not _provider_status_is_inert_tombstone(receipt, status) and any(
                not _provider_states(item)
                or not _provider_states(item) <= {"cancelled", "canceled"}
                or item.get("success") not in (None, False)
                or item.get("fallback_to_inbox") is True
                or item.get("retryable") is True
                or item.get("is_retryable") is True
                or any(item.get(key) for key in ("post_url", "video_url", "url"))
                for item in bound
            ):
                return False, "Provider status conflicts with cancellation"
        elif not _provider_status_is_queued(receipt, status, bound):
            return False, "Provider status no longer describes queued work"
        status_confirmed = True

    active = history.get("in_progress")
    completed = history.get("history")
    if (
        not isinstance(active, list)
        or not isinstance(completed, list)
        or any(not isinstance(item, dict) for item in [*active, *completed])
    ):
        return False, "Provider history is malformed"
    matched_active, matched_completed = [], []
    for collection, matches in (
        (active, matched_active),
        (completed, matched_completed),
    ):
        for item in collection:
            matched, conflict = _identity_evidence(identity, item)
            if matched and (
                conflict or item.get("profile_username") != profile_username
            ):
                return False, "Provider history conflicts with the scheduled receipt"
            if matched:
                matches.append(item)
    if after_delete:
        if (
            matched_active
            or matched_completed
            or not (status_missing or status_confirmed)
        ):
            return False, "Provider history still contains work for the cancelled job"
        return True, None
    if matched_completed:
        return False, "Provider history already contains terminal work for this job"
    if not matched_active:
        return (
            (True, None)
            if status_confirmed
            else (False, "Provider did not confirm queued work for this job")
        )
    try:
        scheduled = _instant(_parse_time(receipt["scheduled_date"]))
        dates = [
            _instant(_parse_remote_schedule_time(item["run_date"]))
            for item in matched_active
        ]
    except (AttributeError, KeyError, TypeError, ValueError):
        return False, "Provider history has an invalid scheduled date"
    platforms = receipt.get("platforms")
    seen = [item.get("platform") for item in matched_active]
    if (
        len(seen) != len(set(seen))
        or set(seen) != set(platforms or [])
        or any(
            not _provider_row_is_queued(item)
            or item.get("is_scheduled") is not True
            or date != scheduled
            for item, date in zip(matched_active, dates, strict=True)
        )
    ):
        return False, "Provider work history is not uniformly queued for this schedule"
    return True, None


class PublishAgent(BaseAgent):
    name = "publish"

    @staticmethod
    def _validated_destination_request(value, contract=ShortDestinationRequest) -> dict:
        return contract.model_validate(value).model_dump(
            mode="json", exclude_unset=True
        )

    def _explicit_destination_request(self) -> dict:
        value = getattr(self, "short_destination_request", None)
        try:
            return self._validated_destination_request(value, ShortDestinationExecution)
        except (TypeError, ValueError) as exc:
            raise AggregatePublicationRetired() from exc

    def run(self) -> dict:
        """Keep distribution selection locked until publish.json is durable."""
        if type(self) is PublishAgent:
            self._explicit_destination_request()
        with publication_lock(self.episode_dir.parent):
            self._publication_lock_held = True
            try:
                return super().run()
            finally:
                self._publication_lock_held = False

    def _previous_publish(self) -> dict:
        try:
            previous = self.load_json("publish.json")
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Cannot inspect the current publish receipt; nothing was submitted"
            ) from error
        if not isinstance(previous, dict):
            raise TypeError(
                "Cannot inspect the current publish receipt; nothing was submitted"
            )
        return previous

    def _inputs(self):
        episode = self.load_json_safe("episode.json")
        snapshot = quality_snapshot(self.episode_dir, config=self.config)
        gate = snapshot["release_gate"]
        if not gate["safe"]:
            reasons = "; ".join(item["message"] for item in gate["blockers"])
            raise RuntimeError(f"release gate blocked — {reasons}")

        api_key = os.getenv("UPLOAD_POST_API_KEY")
        user = os.getenv("UPLOAD_POST_USER", "")
        if not api_key:
            raise RuntimeError("UPLOAD_POST_API_KEY not set in environment")
        if not user:
            raise RuntimeError("UPLOAD_POST_USER not set in environment")

        clips = self.load_json("clips.json").get("clips", [])
        approved = [clip for clip in clips if clip.get("status") == "approved"]
        short_versions = gate.get("short_versions")
        if not isinstance(short_versions, dict) or any(
            not isinstance(short_versions.get(str(clip.get("id", ""))), dict)
            or short_versions[str(clip.get("id", ""))].get("current") is not True
            or short_versions[str(clip.get("id", ""))].get("approval_current")
            is not True
            or short_versions[str(clip.get("id", ""))].get("active_for_new_writes")
            is not True
            for clip in approved
        ):
            raise RuntimeError(
                "The selected short versions are unavailable, unapproved, or retired"
            )
        metadata = canonical_release_metadata(self.episode_dir, episode, clips)
        issues = release_metadata_issues(metadata, approved, self.config)
        if issues:
            issue = issues[0]
            subject = (
                f"{issue['clip_id']} {issue['platform']} copy"
                if issue["scope"] == "clip"
                else f"longform {issue['platform']} copy"
            )
            raise RuntimeError(f"{subject} is missing: {', '.join(issue['fields'])}")

        platform_config = self.config.get("platforms", {})
        platforms = [
            name
            for name in SHORT_DESTINATIONS
            if platform_config.get(name, {}).get("enabled") is True
        ]
        if not platforms:
            raise RuntimeError("No platforms enabled in config")

        previous = self._previous_publish()
        previous_shorts = self._validated_previous_shorts(previous)
        funnel_urls = current_funnel_urls(
            episode,
            episode_dir=self.episode_dir,
            editorial_revision_value=snapshot["approvals"]["editorial"]["revision"],
            quality_revision_value=snapshot["quality"]["current_revision"],
        )
        longform_revision = snapshot["approvals"]["editorial"]["revision"]
        short_metadata = {
            str(item["id"]): item
            for item in metadata.get("clips", [])
            if isinstance(item, dict) and item.get("id")
        }
        approval_short_metadata = {
            str(item["id"]): item
            for item in self.load_json_safe(
                "metadata/metadata.json", {"clips": []}
            ).get("clips", [])
            if isinstance(item, dict) and item.get("id")
        }
        return {
            "episode": episode,
            "snapshot": snapshot,
            "gate": gate,
            "api_key": api_key,
            "user": user,
            "approved": approved,
            "metadata": metadata,
            "platforms": platforms,
            "previous": previous,
            "previous_shorts": previous_shorts,
            "funnel_urls": funnel_urls,
            "longform_revision": longform_revision,
            "short_metadata": short_metadata,
            "approval_short_metadata": approval_short_metadata,
            "short_versions": short_versions,
        }

    def _destination_versions(self, data, overrides):
        versions = dict(data["short_versions"])
        if not overrides:
            return versions
        records = read_render_manifest(self.episode_dir).get("shorts", {})
        by_id = {str(clip.get("id", "")): clip for clip in data["approved"]}
        for clip_id, variant_id in overrides.items():
            if versions[clip_id].get("variant_id") == variant_id:
                raise RuntimeError(
                    f"{clip_id} already selects {variant_id}; remove its override"
                )
            candidate = dict(by_id[clip_id])
            candidate[DISTRIBUTION_VARIANT_FIELD] = variant_id
            candidate.pop(DISTRIBUTION_RELEASE_FIELD, None)
            version = short_distribution_state(
                self.episode_dir,
                data["episode"],
                self.config,
                candidate,
                records.get(clip_id),
                data["approval_short_metadata"].get(clip_id),
            )
            if (
                version.get("current") is not True
                or version.get("approval_current") is not True
            ):
                raise RuntimeError(
                    f"The requested destination variant for {clip_id} is not "
                    "current and approved"
                )
            versions[clip_id] = version
        return versions

    def _enforce_required_short_variants(
        self, clip_ids, short_versions, destinations
    ) -> None:
        for destination, required_variant_id in required_short_variants(
            self.config, destinations
        ).items():
            mismatched = [
                clip_id
                for clip_id in clip_ids
                if short_versions.get(clip_id, {}).get("variant_id")
                != required_variant_id
            ]
            if mismatched:
                raise RuntimeError(
                    f"{destination} requires short variant {required_variant_id} for "
                    f"clips: {', '.join(mismatched)}. Submit a separate {destination} "
                    "destination request with clip_ids and variant_overrides mapping "
                    f"each listed clip ID to {required_variant_id}; no shorts were "
                    "submitted"
                )

    @staticmethod
    def _enforce_variant_destinations(clip_ids, short_versions, destinations) -> None:
        unsupported = sorted(set(destinations) - set(MINECRAFT_SURROUND_DESTINATIONS))
        if not unsupported:
            return
        for clip_id in clip_ids:
            if (
                short_versions.get(clip_id, {}).get("variant_id")
                == MINECRAFT_SURROUND_VARIANT_ID
            ):
                raise RuntimeError(
                    f"{MINECRAFT_SURROUND_VARIANT_ID} does not support "
                    f"destinations: {', '.join(unsupported)}"
                )

    @staticmethod
    def _validated_variant_overrides(data, value):
        overrides = value.get("variant_overrides", {})
        approved_ids = {str(clip.get("id", "")) for clip in data["approved"]}
        if not set(overrides) <= approved_ids:
            raise RuntimeError(
                "variant_overrides must map approved clip IDs to variants"
            )
        try:
            for variant_id in overrides.values():
                require_active_background_variant(variant_id)
        except KeyError as exc:
            raise RuntimeError(str(exc).strip("'")) from exc
        except RetiredShortVariantError as exc:
            raise RuntimeError(str(exc)) from exc
        return overrides

    def _destination_plan(self, data, value, *, inspect_remote=True):
        request_id = value["request_id"]
        actor, reason = value["actor"], value["reason"]
        destinations = sorted(value["destinations"])
        overrides = self._validated_variant_overrides(data, value)
        copy_overrides = value.get("copy_overrides", {})
        schedule_overrides = value.get("schedule_overrides", {})
        publish_now = value.get("publish_now", False)
        if actor != actor.strip() or reason != reason.strip():
            raise RuntimeError("Short destination actor and reason must be trimmed")
        if len(destinations) != len(set(destinations)):
            raise RuntimeError("Choose unique short destinations")
        # A reviewed one-off expansion request may use a verified configured
        # binding while the destination remains disabled for global releases.
        approved_destinations = sorted(
            set(data["platforms"]) | (set(destinations) & EXPANSION_DESTINATIONS)
        )
        if not set(destinations) <= set(approved_destinations):
            raise RuntimeError("Destinations are outside the approved publish plan")
        revision = data["gate"]["revision"]
        if value.get("expected_release_revision") != revision:
            raise RuntimeError("Destination preview does not match the current release")
        destination_schema = (
            ARTIFACT_SHORT_DESTINATION_SCHEMA
            if overrides or copy_overrides or schedule_overrides or publish_now
            else EXPANDED_SHORT_DESTINATION_SCHEMA
            if set(destinations) & EXPANSION_DESTINATIONS
            else SHORT_DESTINATION_SCHEMA
        )

        approved_ids = [str(clip.get("id", "")) for clip in data["approved"]]
        requested_ids = value.get("clip_ids")
        if requested_ids is None:
            selected_ids = approved_ids
        elif (
            not requested_ids
            or len(requested_ids) != len(set(requested_ids))
            or not set(requested_ids) <= set(approved_ids)
        ):
            raise RuntimeError("clip_ids must name unique approved clips")
        else:
            selected_ids = [item for item in approved_ids if item in requested_ids]
        if not set(overrides) <= set(selected_ids):
            raise RuntimeError("variant_overrides must name selected clips")
        if not set(copy_overrides) <= set(selected_ids):
            raise RuntimeError("copy_overrides must name selected clips")
        if schedule_overrides and set(schedule_overrides) != set(selected_ids):
            raise RuntimeError(
                "schedule_overrides must name every selected clip exactly"
            )
        if schedule_overrides and publish_now:
            raise RuntimeError("schedule_overrides cannot be combined with publish_now")
        effective_versions = self._destination_versions(data, overrides)
        self._enforce_variant_destinations(
            selected_ids, effective_versions, destinations
        )
        self._enforce_required_short_variants(
            selected_ids, effective_versions, destinations
        )
        hub_url = episode_hub_url(self.config, self.episode_dir.name)
        if set(destinations) - {"x"} and not hub_url:
            raise RuntimeError("An HTTPS exact-episode hub URL is required")
        try:
            destination_bindings = configured_destination_bindings(
                self.config, destinations
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if destination_bindings and inspect_remote:
            self._verify_destination_bindings(
                data["api_key"], data["user"], destination_bindings
            )

        selected_id_set = set(selected_ids)
        schedule_by_clip = {
            clip_id: {"clip_id": clip_id, "scheduled_date": scheduled_date}
            for clip_id, scheduled_date in schedule_overrides.items()
        }
        if not schedule_overrides:
            schedule = data["metadata"].get("schedule", [])
            for entry in schedule if isinstance(schedule, list) else []:
                clip_id = (
                    str(entry.get("clip_id", "")) if isinstance(entry, dict) else ""
                )
                if clip_id not in selected_id_set:
                    continue
                if clip_id in schedule_by_clip:
                    raise RuntimeError(f"Schedule has duplicate entries for {clip_id}")
                schedule_by_clip[clip_id] = entry
        missing = [
            item
            for item in selected_ids
            if not publish_now and item not in schedule_by_clip
        ]
        if missing:
            raise RuntimeError(
                "Selected clips need explicit schedule entries: " + ", ".join(missing)
            )

        tz_name, weekday, weekend = self._short_schedule_policy()
        reference = self._schedule_reference(data["episode"], tz_name)
        by_id = {str(clip.get("id", "")): clip for clip in data["approved"]}
        prior = data["previous_shorts"]
        targets = []
        delivery_states = []
        identities = set()
        co_schedule_ids = set()
        for clip_id in selected_ids:
            clip, version = by_id[clip_id], effective_versions[clip_id]
            release_request = clip.get(DISTRIBUTION_RELEASE_FIELD)
            scheduled_release = bool(
                isinstance(release_request, dict)
                and release_request.get("schema")
                == SCHEDULED_DESTINATION_DISTRIBUTION_RELEASE_SCHEMA
            )
            scheduled_at = (
                None
                if publish_now
                else self._schedule_to_datetime(
                    schedule_by_clip[clip_id], tz_name, reference=reference
                )
            )
            if scheduled_at is not None and not (
                _instant(reference)
                < _instant(scheduled_at)
                <= _instant(reference + timedelta(days=365))
            ):
                raise RuntimeError(
                    f"Schedule for {clip_id} is not safely in the future"
                )
            scheduled_date = (
                scheduled_at.isoformat() if scheduled_at is not None else None
            )
            if schedule_overrides and not scheduled_release:
                raise RuntimeError(
                    f"{clip_id} schedule_overrides require a scheduled "
                    "re-release authorization"
                )
            version, release_authorized = _authorized_rerelease_version(
                {"shorts": prior},
                clip,
                version,
                destinations,
                scheduled_date,
            )
            if scheduled_release and not release_authorized:
                raise RuntimeError(
                    f"{clip_id} does not match its scheduled re-release authorization"
                )
            if version.get("active_for_new_writes") is not True:
                raise RuntimeError(
                    f"The selected short variant for {clip_id} is retired; "
                    "nothing was submitted"
                )
            target = ShortDeliverySpec.target_fields(clip_id, version)
            target_revision = _document_revision(target)
            identity = _destination_external_id(
                self.episode_dir.name,
                request_id,
                destinations,
                target_revision,
                clip_id,
                destination_schema,
            )
            recorded = [item for item in prior if item["clip_id"] == clip_id]
            current_waves = [
                item
                for item in recorded
                if ShortDeliverySpec.receipt_matches_artifact(item, version)
            ]
            covered = {
                platform
                for item in recorded
                if item.get("status") != "cancelled"
                for platform in item.get("platforms", [])
            }
            deferred = sorted(set(approved_destinations) - set(destinations) - covered)
            for item in current_waves:
                overlap = set(item.get("platforms", [])) & set(destinations)
                if overlap and item.get("external_id") != identity:
                    raise RuntimeError(
                        f"{clip_id} already has a destination request for: "
                        + ", ".join(sorted(overlap))
                    )
                if not overlap and item.get("external_id"):
                    prior_time = (
                        _parse_time(item["scheduled_date"])
                        if item.get("scheduled") is not False
                        else None
                    )
                    if (
                        prior_time
                        and scheduled_at is not None
                        and _instant(prior_time) > _instant(reference)
                    ):
                        if _instant(prior_time) != _instant(scheduled_at):
                            raise RuntimeError(
                                f"{clip_id} deferred destinations must use its "
                                "existing future schedule"
                            )
                        co_schedule_ids.add(item["external_id"])
            historical = [item for item in recorded if item not in current_waves]
            historical_overlap = {
                platform
                for item in historical
                if item.get("status") != "cancelled"
                for platform in item.get("platforms", [])
            } & set(destinations)
            if historical_overlap and clip_id in overrides and not release_authorized:
                raise RuntimeError(
                    f"{clip_id} already has historical receipts for: "
                    + ", ".join(sorted(historical_overlap))
                )
            if (
                historical
                and clip_id not in overrides
                and not (
                    release_authorized
                    or valid_rerelease_copy_continuation(
                        {"shorts": prior}, clip, version, current_waves
                    )
                )
            ):
                raise RuntimeError(
                    f"A historical publication receipt exists for {clip_id}; "
                    "prepare an explicit re-release identity"
                )
            if scheduled_release and clip_id in copy_overrides:
                raise RuntimeError(
                    f"{clip_id} scheduled re-release waves must use canonical copy"
                )
            copy = copy_overrides.get(clip_id)
            if copy is None:
                copy = short_destination_copy(
                    data["short_metadata"].get(clip_id, {}),
                    destinations,
                    title=str(clip.get("title") or f"Clip {clip_id}"),
                    hub_url=hub_url,
                    youtube_url=data["funnel_urls"]["youtube"],
                    spotify_url=data["funnel_urls"]["spotify"],
                    channel_handle=self.config.get("podcast", {}).get(
                        "channel_handle", ""
                    ),
                )
            elif set(copy) != set(destinations):
                raise RuntimeError(
                    f"{clip_id} copy_override must match requested destinations"
                )
            if not _valid_destination_copy_shape(copy, destinations):
                raise RuntimeError(
                    f"{clip_id} destination copy is missing required string fields"
                )
            copy_issues = validate_destination_copy(copy)
            if copy_issues:
                raise RuntimeError(
                    f"{clip_id} destination copy is invalid: " + "; ".join(copy_issues)
                )
            if scheduled_release:
                prior_wave_copies = {
                    item.get("copy_revision")
                    for item in recorded
                    if item.get("rerelease_request_id")
                    == release_request.get("request_id")
                }
                if prior_wave_copies and prior_wave_copies != {
                    _document_revision(copy)
                }:
                    raise RuntimeError(
                        f"{clip_id} scheduled re-release waves must use identical copy"
                    )
            media_issues = validate_destination_media(
                self.episode_dir / version["path"], destinations
            )
            if media_issues:
                raise RuntimeError(
                    f"{clip_id} destination media is invalid: "
                    + "; ".join(media_issues)
                )
            intent = {
                **ShortDeliverySpec.receipt_identity(
                    clip_id, version, identity, destinations
                ),
                "status": "intent_recorded",
                "scheduled": scheduled_at is not None,
                "scheduled_date": (
                    scheduled_at.isoformat() if scheduled_at is not None else None
                ),
                "timezone": tz_name,
                "destination_schema": destination_schema,
                "destination_episode_id": self.episode_dir.name,
                "destination_profile_username": data["user"],
                "destination_request_id": request_id,
                "destination_actor": actor,
                "destination_reason": reason,
                "deferred_platforms": deferred,
                "target_revision": target_revision,
                "destination_copy": copy,
                "copy_schema": SHORT_COPY_SCHEMA,
                "copy_revision": _document_revision(copy),
            }
            if destination_bindings:
                intent["destination_bindings"] = destination_bindings
            intent["destination_request_revision"] = _document_revision(
                ShortDeliverySpec.request_fields(intent)
            )
            exact = next(
                (item for item in recorded if item.get("external_id") == identity), None
            )
            if exact and (
                exact.get("destination_request_revision")
                != intent["destination_request_revision"]
                or exact.get("status") == "cancelled"
            ):
                raise RuntimeError(f"{clip_id} destination request_id cannot be reused")
            if exact and not ShortDeliverySpec.receipt_matches_version(exact, version):
                raise RuntimeError(
                    f"The recorded receipt for {clip_id} does not match its "
                    "selected version; nothing was submitted"
                )
            targets.append(intent)
            delivery_states.append(
                (ShortDeliverySpec.create(clip, version, intent), exact)
            )
            identities.add(identity)

        request_receipts = [
            item for item in prior if item.get("destination_request_id") == request_id
        ]
        existing_request = {
            item["clip_id"]: item["destination_request_revision"]
            for item in request_receipts
        }
        request_shape = {
            item["clip_id"]: item["destination_request_revision"] for item in targets
        }
        if request_receipts and (
            len(existing_request) != len(request_receipts)
            or existing_request != request_shape
        ):
            raise RuntimeError("Short destination request_id cannot be reused")

        deferred_by_clip = {
            item["clip_id"]: item["deferred_platforms"] for item in targets
        }

        if inspect_remote:
            occupied = [
                item
                for item in self._occupied_schedule(data["api_key"], data["user"])
                if item.get("external_id") not in identities | co_schedule_ids
            ]
            self._reserve_deliveries(
                [spec for spec, _receipt in delivery_states],
                occupied,
                policy=(tz_name, weekday, weekend),
                reference=reference,
            )
        preview_revision = _document_revision(
            {
                "schema": destination_schema,
                "release_revision": revision,
                "request_id": request_id,
                "targets": [item["destination_request_revision"] for item in targets],
            }
        )
        if value.get("preview_revision") not in (None, preview_revision):
            raise RuntimeError("Destination preview revision does not match")
        return {
            "schema": destination_schema,
            "episode_id": self.episode_dir.name,
            "release_revision": revision,
            "request_id": request_id,
            "actor": actor,
            "reason": reason,
            "variant_overrides": overrides,
            "copy_overrides": copy_overrides,
            "schedule_overrides": schedule_overrides,
            "publish_now": publish_now,
            "approved_destinations": approved_destinations,
            "requested_destinations": destinations,
            "deferred_destinations": sorted(
                {
                    platform
                    for values in deferred_by_clip.values()
                    for platform in values
                }
            ),
            "deferred_destinations_by_clip": deferred_by_clip,
            "selected_clip_ids": selected_ids,
            "excluded_clip_ids": [
                item for item in approved_ids if item not in selected_ids
            ],
            "targets": targets,
            "_delivery_states": delivery_states,
            "_co_schedule_ids": co_schedule_ids,
            "preview_revision": preview_revision,
        }

    def preview_short_destinations(self, value):
        value = self._validated_destination_request(value)
        with publication_lock(self.episode_dir.parent):
            data = self._inputs()
            locked_overrides = self._validated_variant_overrides(data, value)
            with self._publication_output_locks(
                data["approved"], data["short_versions"], locked_overrides
            ):
                live_gate = quality_snapshot(self.episode_dir, config=self.config)[
                    "release_gate"
                ]
                if (
                    live_gate.get("safe") is not True
                    or live_gate.get("revision") != data["gate"]["revision"]
                ):
                    raise RuntimeError("Release inputs changed during preview")
                plan = self._destination_plan(data, value)
        public_plan = {
            key: item for key, item in plan.items() if not key.startswith("_")
        }
        return {
            **public_plan,
            "execute": {
                "destinations": public_plan["requested_destinations"],
                "clip_ids": public_plan["selected_clip_ids"],
                "request_id": public_plan["request_id"],
                "actor": public_plan["actor"],
                "reason": public_plan["reason"],
                "expected_release_revision": public_plan["release_revision"],
                "variant_overrides": public_plan["variant_overrides"],
                "copy_overrides": public_plan["copy_overrides"],
                "schedule_overrides": public_plan["schedule_overrides"],
                "publish_now": public_plan["publish_now"],
                "preview_revision": public_plan["preview_revision"],
            },
        }

    def _persist_destination_intents(self, previous, targets):
        receipts = self._validated_previous_shorts(previous)
        existing = {item.get("external_id"): item for item in receipts}
        current = {
            item["external_id"]: (
                existing[item["external_id"]]
                if item["external_id"] in existing
                and existing[item["external_id"]].get("status") != "intent_recorded"
                else item
            )
            for item in targets
        }
        history = [item for item in receipts if item.get("external_id") not in current]
        stored = {**previous, "shorts": [*current.values(), *history]}
        atomic_write_json(self.episode_dir / "publish.json", stored)
        return stored["shorts"]

    def execute(self) -> dict:
        destination_request = self._explicit_destination_request()
        data = self._inputs()
        episode, gate = (data[key] for key in ("episode", "gate"))
        api_key, user = (data[key] for key in ("api_key", "user"))
        approved, short_versions = (data[key] for key in ("approved", "short_versions"))
        previous = data["previous"]
        longform_revision = data["longform_revision"]
        revision = gate["revision"]
        locked_overrides = self._validated_variant_overrides(data, destination_request)
        with self._publication_output_locks(approved, short_versions, locked_overrides):
            live_snapshot = quality_snapshot(self.episode_dir, config=self.config)
            live_gate = live_snapshot["release_gate"]
            if (
                live_gate.get("safe") is not True
                or live_gate.get("revision") != revision
                or live_snapshot.get("approvals", {})
                .get("editorial", {})
                .get("revision")
                != longform_revision
            ):
                raise RuntimeError(
                    "Release media, copy, or approvals changed after publication "
                    "started; nothing was submitted"
                )
            plan = self._destination_plan(
                data, destination_request, inspect_remote=False
            )
            delivery_states = plan["_delivery_states"]
            previous_shorts = self._persist_destination_intents(
                previous,
                [spec.snapshot()["target"] for spec, _receipt in delivery_states],
            )
            shorts = self._publish_short_deliveries(
                delivery_states,
                episode,
                api_key,
                user,
                co_schedule_ids=plan["_co_schedule_ids"],
            )
            return self._result(
                shorts,
                previous.get("longform"),
                plan["requested_destinations"],
                revision,
                user,
                previous_shorts=previous_shorts,
                deferred_destinations=plan["deferred_destinations"],
            )

    @contextmanager
    def _publication_output_locks(self, clips, short_versions, variant_overrides=None):
        paths = {self.episode_dir / "upload_video.mp4"}
        for clip in clips:
            clip_id = str(clip.get("id", ""))
            paths.add(self.episode_dir / "shorts" / f"{clip_id}.mp4")
            version = short_versions.get(clip_id, {})
            if version.get("variant_id") is not None:
                relative_path = version.get("path")
                if not isinstance(relative_path, str):
                    raise RuntimeError(
                        f"The selected short version for {clip_id} has no media path"
                    )
                paths.add(self.episode_dir / relative_path)
        for clip_id, variant_id in (variant_overrides or {}).items():
            paths.add(background_variant_output(self.episode_dir, clip_id, variant_id))
        with ExitStack() as stack:
            for path in sorted(paths):
                stack.enter_context(render_output_lock(path))
            yield

    def _publish_longform(
        self,
        episode,
        metadata,
        previous,
        revision,
        longform_revision,
        quality_revision,
        youtube_url,
        platforms,
        api_key,
        user,
        approval_scope="release",
    ):
        if "youtube" not in platforms:
            return None
        identity = self._identity(longform_revision, "longform")
        legacy_identity = self._identity(revision, "longform")
        raw_url = episode.get("youtube_longform_url")
        if (
            raw_url
            and episode.get("youtube_longform_url_source") == "upload_post_receipt"
        ):
            recorded_revision = episode.get("youtube_longform_url_release_revision")
            recorded_editorial_revision = episode.get(
                "youtube_longform_url_editorial_revision"
            )
            recorded_identity = episode.get("youtube_longform_url_external_id")
            valid_identities = {
                publication_identity(self.episode_dir.name, bound_revision, "longform")
                for bound_revision in (
                    recorded_revision,
                    recorded_editorial_revision,
                )
                if isinstance(bound_revision, str) and bound_revision
            }
            if recorded_identity not in valid_identities:
                raise RuntimeError(
                    "The captured YouTube URL does not belong to a valid release identity"
                )
        if youtube_url:
            return {
                "status": "already_submitted",
                "platform": "youtube",
                "youtube_longform_url": youtube_url,
                "external_id": identity,
                "idempotency_key": identity,
                "editorial_revision": longform_revision,
            }
        retry_rejection = bool(
            isinstance(previous, dict)
            and previous.get("external_id") in {identity, legacy_identity}
            and self._definitive_payload_rejection(previous)
        )

        def reuse_previous():
            return {
                **previous,
                "editorial_revision": longform_revision,
                "reused_receipt": True,
            }

        if (
            isinstance(previous, dict)
            and previous.get("external_id") in {identity, legacy_identity}
            and previous.get("status") in RECORDED_STATES
            and not retry_rejection
        ):
            return reuse_previous()

        transport = self._verified_longform_transport(
            revision,
            longform_revision,
            quality_revision,
            approval_scope=approval_scope,
        )
        if retry_rejection and transport is None:
            return reuse_previous()
        retry_rejection = retry_rejection and transport is not None

        path = self.episode_dir / "upload_video.mp4"
        if not path.exists():
            raise RuntimeError(
                "Current YouTube longform is missing; shorts were not submitted"
            )
        title = metadata.get("title", "Podcast Episode")
        command = self._base_command(
            path,
            title,
            ["youtube"],
            identity,
            api_key,
            user,
            1200,
            video_url=transport["url"] if transport else None,
        )
        command += [
            "--form-string",
            f"youtube_title={title}",
            "--form-string",
            f"youtube_description={metadata.get('description', '')}",
        ]
        if metadata.get("tags"):
            command += ["--form-string", f"tags={','.join(metadata['tags'])}"]
        command += ["-X", "POST", UPLOAD_POST_URL]
        self.logger.info("Uploading longform to YouTube...")
        result = self._submit(command, 1200, identity)
        result.update(
            platform="youtube",
            editorial_revision=longform_revision,
        )
        if transport:
            result["transport"] = transport
        if retry_rejection:
            attempts = previous.get("attempt_history", [])
            if not isinstance(attempts, list):
                attempts = []
            result["attempt_history"] = [
                *attempts,
                {
                    key: previous.get(key)
                    for key in ("status", "error", "http_status", "external_id")
                    if previous.get(key) is not None
                },
            ]
        return result

    def _verified_longform_transport(
        self,
        release_revision: str,
        editorial_revision: str,
        quality_revision: str,
        *,
        approval_scope: str = "release",
    ) -> dict | None:
        """Use a current immutable R2 object instead of a multi-GB request body."""
        path = self.episode_dir / "video_feed.json"
        if not path.exists():
            return None
        try:
            from agents.video_feed import (
                VIDEO_FEED_SCHEMA,
                LongformVideoFeedAgent,
                VideoFeedAgent,
            )

            receipt = json.loads(path.read_text())
            video = receipt["video"]
            remote = video["remote"]
            agent_class = (
                LongformVideoFeedAgent
                if approval_scope == "longform"
                else VideoFeedAgent
            )
            agent = agent_class(self.episode_dir, self.config)
            current = agent._current_inputs(require_publish_approval=True)
            live_remote = agent._head_video_object(agent._r2_client(), current)
        except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Published video transport proof cannot be verified"
            ) from exc
        if not (
            isinstance(receipt, dict)
            and receipt.get("schema") == VIDEO_FEED_SCHEMA
            and receipt.get("status") == "published"
            and receipt.get("episode_id") == self.episode_dir.name
            and receipt.get("release_revision") == release_revision
            and receipt.get("editorial_revision") == editorial_revision
            and receipt.get("quality_revision") == quality_revision
            and receipt.get("publish_approval_current") is True
            and (
                approval_scope != "longform"
                or receipt.get("approval_scope") == "longform"
                and receipt.get("publication_revision")
                == current.get("publication_revision")
            )
            and current["release_revision"] == release_revision
            and current["editorial_revision"] == editorial_revision
            and current["quality_revision"] == quality_revision
            and current["video_url"].startswith("https://")
            and video.get("path") == str(current["video_path"])
            and video.get("object_key") == current["object_key"]
            and video.get("url") == current["video_url"]
            and video.get("sha256") == current["content_sha256"]
            and video.get("size_bytes") == current["video_size"]
            and video.get("render_fingerprint") == current["render_fingerprint"]
            and isinstance(remote, dict)
            and remote.get("status") == "ready"
            and remote.get("sha256") == current["content_sha256"]
            and remote.get("size_bytes") == current["video_size"]
            and remote.get("render_fingerprint") == current["render_fingerprint"]
            and live_remote is not None
        ):
            raise RuntimeError("Published video transport proof is stale")
        return {
            "kind": "verified_public_url",
            "url": current["video_url"],
            "sha256": f"sha256:{current['content_sha256']}",
            "render_fingerprint": current["render_fingerprint"],
            "size_bytes": current["video_size"],
        }

    @staticmethod
    def _definitive_payload_rejection(receipt: dict) -> bool:
        if (
            receipt.get("status") not in {"failed", "unknown"}
            or receipt.get("server_request_id")
            or receipt.get("job_id")
            or receipt.get("response")
        ):
            return False
        if receipt.get("http_status") == 413:
            return True
        error = receipt.get("error")
        return (
            isinstance(error, str) and "413 request entity too large" in error.lower()
        )

    def _publish_short_deliveries(
        self,
        delivery_states,
        episode,
        api_key,
        user,
        *,
        co_schedule_ids=(),
    ):
        results = []
        pending = []
        for spec, receipt in delivery_states:
            if receipt and receipt.get("status") not in {
                "unknown",
                "intent_recorded",
            }:
                results.append({**receipt, "reused_receipt": True})
            else:
                pending.append((spec, receipt))
        if not pending:
            return results

        policy = self._short_schedule_policy()
        tz_name = policy[0]
        reference = self._schedule_reference(episode, tz_name)

        with self._schedule_lock():
            bindings = (
                pending[0][0].snapshot()["target"].get("destination_bindings", {})
            )
            if bindings:
                self._verify_destination_bindings(api_key, user, bindings)
            occupied = self._occupied_schedule(api_key, user)
            current_identities = {spec.identity for spec, _receipt in delivery_states}
            occupied = [
                item
                for item in occupied
                if not (
                    item.get("external_id") in current_identities
                    and item.get("status") == "intent_recorded"
                )
                and item.get("external_id") not in co_schedule_ids
            ]
            remaining = []
            for spec, uncertain in pending:
                matches = [
                    item
                    for item in occupied
                    if item.get("external_id") == spec.identity
                ]
                existing = next(
                    (item for item in matches if item["source"] == "upload-post"),
                    matches[0] if matches else None,
                )
                if existing:
                    results.append(self._scheduled_receipt(spec, existing, tz_name))
                elif uncertain and uncertain.get("status") != "intent_recorded":
                    results.append({**uncertain, "reused_receipt": True})
                else:
                    remaining.append(spec)

            plans = self._reserve_deliveries(
                remaining,
                occupied,
                policy=policy,
                reference=reference,
            )

            for index, (spec, scheduled_at) in enumerate(plans, 1):
                self.report_progress(index, len(plans), f"Uploading {spec.clip_id}")
                result = self._submit_short(spec, scheduled_at, api_key, user)
                if result:
                    results.append(result)
        return results

    @staticmethod
    def _validated_previous_shorts(previous: dict) -> list[dict]:
        try:
            return validated_short_receipts(previous)
        except ValueError as exc:
            raise RuntimeError(
                "Cannot inspect prior short publication receipts; nothing was submitted"
            ) from exc

    def _submit_short(
        self,
        spec,
        scheduled_at,
        api_key,
        user,
    ):
        snapshot = spec.snapshot()
        clip, version = snapshot["clip"], snapshot["version"]
        clip_id, platforms = spec.clip_id, list(spec.platforms)
        path = self.episode_dir / version["path"]
        if not path.exists():
            self.logger.warning("Short not found: %s", clip_id)
            return None
        title = clip.get("title", f"Clip {clip_id}")
        copy = snapshot["target"]["destination_copy"]
        upload_title = (
            copy["instagram"]["text"] if platforms == ["instagram"] else title
        )
        command = self._base_command(
            path, upload_title, platforms, spec.identity, api_key, user, 600
        )
        destination_bindings = snapshot["target"].get("destination_bindings", {})
        for platform in platforms:
            for field, value in upload_fields(platform, copy.get(platform, {})).items():
                command += ["--form-string", f"{field}={value}"]
            binding = destination_bindings.get(platform, {})
            target = SHORT_PLATFORM_SPECS[platform].get("target")
            if target and binding.get("target_kind") in {"page", "board"}:
                _config_key, provider_key, _required = target
                command += [
                    "--form-string",
                    f"{provider_key}={binding['target_id']}",
                ]

        tz_name = self.get_config("schedule", "timezone", default="America/Los_Angeles")
        if scheduled_at:
            command += [
                "--form-string",
                f"scheduled_date={scheduled_at.replace(tzinfo=None).isoformat()}",
                "--form-string",
                f"timezone={tz_name}",
            ]
        command += ["-X", "POST", UPLOAD_POST_URL]

        return spec.receipt(
            self._submit(command, 600, spec.identity), scheduled_at, tz_name
        )

    def _base_command(
        self,
        path,
        title,
        platforms,
        identity,
        api_key,
        user,
        timeout,
        *,
        video_url=None,
    ):
        command = [
            "curl",
            "-sS",
            "--max-time",
            str(timeout),
            "-H",
            f"Authorization: Apikey {api_key}",
            "-H",
            f"Idempotency-Key: {identity}",
        ]
        if video_url:
            command += ["--form-string", f"video={video_url}"]
        else:
            command += ["-F", f"video=@{path}"]
        command += [
            "--form-string",
            f"user={user}",
            "--form-string",
            f"title={title}",
            "--form-string",
            f"request_id={identity}",
            "--form-string",
            f"external_id={identity}",
            "--form-string",
            "async_upload=true",
        ]
        for platform in platforms:
            command += ["--form-string", f"platform[]={platform}"]
        if "youtube" in platforms:
            declared = str(youtube_made_for_kids(self.config)).lower()
            command += ["--form-string", f"selfDeclaredMadeForKids={declared}"]
        return command

    @staticmethod
    def _run_json_command(command):
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )
        if process.returncode:
            raise ValueError(process.stderr.strip() or process.stdout.strip())
        return json.loads(process.stdout)

    @staticmethod
    def _provider_json(api_key: str, url: str, query: dict[str, str] | None = None):
        command = [
            "curl",
            "-sS",
            "--fail-with-body",
            "--max-time",
            "30",
            "-H",
            f"Authorization: Apikey {api_key}",
        ]
        if query:
            command.append("--get")
            for key, value in query.items():
                command += ["--data-urlencode", f"{key}={value}"]
        command.append(url)
        try:
            response = PublishAgent._run_json_command(command)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "Upload-Post destination account check timed out; "
                "no shorts were submitted"
            ) from None
        except OSError:
            raise RuntimeError(
                "Upload-Post destination account check could not run; "
                "no shorts were submitted"
            ) from None
        except json.JSONDecodeError:
            raise RuntimeError(
                "Upload-Post destination account response was not JSON; "
                "no shorts were submitted"
            ) from None
        except ValueError:
            raise RuntimeError(
                "Upload-Post rejected the destination account check; "
                "no shorts were submitted"
            ) from None
        if not isinstance(response, dict) or response.get("success") is not True:
            raise RuntimeError(
                "Upload-Post destination account response was invalid; "
                "no shorts were submitted"
            )
        return response

    @staticmethod
    def _provider_target_rows(response: dict, key: str, platform: str) -> list[dict]:
        rows = response.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError(
                f"Upload-Post {platform} target inventory is malformed; "
                "no shorts were submitted"
            )
        if any(
            not isinstance(row.get(field), str) or not row[field]
            for row in rows
            for field in ("id", "name")
        ) or len({row["id"] for row in rows}) != len(rows):
            raise RuntimeError(
                f"Upload-Post {platform} target inventory is ambiguous; "
                "no shorts were submitted"
            )
        return rows

    def _verify_destination_bindings(
        self, api_key: str, profile_username: str, bindings: dict[str, dict[str, str]]
    ) -> None:
        """Verify selected expansion accounts and exact native targets."""
        response = self._provider_json(
            api_key, f"{PROFILE_URL}/{quote(profile_username, safe='')}"
        )
        profile = response.get("profile")
        accounts = profile.get("social_accounts") if isinstance(profile, dict) else None
        if (
            not isinstance(profile, dict)
            or profile.get("username") != profile_username
            or not isinstance(accounts, dict)
        ):
            raise RuntimeError(
                "Upload-Post profile identity cannot be verified; no shorts were submitted"
            )
        for platform, binding in bindings.items():
            account = accounts.get(platform)
            reauth = (
                account.get("reauth_required") if isinstance(account, dict) else None
            )
            if (
                not isinstance(account, dict)
                or account.get("username") != binding["account_id"]
                or reauth is True
                or (reauth is not None and not isinstance(reauth, bool))
            ):
                raise RuntimeError(
                    f"Upload-Post {platform} account identity is unavailable or stale; "
                    "no shorts were submitted"
                )

        page_endpoints = {
            "facebook": (FACEBOOK_PAGES_URL, FACEBOOK_PAGE_PIN_URL),
            "linkedin": (LINKEDIN_PAGES_URL, LINKEDIN_PAGE_PIN_URL),
        }
        for platform, (pages_url, pin_url) in page_endpoints.items():
            binding = bindings.get(platform)
            if not binding:
                continue
            discovery = self._provider_json(
                api_key, pages_url, {"profile": profile_username}
            )
            pages = self._provider_target_rows(discovery, "pages", platform)
            pinned = self._provider_json(
                api_key, pin_url, {"profile_username": profile_username}
            )
            pinned_pages = self._provider_target_rows(pinned, "pages", platform)
            if "selected_page_id" not in pinned:
                raise RuntimeError(
                    f"Upload-Post {platform} pinned Page is malformed; "
                    "no shorts were submitted"
                )
            selected = pinned["selected_page_id"]
            if selected is not None and (not isinstance(selected, str) or not selected):
                raise RuntimeError(
                    f"Upload-Post {platform} pinned Page is malformed; "
                    "no shorts were submitted"
                )
            if binding["target_kind"] == "personal":
                if selected is not None:
                    raise RuntimeError(
                        "Upload-Post LinkedIn is pinned to a Page; no shorts were submitted"
                    )
                continue
            if (
                sum(page["id"] == binding["target_id"] for page in pages) != 1
                or sum(page["id"] == binding["target_id"] for page in pinned_pages) != 1
                or selected not in (None, binding["target_id"])
            ):
                raise RuntimeError(
                    f"Upload-Post {platform} Page target cannot be verified; "
                    "no shorts were submitted"
                )

        pinterest = bindings.get("pinterest")
        if pinterest:
            board_response = self._provider_json(
                api_key,
                PINTEREST_BOARD_URL,
                {"profile": profile_username},
            )
            boards = self._provider_target_rows(board_response, "boards", "pinterest")
            if (
                sum(board["id"] == pinterest["target_id"] for board in boards) != 1
                or not isinstance(board_response.get("pinterest_account_used"), str)
                or not board_response["pinterest_account_used"]
            ):
                raise RuntimeError(
                    "Upload-Post Pinterest board target cannot be verified; "
                    "no shorts were submitted"
                )

    @staticmethod
    def _submit(command, timeout, identity):
        base = {
            "external_id": identity,
            "idempotency_key": identity,
            "request_id": identity,
        }

        def failure(status, error, receipt=base, **details):
            return {**receipt, **details, "status": status, "error": error}

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return failure("unknown", f"Upload timed out ({timeout}s)")
        except OSError as error:
            return failure("failed", str(error))
        if process.returncode:
            return failure(
                "unknown",
                f"curl error: {process.stderr[:500]}",
                stdout=process.stdout,
            )
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError:
            payload_rejected = "413 request entity too large" in process.stdout.lower()
            return failure(
                "failed" if payload_rejected else "unknown",
                (
                    "Upload-Post rejected the request with HTTP 413"
                    if payload_rejected
                    else f"non-JSON response from Upload-Post: {process.stdout[:200]}"
                ),
                **({"http_status": 413} if payload_rejected else {}),
            )

        receipt = {**base, "response": response}
        if response.get("job_id"):
            receipt["job_id"] = response["job_id"]
        if response.get("request_id") and response["request_id"] != identity:
            receipt["server_request_id"] = response["request_id"]
        if response.get("success") is False or (
            response.get("error")
            and not response.get("request_id")
            and not response.get("job_id")
        ):
            return failure(
                "failed",
                response.get("error") or response.get("message"),
                receipt,
            )
        platform_results = response.get("results")
        if isinstance(platform_results, dict):
            failures = {
                platform: value
                for platform, value in platform_results.items()
                if isinstance(value, dict) and value.get("success") is False
            }
            requested = {
                value.removeprefix("platform[]=")
                for value in command
                if value.startswith("platform[]=")
            }
            for platform in requested - platform_results.keys():
                failures[platform] = {
                    "success": False,
                    "error": "Upload-Post omitted this requested platform",
                }
            if failures:
                return failure(
                    "partial_failure",
                    (
                        "Upload-Post reported platform failures: "
                        + ", ".join(sorted(failures))
                    ),
                    receipt,
                    platform_failures=failures,
                )
            return {**receipt, "status": "published"}
        request_id = response.get("request_id", response.get("job_id"))
        if not request_id:
            return failure(
                "unknown",
                response.get("error") or response.get("message"),
                receipt,
            )
        return {**receipt, "status": "submitted"}

    def _remote_schedule(self, api_key, user):
        command = [
            "curl",
            "-sS",
            "--fail-with-body",
            "--max-time",
            "30",
            "-H",
            f"Authorization: Apikey {api_key}",
            "--get",
            "--data-urlencode",
            f"profile_username={user}",
            SCHEDULE_URL,
        ]
        try:
            remote = self._run_json_command(command).get("scheduled_posts")
            if not isinstance(remote, list):
                raise TypeError("scheduled_posts is missing")
        except (
            OSError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            raise RuntimeError(
                "Cannot inspect Upload-Post schedule; no shorts were submitted: "
                f"{error}"
            ) from error
        return remote

    def _occupied_schedule(self, api_key, user):
        remote = self._remote_schedule(api_key, user)
        records = []
        for item in remote:
            if not isinstance(item, dict):
                raise TypeError(
                    "Cannot inspect Upload-Post schedule; a job is not an object"
                )
            try:
                scheduled_at = _parse_remote_schedule_time(item["scheduled_date"])
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    "Cannot inspect Upload-Post schedule; a job has no valid time"
                ) from error
            records.append(
                {
                    "scheduled_at": scheduled_at,
                    "external_id": item.get("external_id"),
                    "job_id": item.get("job_id"),
                    "status": "submitted",
                    "source": "upload-post",
                    "provider_record": item,
                }
            )

        for path in sorted(self.episode_dir.parent.glob("*/publish.json")):
            try:
                publish = json.loads(path.read_text())
                if not isinstance(publish, dict):
                    raise TypeError("top-level value is not an object")
                shorts = publish.get("shorts", [])
                if not isinstance(shorts, list) or any(
                    not isinstance(item, dict)
                    or (
                        "schedule_cancellation" in item
                        and validated_schedule_cancellation(item) is None
                    )
                    for item in shorts
                ):
                    raise TypeError("shorts is not a list")
            except (OSError, json.JSONDecodeError, TypeError) as error:
                raise RuntimeError(
                    f"Cannot inspect local publish receipt {path}; "
                    "no shorts were submitted"
                ) from error
            if publish.get("profile_username") not in (None, user):
                continue
            for item in shorts:
                if item.get("status") not in RECORDED_STATES or not item.get(
                    "scheduled"
                ):
                    continue
                try:
                    scheduled_at = _parse_time(item["scheduled_date"])
                except (AttributeError, KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(
                        f"Cannot inspect local publish receipt {path}; "
                        "a scheduled short has no valid time"
                    ) from error
                external_id = item.get("external_id")
                matching_remote = next(
                    (
                        remote_item
                        for remote_item in records
                        if remote_item["source"] == "upload-post"
                        and external_id
                        and remote_item.get("external_id") == external_id
                        and _instant(remote_item["scheduled_at"])
                        == _instant(scheduled_at)
                    ),
                    None,
                )
                content_identity = _schedule_content_identity(
                    item, user, path.parent.name
                )
                if matching_remote is not None:
                    if content_identity and _matching_schedule_row(
                        item, matching_remote.get("provider_record"), user
                    ):
                        matching_remote["_schedule_content_identity"] = content_identity
                        matching_remote["_schedule_placement_identity"] = (
                            _schedule_placement_identity(
                                item.get("destination_episode_id"),
                                item.get("clip_id"),
                            )
                        )
                        matching_remote["_schedule_platforms"] = tuple(
                            sorted(item["platforms"])
                        )
                    continue
                records.append(
                    {
                        "scheduled_at": scheduled_at,
                        "external_id": external_id,
                        "job_id": item.get("job_id")
                        or item.get("server_request_id")
                        or (
                            item.get("request_id")
                            if not item.get("idempotency_key")
                            else None
                        ),
                        "status": item.get("status"),
                        "source": str(path),
                    }
                )

        unique = {}
        for index, item in enumerate(records):
            job_id = item.get("job_id")
            key = ("job", job_id) if job_id else ("record", index)
            unique.setdefault(key, item)
        return list(unique.values())

    @contextmanager
    def _schedule_lock(self):
        if getattr(self, "_publication_lock_held", False):
            yield
            return
        with publication_lock(self.episode_dir.parent):
            yield

    def _short_schedule_policy(self):
        timezone_name = self.get_config(
            "schedule", "timezone", default="America/Los_Angeles"
        )
        limits = (
            int(self.get_config("schedule", "shorts_per_day_weekday", default=1)),
            int(self.get_config("schedule", "shorts_per_day_weekend", default=2)),
        )
        if any(not 1 <= limit <= 3 for limit in limits):
            raise RuntimeError("Shorts-per-day limits must be between 1 and 3")
        return timezone_name, *limits

    @staticmethod
    def _schedule_reference(episode, tz_name):
        value = (episode.get("publish_approval") or {}).get("approved_at")
        if not isinstance(value, str):
            raise TypeError("Publish approval needs an approved_at time")
        try:
            zone = ZoneInfo(tz_name)
            approved = _parse_time(value).astimezone(zone)
            now = datetime.now(zone)
            return max((approved, now), key=_instant)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "Publish approval has an invalid approved_at time"
            ) from error

    def _identity(self, revision, kind, clip_id=""):
        return publication_identity(self.episode_dir.name, revision, kind, clip_id)

    @staticmethod
    def _scheduled_receipt(
        spec,
        existing,
        tz_name,
    ):
        scheduled_at = existing["scheduled_at"].astimezone(ZoneInfo(tz_name))
        destination_target = spec.snapshot()["target"]
        candidate = {**destination_target, "job_id": existing.get("job_id")}
        if existing.get("source") != "upload-post" or not _matching_schedule_row(
            candidate,
            existing.get("provider_record"),
            destination_target["destination_profile_username"],
        ):
            raise RuntimeError("Existing provider schedule conflicts with preview")
        provider_receipt = {
            "status": existing.get("status", "submitted"),
            "reused_receipt": True,
            "receipt_source": existing["source"],
        }
        if existing.get("job_id"):
            provider_receipt["job_id"] = existing["job_id"]
        return spec.receipt(provider_receipt, scheduled_at, tz_name)

    def _reserve_deliveries(
        self,
        deliveries,
        occupied,
        *,
        policy,
        reference,
    ):
        tz_name, weekday_limit, weekend_limit = policy
        reservations = list(occupied)
        plans = []
        for spec in deliveries:
            scheduled_at = (
                _parse_time(spec.snapshot()["target"]["scheduled_date"])
                if spec.scheduled
                else None
            )
            if scheduled_at is not None:
                if _instant(scheduled_at) <= _instant(reference):
                    raise RuntimeError(
                        f"Schedule for {spec.clip_id} is not in the future; "
                        "no shorts were submitted"
                    )
                if _instant(scheduled_at) > _instant(reference + timedelta(days=365)):
                    raise RuntimeError(
                        f"Schedule for {spec.clip_id} is more than 365 days away; "
                        "no shorts were submitted"
                    )
                self._reserve(
                    reservations,
                    scheduled_at,
                    spec.identity,
                    spec.clip_id,
                    tz_name,
                    weekday_limit,
                    weekend_limit,
                    episode_id=self.episode_dir.name,
                    platforms=spec.platforms,
                )
            plans.append((spec, scheduled_at))
        return plans

    @staticmethod
    def _reserve(
        reservations,
        scheduled_at,
        identity,
        clip_id,
        tz_name,
        weekday_limit,
        weekend_limit,
        *,
        episode_id,
        platforms,
    ):
        zone = ZoneInfo(tz_name)
        local = scheduled_at.astimezone(zone)
        placement_identity = _schedule_placement_identity(episode_id, clip_id)
        if (
            placement_identity is None
            or not isinstance(platforms, (list, tuple))
            or not platforms
            or any(
                not isinstance(platform, str) or not platform for platform in platforms
            )
            or len(platforms) != len(set(platforms))
        ):
            raise TypeError("Schedule placement identity and platforms are required")
        platform_set = set(platforms)
        same_day = [
            item
            for item in reservations
            if item["scheduled_at"].astimezone(zone).date() == local.date()
        ]
        limit = weekend_limit if local.weekday() >= 4 else weekday_limit
        exact = [
            item
            for item in same_day
            if _instant(item["scheduled_at"]) == _instant(scheduled_at)
        ]
        used_platforms = set()
        exact_conflict = False
        for item in exact:
            item_platforms = item.get("_schedule_platforms")
            if not (
                item.get("_schedule_placement_identity") == placement_identity
                and isinstance(item_platforms, (list, tuple))
                and item_platforms
                and all(
                    isinstance(platform, str) and platform
                    for platform in item_platforms
                )
                and len(item_platforms) == len(set(item_platforms))
            ):
                exact_conflict = True
                break
            item_platform_set = set(item_platforms)
            if used_platforms & item_platform_set:
                exact_conflict = True
                break
            used_platforms.update(item_platform_set)
        if used_platforms & platform_set:
            exact_conflict = True
        candidate = {
            "scheduled_at": scheduled_at,
            "external_id": identity,
            "source": "current-release",
            "_schedule_placement_identity": placement_identity,
            "_schedule_platforms": tuple(sorted(platform_set)),
        }
        if exact_conflict or _schedule_capacity_count([*same_day, candidate]) > limit:
            raise ShortDestinationConflict(
                f"Schedule collision for {clip_id} at {local.isoformat()}; "
                "no shorts were submitted"
            )
        reservations.append(candidate)

    @staticmethod
    def _schedule_to_datetime(sched, tz_name, *, reference=None):
        zone = ZoneInfo(tz_name)
        if sched.get("scheduled_date"):
            supplied = _parse_time(sched["scheduled_date"])
            local = supplied.astimezone(zone)
            if supplied.utcoffset() != local.utcoffset():
                raise ValueError(
                    f"scheduled_date offset does not match timezone {tz_name}"
                )
            wall_time = local.replace(tzinfo=None)
            if (
                wall_time.replace(tzinfo=zone, fold=0).utcoffset()
                != wall_time.replace(tzinfo=zone, fold=1).utcoffset()
            ):
                raise ValueError(
                    f"scheduled_date is ambiguous or nonexistent in {tz_name}"
                )
            return local
        reference = reference or datetime.now(zone)
        if reference.tzinfo is None:
            raise ValueError("schedule reference must include a UTC offset")
        day_offset = int(sched.get("day_offset", 0))
        if day_offset < 0:
            raise ValueError("day_offset cannot be negative")
        slot = sched.get("time_slot", "morning")
        if slot not in SLOT_HOURS:
            raise ValueError(f"unknown time_slot: {slot}")
        date = reference.astimezone(zone).date() + timedelta(days=day_offset)
        hour = SLOT_HOURS[slot]
        return datetime.combine(date, time(hour=hour), tzinfo=zone)

    @staticmethod
    def _merge_short_receipts(current, previous):
        current = [item for item in current if isinstance(item, dict)]
        previous = previous if isinstance(previous, list) else []
        current_keys = {
            key for item in current if (key := receipt_key(item)) is not None
        }
        history = []
        for item in previous:
            if not isinstance(item, dict):
                continue
            key = receipt_key(item)
            if key is not None and key in current_keys:
                continue
            history.append({**item, "historical_receipt": True})
        return current + history

    @classmethod
    def _result(
        cls,
        shorts,
        longform,
        platforms,
        revision,
        user,
        previous_shorts=None,
        deferred_destinations=None,
    ):
        counts = {
            "shorts_submitted": 0,
            "shorts_reused": 0,
            "shorts_published": 0,
            "shorts_failed": 0,
            "shorts_unknown": 0,
        }
        for item in shorts:
            reused = bool(item.get("reused_receipt"))
            status = item.get("status")
            counts["shorts_submitted"] += status == "submitted" and not reused
            counts["shorts_reused"] += reused
            counts["shorts_published"] += status == "published"
            counts["shorts_failed"] += status in {"failed", "partial_failure"}
            counts["shorts_unknown"] += status == "unknown"
        result = {
            "shorts": cls._merge_short_receipts(shorts, previous_shorts),
            "longform": longform,
            **counts,
            "platforms": platforms,
            "release_revision": revision,
            "profile_username": user,
        }
        if deferred_destinations is not None:
            result["deferred_destinations"] = deferred_destinations
        if result["shorts_failed"]:
            result.update(
                publish_status="shorts_failed",
                action_required=True,
                next_action=(
                    "Inspect each platform result and use Upload-Post's status or "
                    "failed-platform retry workflow. The full batch is not "
                    "automatically resubmitted."
                ),
            )
        elif result["shorts_unknown"]:
            result.update(
                publish_status="shorts_status_unknown",
                action_required=True,
                next_action=(
                    "Poll Upload-Post with the saved request_id or job_id before "
                    "attempting another submission."
                ),
            )
        elif shorts and counts["shorts_reused"] == len(shorts):
            result["publish_status"] = "already_submitted"
        elif shorts and counts["shorts_published"] == len(shorts):
            result["publish_status"] = "published"
        else:
            result["publish_status"] = "submitted"
        return result


class LongformPublishAgent(PublishAgent):
    """Upload only the approved YouTube full episode and never inspect shorts."""

    def execute(self) -> dict:
        gate = longform_publication_snapshot(self.episode_dir, config=self.config)
        if gate.get("safe") is not True:
            reasons = "; ".join(
                str(item.get("message", ""))
                for item in gate.get("blockers", [])
                if item.get("message")
            )
            raise RuntimeError(f"longform publication gate blocked — {reasons}")
        youtube_plan = gate.get("publish_plan", {}).get("youtube", {})
        if youtube_plan.get("enabled") is not True:
            raise RuntimeError("The approved longform plan does not enable YouTube")

        api_key = os.getenv("UPLOAD_POST_API_KEY")
        user = os.getenv("UPLOAD_POST_USER", "")
        if not api_key or not user:
            raise RuntimeError("Upload-Post credentials are not configured")
        episode = self.load_json("episode.json")
        metadata = canonical_release_metadata(self.episode_dir, episode, [])
        missing = [
            field
            for field in ("title", "description")
            if not metadata.get("longform", {}).get(field)
        ]
        if missing:
            raise RuntimeError(
                "Longform YouTube copy is missing: " + ", ".join(missing)
            )
        previous = self._previous_publish()

        longform_revision = str(gate["editorial_revision"])
        release_revision = str(gate["source_release_revision"])
        quality_revision = str(gate["quality_revision"])
        funnel_urls = current_funnel_urls(
            episode,
            episode_dir=self.episode_dir,
            editorial_revision_value=longform_revision,
            quality_revision_value=quality_revision,
        )
        with render_output_lock(self.episode_dir / "upload_video.mp4"):
            current = longform_publication_snapshot(
                self.episode_dir, config=self.config
            )
            if current.get("safe") is not True or current.get("revision") != gate.get(
                "revision"
            ):
                raise RuntimeError(
                    "Longform media, copy, or approval changed before submission"
                )
            longform = self._publish_longform(
                episode,
                metadata["longform"],
                previous.get("longform"),
                release_revision,
                longform_revision,
                quality_revision,
                funnel_urls["youtube"],
                ["youtube"],
                api_key,
                user,
                approval_scope="longform",
            )

        state = longform.get("status") if isinstance(longform, dict) else "failed"
        result = {
            **previous,
            "longform": longform,
            "shorts_submitted": 0,
            "shorts_reused": 0,
            "shorts_published": 0,
            "shorts_failed": 0,
            "shorts_unknown": 0,
            "platforms": ["youtube"],
            "release_revision": release_revision,
            "profile_username": user,
            "publish_status": state,
            "approval_scope": "longform",
            "longform_publication_revision": gate["revision"],
            "shorts": previous.get("shorts", []),
        }
        for stale_field in (
            "action_required",
            "next_action",
            "shorts_deferred",
            "shorts_deferred_reason",
            "deferred_destinations",
        ):
            result.pop(stale_field, None)
        if state == "unknown":
            result.update(
                action_required=True,
                next_action=(
                    "Reconcile the saved Upload-Post request before any retry."
                ),
            )
        elif state in {"failed", "partial_failure"}:
            result.update(
                action_required=True,
                next_action="Resolve the saved provider failure before retrying.",
            )

        old_longform = previous.get("longform")
        history = previous.get("longform_history")
        history = (
            [item for item in history if isinstance(item, dict)]
            if isinstance(history, list)
            else []
        )
        if (
            isinstance(old_longform, dict)
            and isinstance(longform, dict)
            and old_longform.get("external_id") != longform.get("external_id")
        ):

            def identity(item):
                fields = ("external_id", "job_id", "server_request_id")
                return tuple(item.get(field) for field in fields)

            if not any(identity(item) == identity(old_longform) for item in history):
                history.append({**old_longform, "historical_receipt": True})
        if history:
            result["longform_history"] = history
        return result
