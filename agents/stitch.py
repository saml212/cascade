"""Stitch agent — concatenate source files into a single merged file."""

import json
import subprocess
import tempfile
from pathlib import Path

from agents.base import BaseAgent


class StitchAgent(BaseAgent):
    name = "stitch"

    def execute(self) -> dict:
        ingest_data = self.load_json("ingest.json")
        files = ingest_data["files"]

        if len(files) == 0:
            raise ValueError("No files to stitch")

        output_path = self.episode_dir / "source_merged.mp4"

        if len(files) == 1:
            # Single file — just symlink or copy
            import shutil
            shutil.copy2(files[0]["dest_path"], output_path)
        else:
            # Write ffmpeg concat list
            concat_list = self.episode_dir / "work" / "concat_list.txt"
            concat_list.parent.mkdir(exist_ok=True)
            with open(concat_list, "w") as f:
                for file_info in files:
                    # Escape single quotes in path
                    safe_path = file_info["dest_path"].replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")

            # Stream-copy merge (files are uniform H.264/AAC)
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(output_path),
            ]
            self.logger.info(f"Stitching {len(files)} files...")
            subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Validate output
        probe = self._ffprobe(output_path)
        output_duration = float(probe["format"]["duration"])
        expected_duration = sum(f["duration_seconds"] for f in files)

        # Allow 1s tolerance for concat boundaries
        if abs(output_duration - expected_duration) > 2.0:
            self.logger.warning(
                f"Duration mismatch: expected {expected_duration:.1f}s, got {output_duration:.1f}s"
            )

        # Extract a frame at ~5s for crop setup UI
        crop_frame_path = self.episode_dir / "crop_frame.jpg"
        frame_time = min(5.0, output_duration / 2)
        try:
            frame_cmd = [
                "ffmpeg", "-y",
                "-ss", str(frame_time),
                "-i", str(output_path),
                "-frames:v", "1",
                "-q:v", "2",
                str(crop_frame_path),
            ]
            subprocess.run(frame_cmd, capture_output=True, text=True, check=True)
            self.logger.info(f"Extracted crop frame at {frame_time:.1f}s → crop_frame.jpg")
        except subprocess.CalledProcessError as e:
            self.logger.warning(f"Failed to extract crop frame: {e}")

        return {
            "output_path": str(output_path),
            "input_count": len(files),
            "duration_seconds": round(output_duration, 3),
            "expected_duration_seconds": round(expected_duration, 3),
        }

    def _ffprobe(self, path: Path) -> dict:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
