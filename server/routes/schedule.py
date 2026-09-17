"""Read-only release proposals from current approvals and publication evidence."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tomllib
from fastapi import APIRouter, HTTPException

from agents.qa import current_funnel_urls_for_episode, quality_snapshot
from lib.paths import get_episodes_dir
from lib.short_variants import (
    DESTINATION_DISTRIBUTION_RELEASE_SCHEMA,
    normalize_destination_distribution_targets,
)
from server.routes.review import review_state

router = APIRouter(prefix="/api", tags=["schedule"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RECORDED_PUBLICATION_STATES = {
    "submitted",
    "published",
    "already_submitted",
    "failed",
    "partial_failure",
    "unknown",
    "cancelled",
}
_PENDING_SCHEDULE_CANCELLATION_STATES = {
    "delete_started",
    "outcome_uncertain",
    "delete_confirmed",
}


def _load_config() -> dict:
    for path in [
        PROJECT_ROOT / "config" / "config.toml",
        PROJECT_ROOT / "config.toml",
    ]:
        if path.exists():
            with path.open("rb") as handle:
                return tomllib.load(handle)
    return {}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _schedule_cancellation_state(receipt: dict) -> str | None:
    """Return the validated cancellation state, if one exists."""
    from agents.publish import validated_schedule_cancellation

    operation = validated_schedule_cancellation(receipt)
    if not isinstance(operation, dict):
        return None
    state = operation.get("state")
    return state if isinstance(state, str) else None


def _publication_evidence(ep_dir: Path, episode: dict, config: dict) -> list[dict]:
    """Return recorded external actions without upgrading submissions to publishes."""
    episode_id = str(episode.get("episode_id", ep_dir.name))
    name = episode.get("name") or episode.get("guest_name") or episode_id
    records = []

    podcast = _read_json(ep_dir / "podcast_feed.json", {})
    audio_url = podcast.get("audio_url")
    feed_url = podcast.get("feed_url")
    if audio_url or feed_url:
        records.append(
            {
                "episode_id": episode_id,
                "name": name,
                "content_type": "podcast_audio",
                "destination": "podcast_rss",
                "status": "published" if audio_url else "recorded",
                "url": audio_url,
                "feed_url": feed_url,
                "evidence_source": "podcast_feed.json",
            }
        )

    youtube_url = current_funnel_urls_for_episode(ep_dir, episode, config)["youtube"]
    if isinstance(youtube_url, str) and youtube_url.strip():
        records.append(
            {
                "episode_id": episode_id,
                "name": name,
                "content_type": "longform",
                "destination": "youtube",
                "status": "published",
                "url": youtube_url.strip(),
                "evidence_source": "episode.json",
            }
        )

    publish = _read_json(ep_dir / "publish.json", {})
    longform = publish.get("longform")
    if (
        isinstance(longform, dict)
        and longform.get("status") in _RECORDED_PUBLICATION_STATES
    ):
        records.append(
            {
                "episode_id": episode_id,
                "name": name,
                "content_type": "longform",
                "destination": longform.get("platform") or "unknown",
                "status": longform["status"],
                "request_id": longform.get("request_id"),
                "job_id": longform.get("job_id"),
                "error": longform.get("error"),
                "evidence_source": "publish.json",
            }
        )
    shorts = publish.get("shorts", [])
    if not isinstance(shorts, list):
        shorts = []
    for short in shorts:
        if (
            not isinstance(short, dict)
            or not short.get("clip_id")
            or short.get("status") not in _RECORDED_PUBLICATION_STATES
        ):
            continue
        cancellation_state = _schedule_cancellation_state(short)
        cancellation_pending = (
            cancellation_state in _PENDING_SCHEDULE_CANCELLATION_STATES
        )
        if short.get("status") == "cancelled" and cancellation_state != "cancelled":
            continue
        destinations = short.get("platforms")
        if not isinstance(destinations, list) or not destinations:
            destinations = ["unknown"]
        record = {
            "episode_id": episode_id,
            "name": name,
            "content_type": "short",
            "clip_id": str(short["clip_id"]),
            "version": short.get("version") or "base",
            "variant_id": short.get("variant_id"),
            "destinations": [str(value) for value in destinations],
            "status": (
                "cancellation_pending" if cancellation_pending else short["status"]
            ),
            "scheduled": (
                False if cancellation_pending else short.get("scheduled") is True
            ),
            "scheduled_date": short.get("scheduled_date"),
            "request_id": short.get("request_id"),
            "job_id": short.get("job_id"),
            "error": short.get("error"),
            "evidence_source": "publish.json",
        }
        if cancellation_pending:
            record["schedule_cancellation_state"] = cancellation_state
        records.append(record)
    return records


def _youtube_longform_recorded(records: list[dict]) -> bool:
    return any(
        record["content_type"] == "longform"
        and record.get("destination") in {"youtube", "unknown"}
        for record in records
    )


def _receipt_state(receipt: dict) -> str | None:
    if _schedule_cancellation_state(receipt) in _PENDING_SCHEDULE_CANCELLATION_STATES:
        return "cancellation_pending"
    if receipt.get("status") in {"failed", "partial_failure"}:
        return "failed"
    if receipt.get("status") == "unknown":
        return "unknown"
    if receipt.get("status") in {"submitted", "already_submitted"}:
        return "scheduled"
    return None


def _receipt_artifact_current(receipt: dict, version: object) -> bool | None:
    """Compare a complete receipt identity with its current short artifact."""
    fields = ("version", "variant_id", "render_fingerprint")
    if not isinstance(version, dict) or not all(
        field in receipt and field in version for field in fields
    ):
        return None
    request = version.get("re_release_request")
    if request is not None and (
        not isinstance(request, dict)
        or any(
            not isinstance(request.get(field), str) or not request[field]
            for field in ("request_id", "revision")
        )
    ):
        return None
    actual = tuple(receipt[field] for field in fields) + (
        receipt.get("rerelease_request_id"),
        receipt.get("rerelease_authorization_revision"),
    )
    expected = tuple(version[field] for field in fields) + (
        request.get("request_id")
        if isinstance(request, dict)
        else version.get("rerelease_request_id"),
        request.get("revision")
        if isinstance(request, dict)
        else version.get("rerelease_authorization_revision"),
    )
    for identity in (actual, expected):
        if (
            not isinstance(identity[0], str)
            or not identity[0]
            or (identity[1] is not None and not isinstance(identity[1], str))
            or not isinstance(identity[2], str)
            or not identity[2]
            or any(
                value is not None and not isinstance(value, str)
                for value in identity[3:]
            )
            or (identity[3] is None) != (identity[4] is None)
        ):
            return None
    current = version.get("current", True)
    if not isinstance(current, bool):
        return None
    return current and actual == expected


def _release_request_targets_artifact(request: dict, version: dict) -> bool:
    """Return whether one release request binds this exact variant render."""
    if request.get("schema") == DESTINATION_DISTRIBUTION_RELEASE_SCHEMA:
        targets = normalize_destination_distribution_targets(request.get("targets"))
        target = next(
            (
                item
                for item in targets or []
                if item["variant_id"] == version["variant_id"]
            ),
            None,
        )
        return bool(
            target and target["render_fingerprint"] == version["render_fingerprint"]
        )
    return (
        request.get("variant_id") == version["variant_id"]
        and request.get("render_fingerprint") == version["render_fingerprint"]
    )


def _receipt_artifact_version(
    receipt: dict, clip: dict, selected_version: object
) -> dict | None:
    """Resolve the current render for the variant named by a receipt."""
    identity = (receipt.get("version"), receipt.get("variant_id"))
    if isinstance(selected_version, dict) and identity == (
        selected_version.get("version"),
        selected_version.get("variant_id"),
    ):
        return selected_version

    version, variant_id = identity
    if (
        not isinstance(version, str)
        or not version
        or (variant_id is not None and not isinstance(variant_id, str))
    ):
        return None
    review = clip.get("review") if isinstance(clip, dict) else None
    if not isinstance(review, dict):
        return None
    if variant_id is None:
        if version != "base":
            return None
        render = review.get("render")
    else:
        if version != variant_id:
            return None
        variants = review.get("variants")
        state = variants.get(variant_id) if isinstance(variants, dict) else None
        render = state.get("render") if isinstance(state, dict) else None
    if not isinstance(render, dict):
        return None

    candidate = {
        "version": version,
        "variant_id": variant_id,
        "render_fingerprint": render.get("fingerprint"),
        "current": render.get("current") is True,
    }
    distribution = review.get("distribution")
    request = (
        distribution.get("re_release_request")
        if isinstance(distribution, dict)
        else None
    )
    if request is not None:
        if not isinstance(request, dict):
            candidate["current"] = False
        else:
            candidate["re_release_request"] = request
            if not _release_request_targets_artifact(request, candidate):
                candidate["current"] = False
    return candidate


def _release_gate(ep_dir: Path, config: dict) -> dict:
    try:
        gate = quality_snapshot(ep_dir, include_findings=False, config=config).get(
            "release_gate", {}
        )
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        gate = {}
    if isinstance(gate, dict) and "can_approve_publish" in gate:
        return gate
    return {
        "can_approve_publish": False,
        "blockers": [{"message": "Current release checks are unavailable."}],
    }


def _short_item(
    episode_id: str,
    name: str,
    clip_id: str,
    clip: dict,
    destinations: list[str],
    distribution: dict | None = None,
) -> dict:
    metadata = clip.get("metadata", {})
    youtube = metadata.get("youtube", {}) if isinstance(metadata, dict) else {}
    title = (
        (youtube.get("title") if isinstance(youtube, dict) else None)
        or clip.get("title")
        or f"Clip {clip_id}"
    )
    distribution = distribution if isinstance(distribution, dict) else {}
    variant_id = distribution.get("variant_id")
    return {
        "type": "short",
        "episode_id": episode_id,
        "clip_id": clip_id,
        "name": name,
        "title": title,
        "destinations": destinations,
        "version": distribution.get("version") or variant_id or "base",
        "variant_id": variant_id if isinstance(variant_id, str) else None,
    }


async def _get_approved_items(
    episodes_dir: Path, config: dict
) -> tuple[list[dict], list[dict], list[dict]]:
    """Collect exact plans, receipts, suggestions, and QA-held items."""
    items = []
    publication_evidence = []
    held_episodes = []
    if not episodes_dir.exists():
        return items, publication_evidence, held_episodes

    for ep_dir in sorted(episodes_dir.iterdir()):
        episode = _read_json(ep_dir / "episode.json", {})
        if not episode:
            continue
        episode_id = str(episode.get("episode_id", ep_dir.name))
        name = episode.get("name") or episode.get("guest_name") or episode_id
        evidence = _publication_evidence(ep_dir, episode, config)
        publication_evidence.extend(evidence)
        gate = _release_gate(ep_dir, config)
        publish = _read_json(ep_dir / "publish.json", {})
        publish = publish if isinstance(publish, dict) else {}
        receipts = publish.get("shorts", [])
        receipts = receipts if isinstance(receipts, list) else []
        try:
            review = await review_state(episode_id)
        except (HTTPException, OSError, TypeError, ValueError):
            review = {}

        reviewed_clips = [
            clip for clip in review.get("clips", []) if isinstance(clip, dict)
        ]
        planned = {
            str(entry["clip_id"]): entry
            for entry in episode.get("publish_schedule", []) or []
            if isinstance(entry, dict)
            and entry.get("clip_id")
            and isinstance(entry.get("scheduled_date"), str)
        }
        gate_ready = gate.get("can_approve_publish") is True
        blockers = [
            str(blocker.get("message"))
            for blocker in gate.get("blockers", [])
            if isinstance(blocker, dict) and blocker.get("message")
        ]
        enabled_destinations = [
            str(destination["key"])
            for destination in review.get("enabled_destinations", [])
            if isinstance(destination, dict) and destination.get("key")
        ]
        clips_by_id = {
            str(clip["id"]): clip for clip in reviewed_clips if clip.get("id")
        }
        prepared_ids = {
            str(clip["id"])
            for clip in reviewed_clips
            if clip.get("id")
            and isinstance(clip.get("review", {}).get("distribution"), dict)
            and isinstance(
                clip["review"]["distribution"].get("re_release_request"), dict
            )
            and clip["review"]["distribution"].get("re_release_request_consumed")
            is False
        }
        receipt_ids = {
            str(receipt["clip_id"])
            for receipt in receipts
            if isinstance(receipt, dict) and receipt.get("clip_id")
        } - prepared_ids
        short_versions = gate.get("short_versions", {})
        short_versions = short_versions if isinstance(short_versions, dict) else {}
        for receipt in receipts:
            receipt_state = (
                _receipt_state(receipt) if isinstance(receipt, dict) else None
            )
            if (
                not isinstance(receipt, dict)
                or not receipt.get("clip_id")
                or receipt.get("scheduled") is not True
                or not receipt.get("scheduled_date")
                or receipt_state is None
            ):
                continue
            clip_id = str(receipt["clip_id"])
            clip = clips_by_id.get(clip_id, {})
            plan = planned.get(clip_id)
            item = _short_item(
                episode_id,
                name,
                clip_id,
                clip,
                receipt.get("platforms") or ["unknown"],
                receipt,
            )
            item.update(
                state=receipt_state,
                scheduled_date=receipt["scheduled_date"],
                planned_date=(plan or {}).get("scheduled_date"),
                job_id=receipt.get("job_id"),
                request_id=receipt.get("request_id"),
                error=receipt.get("error"),
                artifact_current=_receipt_artifact_current(
                    receipt,
                    _receipt_artifact_version(
                        receipt, clip, short_versions.get(clip_id)
                    ),
                ),
            )
            items.append(item)

        pending = []
        longform = review.get("longform", {})
        canonical_render = longform.get("canonical_render", {})
        approval = longform.get("approval", {})
        if (
            "youtube" in enabled_destinations
            and canonical_render.get("current") is True
            and approval.get("current") is True
            and not _youtube_longform_recorded(evidence)
        ):
            pending.append(
                {
                    "type": "longform",
                    "episode_id": episode_id,
                    "name": name,
                    "title": episode.get("title") or name,
                    "destination": "youtube",
                    "state": "suggested",
                }
            )

        for clip in reviewed_clips:
            if not isinstance(clip, dict) or not clip.get("id"):
                continue
            clip_id = str(clip["id"])
            clip_review = clip.get("review", {})
            distribution = clip_review.get("distribution")
            distribution_ready = (
                distribution.get("current") is True
                and distribution.get("approval_current") is True
                if isinstance(distribution, dict)
                else clip_review.get("render", {}).get("current") is True
                and clip_review.get("approval", {}).get("current") is True
            )
            if (
                not enabled_destinations
                or clip_review.get("selection", {}).get("status") != "selected"
                or not distribution_ready
            ):
                continue
            if clip_id in receipt_ids:
                continue
            item = _short_item(
                episode_id,
                name,
                clip_id,
                clip,
                enabled_destinations,
                distribution,
            )
            plan = planned.get(clip_id)
            if plan:
                item.update(state="planned", scheduled_date=plan["scheduled_date"])
            else:
                item["state"] = "suggested"
            pending.append(item)

        if gate_ready:
            items.extend(pending)
        elif pending:
            held_episodes.append(
                {"episode_id": episode_id, "name": name, "blockers": blockers}
            )

    return items, publication_evidence, held_episodes


def _schedule_datetime(item: dict, zone: ZoneInfo) -> datetime | None:
    value = item.get("scheduled_date")
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(zone) if parsed.tzinfo else None


@router.get("/schedule")
async def get_schedule():
    """Build a read-only calendar from exact plans, receipts, and suggestions."""
    config = _load_config()
    sched_cfg = config.get("schedule", {})
    weekday_limit = sched_cfg.get("shorts_per_day_weekday", 1)
    weekend_limit = sched_cfg.get("shorts_per_day_weekend", 2)
    longform_delay = sched_cfg.get("longform_delay_days", 0)
    tz_name = sched_cfg.get("timezone", "America/Los_Angeles")

    zone = ZoneInfo(tz_name)
    items, publication_evidence, held_items = await _get_approved_items(
        get_episodes_dir(), config
    )
    exact = []
    for item in items:
        scheduled = _schedule_datetime(item, zone)
        if scheduled:
            exact.append((scheduled, item))
    longforms = [
        item
        for item in items
        if item["type"] == "longform" and item.get("state") == "suggested"
    ]
    shorts = [
        item
        for item in items
        if item["type"] == "short" and item.get("state") == "suggested"
    ]

    today = datetime.now(zone).date()
    dates = {today + timedelta(days=offset) for offset in range(7)}
    dates.update(scheduled.date() for scheduled, _item in exact)
    days = []
    day_by_date = {}
    for date in sorted(dates):
        day = {
            "date": date.isoformat(),
            "day_name": date.strftime("%A"),
            "items": [],
        }
        days.append(day)
        day_by_date[date] = day
    for scheduled, item in sorted(exact, key=lambda value: value[0]):
        if scheduled and scheduled.date() in day_by_date:
            day_by_date[scheduled.date()]["items"].append(item)

    short_idx = 0
    for offset in range(7):
        date = today + timedelta(days=offset)
        day = day_by_date[date]
        is_weekend = date.weekday() >= 4
        limit = weekend_limit if is_weekend else weekday_limit
        if longforms and offset >= longform_delay:
            longform = longforms.pop(0)
            day["items"].append({**longform, "scheduled_date": date.isoformat()})
        while (
            short_idx < len(shorts)
            and sum(item["type"] == "short" for item in day["items"]) < limit
        ):
            short = shorts[short_idx]
            day["items"].append({**short, "scheduled_date": date.isoformat()})
            short_idx += 1

    return {
        "schedule": days,
        "total_items": sum(len(day["items"]) for day in days),
        "unscheduled_shorts": len(shorts) - short_idx,
        "unscheduled_longforms": len(longforms),
        "held_items": held_items,
        "publication_evidence": publication_evidence,
        "mode": "proposal",
        "timezone": tz_name,
    }
