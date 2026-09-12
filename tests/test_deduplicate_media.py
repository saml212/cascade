from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib import apfs_clone
from lib.apfs_clone import CloneSafetyError, replace_with_clone, snapshot_path
from scripts import deduplicate_media

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_record(path: Path) -> dict:
    details = path.stat()
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "size": details.st_size,
        "mtime_ns": details.st_mtime_ns,
        "inode": details.st_ino,
        "device": details.st_dev,
        "blocks": details.st_blocks,
        "stable": True,
    }


def _write_manifest(path: Path, groups: list[list[dict]]) -> None:
    path.write_text(json.dumps({"duplicate_groups": groups}) + "\n")


def _duplicate_pair(tmp_path: Path) -> tuple[Path, Path]:
    content = bytes(range(256)) * 16_384
    source = tmp_path / "source.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(content)
    target.write_bytes(content)
    return source, target


def _build_plan(tmp_path: Path, source: Path, target: Path) -> Path:
    manifest = tmp_path / "duplicates.json"
    plan = tmp_path / "plan.json"
    _write_manifest(manifest, [[_manifest_record(source), _manifest_record(target)]])
    assert (
        deduplicate_media.main(["--manifest", str(manifest), "--plan", str(plan)]) == 0
    )
    return plan


def _temporary_clone_files(path: Path) -> list[Path]:
    return list(path.glob(".*.deduplicate-*"))


def test_plan_apply_uses_independent_clone_and_preserves_target_metadata(
    tmp_path, monkeypatch
):
    source, target = _duplicate_pair(tmp_path)
    source.chmod(0o600)
    target.chmod(0o640)
    subprocess.run(
        ["xattr", "-w", "user.source-only", "remove-from-clone", source],
        check=True,
    )
    subprocess.run(
        ["xattr", "-w", "user.target-only", "keep-on-target", target],
        check=True,
    )
    os.utime(target, ns=(1_700_000_000_123_456_789,) * 2)
    target_before = snapshot_path(target)
    source_before = snapshot_path(source)
    plan = _build_plan(tmp_path, source, target)
    digest = _sha256(plan)
    result_path = tmp_path / "result.json"
    recorded_states = []
    append_result = deduplicate_media._append_result

    def record_json(output, payload):
        recorded_states.append(json.loads(json.dumps(payload)))
        append_result(output, payload)

    monkeypatch.setattr(deduplicate_media, "_append_result", record_json)
    monkeypatch.setattr(
        deduplicate_media,
        "_settled_available_bytes",
        lambda devices: {
            device: deduplicate_media._available_bytes(path)
            for device, path in devices.items()
        },
    )

    assert (
        deduplicate_media.main(
            [
                "--plan",
                str(plan),
                "--apply",
                "--confirm-plan-sha256",
                digest,
                "--result",
                str(result_path),
            ]
        )
        == 0
    )

    target_after = snapshot_path(target)
    assert target_after["inode"] not in {
        source_before["inode"],
        target_before["inode"],
    }
    assert target.stat().st_nlink == 1
    assert source.stat().st_nlink == 1
    for field in (
        "sha256",
        "size_bytes",
        "mode",
        "uid",
        "gid",
        "mtime_ns",
        "flags",
        "xattrs",
    ):
        assert target_after[field] == target_before[field]
    target_xattrs = {item["name"]: item for item in target_after["xattrs"]}
    assert "user.source-only" not in target_xattrs
    assert (
        target_xattrs["user.target-only"]["sha256"]
        == hashlib.sha256(b"keep-on-target").hexdigest()
    )

    with target.open("r+b") as output:
        output.write(b"changed target")
    assert snapshot_path(source)["sha256"] == source_before["sha256"]

    result = json.loads(result_path.read_text().splitlines()[-1])
    assert result["event"] == "completed"
    assert result["replaced_file_count"] == 1
    assert [state["event"] for state in recorded_states] == [
        "reserved",
        "started",
        "before",
        "after",
        "completed",
    ]
    assert recorded_states[3]["operation"]["old_inode"] == target_before["inode"]


def test_apply_rejects_stale_same_size_target_before_replacement(tmp_path):
    source, target = _duplicate_pair(tmp_path)
    plan = _build_plan(tmp_path, source, target)
    digest = _sha256(plan)
    old_inode = target.stat().st_ino
    old_mtime_ns = target.stat().st_mtime_ns
    target.write_bytes(b"x" * target.stat().st_size)
    os.utime(target, ns=(old_mtime_ns, old_mtime_ns))
    result = tmp_path / "result.json"

    assert (
        deduplicate_media.main(
            [
                "--plan",
                str(plan),
                "--apply",
                "--confirm-plan-sha256",
                digest,
                "--result",
                str(result),
            ]
        )
        == 2
    )
    assert target.stat().st_ino == old_inode
    assert target.read_bytes() == b"x" * target.stat().st_size
    assert json.loads(result.read_text().splitlines()[-1])["event"] == "failed"
    assert not _temporary_clone_files(tmp_path)


