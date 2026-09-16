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
    publication_identity,
    quality_snapshot,
    release_metadata_issues,
    short_distribution_state,
    youtube_made_for_kids,
)
from lib.atomic_write import atomic_write_json
from lib.delivery_video import read_render_manifest, render_output_lock
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
    BACKGROUND_VARIANT_ID,
    DISTRIBUTION_RELEASE_FIELD,
    DISTRIBUTION_VARIANT_FIELD,
    background_variant_output,
    distribution_release_revision,
    require_background_variant,
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
        or (
            "destination_request_id" in receipt
            and not _destination_receipt_valid(receipt)
        )
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
    values = response.get("results")
    items = list(values.values()) if isinstance(values, dict) else values
    observed = [response]
    if isinstance(items, list):
        observed.extend(item for item in items if isinstance(item, dict))
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


def _short_target_fields(clip_id, version):
    rerelease = version.get("re_release_request")
    return {
        "clip_id": clip_id,
        "version": version.get("version"),
        "variant_id": version.get("variant_id"),
        "render_fingerprint": version.get("render_fingerprint"),
        "approval_revision": version.get("revision")
        or version.get("approval_revision"),
        "rerelease_request_id": (
            rerelease.get("request_id")
            if isinstance(rerelease, dict)
            else version.get("rerelease_request_id")
        ),
        "rerelease_authorization_revision": (
            rerelease.get("revision")
            if isinstance(rerelease, dict)
            else version.get("rerelease_authorization_revision")
        ),
    }


def _rerelease_receipt_fields(request):
    if not isinstance(request, dict):
        return {}
    fields = {
        "rerelease_request_id": request.get("request_id"),
        "rerelease_actor": request.get("actor"),
        "rerelease_reason": request.get("reason"),
        "rerelease_authorization_revision": request.get("revision"),
        "parent_receipt_history_revision": request.get("receipt_history_revision"),
    }
    acknowledgement = request.get("unresolved_history_acknowledgement")
    if acknowledgement is not None:
        fields["unresolved_history_acknowledgement"] = acknowledgement
    return fields


def _destination_request_fields(receipt):
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
        target = _short_target_fields(receipt["clip_id"], receipt)
        request = _destination_request_fields(receipt)
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
            **(
                {
                    "first_comment": _build_first_comment(
                        youtube_url, spotify_url, channel_handle
                    )
                }
                if youtube_url
                else {}
            ),
        }
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


def _schedule_capacity_count(records: list[dict]) -> int:
    """Count disjoint destination jobs for one exact artifact as one slot."""
    count = 0
    groups = {}
    for item in records:
        identity = item.get("_schedule_content_identity")
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


