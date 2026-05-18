"""Stitch agent — concatenate source files into a single merged file.

Inputs:
    - ingest.json (file list from ingest agent)
Outputs:
    - source_merged.mp4 (stitched video)
    - crop_frame.jpg (frame capture for crop setup UI)
    - stitch.json (output path, duration, file count)
Dependencies:
    - ffmpeg (concat + frame extraction), ffprobe (validation)
"""

import subprocess

from agents.base import BaseAgent
from lib.ffprobe import probe as ffprobe


class StitchAgent(BaseAgent):
    name = "stitch"

    def execute(self) -> dict:
        ingest_data = self.load_json("ingest.json")
        files = ingest_data["files"]

        if len(files) == 0:
            raise ValueError("No files to stitch")

        output_path = self.episode_dir / "source_merged.mp4"

        if len(files) == 1:
            # Single file — symlink to avoid duplicating 10-20GB
            import os

            os.symlink(files[0]["dest_path"], output_path)
        else:
            # Write ffmpeg concat list
            concat_list = self.episode_dir / "work" / "concat_list.txt"
            concat_list.parent.mkdir(exist_ok=True)
            with open(concat_list, "w") as f:
                for file_info in files:
                    # Escape single quotes in path
                    safe_path = file_info["dest_path"].replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")

            # Stream-copy merge (files must have uniform codec/resolution)
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(output_path),
            ]
            self.logger.info(f"Stitching {len(files)} files...")
            subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Validate output
        probe = ffprobe(output_path)
        output_duration = float(probe["format"]["duration"])
        expected_duration = sum(f["duration_seconds"] for f in files)

        # Allow 1s tolerance for concat boundaries
        if abs(output_duration - expected_duration) > 2.0:
            self.logger.warning(
                f"Duration mismatch: expected {expected_duration:.1f}s, got {output_duration:.1f}s"
            )

        # Extract a frame for crop setup UI. The first few seconds are often
        # before everyone is seated / before the conversation starts (Christopher
        # Apr 8 had only one of two guests at t=5s), so pull from ~10% in —
        # capped at 5 min so we don't seek halfway through a 4-hour test record.
        crop_frame_path = self.episode_dir / "crop_frame.jpg"
        frame_time = min(300.0, max(5.0, output_duration * 0.10))
        try:
            frame_cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                str(frame_time),
                "-i",
                str(output_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(crop_frame_path),
            ]
            subprocess.run(frame_cmd, capture_output=True, text=True, check=True)
            self.logger.info(
                f"Extracted crop frame at {frame_time:.1f}s → crop_frame.jpg"
            )
        except subprocess.CalledProcessError as e:
            self.logger.warning(f"Failed to extract crop frame: {e}")

        result = {
            "output_path": str(output_path),
            "input_count": len(files),
            "duration_seconds": round(output_duration, 3),
            "expected_duration_seconds": round(expected_duration, 3),
        }

        # ── Camera-audio fallback ──────────────────────────────────────────
        # When no external recorder was used (DJI mics → camera direct), the
        # two wireless mics land on L/R of the camera's stereo audio. Demux
        # them into per-mic mono WAVs so speaker_cut / audio_mix / the crop
        # UI mixer see them like the H6E inputs they're standing in for.
        # Skipped when ingest already produced audio_tracks (H6E mode), and
        # gated on episode.json existing so unit tests with mocked subprocess
        # / ffprobe don't trigger the real extractor.
        if (self.episode_dir / "episode.json").exists():
            episode_data = self.load_json_safe("episode.json")
            if not episode_data.get("audio_tracks"):
                from lib.camera_audio import extract_camera_channels

                audio_dir = self.episode_dir / "audio"
                extracted = extract_camera_channels(output_path, audio_dir)
                if extracted:
                    result["audio_tracks"] = extracted

        return result