@pytest.mark.parametrize("link_ancestor", [False, True])
def test_plan_rejects_symlink_leaf_or_ancestor(tmp_path, link_ancestor):
    source, target = _duplicate_pair(tmp_path)
    record = _manifest_record(target)
    if link_ancestor:
        real_parent = tmp_path / "real"
        real_parent.mkdir()
        linked_target = real_parent / "target.bin"
        target.replace(linked_target)
        link = tmp_path / "linked"
        link.symlink_to(real_parent, target_is_directory=True)
        record["path"] = str(link / linked_target.name)
    else:
        link = tmp_path / "linked-target.bin"
        link.symlink_to(target)
        record["path"] = str(link)
    manifest = tmp_path / "duplicates.json"
    plan = tmp_path / "plan.json"
    _write_manifest(manifest, [[_manifest_record(source), record]])

    assert (
        deduplicate_media.main(["--manifest", str(manifest), "--plan", str(plan)]) == 2
    )
    assert not plan.exists()


def test_plan_rejects_hard_link(tmp_path):
    source, target = _duplicate_pair(tmp_path)
    linked = tmp_path / "linked.bin"
    os.link(target, linked)
    manifest = tmp_path / "duplicates.json"
    plan = tmp_path / "plan.json"
    _write_manifest(manifest, [[_manifest_record(source), _manifest_record(target)]])

    assert (
        deduplicate_media.main(["--manifest", str(manifest), "--plan", str(plan)]) == 2
    )
    assert not plan.exists()


@pytest.mark.parametrize("error_number", [errno.ENOTSUP, errno.ENOSPC, errno.EXDEV])
def test_clone_unavailable_has_no_byte_copy_fallback(
    tmp_path, monkeypatch, error_number
):
    source, target = _duplicate_pair(tmp_path)
    source_snapshot = snapshot_path(source)
    target_snapshot = snapshot_path(target)
    old_inode = target.stat().st_ino

    def unsupported_clone(*_args):
        ctypes.set_errno(error_number)
        return -1

    monkeypatch.setattr(apfs_clone, "_fclonefileat", unsupported_clone)
    with pytest.raises(CloneSafetyError, match="no byte-copy fallback"):
        replace_with_clone(source_snapshot, target_snapshot)
    assert target.stat().st_ino == old_inode
    assert target.read_bytes() == source.read_bytes()
    assert not _temporary_clone_files(tmp_path)


