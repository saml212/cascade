"""Publish agent — distribute shorts and longform via Upload-Post to all platforms.

Inputs:
    - clips.json, metadata/metadata.json
    - shorts/<clip_id>.mp4, longform.mp4
Outputs:
    - publish.json (submission results, request IDs)
Dependencies:
    - curl (Upload-Post REST API — httpx can't handle repeated platform[] fields)
Config:
    - platforms.youtube.enabled, platforms.tiktok.enabled, platforms.instagram.enabled
    - schedule.timezone, schedule.shorts_per_day_weekday, schedule.shorts_per_day_weekend
Environment:
    - UPLOAD_POST_API_KEY, UPLOAD_POST_USER
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.base import BaseAgent

UPLOAD_POST_URL = "https://api.upload-post.com/api/upload"
STATUS_URL = "https://api.upload-post.com/api/uploadposts/status"


class PublishAgent(BaseAgent):
    name = "publish"

    def execute(self) -> dict:
        api_key = os.getenv("UPLOAD_POST_API_KEY")
        if not api_key:
            raise RuntimeError("UPLOAD_POST_API_KEY not set in environment")

        user = os.getenv("UPLOAD_POST_USER", "")
        if not user:
            raise RuntimeError("UPLOAD_POST_USER not set in environment")

        clips_data = self.load_json("clips.json")
        clips = clips_data.get("clips", [])

        # Load metadata for per-platform captions
        metadata = {}
        metadata_path = self.episode_dir / "metadata" / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path) as f:
                metadata = json.load(f)

        # Load schedule from metadata
        schedule = metadata.get("schedule", [])
        clip_metadata = {m["id"]: m for m in metadata.get("clips", [])}
        longform_meta = metadata.get("longform", {})

        # Determine platforms from config
        platforms = []
        platform_cfg = self.config.get("platforms", {})
        if platform_cfg.get("youtube", {}).get("enabled"):
            platforms.append("youtube")
        if platform_cfg.get("tiktok", {}).get("enabled"):
            platforms.append("tiktok")
        if platform_cfg.get("instagram", {}).get("enabled"):
            platforms.append("instagram")
        if platform_cfg.get("x", {}).get("enabled"):
            platforms.append("x")

        if not platforms:
            raise RuntimeError("No platforms enabled in config")

        tz_name = self.config.get("schedule", {}).get("timezone", "America/Los_Angeles")
        shorts_weekday = self.config.get("schedule", {}).get("shorts_per_day_weekday", 1)
        shorts_weekend = self.config.get("schedule", {}).get("shorts_per_day_weekend", 2)

        results = []

        # === Publish shorts ===
        self.logger.info("Publishing %d shorts to %s..." % (len(clips), platforms))

        # Build schedule if metadata didn't provide one
        if not schedule:
            schedule = self._generate_schedule(clips, shorts_weekday, shorts_weekend)

        # Group schedule by clip_id for lookup
        clip_schedules = {}
        for entry in schedule:
            cid = entry.get("clip_id")
            if cid not in clip_schedules:
                clip_schedules[cid] = entry

        for i, clip in enumerate(clips):
            clip_id = clip.get("id", "")
            if clip.get("status") == "rejected":
                self.logger.info("  Skipping rejected clip %s" % clip_id)
                continue

            short_path = self.episode_dir / "shorts" / ("%s.mp4" % clip_id)
            if not short_path.exists():
                self.logger.warning("  Short not found: %s" % clip_id)
                continue

            # Get per-platform metadata — prefer inline clips.json metadata,
            # fall back to metadata.json lookup
            cmeta = clip.get("metadata", {}) or clip_metadata.get(clip_id, {})
            title = clip.get("title", "Clip %s" % clip_id)

            # Build curl command with repeated platform[] fields
            cmd = [
                "curl", "-s", "--max-time", "600",
                "-H", "Authorization: Apikey %s" % api_key,
                "-F", "video=@%s" % str(short_path),
                "-F", "user=%s" % user,
                "-F", "title=%s" % title,
                "-F", "async_upload=true",
            ]

            # Add platforms (repeated platform[] fields)
            for p in platforms:
                cmd.extend(["-F", "platform[]=%s" % p])

            # Platform-specific captions
            yt = cmeta.get("youtube", {})
            tt = cmeta.get("tiktok", {})
            ig = cmeta.get("instagram", {})
            x_meta = cmeta.get("x", {})
            li = cmeta.get("linkedin", {})
            fb = cmeta.get("facebook", {})
            th = cmeta.get("threads", {})
            pin = cmeta.get("pinterest", {})
            bs = cmeta.get("bluesky", {})

            if yt:
                cmd.extend(["-F", "youtube_title=%s" % yt.get("title", title)])
                cmd.extend(["-F", "youtube_description=%s" % yt.get("description", "")])
            if tt:
                caption = tt.get("caption", title)
                hashtags = " ".join(tt.get("hashtags", []))
                cmd.extend(["-F", "tiktok_title=%s" % ("%s %s" % (caption, hashtags)).strip()])
            if ig:
                caption = ig.get("caption", title)
                hashtags = " ".join(ig.get("hashtags", []))
                cmd.extend(["-F", "instagram_title=%s" % ("%s\n\n%s" % (caption, hashtags)).strip()])
            if x_meta:
                cmd.extend(["-F", "x_title=%s" % x_meta.get("text", title)])
            if li:
                cmd.extend(["-F", "linkedin_title=%s" % li.get("title", title)])
                cmd.extend(["-F", "linkedin_description=%s" % li.get("description", "")])
            if fb:
                cmd.extend(["-F", "facebook_title=%s" % fb.get("title", title)])
            if th:
                cmd.extend(["-F", "threads_title=%s" % th.get("text", title)])
            if pin:
                cmd.extend(["-F", "pinterest_title=%s" % pin.get("title", title)])
                cmd.extend(["-F", "pinterest_description=%s" % pin.get("description", "")])
            if bs:
                cmd.extend(["-F", "bluesky_title=%s" % bs.get("text", title)])

            # Scheduling
            sched = clip_schedules.get(clip_id)
            if sched:
                scheduled_dt = self._schedule_to_datetime(sched, tz_name)
                cmd.extend(["-F", "scheduled_date=%s" % scheduled_dt.isoformat()])
                cmd.extend(["-F", "timezone=%s" % tz_name])

            cmd.extend(["-X", "POST", UPLOAD_POST_URL])

            size_mb = short_path.stat().st_size / 1e6
            self.logger.info("  Uploading %s (%.1f MB)..." % (clip_id, size_mb))
            self.report_progress(i + 1, len(clips), "Uploading %s" % clip_id)

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if proc.returncode != 0:
                    results.append({
                        "clip_id": clip_id,
                        "status": "failed",
                        "error": "curl error: %s" % proc.stderr[:500],
                    })
                    self.logger.error("  %s curl failed: %s" % (clip_id, proc.stderr[:200]))
                    continue

                resp_data = json.loads(proc.stdout)
                request_id = resp_data.get("request_id", resp_data.get("job_id", ""))
                results.append({
                    "clip_id": clip_id,
                    "status": "submitted",
                    "platforms": platforms,
                    "request_id": request_id,
                    "scheduled": sched is not None,
                })
                self.logger.info("  %s submitted (id: %s)" % (clip_id, request_id))

            except subprocess.TimeoutExpired:
                results.append({
                    "clip_id": clip_id,
                    "status": "failed",
                    "error": "Upload timed out (600s)",
                })
                self.logger.error("  %s upload timed out" % clip_id)
            except (json.JSONDecodeError, Exception) as e:
                results.append({
                    "clip_id": clip_id,
                    "status": "failed",
                    "error": str(e),
                })
                self.logger.error("  %s failed: %s" % (clip_id, e))

        # === Publish longform to YouTube ===
        longform_path = self.episode_dir / "longform.mp4"
        longform_result = None
        if longform_path.exists() and "youtube" in platforms:
            self.logger.info("Uploading longform to YouTube...")
            lf_title = longform_meta.get("title", "Podcast Episode")
            lf_desc = longform_meta.get("description", "")
            lf_tags = longform_meta.get("tags", [])

            cmd = [
                "curl", "-s", "--max-time", "1200",
                "-H", "Authorization: Apikey %s" % api_key,
                "-F", "video=@%s" % str(longform_path),
                "-F", "user=%s" % user,
                "-F", "title=%s" % lf_title,
                "-F", "platform[]=youtube",
                "-F", "async_upload=true",
                "-F", "youtube_title=%s" % lf_title,
                "-F", "youtube_description=%s" % lf_desc,
            ]
            if lf_tags:
                cmd.extend(["-F", "tags=%s" % ",".join(lf_tags)])

            cmd.extend(["-X", "POST", UPLOAD_POST_URL])

            size_mb = longform_path.stat().st_size / 1e6
            self.logger.info("  Longform: %.0f MB" % size_mb)

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
                if proc.returncode != 0:
                    longform_result = {
                        "status": "failed",
                        "error": "curl error: %s" % proc.stderr[:500],
                    }
                    self.logger.error("  Longform curl failed")
                else:
                    resp_data = json.loads(proc.stdout)
                    longform_result = {
                        "status": "submitted",
                        "platform": "youtube",
                        "request_id": resp_data.get("request_id", resp_data.get("job_id", "")),
                    }
                    self.logger.info("  Longform submitted")
            except subprocess.TimeoutExpired:
                longform_result = {"status": "failed", "error": "Upload timed out (1200s)"}
                self.logger.error("  Longform upload timed out")
            except Exception as e:
                longform_result = {"status": "failed", "error": str(e)}
                self.logger.error("  Longform failed: %s" % e)

        submitted = sum(1 for r in results if r["status"] == "submitted")
        failed = sum(1 for r in results if r["status"] == "failed")

        return {
            "shorts": results,
            "longform": longform_result,
            "shorts_submitted": submitted,
            "shorts_failed": failed,
            "platforms": platforms,
        }

    def _generate_schedule(self, clips, weekday_per_day, weekend_per_day):
        """Generate a simple schedule: assign clips to upcoming days."""
        schedule = []
        now = datetime.now(timezone.utc)
        day_offset = 1  # Start scheduling from tomorrow
        clip_idx = 0

        while clip_idx < len(clips):
            target_date = now + timedelta(days=day_offset)
            weekday = target_date.weekday()  # 0=Mon, 6=Sun
            is_weekend = weekday >= 4  # Fri=4, Sat=5, Sun=6
            slots = weekend_per_day if is_weekend else weekday_per_day

            for slot in range(slots):
                if clip_idx >= len(clips):
                    break
                clip = clips[clip_idx]
                time_slot = "morning" if slot == 0 else "evening"
                schedule.append({
                    "clip_id": clip.get("id"),
                    "platform": "all",
                    "day_offset": day_offset,
                    "time_slot": time_slot,
                })
                clip_idx += 1
            day_offset += 1

        return schedule

    def _schedule_to_datetime(self, sched, tz_name):
        """Convert a schedule entry to an ISO datetime."""
        now = datetime.now(timezone.utc)
        day_offset = sched.get("day_offset", 0)
        time_slot = sched.get("time_slot", "morning")

        target_date = now + timedelta(days=day_offset)

        # Map time slots to hours
        hour_map = {"morning": 9, "afternoon": 14, "evening": 18}
        hour = hour_map.get(time_slot, 12)

        return target_date.replace(hour=hour, minute=0, second=0, microsecond=0)
