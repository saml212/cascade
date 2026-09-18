#!/usr/bin/env python3
"""Safely change dates on exact existing Upload-Post scheduled jobs.

The command is read-only unless ``--apply`` is supplied.  It never uploads,
cancels, or changes post copy.  A successful apply writes an append-only run
ledger that binds each old schedule row, PATCH response, and verified new row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.publish import (
    complete_schedule_reschedule,
    publication_lock,
    start_schedule_reschedule,
    validated_schedule_reschedule,
    validated_short_receipts,
)
from lib.atomic_write import atomic_write_json

PLAN_SCHEMA = "cascade.upload-post-reschedule-plan/v1"
RESULT_SCHEMA = "cascade.upload-post-reschedule-result/v1"
JOURNAL_SCHEMA = "cascade.upload-post-reschedule-event/v1"
SCHEDULE_URL = "https://api.upload-post.com/api/uploadposts/schedule"


class RescheduleError(RuntimeError):
    """The requested provider date change is unsafe or unverifiable."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _revision(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise RescheduleError(f"invalid ISO-8601 date: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _same_instant(left: str, right: str) -> bool:
    return _utc(left) == _utc(right)


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RescheduleError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise RescheduleError(f"expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as output:
        output.write(json.dumps(value, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_identity(job: dict) -> dict:
    """Fields a date-only PATCH must leave unchanged."""
    return {
        key: value
        for key, value in job.items()
        if key not in {"scheduled_date", "original_scheduled_str"}
    }


def _validate_plan(value: dict) -> dict:
    expected_keys = {
        "schema",
        "profile_username",
        "timezone",
        "expected_provider_jobs",
        "actor",
        "reason",
        "calendar_policy",
        "changes",
    }
    if set(value) != expected_keys or value.get("schema") != PLAN_SCHEMA:
        raise RescheduleError("plan schema or top-level fields do not match")
    profile = value.get("profile_username")
    timezone_name = value.get("timezone")
    expected_count = value.get("expected_provider_jobs")
    changes = value.get("changes")
    policy = value.get("calendar_policy")
    if not isinstance(profile, str) or not profile:
        raise RescheduleError("profile_username is required")
    if (
        not isinstance(value.get("actor"), str)
        or not value["actor"].strip()
        or not isinstance(value.get("reason"), str)
        or len(value["reason"].strip()) < 3
    ):
        raise RescheduleError("actor and reason are required")
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise RescheduleError("timezone must be an IANA name") from error
    if type(expected_count) is not int or expected_count < 1:
        raise RescheduleError("expected_provider_jobs must be a positive integer")
    if not isinstance(changes, list) or not changes:
        raise RescheduleError("changes must be a non-empty list")
    if not isinstance(policy, dict) or set(policy) != {
        "weekday_limit",
        "weekend_limit",
        "prior_published_counts",
        "approved_daily_overrides",
        "expected_touched_day_counts",
    }:
        raise RescheduleError("calendar_policy must contain the exact policy fields")
    weekday_limit = policy.get("weekday_limit")
    weekend_limit = policy.get("weekend_limit")
    if (
        type(weekday_limit) is not int
        or weekday_limit < 1
        or type(weekend_limit) is not int
        or weekend_limit < 1
    ):
        raise RescheduleError("calendar limits must be positive integers")
    for field in (
        "prior_published_counts",
        "approved_daily_overrides",
        "expected_touched_day_counts",
    ):
        counts = policy[field]
        if not isinstance(counts, dict) or any(
            not isinstance(day, str) or not day or type(count) is not int or count < 0
            for day, count in counts.items()
        ):
            raise RescheduleError(f"calendar_policy {field} is invalid")
        try:
            if any(
                datetime.fromisoformat(day).date().isoformat() != day for day in counts
            ):
                raise ValueError
        except ValueError as error:
            raise RescheduleError(
                f"calendar_policy {field} has an invalid date"
            ) from error
    if not set(policy["prior_published_counts"]) <= set(
        policy["expected_touched_day_counts"]
    ) or not set(policy["approved_daily_overrides"]) <= set(
        policy["expected_touched_day_counts"]
    ):
        raise RescheduleError("calendar policy exceptions must be on touched dates")

    job_ids: set[str] = set()
    operation_ids: set[str] = set()
    targets: set[datetime] = set()
    touched_dates: set[str] = set()
    sequences: list[int] = []
    required_change_keys = {
        "sequence",
        "operation_id",
        "job_id",
        "episode_id",
        "clip_id",
        "role",
        "arm",
        "round",
        "from_scheduled_date",
        "to_scheduled_date",
        "external_id",
        "source_filename",
        "platforms",
        "receipt_revision",
        "variant_id",
        "render_fingerprint",
        "approval_revision",
        "copy_revision",
        "destination_request_revision",
    }
    for change in changes:
        if not isinstance(change, dict) or set(change) != required_change_keys:
            raise RescheduleError("each change must contain the exact plan fields")
        sequence = change.get("sequence")
        operation_id = change.get("operation_id")
        job_id = change.get("job_id")
        if type(sequence) is not int or sequence < 1:
            raise RescheduleError("each sequence must be a positive integer")
        if not isinstance(job_id, str) or not job_id or job_id in job_ids:
            raise RescheduleError("job IDs must be non-empty and unique")
        sequences.append(sequence)
        job_ids.add(job_id)
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or operation_id in operation_ids
        ):
            raise RescheduleError("operation IDs must be non-empty and unique")
        operation_ids.add(operation_id)
        required_text = (
            "episode_id",
            "clip_id",
            "role",
            "external_id",
            "source_filename",
            "receipt_revision",
            "variant_id",
            "render_fingerprint",
            "approval_revision",
            "copy_revision",
            "destination_request_revision",
        )
        if not all(
            isinstance(change.get(field), str) and change[field]
            for field in required_text
        ):
            raise RescheduleError(f"{job_id} has incomplete frozen identity")
        if change["source_filename"] != f"{change['clip_id']}.mp4":
            raise RescheduleError(f"{job_id} source filename disagrees with clip")
        if change["role"] not in {"pilot", "displaced_ordinary"}:
            raise RescheduleError(f"{job_id} has an invalid operation role")
        if change["role"] == "pilot":
            if change.get("arm") not in {"A", "B", "C"}:
                raise RescheduleError(f"{job_id} pilot arm is invalid")
        elif change.get("arm") is not None:
            raise RescheduleError(f"{job_id} ordinary move cannot name a pilot arm")
        if change.get("round") not in {1, 2, 3}:
            raise RescheduleError(f"{job_id} experiment round is invalid")
        platforms = change.get("platforms")
        if (
            not isinstance(platforms, list)
            or not platforms
            or len(platforms) != len(set(platforms))
            or not all(isinstance(item, str) and item for item in platforms)
        ):
            raise RescheduleError(f"{job_id} has invalid platforms")
        source_local = datetime.fromisoformat(change["from_scheduled_date"])
        target_local = datetime.fromisoformat(change["to_scheduled_date"])
        source = _utc(change["from_scheduled_date"])
        target = _utc(change["to_scheduled_date"])
        if (
            source_local.tzinfo is None
            or source_local.utcoffset() != source_local.astimezone(zone).utcoffset()
            or target_local.tzinfo is None
            or target_local.utcoffset() != target_local.astimezone(zone).utcoffset()
        ):
            raise RescheduleError(
                f"{job_id} target offset disagrees with {timezone_name}"
            )
        if source == target or target in targets:
            raise RescheduleError(
                "source and target dates must differ and targets be unique"
            )
        targets.add(target)
        touched_dates.update(
            {source_local.date().isoformat(), target_local.date().isoformat()}
        )
    if sorted(sequences) != list(range(1, len(changes) + 1)):
        raise RescheduleError("change sequences must be contiguous starting at 1")
    if touched_dates != set(policy["expected_touched_day_counts"]):
        raise RescheduleError("expected touched calendar dates do not match the moves")
    return value


@dataclass
class ProviderClient:
    api_key: str
    profile_username: str
    timeout: float = 30.0
    min_interval: float = 1.0
    _last_request_at: float | None = None

    def _request(
        self, request: urllib.request.Request, *, include_status: bool = False
    ) -> dict:
        if self._last_request_at is not None:
            remaining = self.min_interval - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = response.status
                self._last_request_at = time.monotonic()
        except urllib.error.HTTPError as error:
            body = error.read()
            self._last_request_at = time.monotonic()
            raise RescheduleError(
                f"Upload-Post returned HTTP {error.code}: {body[:500]!r}"
            ) from error
        except (OSError, urllib.error.URLError) as error:
            self._last_request_at = time.monotonic()
            raise RescheduleError(
                f"Upload-Post outcome is uncertain: {error}"
            ) from error
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            raise RescheduleError(
                f"Upload-Post returned non-JSON HTTP {status}: {body[:500]!r}"
            ) from error
        if not isinstance(value, dict):
            raise RescheduleError("Upload-Post returned a non-object response")
        return {"http_status": status, "response": value} if include_status else value

    def schedule(self) -> dict:
        query = urllib.parse.urlencode({"profile_username": self.profile_username})
        request = urllib.request.Request(
            f"{SCHEDULE_URL}?{query}",
            headers={"Authorization": f"Apikey {self.api_key}"},
        )
        value = self._request(request)
        jobs = value.get("scheduled_posts")
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise RescheduleError("Upload-Post schedule response is malformed")
        if value.get("total") != len(jobs):
            raise RescheduleError("Upload-Post schedule response is incomplete")
        return value

    def patch_date(
        self,
        job_id: str,
        request_payload: dict,
        *,
        attempted_at: str,
    ) -> dict:
        request = urllib.request.Request(
            f"{SCHEDULE_URL}/{urllib.parse.quote(job_id, safe='')}",
            data=_canonical(request_payload),
            method="PATCH",
            headers={
                "Authorization": f"Apikey {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        result = self._request(request, include_status=True)
        return {
            "outcome": "response",
            "attempted_at": attempted_at,
            "http_status": result["http_status"],
            "response": result["response"],
        }


def _jobs_by_id(schedule: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for job in schedule["scheduled_posts"]:
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id or job_id in result:
            raise RescheduleError(
                "provider schedule contains a missing/duplicate job ID"
            )
        result[job_id] = job
    return result


def _assert_identity(change: dict, job: dict, profile: str) -> None:
    expected = {
        "job_id": change["job_id"],
        "profile_username": profile,
        "external_id": change["external_id"],
        "source_filename": change["source_filename"],
    }
    for field, value in expected.items():
        if job.get(field) != value:
            raise RescheduleError(
                f"{change['job_id']} provider {field} does not match the plan"
            )
    platforms = job.get("platforms")
    if not isinstance(platforms, list) or set(platforms) != set(change["platforms"]):
        raise RescheduleError(f"{change['job_id']} provider platforms do not match")
    if job.get("has_preview") is not True:
        raise RescheduleError(f"{change['job_id']} has no provider media preview")


def _load_publish(episodes_dir: Path, episode_id: str) -> tuple[Path, dict, list[dict]]:
    path = episodes_dir / episode_id / "publish.json"
    publish = _read_object(path)
    try:
        receipts = validated_short_receipts(publish)
    except ValueError as error:
        raise RescheduleError(f"cannot validate {path}: {error}") from error
    return path, publish, receipts


def _receipt_for_change(
    episodes_dir: Path, change: dict
) -> tuple[Path, dict, list[dict], int, dict]:
    path, publish, receipts = _load_publish(episodes_dir, change["episode_id"])
    matches = [
        (index, receipt)
        for index, receipt in enumerate(receipts)
        if receipt.get("clip_id") == change["clip_id"]
        and receipt.get("job_id") == change["job_id"]
        and receipt.get("external_id") == change["external_id"]
    ]
    if len(matches) != 1:
        raise RescheduleError(
            f"cannot resolve one local receipt for {change['job_id']}"
        )
    index, receipt = matches[0]
    original = receipt.get("pre_reschedule_receipt", receipt)
    expected = {
        "variant_id": change["variant_id"],
        "render_fingerprint": change["render_fingerprint"],
        "approval_revision": change["approval_revision"],
        "copy_revision": change["copy_revision"],
        "destination_request_revision": change["destination_request_revision"],
    }
    if _revision(original) != change["receipt_revision"] or any(
        original.get(field) != value for field, value in expected.items()
    ):
        raise RescheduleError(f"local receipt identity changed for {change['job_id']}")
    return path, publish, receipts, index, receipt


def preflight(plan: dict, schedule: dict, episodes_dir: Path) -> dict:
    if schedule.get("total") != plan["expected_provider_jobs"]:
        raise RescheduleError(
            "provider job count changed: "
            f"expected {plan['expected_provider_jobs']}, got {schedule.get('total')}"
        )
    jobs = _jobs_by_id(schedule)
    states = []
    simulated_dates = {
        job_id: _utc(job["scheduled_date"]) for job_id, job in jobs.items()
    }
    for change in sorted(plan["changes"], key=lambda item: item["sequence"]):
        _path, _publish, _receipts, _index, receipt = _receipt_for_change(
            episodes_dir, change
        )
        job = jobs.get(change["job_id"])
        if job is None:
            raise RescheduleError(f"missing provider job {change['job_id']}")
        _assert_identity(change, job, plan["profile_username"])
        current = job.get("scheduled_date")
        if not isinstance(current, str):
            raise RescheduleError(f"{change['job_id']} has no scheduled_date")
        operation = validated_schedule_reschedule(receipt)
        if operation is not None and (
            operation.get("operation_id") != change["operation_id"]
            or operation.get("to_scheduled_date") != change["to_scheduled_date"]
        ):
            raise RescheduleError(f"{change['job_id']} has a different amendment")
        if _same_instant(current, change["from_scheduled_date"]):
            state = "pending"
        elif _same_instant(current, change["to_scheduled_date"]):
            state = (
                "already_applied"
                if operation is not None and operation.get("state") == "complete"
                else "provider_target_needs_reconciliation"
            )
        else:
            raise RescheduleError(
                f"{change['job_id']} is at an unexpected provider date {current}"
            )
        target = _utc(change["to_scheduled_date"])
        occupied_by = [
            job_id
            for job_id, scheduled_at in simulated_dates.items()
            if job_id != change["job_id"] and scheduled_at == target
        ]
        if occupied_by:
            raise RescheduleError(
                f"{change['job_id']} target is occupied by {occupied_by[0]} "
                "at this operation sequence"
            )
        simulated_dates[change["job_id"]] = target
        states.append(
            {
                "sequence": change["sequence"],
                "job_id": change["job_id"],
                "state": state,
                "provider_scheduled_date": current,
                "content_revision": _revision(_content_identity(job)),
                "receipt_revision": change["receipt_revision"],
            }
        )
    if len(set(simulated_dates.values())) != len(jobs):
        raise RescheduleError(
            "provider schedule simulation has an exact-slot collision"
        )
    zone = ZoneInfo(plan["timezone"])
    counts: dict[str, int] = {}
    for scheduled_at in simulated_dates.values():
        day = scheduled_at.astimezone(zone).date().isoformat()
        counts[day] = counts.get(day, 0) + 1
    policy = plan["calendar_policy"]
    for day, published in policy["prior_published_counts"].items():
        counts[day] = counts.get(day, 0) + published
    for day, count in counts.items():
        weekday = datetime.fromisoformat(day).weekday()
        ordinary_limit = (
            policy["weekend_limit"] if weekday >= 4 else policy["weekday_limit"]
        )
        limit = policy["approved_daily_overrides"].get(day, ordinary_limit)
        if count > limit:
            raise RescheduleError(
                f"final calendar count {count} exceeds approved limit {limit} on {day}"
            )
    for day, expected in policy["expected_touched_day_counts"].items():
        if counts.get(day, 0) != expected:
            raise RescheduleError(
                f"final calendar count on {day} is {counts.get(day, 0)}, "
                f"expected {expected}"
            )
    return {
        "schema": RESULT_SCHEMA,
        "mode": "preflight",
        "checked_at": _now(),
        "plan_revision": _revision(plan),
        "provider_jobs": schedule["total"],
        "verified_daily_counts": {
            day: counts.get(day, 0)
            for day in sorted(policy["expected_touched_day_counts"])
        },
        "changes": states,
    }


def apply(
    plan: dict,
    client: ProviderClient,
    episodes_dir: Path,
    output_dir: Path,
    *,
    now: Callable[[], str] = _now,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_revision = _revision(plan)
    _write_json(output_dir / "plan.json", {**plan, "revision": plan_revision})
    journal_path = output_dir / "journal.jsonl"
    events = []
    with publication_lock(episodes_dir):
        before = client.schedule()
        preview = preflight(plan, before, episodes_dir)
        _write_json(output_dir / "before-provider.json", before)
        _write_json(output_dir / "preflight.json", preview)
        before_jobs = _jobs_by_id(before)
        for change in sorted(plan["changes"], key=lambda item: item["sequence"]):
            path, publish, receipts, index, receipt = _receipt_for_change(
                episodes_dir, change
            )
            fresh = client.schedule()
            current = _jobs_by_id(fresh).get(change["job_id"])
            if current is None:
                raise RescheduleError(f"provider job disappeared: {change['job_id']}")
            _assert_identity(change, current, plan["profile_username"])
            operation = validated_schedule_reschedule(receipt)
            if operation is None:
                if not _same_instant(
                    current["scheduled_date"], change["from_scheduled_date"]
                ):
                    raise RescheduleError(
                        f"{change['job_id']} changed before durable intent"
                    )
                started_at = now()
                try:
                    receipt = start_schedule_reschedule(
                        receipt,
                        current,
                        profile_username=plan["profile_username"],
                        to_scheduled_date=change["to_scheduled_date"],
                        timezone_name=plan["timezone"],
                        operation_id=change["operation_id"],
                        actor=plan["actor"],
                        reason=plan["reason"],
                        started_at=started_at,
                    )
                except ValueError as error:
                    raise RescheduleError(str(error)) from error
                receipts[index] = receipt
                publish["shorts"] = receipts
                atomic_write_json(path, publish)
                operation = receipt["schedule_reschedule"]
                _append_jsonl(
                    journal_path,
                    {
                        "schema": JOURNAL_SCHEMA,
                        "plan_revision": plan_revision,
                        "sequence": change["sequence"],
                        "job_id": change["job_id"],
                        "event": "intent_recorded",
                        "recorded_at": now(),
                        "operation": operation,
                    },
                )
            elif (
                operation.get("operation_id") != change["operation_id"]
                or operation.get("to_scheduled_date") != change["to_scheduled_date"]
            ):
                raise RescheduleError(f"{change['job_id']} has a different amendment")

            if operation["state"] == "complete":
                if not _same_instant(
                    current["scheduled_date"], change["to_scheduled_date"]
                ):
                    raise RescheduleError(
                        f"{change['job_id']} completed amendment conflicts with provider"
                    )
                action = "already_applied"
                patch = operation["patch"]
            elif _same_instant(current["scheduled_date"], change["to_scheduled_date"]):
                action = "reconciled_after_uncertain"
                patch = {
                    "outcome": action,
                    "attempted_at": operation["started_at"],
                    "http_status": None,
                    "response": None,
                }
            elif _same_instant(
                current["scheduled_date"], change["from_scheduled_date"]
            ):
                action = "patched"
                attempted_at = now()
                try:
                    patch = client.patch_date(
                        change["job_id"],
                        operation["request"],
                        attempted_at=attempted_at,
                    )
                except RescheduleError:
                    _append_jsonl(
                        journal_path,
                        {
                            "schema": JOURNAL_SCHEMA,
                            "plan_revision": plan_revision,
                            "sequence": change["sequence"],
                            "job_id": change["job_id"],
                            "event": "patch_outcome_uncertain",
                            "attempted_at": attempted_at,
                            "recorded_at": now(),
                        },
                    )
                    raise
                _append_jsonl(
                    journal_path,
                    {
                        "schema": JOURNAL_SCHEMA,
                        "plan_revision": plan_revision,
                        "sequence": change["sequence"],
                        "job_id": change["job_id"],
                        "event": "patch_response",
                        "recorded_at": now(),
                        "patch": patch,
                    },
                )
            else:
                raise RescheduleError(
                    f"{change['job_id']} is neither at the source nor target date"
                )

            verified_schedule = client.schedule()
            verified = _jobs_by_id(verified_schedule).get(change["job_id"])
            if verified is None or not _same_instant(
                verified.get("scheduled_date", ""), change["to_scheduled_date"]
            ):
                raise RescheduleError(
                    f"{change['job_id']} target date was not verified after PATCH"
                )
            _assert_identity(change, verified, plan["profile_username"])
            if _content_identity(verified) != _content_identity(operation["before"]):
                raise RescheduleError(f"{change['job_id']} content identity changed")
            if operation["state"] != "complete":
                try:
                    receipt = complete_schedule_reschedule(
                        receipt, verified, patch=patch, completed_at=now()
                    )
                except ValueError as error:
                    raise RescheduleError(str(error)) from error
                path, publish, receipts, index, current_receipt = _receipt_for_change(
                    episodes_dir, change
                )
                if current_receipt != receipt.get("pre_reschedule_receipt") and (
                    current_receipt.get("schedule_reschedule", {}).get("state")
                    != "intent_recorded"
                ):
                    raise RescheduleError("local receipt changed during reconciliation")
                receipts[index] = receipt
                publish["shorts"] = receipts
                atomic_write_json(path, publish)
            event = {
                "schema": JOURNAL_SCHEMA,
                "plan_revision": plan_revision,
                "sequence": change["sequence"],
                "job_id": change["job_id"],
                "event": "verified",
                "action": action,
                "recorded_at": now(),
                "before": operation["before"],
                "patch": patch,
                "after": verified,
                "receipt_amendment_revision": receipt["schedule_reschedule"][
                    "revision"
                ],
            }
            _append_jsonl(journal_path, event)
            events.append(event)

        after = client.schedule()
        after_preview = preflight(plan, after, episodes_dir)
    if any(item["state"] != "already_applied" for item in after_preview["changes"]):
        raise RescheduleError("final provider state is not fully applied")
    after_jobs = _jobs_by_id(after)
    changed_ids = {item["job_id"] for item in plan["changes"]}
    if {
        job_id: job for job_id, job in before_jobs.items() if job_id not in changed_ids
    } != {
        job_id: job for job_id, job in after_jobs.items() if job_id not in changed_ids
    }:
        raise RescheduleError(
            "an unrelated provider schedule row changed during the run"
        )

    _write_json(output_dir / "after-provider.json", after)
    result = {
        "schema": RESULT_SCHEMA,
        "mode": "applied",
        "status": "complete",
        "completed_at": now(),
        "plan_revision": plan_revision,
        "provider_jobs_before": before["total"],
        "provider_jobs_after": after["total"],
        "patched_jobs": len(
            [event for event in events if event["action"] == "patched"]
        ),
        "already_applied_jobs": len(
            [event for event in events if event["action"] == "already_applied"]
        ),
        "unchanged_job_ids": len(before_jobs) - len(changed_ids),
        "date_only": True,
        "content_identity_preserved": True,
        "cancelled_jobs": 0,
        "created_jobs": 0,
        "events": events,
    }
    _write_json(output_dir / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--episodes-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--pace-seconds", type=float, default=2.0)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="PATCH exact provider dates; omission performs a read-only preflight",
    )
    args = parser.parse_args()

    try:
        plan = _validate_plan(_read_object(args.plan))
        api_key = os.getenv("UPLOAD_POST_API_KEY")
        profile = os.getenv("UPLOAD_POST_USER")
        if not api_key or not profile:
            raise RescheduleError(
                "UPLOAD_POST_API_KEY and UPLOAD_POST_USER are required"
            )
        if profile != plan["profile_username"]:
            raise RescheduleError("credential profile does not match the plan")
        if args.pace_seconds < 0:
            raise RescheduleError("--pace-seconds must be non-negative")
        client = ProviderClient(api_key, profile, min_interval=args.pace_seconds)
        episodes_dir = args.episodes_dir.resolve()
        if args.apply:
            if args.output_dir is None:
                raise RescheduleError("--output-dir is required with --apply")
            result = apply(plan, client, episodes_dir, args.output_dir.resolve())
        else:
            result = preflight(plan, client.schedule(), episodes_dir)
        print(json.dumps(result, indent=2, sort_keys=True))
    except RescheduleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