def test_equal_metadata_does_not_rewrite_clone_metadata(tmp_path, monkeypatch):
    source, target = _duplicate_pair(tmp_path)
    source_stat = source.stat()
    os.utime(target, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    for path in (source, target):
        subprocess.run(
            ["xattr", "-wx", "com.apple.provenance", "0102007C05C2C960A50211", path],
            check=True,
        )
    source_snapshot = snapshot_path(source)
    target_snapshot = snapshot_path(target)

    def unexpected_metadata_copy(*_args):
        raise AssertionError("identical metadata must not be rewritten")

    monkeypatch.setattr(apfs_clone, "_fcopyfile", unexpected_metadata_copy)
    result = replace_with_clone(source_snapshot, target_snapshot)

    assert snapshot_path(target)["sha256"] == source_snapshot["sha256"]
    assert target.stat().st_ino not in {
        source_snapshot["inode"],
        target_snapshot["inode"],
    }
    assert result["system_xattr_changes"] == []


def test_only_inode_provenance_may_change_during_clone():
    expected = {
        "size_bytes": 1026755072,
        "mode": 0o700,
        "uid": 501,
        "gid": 20,
        "mtime_ns": 1773276784000000000,
        "flags": 0,
        "xattrs": [
            {
                "name": "com.apple.provenance",
                "size_bytes": 11,
                "sha256": hashlib.sha256(
                    bytes.fromhex("0102007c05c2c960a50211")
                ).hexdigest(),
            }
        ],
    }
    actual = json.loads(json.dumps(expected))
    actual["xattrs"][0]["sha256"] = hashlib.sha256(
        bytes.fromhex("01020043758024f311838c")
    ).hexdigest()

    differences, changes = apfs_clone._metadata_differences(actual, expected)

    assert differences == []
    assert changes == [
        {
            "name": "com.apple.provenance",
            "expected_sha256": expected["xattrs"][0]["sha256"],
            "installed_sha256": actual["xattrs"][0]["sha256"],
            "size_bytes": 11,
            "reason": "macOS regenerated inode provenance during APFS clone",
        }
    ]

    actual["xattrs"][0]["name"] = "user.provenance"
    differences, _ = apfs_clone._metadata_differences(actual, expected)
    assert differences == ["xattrs:com.apple.provenance", "xattrs:user.provenance"]


def test_full_preflight_finds_late_stale_target_before_any_replacement(tmp_path):
    source, first_target = _duplicate_pair(tmp_path)
    second_target = tmp_path / "second-target.bin"
    second_target.write_bytes(source.read_bytes())
    manifest = tmp_path / "duplicates.json"
    plan = tmp_path / "plan.json"
    _write_manifest(
        manifest,
        [
            [
                _manifest_record(source),
                _manifest_record(first_target),
                _manifest_record(second_target),
            ]
        ],
    )
    assert (
        deduplicate_media.main(["--manifest", str(manifest), "--plan", str(plan)]) == 0
    )
    digest = _sha256(plan)
    first_inode = first_target.stat().st_ino
    second_target.write_bytes(b"z" * second_target.stat().st_size)

    assert (
        deduplicate_media.main(
            [
                "--plan",
                str(plan),
                "--apply",
                "--confirm-plan-sha256",
                digest,
                "--result",
                str(tmp_path / "result.json"),
            ]
        )
        == 2
    )
    assert first_target.stat().st_ino == first_inode
    assert not _temporary_clone_files(tmp_path)


def test_source_change_after_swap_rolls_target_back(tmp_path, monkeypatch):
    source, target = _duplicate_pair(tmp_path)
    source_snapshot = snapshot_path(source)
    target_snapshot = snapshot_path(target)
    old_inode = target.stat().st_ino
    renameatx_np = apfs_clone._renameatx_np
    swap_count = 0

    def mutate_source_after_swap(*args):
        nonlocal swap_count
        result = renameatx_np(*args)
        swap_count += 1
        if swap_count == 1:
            with source.open("r+b") as output:
                output.write(b"source changed")
        return result

    monkeypatch.setattr(apfs_clone, "_renameatx_np", mutate_source_after_swap)
    with pytest.raises(CloneSafetyError, match="source.bin changed during swap"):
        replace_with_clone(source_snapshot, target_snapshot)
    assert swap_count == 2
    assert target.stat().st_ino == old_inode
    assert target.read_bytes() == bytes(range(256)) * 16_384
    assert not _temporary_clone_files(tmp_path)


def test_parent_rename_during_clone_aborts_without_replacement(tmp_path, monkeypatch):
    episode = tmp_path / "episode"
    episode.mkdir()
    source, target = _duplicate_pair(episode)
    source_snapshot = snapshot_path(source)
    target_snapshot = snapshot_path(target)
    old_inode = target.stat().st_ino
    moved_episode = tmp_path / "episode-moved"
    fcopyfile = apfs_clone._fcopyfile

    def rename_parent_after_metadata_copy(*args):
        result = fcopyfile(*args)
        episode.rename(moved_episode)
        return result

    monkeypatch.setattr(apfs_clone, "_fcopyfile", rename_parent_after_metadata_copy)
    with pytest.raises(OSError):
        replace_with_clone(source_snapshot, target_snapshot)
    moved_episode.rename(episode)
    assert target.stat().st_ino == old_inode
    assert target.read_bytes() == source.read_bytes()
    assert not _temporary_clone_files(episode)


def test_apply_rejects_existing_result_before_dispatch(tmp_path, monkeypatch):
    source, target = _duplicate_pair(tmp_path)
    plan = _build_plan(tmp_path, source, target)
    digest = _sha256(plan)
    old_inode = target.stat().st_ino
    result = tmp_path / "result.json"
    result.write_text("keep me")

    def unexpected_apply(*_args, **_kwargs):
        raise AssertionError("apply must not run")

    monkeypatch.setattr(deduplicate_media, "apply_plan", unexpected_apply)
    assert (
        deduplicate_media.main(
            [
                "--plan",
                str(plan),
                "--apply",
                "--confirm-plan-sha256",
                digest,
                "--result",
                str(result),
            ]
        )
        == 2
    )
    assert result.read_text() == "keep me"
    assert target.stat().st_ino == old_inode


def test_plan_rejects_repeated_path_within_group(tmp_path):
    source, target = _duplicate_pair(tmp_path)
    plan_path = _build_plan(tmp_path, source, target)
    plan = json.loads(plan_path.read_text())
    plan["groups"][0]["targets"].append(plan["groups"][0]["targets"][0])

    with pytest.raises(CloneSafetyError, match="more than once"):
        deduplicate_media._validate_plan(plan)
