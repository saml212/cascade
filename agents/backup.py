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
            self.logger.info("No backup_dir configured — skipping backup")
            return {
                "backup_path": "",
                "backup_size": "0",
                "episode_id": self.episode_dir.name,
                "skipped": True,
                "reason": "backup_dir not set in config.toml (set it to enable backups)",
            }

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

        # Clean up source files from SD card after successful backup
        cleaned = self._cleanup_sd_card()

        return {
            "backup_path": str(dest),
            "backup_size": backup_size,
            "episode_id": episode_id,
            "sd_cleanup": cleaned,
        }

    def _cleanup_sd_card(self) -> dict:
        """Delete ingested source files from the SD card.

        Reads ingest.json to find the original source paths and removes
        the MP4 files plus their corresponding LRF proxy files.
        """
        ingest_file = self.episode_dir / "ingest.json"
        if not ingest_file.exists():
            self.logger.info("No ingest.json found — skipping SD cleanup")
            return {"skipped": True, "reason": "no ingest.json"}

        import json
        with open(ingest_file) as f:
            ingest_data = json.load(f)

        files = ingest_data.get("files", [])
        if not files:
            return {"skipped": True, "reason": "no files in ingest.json"}

        deleted = []
        skipped = []

        for file_info in files:
            src_path = Path(file_info.get("source_path", ""))
            if not src_path.exists():
                skipped.append(str(src_path))
                continue

            # Verify the file is on a removable volume (safety check)
            if not str(src_path).startswith("/Volumes/"):
                self.logger.warning(
                    "Skipping non-volume source: %s" % src_path
                )
                skipped.append(str(src_path))
                continue

            # Delete the MP4 file
            self.logger.info("Deleting source: %s" % src_path)
            src_path.unlink()
            deleted.append(str(src_path))

            # Also delete corresponding LRF proxy file if it exists
            lrf_path = src_path.with_suffix(".LRF")
            if lrf_path.exists():
                self.logger.info("Deleting LRF proxy: %s" % lrf_path)
                lrf_path.unlink()
                deleted.append(str(lrf_path))

        self.logger.info(
            "SD cleanup: deleted %d files, skipped %d" % (len(deleted), len(skipped))
        )

        return {
            "deleted": deleted,
            "skipped": skipped,
            "deleted_count": len(deleted),
        }
