"""Atomic JSON file writer — prevents partial writes via tempfile + os.replace."""

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, data: dict, indent: int = 2):
    """Atomically write a JSON file using tempfile + os.replace."""
    path = Path(path)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=indent, default=str)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
