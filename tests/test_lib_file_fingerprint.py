"""Regression tests for exact, identity-aware file fingerprints."""

import hashlib
import os
import time
from unittest.mock import Mock

import pytest

from lib import ffprobe


def _rewrite_with_preserved_mtime(path, content: bytes, original_stat) -> None:
    time.sleep(0.002)
    path.write_bytes(content)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))


def test_unchanged_file_reuses_full_hash(tmp_path, monkeypatch):
    path = tmp_path / "artifact.mp4"
    path.write_bytes(b"unchanged content")
    hasher = Mock(side_effect=ffprobe._hash_open_file)
    monkeypatch.setattr(ffprobe, "_hash_open_file", hasher)

    first = ffprobe.file_fingerprint(path)
    second = ffprobe.file_fingerprint(path)

    assert (
        first
        == second
        == {
            "id": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            "method": "sha256-full/v1",
            "size_bytes": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
    )
    assert hasher.call_count == 1


def test_same_size_and_mtime_rewrite_invalidates_cache(tmp_path):
    path = tmp_path / "artifact.mp4"
    path.write_bytes(b"before")
    original_stat = path.stat()
    before = ffprobe.file_fingerprint(path)

    _rewrite_with_preserved_mtime(path, b"after!", original_stat)
    changed_stat = path.stat()
    after = ffprobe.file_fingerprint(path)

    assert changed_stat.st_size == original_stat.st_size
    assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns
    assert changed_stat.st_ctime_ns != original_stat.st_ctime_ns
    assert after["id"] != before["id"]


def test_same_size_and_mtime_replacement_invalidates_cache(tmp_path):
    path = tmp_path / "artifact.mp4"
    path.write_bytes(b"before")
    original_stat = path.stat()
    before = ffprobe.file_fingerprint(path)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"after!")
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    os.replace(replacement, path)
    changed_stat = path.stat()
    after = ffprobe.file_fingerprint(path)

    assert changed_stat.st_size == original_stat.st_size
    assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns
    assert changed_stat.st_ino != original_stat.st_ino
    assert after["id"] != before["id"]


def test_change_during_hash_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "artifact.mp4"
    path.write_bytes(b"before")
    original_stat = path.stat()
    original_hasher = ffprobe._hash_open_file

    def mutate_while_hashing(handle):
        digest = original_hasher(handle)
        _rewrite_with_preserved_mtime(path, b"after!", original_stat)
        return digest

    monkeypatch.setattr(ffprobe, "_hash_open_file", mutate_while_hashing)

    with pytest.raises(OSError, match="changed while its fingerprint was read"):
        ffprobe.file_fingerprint(path)


def test_symlink_retarget_during_hash_fails_closed(tmp_path, monkeypatch):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first!")
    second.write_bytes(b"second")
    link = tmp_path / "artifact.mp4"
    link.symlink_to(first)
    original_hasher = ffprobe._hash_open_file

    def retarget_while_hashing(handle):
        digest = original_hasher(handle)
        link.unlink()
        link.symlink_to(second)
        return digest

    monkeypatch.setattr(ffprobe, "_hash_open_file", retarget_while_hashing)

    with pytest.raises(OSError, match="changed while its fingerprint was read"):
        ffprobe.file_fingerprint(link)


def test_symlink_retarget_after_cached_hash_fails_closed(tmp_path, monkeypatch):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first!")
    second.write_bytes(b"second")
    link = tmp_path / "artifact.mp4"
    link.symlink_to(first)
    ffprobe.file_fingerprint(link)
    cached_digest = ffprobe._file_digest

    def retarget_after_cache_hit(*args):
        digest = cached_digest(*args)
        link.unlink()
        link.symlink_to(second)
        return digest

    monkeypatch.setattr(ffprobe, "_file_digest", retarget_after_cache_hit)

    with pytest.raises(OSError, match="changed while its fingerprint was read"):
        ffprobe.file_fingerprint(link)
