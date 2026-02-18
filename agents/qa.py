"""QA agent — validate all pipeline outputs."""

import json
import subprocess
from pathlib import Path

from agents.base import BaseAgent


class QAAgent(BaseAgent):
    name = "qa"

    def execute(self) -> dict:
        checks = []
        warnings = []

        # === Hard checks (must pass) ===

        # 1. source_merged.mp4 exists and has audio
        merged = self.episode_dir / "source_merged.mp4"
        if merged.exists():
            probe = self._ffprobe(merged)
            duration = float(probe.get("format", {}).get("duration", 0))
            has_audio = any(
                s["codec_type"] == "audio" for s in probe.get("streams", [])
            )
            checks.append({
                "name": "source_merged_exists",
                "pass": True,
                "detail": f"Duration: {duration:.1f}s",
            })
            checks.append({
                "name": "source_merged_duration",
                "pass": duration > 60,
                "detail": f"{duration:.1f}s (min 60s)",
            })
            checks.append({
                "name": "source_merged_has_audio",
                "pass": has_audio,
                "detail": f"Audio stream: {has_audio}",
            })
        else:
            checks.append({
                "name": "source_merged_exists",
                "pass": False,
                "detail": "File not found",
            })

        # 2. longform.mp4 exists
        longform = self.episode_dir / "longform.mp4"
        if longform.exists():
            probe = self._ffprobe(longform)
            lf_duration = float(probe.get("format", {}).get("duration", 0))
            checks.append({
                "name": "longform_exists",
                "pass": True,
                "detail": f"Duration: {lf_duration:.1f}s, Size: {longform.stat().st_size / 1e6:.1f} MB",
            })
        else:
            checks.append({
                "name": "longform_exists",
                "pass": False,
                "detail": "File not found",
            })

        # 3. All shorts rendered
        clips_file = self.episode_dir / "clips.json"
        if clips_file.exists():
            clips_data = json.loads(clips_file.read_text())
            clips = clips_data.get("clips", [])
            shorts_dir = self.episode_dir / "shorts"

            rendered = []
            missing = []
            for clip in clips:
                clip_id = clip["id"]
                short_path = shorts_dir / f"{clip_id}.mp4"
                if short_path.exists():
                    rendered.append(clip_id)
                else:
                    missing.append(clip_id)

            checks.append({
                "name": "all_shorts_rendered",
                "pass": len(missing) == 0,
                "detail": f"{len(rendered)}/{len(clips)} rendered"
                + (f", missing: {missing}" if missing else ""),
            })

            # === Soft checks (warnings) ===

            # Clip durations in range
            clip_min = self.config.get("processing", {}).get("clip_min_seconds", 30)
            clip_max = self.config.get("processing", {}).get("clip_max_seconds", 90)
            for clip in clips:
                dur = clip.get("duration", 0)
                if dur < clip_min or dur > clip_max:
                    warnings.append({
                        "name": f"clip_duration_{clip['id']}",
                        "detail": f"{dur:.1f}s (expected {clip_min}-{clip_max}s)",
                    })
        else:
            checks.append({
                "name": "clips_json_exists",
                "pass": False,
                "detail": "clips.json not found",
            })

        # 4. SRT files exist
        srt_dir = self.episode_dir / "subtitles"
        transcript_srt = srt_dir / "transcript.srt"
        checks.append({
            "name": "transcript_srt_exists",
            "pass": transcript_srt.exists(),
            "detail": str(transcript_srt),
        })

        # 5. Metadata exists
        metadata_file = self.episode_dir / "metadata" / "metadata.json"
        if metadata_file.exists():
            meta = json.loads(metadata_file.read_text())
            has_longform = "longform" in meta
            has_clips = len(meta.get("clips", [])) > 0
            has_schedule = len(meta.get("schedule", [])) > 0
            checks.append({
                "name": "metadata_valid",
                "pass": has_longform and has_clips,
                "detail": f"longform={has_longform}, clips={len(meta.get('clips', []))}, schedule={len(meta.get('schedule', []))}",
            })
            if not has_schedule:
                warnings.append({
                    "name": "metadata_schedule",
                    "detail": "No publish schedule in metadata",
                })
        else:
            checks.append({
                "name": "metadata_exists",
                "pass": False,
                "detail": "metadata.json not found",
            })

        # Summarize
        hard_pass = all(c["pass"] for c in checks)
        overall = "pass" if hard_pass else "fail"

        result = {
            "overall": overall,
            "checks": checks,
            "warnings": warnings,
            "hard_checks_passed": sum(1 for c in checks if c["pass"]),
            "hard_checks_total": len(checks),
            "warning_count": len(warnings),
        }

        # Save to qa/
        qa_dir = self.episode_dir / "qa"
        qa_dir.mkdir(exist_ok=True)
        self.save_json("qa/qa.json", result)

        self.logger.info(
            f"QA: {overall.upper()} — {result['hard_checks_passed']}/{result['hard_checks_total']} checks, "
            f"{result['warning_count']} warnings"
        )
        return result

    def _ffprobe(self, path: Path) -> dict:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
