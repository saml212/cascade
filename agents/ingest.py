"""Ingest agent — copy source files from SD card to SSD working storage.

Inputs:
    - source_path: SD card directory (e.g., /Volumes/7/DCIM/100CANON/)
Outputs:
    - ingest.json: File manifest with paths, durations, sizes
    - source/: Copied MP4 files on SSD
Dependencies:
    - ffprobe (duration validation)
Config:
    - paths.output_dir (episode output root)
"""

import json
import shutil
import subprocess
from pathlib import Path

from agents.base import BaseAgent
from lib.ffprobe import probe as ffprobe


class IngestAgent(BaseAgent):
    name = "ingest"

    def __init__(self, episode_dir: Path, config: dict):
        super().__init__(episode_dir, config)
        self.source_path = None  # Set by pipeline orchestrator

    def execute(self) -> dict:
        if not self.source_path:
            raise ValueError("source_path not set on IngestAgent")

        source = Path(self.source_path)
        dest_dir = self.episode_dir / "source"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Glob MP4 files (exclude macOS ._ resource forks)
        if source.is_dir():
            files = sorted(
                f for f in list(source.glob("*.MP4")) + list(source.glob("*.mp4"))
                if not f.name.startswith("._")
            )
        else:
            files = [source]

        if not files:
            raise FileNotFoundError(f"No MP4 files found at {source}")

        # Extract creation_time via ffprobe and sort chronologically
        file_info = []
        for f in files:
            probe = ffprobe(f)
            creation_time = probe.get("format", {}).get("tags", {}).get("creation_time", "")
            duration = float(probe.get("format", {}).get("duration", 0))
            file_info.append({
                "source_path": str(f),
                "filename": f.name,
                "creation_time": creation_time,
                "duration_seconds": round(duration, 3),
                "size_bytes": f.stat().st_size,
            })

        file_info.sort(key=lambda x: x["creation_time"])
        self.logger.info(f"Found {len(file_info)} files, total {sum(f['duration_seconds'] for f in file_info):.1f}s")

        # Copy each file to SSD
        copied_files = []
        for idx, info in enumerate(file_info):
            src = Path(info["source_path"])
            dst = dest_dir / info["filename"]
            self.logger.info(f"Copying {info['filename']} ({info['size_bytes'] / 1e9:.2f} GB)...")
            self.report_progress(idx, len(file_info),
                f"Copying {info['filename']}")
            shutil.copy2(src, dst)

            # Validate copy with ffprobe
            probe = ffprobe(dst)
            copy_duration = float(probe.get("format", {}).get("duration", 0))
            if abs(copy_duration - info["duration_seconds"]) > 1.0:
                raise RuntimeError(
                    f"Duration mismatch after copy: {info['filename']} "
                    f"(source={info['duration_seconds']:.1f}s, copy={copy_duration:.1f}s)"
                )

            info["dest_path"] = str(dst)
            info["copy_validated"] = True
            copied_files.append(info)

        total_duration = sum(f["duration_seconds"] for f in copied_files)
        total_size = sum(f["size_bytes"] for f in copied_files)

        return {
            "files": copied_files,
            "file_count": len(copied_files),
            "total_duration_seconds": round(total_duration, 3),
            "total_size_bytes": total_size,
            "duration_seconds": round(total_duration, 3),
        }

