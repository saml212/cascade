"""Backup agent — rsync episode to external HDD.

Inputs:
    - Entire episode directory
Outputs:
    - backup.json (backup path, size)
Dependencies:
    - rsync
Config:
    - paths.backup_dir
"""

import os
import subprocess
from pathlib import Path

from agents.base import BaseAgent


class BackupAgent(BaseAgent):
    name = "backup"

    def execute(self) -> dict:
        backup_dir = self.config.get("paths", {}).get(
            "backup_dir",
            os.getenv("CASCADE_BACKUP_DIR", ""),
        )

        if not backup_dir:
            raise RuntimeError(
                "backup_dir not set in config.toml or CASCADE_BACKUP_DIR env var"
            )

        backup_root = Path(backup_dir)
        if not backup_root.parent.exists():
            raise RuntimeError(
                "Backup drive not mounted: %s" % backup_root.parent
            )

        backup_root.mkdir(parents=True, exist_ok=True)

        episode_id = self.episode_dir.name
        dest = backup_root / episode_id

        # rsync the episode directory to the backup drive
        # --archive preserves permissions/timestamps, --delete keeps them in sync
        src = str(self.episode_dir).rstrip("/") + "/"
        dst = str(dest).rstrip("/") + "/"

        self.logger.info("Backing up %s -> %s" % (src, dst))

        cmd = [
            "rsync", "-a", "--delete",
            "--exclude", "work/",
            src, dst,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)

        if result.returncode != 0:
            raise RuntimeError("rsync failed: %s" % result.stderr[-500:])

        # Calculate backup size
        du_cmd = ["du", "-sh", dst]
        du_result = subprocess.run(du_cmd, capture_output=True, text=True)
        backup_size = du_result.stdout.split()[0] if du_result.returncode == 0 else "unknown"

        self.logger.info("Backup complete: %s (%s)" % (dst, backup_size))

        return {
            "backup_path": str(dest),
            "backup_size": backup_size,
            "episode_id": episode_id,
        }