def validated_schedule_cancellation(receipt: dict) -> dict | None:
    """Validate the durable operation embedded in a scheduled receipt."""
    operation = receipt.get("schedule_cancellation")
    original = receipt.get("pre_cancellation_receipt")
    if not isinstance(operation, dict) or not isinstance(original, dict):
        return None
    schema = operation.get("schema")
    snapshot = operation.get("snapshot")
    target = snapshot.get("target") if isinstance(snapshot, dict) else None
    remote = snapshot.get("remote_job") if isinstance(snapshot, dict) else None
    state = operation.get("state")
    base_keys = {
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
    if not isinstance(state, str) or state not in {
        "delete_started",
        "outcome_uncertain",
        "delete_confirmed",
        "cancelled",
    }:
        return None
    expected_keys = {
        "delete_started": base_keys,
        "outcome_uncertain": base_keys | {"delete"},
        "delete_confirmed": base_keys | {"delete"},
        "cancelled": base_keys
        | {"delete", "post_delete", "completed_at", "terminal_event"},
    }[state]
    if state == "delete_confirmed" and "post_delete" in operation:
        expected_keys = expected_keys | {"post_delete"}
    if not (
        _has_exact_keys(operation, expected_keys)
        and _has_exact_keys(operation.get("pre_delete"), {"checked_at", "evidence"})
        and (
            _has_exact_keys(target, {"variant_id", "revision", "render_fingerprint"})
            if schema == SCHEDULE_CANCELLATION_SCHEMA
            else target is None
        )
    ):
        return None
    if not (
        schema in {SCHEDULE_CANCELLATION_SCHEMA, EXACT_SCHEDULE_CANCELLATION_SCHEMA}
        and all(
            isinstance(operation.get(key), str) and operation[key]
            for key in ("operation_id", "clip_id", "actor", "reason", "started_at")
        )
        and operation["actor"] == operation["actor"].strip()
        and operation["reason"] == operation["reason"].strip()
        and len(operation["reason"]) >= 3
        and operation["clip_id"] == original.get("clip_id")
        and "schedule_cancellation" not in original
        and "pre_cancellation_receipt" not in original
        and original.get("scheduled") is True
        and validated_terminal_destinations(
            original.get("platforms"),
            {
                platform: {"state": "cancelled"}
                for platform in (original.get("platforms") or [])
            },
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
            snapshot.get("profile_username"),
            target,
            remote,
        )
        and (
            schema == EXACT_SCHEDULE_CANCELLATION_SCHEMA
            or (
                isinstance(target, dict)
                and "variant_id" in target
                and (
                    target["variant_id"] is None
                    or isinstance(target["variant_id"], str)
                )
                and all(
                    isinstance(target.get(key), str) and target[key]
                    for key in ("revision", "render_fingerprint")
                )
            )
        )
        and _matching_schedule_row(original, remote, snapshot["profile_username"])
        and isinstance(operation.get("pre_delete"), dict)
        and isinstance(operation["pre_delete"].get("checked_at"), str)
        and schedule_cancellation_provider_safe(
            original,
            operation["pre_delete"].get("evidence"),
            profile_username=snapshot["profile_username"],
            after_delete=False,
        )[0]
    ):
        return None
    if state != "cancelled" and not _matches_exact_receipt(
        receipt,
        {
            **original,
            "pre_cancellation_receipt": original,
            "schedule_cancellation": operation,
        },
    ):
        return None
    deletion = operation.get("delete")
    if state == "delete_started":
        return operation
    if not (
        isinstance(deletion, dict)
        and deletion.get("job_id") == original["job_id"]
        and isinstance(deletion.get("attempted_at"), str)
        and deletion["attempted_at"]
    ):
        return None
    error_shape = (
        _has_exact_keys(deletion, {"job_id", "attempted_at", "error"})
        and isinstance(deletion.get("error"), str)
        and bool(deletion["error"])
    )
    response_keys = {"job_id", "attempted_at", "http_status"}
    if "response" in deletion:
        response_keys.add("response")
    response_shape = (
        _has_exact_keys(deletion, response_keys)
        and isinstance(deletion.get("http_status"), int)
        and not isinstance(deletion.get("http_status"), bool)
        and ("response" not in deletion or isinstance(deletion["response"], dict))
        and not (
            deletion["http_status"] == 200
            and isinstance(deletion.get("response"), dict)
            and deletion["response"].get("success") is True
        )
    )
    uncertain_deletion = error_shape or response_shape
    if state == "outcome_uncertain":
        return operation if uncertain_deletion else None
    confirmed_deletion = (
        _has_exact_keys(
            deletion,
            {"job_id", "attempted_at", "http_status", "response", "confirmed_at"},
        )
        and deletion.get("http_status") == 200
        and isinstance(deletion.get("response"), dict)
        and deletion["response"].get("success") is True
        and isinstance(deletion.get("confirmed_at"), str)
        and deletion["confirmed_at"]
    )
    if not confirmed_deletion and not (
        state == "cancelled"
        and schema == EXACT_SCHEDULE_CANCELLATION_SCHEMA
        and uncertain_deletion
    ):
        return None
    if state == "delete_confirmed" and "post_delete" not in operation:
        return operation
    if state == "delete_confirmed":
        post = operation["post_delete"]
        if not (
            _has_exact_keys(post, {"checked_at", "calendar", "evidence"})
            and isinstance(post["checked_at"], str)
            and post["checked_at"]
            and (
                post["calendar"] is None
                or (
                    isinstance(post["calendar"], list)
                    and all(isinstance(item, dict) for item in post["calendar"])
                )
            )
            and (post["evidence"] is None or isinstance(post["evidence"], dict))
        ):
            return None
    if state != "cancelled":
        return operation

    post = operation.get("post_delete")
    destinations = {
        platform: {"state": "cancelled"} for platform in original["platforms"]
    }
    event = operation.get("terminal_event")
    history = original.get("status_history", [])
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
        and event
        == {
            "observed_at": operation["completed_at"],
            "previous_status": original.get("status"),
            "status": "cancelled",
            "provider_status": "cancelled",
            "profile_username": snapshot["profile_username"],
            "evidence_source": "schedule_cancellation",
            "terminal_destinations": destinations,
        }
        and _matches_exact_receipt(
            receipt,
            {
                **original,
                "status": "cancelled",
                "scheduled": False,
                "terminal_destinations": destinations,
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
    results = response.get("results")
    items = list(results.values()) if isinstance(results, dict) else results
    evidence = [response]
    if isinstance(items, list):
        evidence.extend(item for item in items if isinstance(item, dict))
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
    items: list[tuple[str, dict]] = []
    if isinstance(results, dict):
        if any(not isinstance(value, dict) for value in results.values()):
            return None
        items = [
            (str(platform), value)
            for platform, value in results.items()
            if isinstance(value, dict)
        ]
    elif isinstance(results, list) and response.get("status") in {
        "completed",
        "failed",
        "cancelled",
    }:
        if any(not isinstance(value, dict) for value in results):
            return None
        total = response.get("total")
        completed = response.get("completed")
        if total is not None and (not isinstance(total, int) or total != len(expected)):
            return None
        if completed is not None and (
            not isinstance(completed, int) or completed != total
        ):
            return None
        items = [
            (str(value.get("platform", "")), value)
            for value in results
            if isinstance(value, dict)
        ]
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
    encoded = json.dumps(receipts, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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


def valid_rerelease_authorization(publish: dict, clip: dict, version: dict) -> bool:
    authorization = clip.get(DISTRIBUTION_RELEASE_FIELD)
    if not isinstance(authorization, dict):
        return False
    required = (
        "request_id",
        "actor",
        "reason",
        "target_revision",
        "render_fingerprint",
        "receipt_history_revision",
        "revision",
        "created_at",
    )
    if any(
        not isinstance(authorization.get(field), str) or not authorization[field]
        for field in required
    ):
        return False
    if (
        "variant_id" not in authorization
        or authorization.get("variant_id") != version.get("variant_id")
        or authorization["target_revision"] != version.get("revision")
        or authorization["render_fingerprint"] != version.get("render_fingerprint")
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
    expected = distribution_release_revision(
        request_id=authorization["request_id"],
        actor=authorization["actor"],
        reason=authorization["reason"],
        variant_id=authorization.get("variant_id"),
        target_revision=authorization["target_revision"],
        render_fingerprint=authorization["render_fingerprint"],
        receipt_history_revision=history_revision,
        unresolved_history_acknowledgement=authorization.get(
            "unresolved_history_acknowledgement"
        ),
    )
    acknowledgement = authorization.get("unresolved_history_acknowledgement")
    obligations, acknowledgement_allowed = unresolved_receipt_obligations(
        publish,
        str(clip.get("id", "")),
        exclude_request_id=authorization["request_id"],
    )
    acknowledgement_valid = acknowledgement is None and not obligations
    if isinstance(acknowledgement, dict):
        acknowledgement_valid = bool(
            acknowledgement_allowed
            and version.get("variant_id") == BACKGROUND_VARIANT_ID
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
    if not (
        identity
        and all(status.get(field) == value for field, value in identity.items())
        and _provider_states(status) == {"queued"}
        and str(status.get("scheduler_status", "")).strip().lower() == "pending"
        and isinstance(platforms, list)
        and platforms
        and len(platforms) == len(set(platforms))
        and isinstance(results, list)
        and len(results) == len(platforms)
        and all(isinstance(item, dict) for item in results)
        and all(
            isinstance(item.get("platform"), str) and item["platform"]
            for item in results
        )
        and len({item["platform"] for item in results}) == len(results)
        and {item["platform"] for item in results} == set(platforms)
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
        for item in results
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
        results = status.get("results")
        children = (
            list(results.values()) if isinstance(results, dict) else results or []
        )
        if not isinstance(children, list) or any(
            not isinstance(item, dict) for item in children
        ):
            return False, "Provider status results are malformed"
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

    def run(self) -> dict:
        """Keep distribution selection locked until publish.json is durable."""
        with publication_lock(self.episode_dir.parent):
            self._publication_lock_held = True
            try:
                return super().run()
            finally:
                self._publication_lock_held = False

    def _inputs(self, *, bind_legacy=False):
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

        try:
            previous = self.load_json("publish.json")
        except FileNotFoundError:
            previous = {}
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Cannot inspect the current publish receipt; nothing was submitted"
            ) from error
        if not isinstance(previous, dict):
            raise TypeError(
                "Cannot inspect the current publish receipt; nothing was submitted"
            )
        previous_shorts = self._validated_previous_shorts(previous)
        funnel_urls = current_funnel_urls(
            episode,
            episode_dir=self.episode_dir,
            editorial_revision_value=snapshot["approvals"]["editorial"]["revision"],
            quality_revision_value=snapshot["quality"]["current_revision"],
        )
        longform_revision = snapshot["approvals"]["editorial"]["revision"]
        if bind_legacy:
            self._bind_legacy_funnel_urls(episode, funnel_urls, longform_revision)
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
        short_versions = gate.get("short_versions")
        if not isinstance(short_versions, dict) or any(
            not isinstance(short_versions.get(str(clip.get("id", ""))), dict)
            or short_versions[str(clip.get("id", ""))].get("current") is not True
            or short_versions[str(clip.get("id", ""))].get("approval_current")
            is not True
            for clip in approved
        ):
            raise RuntimeError(
                "The selected short versions are unavailable or unapproved"
            )
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

    @staticmethod
    def _validated_variant_overrides(data, value):
        overrides = value.get("variant_overrides", {})
        approved_ids = {str(clip.get("id", "")) for clip in data["approved"]}
        if not isinstance(overrides, dict) or any(
            not isinstance(clip_id, str)
            or clip_id not in approved_ids
            or not isinstance(variant_id, str)
            for clip_id, variant_id in overrides.items()
        ):
            raise RuntimeError(
                "variant_overrides must map approved clip IDs to variants"
            )
        try:
            for variant_id in overrides.values():
                require_background_variant(variant_id)
        except KeyError as exc:
            raise RuntimeError(str(exc).strip("'")) from exc
        return overrides

    def _destination_plan(self, data, value):
        if not isinstance(value, dict):
            raise TypeError("Short destination request is malformed")
        try:
            request_id = str(uuid.UUID(str(value.get("request_id"))))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("Short destination request_id must be a UUID") from exc
        actor, reason = value.get("actor"), value.get("reason")
        destinations = value.get("destinations")
        overrides = self._validated_variant_overrides(data, value)
        copy_overrides = value.get("copy_overrides", {})
        publish_now = value.get("publish_now", False)
        if value.get("request_id") != request_id:
            raise RuntimeError("Short destination request_id must be canonical")
        if not isinstance(actor, str) or actor != actor.strip() or not actor:
            raise RuntimeError("Short destination actor is required")
        if not isinstance(reason, str) or reason != reason.strip() or len(reason) < 3:
            raise RuntimeError("Short destination reason is required")
        if (
            not isinstance(destinations, list)
            or not destinations
            or any(not isinstance(item, str) for item in destinations)
            or len(destinations) != len(set(destinations))
        ):
            raise RuntimeError("Choose at least one unique short destination")
        destinations = sorted(destinations)
        if not isinstance(publish_now, bool):
            raise TypeError("publish_now must be true or false")
        if not isinstance(copy_overrides, dict) or any(
            not isinstance(clip_id, str) or not isinstance(copy, dict)
            for clip_id, copy in copy_overrides.items()
        ):
            raise RuntimeError("copy_overrides must map clip IDs to copy objects")
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
            if overrides or copy_overrides or publish_now
            else EXPANDED_SHORT_DESTINATION_SCHEMA
            if set(destinations) & EXPANSION_DESTINATIONS
            else SHORT_DESTINATION_SCHEMA
        )

        approved_ids = [str(clip.get("id", "")) for clip in data["approved"]]
        requested_ids = value.get("clip_ids")
        if requested_ids is None:
            selected_ids = approved_ids
        elif (
            not isinstance(requested_ids, list)
            or not requested_ids
            or any(not isinstance(item, str) or not item for item in requested_ids)
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
        effective_versions = self._destination_versions(data, overrides)
        hub_url = episode_hub_url(self.config, self.episode_dir.name)
        if set(destinations) - {"x"} and not hub_url:
            raise RuntimeError("An HTTPS exact-episode hub URL is required")
        try:
            destination_bindings = configured_destination_bindings(
                self.config, destinations
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if destination_bindings:
            self._verify_destination_bindings(
                data["api_key"], data["user"], destination_bindings
            )

        schedule = data["metadata"].get("schedule", [])
        schedule_by_clip = {}
        for entry in schedule if isinstance(schedule, list) else []:
            clip_id = str(entry.get("clip_id", "")) if isinstance(entry, dict) else ""
            if clip_id not in selected_ids:
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

        tz_name = self.get_config("schedule", "timezone", default="America/Los_Angeles")
        weekday = int(self.get_config("schedule", "shorts_per_day_weekday", default=1))
        weekend = int(self.get_config("schedule", "shorts_per_day_weekend", default=2))
        if not 1 <= weekday <= 3 or not 1 <= weekend <= 3:
            raise RuntimeError("Shorts-per-day limits must be between 1 and 3")
        reference = self._schedule_reference(data["episode"], tz_name)
        by_id = {str(clip.get("id", "")): clip for clip in data["approved"]}
        prior = data["previous_shorts"]
        targets = []
        identities = set()
        co_schedule_ids = set()
        for clip_id in selected_ids:
            clip, version = by_id[clip_id], effective_versions[clip_id]
            rerelease = version.get("re_release_request")
            target = _short_target_fields(clip_id, version)
            target_revision = _document_revision(target)
            identity = _destination_external_id(
                self.episode_dir.name,
                request_id,
                destinations,
                target_revision,
                clip_id,
                destination_schema,
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
            recorded = [item for item in prior if item["clip_id"] == clip_id]
            current_waves = [
                item
                for item in recorded
                if self._receipt_matches_artifact(item, version)
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
            if historical_overlap and clip_id in overrides:
                raise RuntimeError(
                    f"{clip_id} already has historical receipts for: "
                    + ", ".join(sorted(historical_overlap))
                )
            if (
                historical
                and clip_id not in overrides
                and not (
                    valid_rerelease_authorization({"shorts": prior}, clip, version)
                    or valid_rerelease_copy_continuation(
                        {"shorts": prior}, clip, version, current_waves
                    )
                )
            ):
                raise RuntimeError(
                    f"A historical publication receipt exists for {clip_id}; "
                    "prepare an explicit re-release identity"
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
            media_issues = validate_destination_media(
                self.episode_dir / version["path"], destinations
            )
            if media_issues:
                raise RuntimeError(
                    f"{clip_id} destination media is invalid: "
                    + "; ".join(media_issues)
                )
            intent = {
                "clip_id": clip_id,
                "status": "intent_recorded",
                "platforms": destinations,
                "request_id": identity,
                "external_id": identity,
                "idempotency_key": identity,
                "scheduled": scheduled_at is not None,
                "scheduled_date": (
                    scheduled_at.isoformat() if scheduled_at is not None else None
                ),
                "timezone": tz_name,
                "version": version["version"],
                "variant_id": version["variant_id"],
                "render_fingerprint": version["render_fingerprint"],
                "approval_revision": version["revision"],
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
                **_rerelease_receipt_fields(rerelease),
            }
            if destination_bindings:
                intent["destination_bindings"] = destination_bindings
            intent["destination_request_revision"] = _document_revision(
                _destination_request_fields(intent)
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
            targets.append(intent)
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

        occupied = [
            item
            for item in self._occupied_schedule(data["api_key"], data["user"])
            if item.get("external_id") not in identities | co_schedule_ids
        ]
        reservations = list(occupied)
        for target in targets:
            if target["scheduled"]:
                self._reserve(
                    reservations,
                    _parse_time(target["scheduled_date"]),
                    target["external_id"],
                    target["clip_id"],
                    tz_name,
                    weekday,
                    weekend,
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
            "preview_revision": preview_revision,
        }

    def preview_short_destinations(self, value):
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
        return {
            **plan,
            "execute": {
                "destinations": plan["requested_destinations"],
                "clip_ids": plan["selected_clip_ids"],
                "request_id": plan["request_id"],
                "actor": plan["actor"],
                "reason": plan["reason"],
                "expected_release_revision": plan["release_revision"],
                "variant_overrides": plan["variant_overrides"],
                "copy_overrides": plan["copy_overrides"],
                "publish_now": plan["publish_now"],
                "preview_revision": plan["preview_revision"],
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
        data = self._inputs(bind_legacy=True)
        episode, snapshot, gate = (data[key] for key in ("episode", "snapshot", "gate"))
        api_key, user = (data[key] for key in ("api_key", "user"))
        approved, metadata, platforms = (
            data[key] for key in ("approved", "metadata", "platforms")
        )
        previous, previous_shorts = (
            data[key] for key in ("previous", "previous_shorts")
        )
        funnel_urls = data["funnel_urls"]
        longform_revision = data["longform_revision"]
        short_metadata, short_versions = (
            data[key] for key in ("short_metadata", "short_versions")
        )
        revision = gate["revision"]
        youtube_url = funnel_urls["youtube"]
        destination_request = getattr(self, "short_destination_request", None)
        locked_overrides = (
            self._validated_variant_overrides(data, destination_request)
            if isinstance(destination_request, dict)
            else {}
        )
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
            if destination_request is not None:
                plan = self._destination_plan(data, destination_request)
                destination_versions = self._destination_versions(
                    data, plan["variant_overrides"]
                )
                previous_shorts = self._persist_destination_intents(
                    previous, plan["targets"]
                )
                selected = set(plan["selected_clip_ids"])
                shorts = self._publish_shorts(
                    [clip for clip in approved if str(clip.get("id", "")) in selected],
                    destination_versions,
                    short_metadata,
                    [
                        {
                            "clip_id": target["clip_id"],
                            "scheduled_date": target["scheduled_date"],
                        }
                        for target in plan["targets"]
                        if target["scheduled"]
                    ],
                    previous_shorts,
                    episode,
                    plan["requested_destinations"],
                    youtube_url,
                    funnel_urls["spotify"],
                    revision,
                    api_key,
                    user,
                    destination_targets={
                        target["clip_id"]: target for target in plan["targets"]
                    },
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
            if any(
                receipt.get("destination_request_id") for receipt in previous_shorts
            ) or any(
                isinstance(version.get("re_release_request"), dict)
                and version["re_release_request"].get(
                    "unresolved_history_acknowledgement"
                )
                for version in short_versions.values()
            ):
                raise RuntimeError(
                    "An existing destination request requires a reviewed explicit "
                    "destination subset"
                )
            longform = self._publish_longform(
                episode,
                metadata.get("longform", {}),
                previous.get("longform"),
                revision,
                longform_revision,
                snapshot["quality"]["current_revision"],
                youtube_url,
                platforms,
                api_key,
                user,
            )
            if "youtube" in platforms and not youtube_url:
                state = (
                    longform.get("status") if isinstance(longform, dict) else "failed"
                )
                if state in {
                    "submitted",
                    "unknown",
                    "published",
                    "already_submitted",
                }:
                    publish_status = "waiting_for_longform_publication"
                    reason = (
                        "The YouTube longform submission is awaiting a public URL."
                        if state != "unknown"
                        else "The YouTube longform submission outcome is unknown."
                    )
                    next_action = (
                        f"Poll POST /api/episodes/{self.episode_dir.name}/"
                        "check-upload-urls, then rerun publish after it records the "
                        "current public URL."
                    )
                else:
                    publish_status = "longform_failed"
                    reason = "The YouTube longform submission failed."
                    next_action = (
                        "Resolve the reported longform error and rerun the current "
                        "approved publish job."
                    )
                result = self._result(
                    [],
                    longform,
                    platforms,
                    revision,
                    user,
                    reason,
                    previous_shorts,
                )
                result.update(
                    publish_status=publish_status,
                    action_required=True,
                    next_action=next_action,
                )
                return result
            shorts = self._publish_shorts(
                approved,
                short_versions,
                short_metadata,
                metadata.get("schedule", []),
                previous_shorts,
                episode,
                platforms,
                youtube_url,
                funnel_urls["spotify"],
                revision,
                api_key,
                user,
            )
        return self._result(
            shorts,
            longform,
            platforms,
            revision,
            user,
            previous_shorts=previous_shorts,
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
        if (
            isinstance(previous, dict)
            and previous.get("external_id") in {identity, legacy_identity}
            and previous.get("status") in RECORDED_STATES
            and not retry_rejection
        ):
            return {
                **previous,
                "editorial_revision": longform_revision,
                "reused_receipt": True,
            }

        transport = self._verified_longform_transport(
            revision,
            longform_revision,
            quality_revision,
        )
        if retry_rejection and transport is None:
            return {
                **previous,
                "editorial_revision": longform_revision,
                "reused_receipt": True,
            }
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
    ) -> dict | None:
        """Use a current immutable R2 object instead of a multi-GB request body."""
        path = self.episode_dir / "video_feed.json"
        if not path.exists():
            return None
        try:
            from agents.video_feed import VIDEO_FEED_SCHEMA, VideoFeedAgent

            receipt = json.loads(path.read_text())
            video = receipt["video"]
            remote = video["remote"]
            agent = VideoFeedAgent(self.episode_dir, self.config)
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

    def _bind_legacy_funnel_urls(
        self, episode: dict, funnel_urls: dict, longform_revision: str
    ) -> None:
        changed = False
        if (
            funnel_urls["youtube"]
            and episode.get("youtube_longform_url_source")
            in {"supplied", "upload_post_receipt"}
            and episode.get("youtube_longform_url_editorial_revision") is None
        ):
            episode["youtube_longform_url_editorial_revision"] = longform_revision
            changed = True
        if (
            funnel_urls["spotify"]
            and episode.get("spotify_longform_url_editorial_revision") is None
        ):
            episode["spotify_longform_url_source"] = "supplied"
            episode["spotify_longform_url_editorial_revision"] = longform_revision
            changed = True
        if changed:
            self.save_json("episode.json", episode)

    def _publish_shorts(
        self,
        clips,
        short_versions,
        metadata,
        schedule,
        previous,
        episode,
        platforms,
        youtube_url,
        spotify_url,
        revision,
        api_key,
        user,
        *,
        destination_targets=None,
    ):
        previous = previous if isinstance(previous, list) else []
        destination_targets = destination_targets or {}
        results = []
        pending = []
        for clip in clips:
            clip_id = str(clip.get("id", ""))
            version = short_versions[clip_id]
            destination_target = destination_targets.get(clip_id)
            identity = (
                destination_target["external_id"]
                if destination_target
                else self._identity(revision, "short", clip_id)
            )
            recorded_for_clip = [
                item
                for item in previous
                if isinstance(item, dict) and str(item.get("clip_id", "")) == clip_id
            ]
            receipt = next(
                (
                    item
                    for item in recorded_for_clip
                    if item.get("external_id") == identity
                    and item.get("status") != "cancelled"
                ),
                None,
            )
            if (
                receipt is None
                and recorded_for_clip
                and not destination_target
                and not valid_rerelease_authorization(
                    {"shorts": previous}, clip, version
                )
            ):
                raise RuntimeError(
                    f"A historical publication receipt exists for {clip_id}; "
                    "prepare an explicit re-release identity before submitting "
                    "it again"
                )
            if receipt and not self._receipt_matches_version(receipt, version):
                raise RuntimeError(
                    f"The recorded receipt for {clip_id} does not match its "
                    "selected version; nothing was submitted"
                )
            if receipt and receipt.get("status") not in {
                "unknown",
                "intent_recorded",
            }:
                results.append({**receipt, "reused_receipt": True})
            else:
                pending.append((clip, version, identity, receipt, destination_target))
        if not pending:
            return results

        tz_name = self.get_config("schedule", "timezone", default="America/Los_Angeles")
        weekday_limit = int(
            self.get_config("schedule", "shorts_per_day_weekday", default=1)
        )
        weekend_limit = int(
            self.get_config("schedule", "shorts_per_day_weekend", default=2)
        )
        if not 1 <= weekday_limit <= 3 or not 1 <= weekend_limit <= 3:
            raise RuntimeError("Shorts-per-day limits must be between 1 and 3")
        reference = self._schedule_reference(episode, tz_name)

        with self._schedule_lock():
            occupied = self._occupied_schedule(api_key, user)
            current_identities = {
                item["external_id"] for item in destination_targets.values()
            }
            co_schedule_ids = {
                receipt.get("external_id")
                for target in destination_targets.values()
                for receipt in previous
                if receipt.get("clip_id") == target["clip_id"]
                and self._receipt_matches_artifact(receipt, target)
                and not set(receipt.get("platforms", [])) & set(platforms)
            }
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
            for clip, version, identity, uncertain, destination_target in pending:
                matches = [
                    item for item in occupied if item.get("external_id") == identity
                ]
                existing = next(
                    (item for item in matches if item["source"] == "upload-post"),
                    matches[0] if matches else None,
                )
                if existing:
                    results.append(
                        self._scheduled_receipt(
                            clip,
                            version,
                            identity,
                            existing,
                            platforms,
                            tz_name,
                            destination_target=destination_target,
                        )
                    )
                elif uncertain and uncertain.get("status") != "intent_recorded":
                    results.append({**uncertain, "reused_receipt": True})
                else:
                    remaining.append((clip, version, identity, destination_target))

            scheduled_remaining = [
                item
                for item in remaining
                if not item[3] or item[3].get("scheduled") is not False
            ]
            if not schedule and scheduled_remaining:
                schedule = self._generate_schedule(
                    [clip for clip, _, _, _ in scheduled_remaining],
                    weekday_limit,
                    weekend_limit,
                    tz_name=tz_name,
                    reference=reference,
                    occupied=occupied,
                )
            schedule_by_clip = {}
            for entry in schedule if isinstance(schedule, list) else []:
                clip_id = entry.get("clip_id") if isinstance(entry, dict) else None
                if clip_id is not None:
                    clip_id = str(clip_id)
                    if clip_id in schedule_by_clip:
                        raise RuntimeError(
                            f"Schedule has duplicate entries for {clip_id}; "
                            "no shorts were submitted"
                        )
                    schedule_by_clip[clip_id] = entry
            unscheduled = [
                str(clip.get("id", ""))
                for clip, _, _, destination_target in remaining
                if not destination_target
                or destination_target.get("scheduled") is not False
                if str(clip.get("id", "")) not in schedule_by_clip
            ]
            if unscheduled:
                raise RuntimeError(
                    "Approved clips are missing schedule entries: "
                    + ", ".join(unscheduled)
                )

            plans = []
            reservations = list(occupied)
            for clip, version, identity, destination_target in remaining:
                clip_id = str(clip.get("id", ""))
                entry = schedule_by_clip.get(clip_id)
                immediate = bool(
                    destination_target and destination_target.get("scheduled") is False
                )
                scheduled_at = (
                    None
                    if immediate
                    else self._schedule_to_datetime(entry, tz_name, reference=reference)
                    if entry
                    else None
                )
                if scheduled_at:
                    if _instant(scheduled_at) <= _instant(reference):
                        raise RuntimeError(
                            f"Schedule for {clip_id} is not in the future; "
                            "no shorts were submitted"
                        )
                    if _instant(scheduled_at) > _instant(
                        reference + timedelta(days=365)
                    ):
                        raise RuntimeError(
                            f"Schedule for {clip_id} is more than 365 days away; "
                            "no shorts were submitted"
                        )
                    self._reserve(
                        reservations,
                        scheduled_at,
                        identity,
                        clip_id,
                        tz_name,
                        weekday_limit,
                        weekend_limit,
                    )
                plans.append(
                    (clip, version, identity, scheduled_at, destination_target)
                )

            for index, (
                clip,
                version,
                identity,
                scheduled_at,
                destination_target,
            ) in enumerate(plans, 1):
                clip_id = str(clip.get("id", ""))
                self.report_progress(index, len(plans), f"Uploading {clip_id}")
                result = self._submit_short(
                    clip,
                    metadata.get(clip_id, {}),
                    platforms,
                    scheduled_at,
                    youtube_url,
                    spotify_url,
                    version,
                    identity,
                    api_key,
                    user,
                    destination_target=destination_target,
                )
                if result:
                    results.append(result)
        return results

    @staticmethod
    def _receipt_matches_artifact(receipt: dict, version: dict) -> bool:
        """Match stable pixels and re-release lineage across fresh copy approval."""
        request = version.get("re_release_request")
        rerelease_id = (
            request.get("request_id")
            if isinstance(request, dict)
            else version.get("rerelease_request_id")
        )
        rerelease_revision = (
            request.get("revision")
            if isinstance(request, dict)
            else version.get("rerelease_authorization_revision")
        )
        return bool(
            receipt.get("version") == version.get("version")
            and receipt.get("variant_id") == version.get("variant_id")
            and receipt.get("render_fingerprint") == version.get("render_fingerprint")
            and receipt.get("rerelease_request_id") == rerelease_id
            and receipt.get("rerelease_authorization_revision") == rerelease_revision
        )

    @staticmethod
    def _receipt_matches_version(receipt: dict, version: dict) -> bool:
        identity_fields = {
            "version",
            "variant_id",
            "render_fingerprint",
            "approval_revision",
        }
        present = identity_fields.intersection(receipt)
        if not present:
            return (
                version.get("version") == "base" and version.get("variant_id") is None
            )
        if present != identity_fields:
            return False
        expected = {
            "version": version.get("version"),
            "variant_id": version.get("variant_id"),
            "render_fingerprint": version.get("render_fingerprint"),
            "approval_revision": version.get("revision"),
        }
        if not all(receipt.get(key) == value for key, value in expected.items()):
            return False
        request = version.get("re_release_request")
        if isinstance(request, dict):
            return receipt.get("rerelease_request_id") == request.get("request_id")
        return receipt.get("rerelease_request_id") is None

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
        clip,
        metadata,
        platforms,
        scheduled_at,
        youtube_url,
        spotify_url,
        version,
        identity,
        api_key,
        user,
        *,
        destination_target=None,
    ):
        clip_id = str(clip.get("id", ""))
        path = self.episode_dir / version["path"]
        if not path.exists():
            self.logger.warning("Short not found: %s", clip_id)
            return None
        title = clip.get("title", f"Clip {clip_id}")
        copy = (
            destination_target["destination_copy"]
            if destination_target
            else short_destination_copy(
                metadata,
                platforms,
                title=title,
                hub_url=episode_hub_url(self.config, self.episode_dir.name),
                youtube_url=youtube_url,
                spotify_url=spotify_url,
                channel_handle=self.config.get("podcast", {}).get("channel_handle", ""),
            )
        )
        upload_title = (
            copy["instagram"]["text"] if platforms == ["instagram"] else title
        )
        command = self._base_command(
            path, upload_title, platforms, identity, api_key, user, 600
        )
        for platform in platforms:
            for field, value in upload_fields(platform, copy.get(platform, {})).items():
                command += ["--form-string", f"{field}={value}"]
            binding = (
                (destination_target or {})
                .get("destination_bindings", {})
                .get(platform, {})
            )
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

        result = self._submit(command, 600, identity)
        if destination_target:
            result = {**destination_target, **result}
        result.update(
            clip_id=clip_id,
            platforms=platforms,
            scheduled=scheduled_at is not None,
            version=version["version"],
            variant_id=version["variant_id"],
            render_fingerprint=version["render_fingerprint"],
            approval_revision=version["revision"],
            destination_copy=copy,
            copy_revision=_document_revision(copy),
        )
        request = version.get("re_release_request")
        result.update(_rerelease_receipt_fields(request))
        if scheduled_at:
            result.update(
                scheduled_date=scheduled_at.isoformat(),
                timezone=tz_name,
            )
        return result

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
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
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
        if process.returncode:
            raise RuntimeError(
                "Upload-Post rejected the destination account check; "
                "no shorts were submitted"
            )
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(
                "Upload-Post destination account response was not JSON; "
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

        for platform in ("facebook", "linkedin"):
            binding = bindings.get(platform)
            if not binding:
                continue
            discovery = self._provider_json(
                api_key,
                FACEBOOK_PAGES_URL if platform == "facebook" else LINKEDIN_PAGES_URL,
                {"profile": profile_username},
            )
            pages = self._provider_target_rows(discovery, "pages", platform)
            pinned = self._provider_json(
                api_key,
                (
                    FACEBOOK_PAGE_PIN_URL
                    if platform == "facebook"
                    else LINKEDIN_PAGE_PIN_URL
                ),
                {"profile_username": profile_username},
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
            matches = [page for page in pages if page["id"] == binding["target_id"]]
            pinned_matches = [
                page for page in pinned_pages if page["id"] == binding["target_id"]
            ]
            if (
                len(matches) != 1
                or len(pinned_matches) != 1
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
            matches = [
                board for board in boards if board["id"] == pinterest["target_id"]
            ]
            if (
                len(matches) != 1
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
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                **base,
                "status": "unknown",
                "error": f"Upload timed out ({timeout}s)",
            }
        except OSError as error:
            return {**base, "status": "failed", "error": str(error)}
        if process.returncode:
            return {
                **base,
                "status": "unknown",
                "error": f"curl error: {process.stderr[:500]}",
                "stdout": process.stdout,
            }
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError:
            payload_rejected = "413 request entity too large" in process.stdout.lower()
            return {
                **base,
                "status": "failed" if payload_rejected else "unknown",
                "error": (
                    "Upload-Post rejected the request with HTTP 413"
                    if payload_rejected
                    else f"non-JSON response from Upload-Post: {process.stdout[:200]}"
                ),
                **({"http_status": 413} if payload_rejected else {}),
            }

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
            return {
                **receipt,
                "status": "failed",
                "error": response.get("error") or response.get("message"),
            }
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
                return {
                    **receipt,
                    "status": "partial_failure",
                    "error": (
                        "Upload-Post reported platform failures: "
                        + ", ".join(sorted(failures))
                    ),
                    "platform_failures": failures,
                }
            return {**receipt, "status": "published"}
        request_id = response.get("request_id", response.get("job_id"))
        if not request_id:
            return {
                **receipt,
                "status": "unknown",
                "error": response.get("error") or response.get("message"),
            }
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
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            if process.returncode:
                raise ValueError(process.stderr.strip() or process.stdout.strip())
            remote = json.loads(process.stdout).get("scheduled_posts")
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
                if not isinstance(item, dict):
                    raise TypeError(
                        f"Cannot inspect local publish receipt {path}; "
                        "no shorts were submitted"
                    )
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
        clip,
        version,
        identity,
        existing,
        platforms,
        tz_name,
        *,
        destination_target=None,
    ):
        scheduled_at = existing["scheduled_at"].astimezone(ZoneInfo(tz_name))
        if destination_target:
            candidate = {
                **destination_target,
                "job_id": existing.get("job_id"),
            }
            if existing.get("source") != "upload-post" or not _matching_schedule_row(
                candidate,
                existing.get("provider_record"),
                destination_target["destination_profile_username"],
            ):
                raise RuntimeError("Existing provider schedule conflicts with preview")
        receipt = {
            **(destination_target or {}),
            "clip_id": str(clip.get("id", "")),
            "status": existing.get("status", "submitted"),
            "platforms": platforms,
            "request_id": identity,
            "external_id": identity,
            "idempotency_key": identity,
            "scheduled": True,
            "scheduled_date": scheduled_at.isoformat(),
            "timezone": tz_name,
            "version": version["version"],
            "variant_id": version["variant_id"],
            "render_fingerprint": version["render_fingerprint"],
            "approval_revision": version["revision"],
            "reused_receipt": True,
            "receipt_source": existing["source"],
        }
        request = version.get("re_release_request")
        receipt.update(_rerelease_receipt_fields(request))
        if existing.get("job_id"):
            receipt["job_id"] = existing["job_id"]
        return receipt

    @staticmethod
    def _reserve(
        reservations,
        scheduled_at,
        identity,
        clip_id,
        tz_name,
        weekday_limit,
        weekend_limit,
    ):
        zone = ZoneInfo(tz_name)
        local = scheduled_at.astimezone(zone)
        same_day = [
            item
            for item in reservations
            if item["scheduled_at"].astimezone(zone).date() == local.date()
        ]
        limit = weekend_limit if local.weekday() >= 4 else weekday_limit
        exact = any(
            _instant(item["scheduled_at"]) == _instant(scheduled_at)
            for item in same_day
        )
        if exact or _schedule_capacity_count(same_day) >= limit:
            raise ShortDestinationConflict(
                f"Schedule collision for {clip_id} at {local.isoformat()}; "
                "no shorts were submitted"
            )
        reservations.append(
            {
                "scheduled_at": scheduled_at,
                "external_id": identity,
                "source": "current-release",
            }
        )

    def _generate_schedule(
        self,
        clips,
        weekday_per_day,
        weekend_per_day,
        *,
        tz_name="America/Los_Angeles",
        reference=None,
        occupied=(),
    ):
        zone = ZoneInfo(tz_name)
        reference = (reference or datetime.now(zone)).astimezone(zone)
        reservations = list(occupied)
        schedule = []
        clip_index = 0
        day_offset = 1
        while clip_index < len(clips):
            date = reference.date() + timedelta(days=day_offset)
            limit = weekend_per_day if date.weekday() >= 4 else weekday_per_day
            same_day = [
                item
                for item in reservations
                if item["scheduled_at"].astimezone(zone).date() == date
            ]
            slots = (
                ["morning"]
                if limit == 1
                else ["morning", "evening"]
                if limit == 2
                else ["morning", "afternoon", "evening"]
            )
            remaining = max(0, limit - _schedule_capacity_count(same_day))
            for slot in slots:
                candidate = datetime.combine(
                    date, time(hour=SLOT_HOURS[slot]), tzinfo=zone
                )
                if not remaining or clip_index >= len(clips):
                    break
                if any(
                    _instant(item["scheduled_at"]) == _instant(candidate)
                    for item in same_day
                ):
                    continue
                schedule.append(
                    {
                        "clip_id": clips[clip_index].get("id"),
                        "platform": "all",
                        "day_offset": day_offset,
                        "time_slot": slot,
                    }
                )
                record = {"scheduled_at": candidate, "source": "generated"}
                reservations.append(record)
                same_day.append(record)
                clip_index += 1
                remaining -= 1
            day_offset += 1
        return schedule

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
    def _receipt_key(receipt):
        return receipt_key(receipt)

    @classmethod
    def _merge_short_receipts(cls, current, previous):
        current = [item for item in current if isinstance(item, dict)]
        previous = previous if isinstance(previous, list) else []
        current_keys = {
            key for item in current if (key := cls._receipt_key(item)) is not None
        }
        history = []
        for item in previous:
            if not isinstance(item, dict):
                continue
            key = cls._receipt_key(item)
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
        deferred_reason=None,
        previous_shorts=None,
        deferred_destinations=None,
    ):
        active_shorts = shorts
        result = {
            "shorts": cls._merge_short_receipts(shorts, previous_shorts),
            "longform": longform,
            "shorts_submitted": sum(
                item.get("status") == "submitted" and not item.get("reused_receipt")
                for item in active_shorts
            ),
            "shorts_reused": sum(
                bool(item.get("reused_receipt")) for item in active_shorts
            ),
            "shorts_published": sum(
                item.get("status") == "published" for item in active_shorts
            ),
            "shorts_failed": sum(
                item.get("status") in {"failed", "partial_failure"}
                for item in active_shorts
            ),
            "shorts_unknown": sum(
                item.get("status") == "unknown" for item in active_shorts
            ),
            "platforms": platforms,
            "release_revision": revision,
            "profile_username": user,
        }
        if deferred_destinations is not None:
            result["deferred_destinations"] = deferred_destinations
        if deferred_reason:
            result["shorts_deferred"] = True
            result["shorts_deferred_reason"] = deferred_reason
        elif result["shorts_failed"]:
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
        elif active_shorts and all(
            item.get("reused_receipt") for item in active_shorts
        ):
            result["publish_status"] = "already_submitted"
        elif active_shorts and result["shorts_published"] == len(active_shorts):
            result["publish_status"] = "published"
        else:
            result["publish_status"] = "submitted"
        return result
