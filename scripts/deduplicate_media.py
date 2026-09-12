#!/usr/bin/env python3
"""Plan or apply verified copy-on-write replacement of duplicate media files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.apfs_clone import (
    CloneSafetyError,
    assert_snapshot,
    replace_with_clone,
    snapshot_path,
)

PLAN_SCHEMA = "cascade.apfs-clone-dedup-plan/v2"
RESULT_SCHEMA = "cascade.apfs-clone-dedup-result-log/v1"


def _fsync_parent(path: Path) -> None:
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def _new_output(path: Path) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o644,
        )
    except FileExistsError as error:
        raise CloneSafetyError(
            f"refusing to overwrite existing file: {path}"
        ) from error
    _fsync_parent(path)
    with os.fdopen(descriptor, "w") as output:
        yield output


def _read_json(path: Path) -> tuple[dict, str]:
    content = path.read_bytes()
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise CloneSafetyError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(payload, dict):
        raise CloneSafetyError(f"expected a JSON object in {path}")
    return payload, hashlib.sha256(content).hexdigest()


def _write_json_new(path: Path, payload: dict) -> None:
    with _new_output(path) as output:
        output.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def _append_result(output: TextIO, payload: dict) -> None:
    output.write(json.dumps(payload, sort_keys=True) + "\n")
    output.flush()
    os.fsync(output.fileno())


def _manifest_record_matches(snapshot: dict, record: dict) -> bool:
    expected = {
        "sha256": record.get("sha256"),
        "size_bytes": record.get("size"),
        "mtime_ns": record.get("mtime_ns"),
        "inode": record.get("inode"),
        "device": record.get("device"),
        "allocated_bytes": record.get("blocks", 0) * 512,
    }
    return all(snapshot.get(key) == value for key, value in expected.items())


def _validate_manifest_group(group: object) -> list[dict]:
    if not isinstance(group, list) or len(group) < 2:
        raise CloneSafetyError("each duplicate group must contain at least two files")
    if not all(isinstance(record, dict) for record in group):
        raise CloneSafetyError("duplicate group entries must be objects")
    sha256s = {record.get("sha256") for record in group}
    sizes = {record.get("size") for record in group}
    if len(sha256s) != 1 or len(sizes) != 1:
        raise CloneSafetyError("duplicate group hashes and sizes must agree")
    if not all(record.get("stable") is True for record in group):
        raise CloneSafetyError("manifest contains an unstable file")
    paths = [record.get("path") for record in group]
    if any(not isinstance(path, str) or not Path(path).is_absolute() for path in paths):
        raise CloneSafetyError("manifest paths must be absolute strings")
    if len(set(paths)) != len(paths):
        raise CloneSafetyError("duplicate group repeats a path")
    return group


def build_plan(
    manifest_path: Path,
    excluded_paths: set[str],
) -> dict:
    manifest, manifest_sha256 = _read_json(manifest_path)
    groups = manifest.get("duplicate_groups")
    if not isinstance(groups, list):
        raise CloneSafetyError("manifest does not contain duplicate_groups")

    manifest_paths = {
        record.get("path")
        for raw_group in groups
        for record in _validate_manifest_group(raw_group)
    }
    unknown_exclusions = excluded_paths - manifest_paths
    if unknown_exclusions:
        raise CloneSafetyError(
            "excluded paths are absent from manifest: "
            + ", ".join(sorted(unknown_exclusions))
        )
    planned_groups = []
    excluded_groups = []
    seen_paths = set()
    for raw_group in groups:
        group = _validate_manifest_group(raw_group)
        group_hash = group[0]["sha256"]
        paths = [record["path"] for record in group]
        if seen_paths.intersection(paths):
            raise CloneSafetyError("a path appears in more than one duplicate group")
        seen_paths.update(paths)

        reason = None
        if excluded_paths.intersection(paths):
            reason = "excluded_path"
        if reason:
            excluded_groups.append(
                {
                    "sha256": group_hash,
                    "size_bytes": group[0]["size"],
                    "paths": paths,
                    "reason": reason,
                }
            )
            continue

        snapshots = []
        for record in group:
            print(f"Verifying {record['path']}")
            snapshot = snapshot_path(record["path"])
            if not _manifest_record_matches(snapshot, record):
                raise CloneSafetyError(
                    f"file no longer matches duplicate manifest: {record['path']}"
                )
            snapshots.append(snapshot)
        if len({snapshot["device"] for snapshot in snapshots}) != 1:
            raise CloneSafetyError(f"duplicate group spans filesystems: {group_hash}")

        planned_groups.append(
            {
                "sha256": group_hash,
                "size_bytes": snapshots[0]["size_bytes"],
                "source": snapshots[0],
                "targets": snapshots[1:],
                "maximum_reclaimable_bytes": snapshots[0]["size_bytes"]
                * (len(snapshots) - 1),
            }
        )

    return {
        "schema": PLAN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": {
            "path": str(manifest_path),
            "sha256": manifest_sha256,
            "checked_at": manifest.get("checked_at"),
        },
        "group_count": len(planned_groups),
        "target_count": sum(len(group["targets"]) for group in planned_groups),
        "maximum_reclaimable_bytes": sum(
            group["maximum_reclaimable_bytes"] for group in planned_groups
        ),
        "groups": planned_groups,
        "excluded_groups": excluded_groups,
    }


def _validate_plan(plan: dict) -> list[dict]:
    if plan.get("schema") != PLAN_SCHEMA:
        raise CloneSafetyError(f"unsupported plan schema: {plan.get('schema')}")
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        raise CloneSafetyError("plan contains no groups")
    seen_paths = set()
    for group in groups:
        source = group.get("source")
        targets = group.get("targets")
        if not isinstance(source, dict) or not isinstance(targets, list) or not targets:
            raise CloneSafetyError("plan group is incomplete")
        records = [source, *targets]
        if any(record.get("sha256") != group.get("sha256") for record in records):
            raise CloneSafetyError("plan group hashes disagree")
        if any(
            record.get("size_bytes") != group.get("size_bytes") for record in records
        ):
            raise CloneSafetyError("plan group sizes disagree")
        paths = [record.get("path") for record in records]
        if any(
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in paths
        ):
            raise CloneSafetyError("plan paths must be absolute without '..'")
        if len(paths) != len(set(paths)) or seen_paths.intersection(paths):
            raise CloneSafetyError("a path appears more than once in the plan")
        seen_paths.update(paths)
    return groups


def _available_bytes(path: str) -> int:
    values = os.statvfs(Path(path).parent)
    return values.f_bavail * values.f_frsize


def _settled_available_bytes(devices: dict[str, str]) -> dict[str, int]:
    observed = {device: _available_bytes(path) for device, path in devices.items()}
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        for device, path in devices.items():
            observed[device] = max(observed[device], _available_bytes(path))
    return observed


def apply_plan(
    plan: dict,
    plan_sha256: str,
    record_result: Callable[[dict], None],
) -> dict:
    groups = _validate_plan(plan)
    print("Preflighting every planned file before the first replacement")
    for group in groups:
        assert_snapshot(group["source"]["path"], group["source"])
        for target in group["targets"]:
            assert_snapshot(target["path"], target)

    devices = {
        str(group["source"]["device"]): group["source"]["path"] for group in groups
    }
    available_before = {
        device: _available_bytes(path) for device, path in devices.items()
    }
    started_at = datetime.now(timezone.utc).isoformat()
    record_result(
        {
            "schema": RESULT_SCHEMA,
            "event": "started",
            "plan_sha256": plan_sha256,
            "recorded_at": started_at,
            "available_bytes": available_before,
        }
    )
    operations = []
    for number, (group, target) in enumerate(
        (
            (candidate_group, target)
            for candidate_group in groups
            for target in candidate_group["targets"]
        ),
        start=1,
    ):
        event = {
            "schema": RESULT_SCHEMA,
            "plan_sha256": plan_sha256,
            "number": number,
            "source": group["source"]["path"],
            "target": target["path"],
        }
        record_result({**event, "event": "before"})
        print(f"Cloning {event['source']} -> {event['target']}")
        operation = replace_with_clone(group["source"], target)
        operations.append(operation)
        record_result({**event, "event": "after", "operation": operation})

    available_after = _settled_available_bytes(devices)
    return {
        "schema": RESULT_SCHEMA,
        "event": "completed",
        "plan_sha256": plan_sha256,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "available_bytes_before": available_before,
        "available_bytes_after": available_after,
        "measured_available_byte_change": {
            device: available_after[device] - available_before[device]
            for device in devices
        },
        "replaced_file_count": len(operations),
        "logical_bytes_replaced": sum(item["bytes"] for item in operations),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--exclude-path", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--result", type=Path, help="new append-only JSON Lines log")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.apply:
            if args.manifest is None:
                raise CloneSafetyError("--manifest is required when creating a plan")
            if args.confirm_plan_sha256 or args.result:
                raise CloneSafetyError(
                    "--confirm-plan-sha256 and --result are apply-only options"
                )
            plan = build_plan(
                args.manifest,
                set(args.exclude_path),
            )
            _write_json_new(args.plan, plan)
            digest = hashlib.sha256(args.plan.read_bytes()).hexdigest()
            print(
                f"Plan: {args.plan}\n"
                f"Plan SHA-256: {digest}\n"
                f"Groups: {plan['group_count']}\n"
                f"Targets: {plan['target_count']}\n"
                f"Maximum reclaimable bytes: {plan['maximum_reclaimable_bytes']}\n"
                "No media files changed."
            )
            return 0

        if args.manifest or args.exclude_path:
            raise CloneSafetyError(
                "apply reads only the reviewed plan; manifest selection options are invalid"
            )
        if not args.confirm_plan_sha256:
            raise CloneSafetyError("--confirm-plan-sha256 is required with --apply")
        if args.result is None:
            raise CloneSafetyError("--result is required with --apply")
        plan, digest = _read_json(args.plan)
        expected_digest = args.confirm_plan_sha256.removeprefix("sha256:")
        if digest != expected_digest:
            raise CloneSafetyError(
                f"plan digest mismatch: expected {expected_digest}, current {digest}"
            )
        _validate_plan(plan)
        with _new_output(args.result) as result_log:
            reservation = {
                "schema": RESULT_SCHEMA,
                "plan_sha256": digest,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "event": "reserved",
            }
            _append_result(result_log, reservation)
            try:
                result = apply_plan(
                    plan,
                    digest,
                    lambda payload: _append_result(result_log, payload),
                )
                _append_result(result_log, result)
            except (CloneSafetyError, OSError) as error:
                reservation.update(
                    {
                        "event": "failed",
                        "error": f"{type(error).__name__}: {error}",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                _append_result(result_log, reservation)
                raise
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (CloneSafetyError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
