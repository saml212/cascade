"""Verified copy-on-write file replacement for macOS filesystems."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class CloneSafetyError(RuntimeError):
    """The requested clone operation could not be proven safe."""


class StaleFileError(CloneSafetyError):
    """A file changed after its expected snapshot was recorded."""


_CHUNK_SIZE = 8 * 1024 * 1024
_CLONE_ACL = 0x0004
_CLONE_NOFOLLOW_ANY = 0x0008
_COPYFILE_ACL = 1 << 0
_COPYFILE_STAT = 1 << 1
_COPYFILE_XATTR = 1 << 2
_COPYFILE_METADATA = _COPYFILE_ACL | _COPYFILE_STAT | _COPYFILE_XATTR
_RENAME_SWAP = 0x00000002
_RENAME_NOFOLLOW_ANY = 0x00000010

_libc = ctypes.CDLL(None, use_errno=True)


def _libc_function(name: str, arguments: list, result=ctypes.c_int):
    function = getattr(_libc, name, None)
    if function is not None:
        function.argtypes = arguments
        function.restype = result
    return function


_fclonefileat = _libc_function(
    "fclonefileat",
    [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ],
)
_fcopyfile = _libc_function(
    "fcopyfile",
    [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
)
_renameatx_np = _libc_function(
    "renameatx_np",
    [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ],
)
_flistxattr = _libc_function(
    "flistxattr",
    [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int],
    ctypes.c_ssize_t,
)
_fgetxattr = _libc_function(
    "fgetxattr",
    [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ],
    ctypes.c_ssize_t,
)
_fremovexattr = _libc_function(
    "fremovexattr", [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
)


def _require_macos() -> None:
    required = (
        _fclonefileat,
        _fcopyfile,
        _renameatx_np,
        _flistxattr,
        _fgetxattr,
        _fremovexattr,
    )
    if sys.platform != "darwin" or any(function is None for function in required):
        raise CloneSafetyError("verified clone replacement requires macOS")


def _raise_errno(operation: str) -> None:
    error_number = ctypes.get_errno()
    raise OSError(error_number, f"{operation}: {os.strerror(error_number)}")


def _checked_call(function, operation: str, *args) -> None:
    ctypes.set_errno(0)
    if function(*args) != 0:
        _raise_errno(operation)


def _absolute_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise CloneSafetyError(f"path must be absolute without '..': {path}")
    return candidate


@contextmanager
def _open_directory(path: str | Path) -> Iterator[int]:
    candidate = _absolute_path(path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open("/", directory_flags)
    try:
        for part in candidate.parts[1:]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        yield directory_fd
    finally:
        os.close(directory_fd)


@contextmanager
def _open_regular(path: str | Path) -> Iterator[tuple[int, bytes, int]]:
    candidate = _absolute_path(path)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    with _open_directory(candidate.parent) as directory_fd:
        name = os.fsencode(candidate.name)
        file_fd = os.open(name, file_flags, dir_fd=directory_fd)
        try:
            yield directory_fd, name, file_fd
        finally:
            os.close(file_fd)


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        getattr(value, "st_flags", 0),
    )


def _validate_stat(value: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise CloneSafetyError(f"not a regular file: {path}")
    if value.st_nlink != 1:
        raise CloneSafetyError(f"hard-linked files are not eligible: {path}")
    immutable = sum(
        getattr(stat, name, 0)
        for name in ("UF_IMMUTABLE", "SF_IMMUTABLE", "UF_APPEND", "SF_APPEND")
    )
    if getattr(value, "st_flags", 0) & immutable:
        raise CloneSafetyError(f"immutable or append-only file is not eligible: {path}")


def _sha256_fd(file_fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(file_fd, _CHUNK_SIZE, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _xattr_names(file_fd: int) -> list[bytes]:
    for _ in range(3):
        size = _flistxattr(file_fd, None, 0, 0)
        if size < 0:
            _raise_errno("flistxattr")
        if size == 0:
            return []
        buffer = ctypes.create_string_buffer(size)
        count = _flistxattr(file_fd, buffer, size, 0)
        if count >= 0:
            return sorted(name for name in buffer.raw[:count].split(b"\0") if name)
        if ctypes.get_errno() != errno.ERANGE:
            _raise_errno("flistxattr")
    raise StaleFileError("extended attributes changed while being read")


def _xattr_value(file_fd: int, name: bytes) -> bytes:
    size = _fgetxattr(file_fd, name, None, 0, 0, 0)
    if size < 0:
        _raise_errno(f"fgetxattr {os.fsdecode(name)}")
    if size == 0:
        return b""
    buffer = ctypes.create_string_buffer(size)
    count = _fgetxattr(file_fd, name, buffer, size, 0, 0)
    if count < 0:
        _raise_errno(f"fgetxattr {os.fsdecode(name)}")
    return buffer.raw[:count]


def _xattr_snapshot(file_fd: int) -> list[dict]:
    return [
        {
            "name": os.fsdecode(name),
            "size_bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
        for name in _xattr_names(file_fd)
        for value in [_xattr_value(file_fd, name)]
    ]


def _remove_unwanted_xattrs(file_fd: int, target_snapshot: dict) -> None:
    wanted = {os.fsencode(item["name"]) for item in target_snapshot["xattrs"]}
    for name in _xattr_names(file_fd):
        if name not in wanted:
            _checked_call(
                _fremovexattr,
                f"fremovexattr {os.fsdecode(name)}",
                file_fd,
                name,
                0,
            )


def snapshot_fd(file_fd: int, path: str | Path) -> dict:
    """Hash an open file and capture the metadata used by safety checks."""
    _require_macos()
    candidate = _absolute_path(path)
    before = os.fstat(file_fd)
    _validate_stat(before, candidate)
    digest = _sha256_fd(file_fd)
    xattrs = _xattr_snapshot(file_fd)
    after = os.fstat(file_fd)
    if _stat_signature(before) != _stat_signature(after):
        raise StaleFileError(f"file changed while being inspected: {candidate}")
    return {
        "path": str(candidate),
        "sha256": digest,
        "size_bytes": after.st_size,
        "device": after.st_dev,
        "inode": after.st_ino,
        "mode": stat.S_IMODE(after.st_mode),
        "uid": after.st_uid,
        "gid": after.st_gid,
        "mtime_ns": after.st_mtime_ns,
        "ctime_ns": after.st_ctime_ns,
        "flags": getattr(after, "st_flags", 0),
        "allocated_bytes": after.st_blocks * 512,
        "xattrs": xattrs,
    }


def snapshot_path(path: str | Path) -> dict:
    """Open a path without following symlinks and return its safety snapshot."""
    with _open_regular(path) as (_, _, file_fd):
        return snapshot_fd(file_fd, path)


def _assert_snapshot(actual: dict, expected: dict) -> None:
    checked = (
        "path",
        "sha256",
        "size_bytes",
        "device",
        "inode",
        "mode",
        "uid",
        "gid",
        "mtime_ns",
        "ctime_ns",
        "flags",
        "xattrs",
    )
    differences = [key for key in checked if actual.get(key) != expected.get(key)]
    if differences:
        raise StaleFileError(
            f"file no longer matches plan: {actual['path']} ({', '.join(differences)})"
        )


def assert_snapshot(path: str | Path, expected: dict) -> dict:
    """Require a path to match a previously recorded snapshot."""
    actual = snapshot_path(path)
    _assert_snapshot(actual, expected)
    return actual


def _metadata(snapshot: dict) -> dict:
    keys = (
        "size_bytes",
        "mode",
        "uid",
        "gid",
        "mtime_ns",
        "flags",
        "xattrs",
    )
    return {key: snapshot[key] for key in keys}


def _same_open_file(file_fd: int, directory_fd: int, name: bytes) -> bool:
    opened = os.fstat(file_fd)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


def _assert_open_file(
    file_fd: int,
    directory_fd: int,
    name: bytes,
    path: Path,
    phase: str,
    signature: tuple[int, ...] | None = None,
) -> None:
    if (signature is not None and signature != _stat_signature(os.fstat(file_fd))) or (
        not _same_open_file(file_fd, directory_fd, name)
    ):
        raise StaleFileError(f"{path} changed {phase}")


def _assert_directory_anchor(directory_fd: int, path: Path) -> None:
    with _open_directory(path.parent) as current_fd:
        opened = os.fstat(directory_fd)
        current = os.fstat(current_fd)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise StaleFileError(
                f"parent directory changed during clone: {path.parent}"
            )


def replace_with_clone(source: dict, target: dict) -> dict:
    """Atomically replace target with a verified clone while retaining its metadata."""
    _require_macos()
    src = _absolute_path(source["path"])
    dst = _absolute_path(target["path"])
    if src == dst:
        raise CloneSafetyError("source and target paths must differ")

    with (
        _open_regular(src) as (src_dir, src_name, src_fd),
        _open_regular(dst) as (dst_dir, dst_name, dst_fd),
    ):
        src_actual = snapshot_fd(src_fd, src)
        dst_actual = snapshot_fd(dst_fd, dst)
        _assert_snapshot(src_actual, source)
        _assert_snapshot(dst_actual, target)
        src_signature = _stat_signature(os.fstat(src_fd))
        dst_signature = _stat_signature(os.fstat(dst_fd))
        if src_actual["sha256"] != dst_actual["sha256"]:
            raise CloneSafetyError("source and target content hashes differ")
        if src_actual["device"] != dst_actual["device"]:
            raise CloneSafetyError("source and target are on different filesystems")

        temporary_name = (
            b"." + dst_name + b".deduplicate-" + secrets.token_hex(8).encode()
        )
        swapped = False
        temp_fd = None
        try:
            try:
                _checked_call(
                    _fclonefileat,
                    "fclonefileat",
                    src_fd,
                    dst_dir,
                    temporary_name,
                    _CLONE_ACL | _CLONE_NOFOLLOW_ANY,
                )
            except OSError as error:
                if error.errno in {
                    errno.ENOSPC,
                    errno.ENOTSUP,
                    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
                    errno.EXDEV,
                }:
                    raise CloneSafetyError(
                        f"clone unavailable; no byte-copy fallback used: {error}"
                    ) from error
                raise

            temp_fd = os.open(
                temporary_name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=dst_dir,
            )
            cloned_mode = stat.S_IMODE(os.fstat(temp_fd).st_mode)
            if not cloned_mode & stat.S_IWUSR:
                os.fchmod(temp_fd, cloned_mode | stat.S_IWUSR)
            _remove_unwanted_xattrs(temp_fd, dst_actual)
            _checked_call(
                _fcopyfile,
                "fcopyfile metadata",
                dst_fd,
                temp_fd,
                None,
                _COPYFILE_METADATA,
            )
            os.fsync(temp_fd)

            cloned = snapshot_fd(temp_fd, dst)
            if cloned["sha256"] != src_actual["sha256"]:
                raise CloneSafetyError("cloned content hash does not match source")
            if _metadata(cloned) != _metadata(dst_actual):
                raise CloneSafetyError("clone does not preserve target metadata")
            if cloned["inode"] in {src_actual["inode"], dst_actual["inode"]}:
                raise CloneSafetyError("clone did not create an independent inode")
            _assert_directory_anchor(src_dir, src)
            _assert_directory_anchor(dst_dir, dst)
            _assert_open_file(
                src_fd,
                src_dir,
                src_name,
                src,
                "during clone",
                src_signature,
            )
            _assert_open_file(
                dst_fd,
                dst_dir,
                dst_name,
                dst,
                "during clone",
                dst_signature,
            )

            _checked_call(
                _renameatx_np,
                "renameatx_np swap",
                dst_dir,
                temporary_name,
                dst_dir,
                dst_name,
                _RENAME_SWAP | _RENAME_NOFOLLOW_ANY,
            )
            swapped = True
            _assert_directory_anchor(src_dir, src)
            _assert_directory_anchor(dst_dir, dst)
            _assert_open_file(
                src_fd,
                src_dir,
                src_name,
                src,
                "during swap",
                src_signature,
            )
            installed = os.stat(dst_name, dir_fd=dst_dir, follow_symlinks=False)
            displaced = os.stat(temporary_name, dir_fd=dst_dir, follow_symlinks=False)
            if (
                installed.st_ino != cloned["inode"]
                or displaced.st_ino != dst_actual["inode"]
            ):
                raise CloneSafetyError("atomic swap installed unexpected inodes")
            _assert_open_file(temp_fd, dst_dir, dst_name, dst, "during swap")
            _assert_open_file(
                dst_fd,
                dst_dir,
                temporary_name,
                dst,
                "while retaining its prior inode",
            )
            os.fsync(dst_dir)
            os.unlink(temporary_name, dir_fd=dst_dir)
            swapped = False
            os.fsync(dst_dir)
            return {
                "path": str(dst),
                "bytes": dst_actual["size_bytes"],
                "old_inode": dst_actual["inode"],
                "new_inode": cloned["inode"],
                "source_inode": src_actual["inode"],
            }
        except BaseException:
            if swapped:
                try:
                    _checked_call(
                        _renameatx_np,
                        "renameatx_np rollback",
                        dst_dir,
                        temporary_name,
                        dst_dir,
                        dst_name,
                        _RENAME_SWAP | _RENAME_NOFOLLOW_ANY,
                    )
                    swapped = False
                except OSError as rollback_error:
                    raise CloneSafetyError(
                        f"clone failed and atomic rollback failed: {rollback_error}"
                    ) from rollback_error
            if not swapped:
                try:
                    os.unlink(temporary_name, dir_fd=dst_dir)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
