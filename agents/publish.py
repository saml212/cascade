"""Publish agent — distribute shorts and longform via Upload-Post to all platforms.

Inputs:
    - clips.json, metadata/metadata.json
    - shorts/<clip_id>.mp4, longform.mp4
Outputs:
    - publish.json (submission results, request IDs)
Dependencies:
    - httpx (Upload-Post REST API)
Config:
    - platforms.youtube.enabled, platforms.tiktok.enabled, platforms.instagram.enabled
    - schedule.timezone, schedule.shorts_per_day_weekday, schedule.shorts_per_day_weekend
Environment:
    - UPLOAD_POST_API_KEY, UPLOAD_POST_USER
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from agents.base import BaseAgent

UPLOAD_POST_URL = "https://api.upload-post.com/api/upload_videos"
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
        # X is always included if we have Upload-Post
        platforms.append("x")

        tz_name = self.config.get("schedule", {}).get("timezone", "America/Los_Angeles")
        shorts_weekday = self.config.get("schedule", {}).get("shorts_per_day_weekday", 1)
        shorts_weekend = self.config.get("schedule", {}).get("shorts_per_day_weekend", 2)

        headers = {"Authorization": f"Apikey {api_key}"}

        results = []

        # === Publish shorts ===
        self.logger.info(f"Publishing {len(clips)} shorts to {platforms}...")

        # Build schedule if metadata didn't provide one
        if not schedule:
            schedule = self._generate_schedule(clips, shorts_weekday, shorts_weekend)

        # Group schedule by clip_id for lookup
        clip_schedules = {}
        for entry in schedule:
            cid = entry.get("clip_id")
            if cid not in clip_schedules:
                clip_schedules[cid] = entry

        for clip in clips:
            clip_id = clip.get("id", "")
            if clip.get("status") == "rejected":
                self.logger.info(f"  Skipping rejected clip {clip_id}")
                continue

            short_path = self.episode_dir / "shorts" / f"{clip_id}.mp4"
            if not short_path.exists():
                self.logger.warning(f"  Short not found: {clip_id}")
                continue

            # Get per-platform metadata
            cmeta = clip_metadata.get(clip_id, {})
            title = clip.get("title", f"Clip {clip_id}")

            # Build platform-specific fields
            data = {
                "user": user,
                "title": title,
                "async_upload": "true",
            }

            # Add platforms
            for i, p in enumerate(platforms):
                data[f"platform[{i}]"] = p

            # Platform-specific captions
            yt = cmeta.get("youtube", {})
            tt = cmeta.get("tiktok", {})
            ig = cmeta.get("instagram", {})

            if yt:
                data["youtube_title"] = yt.get("title", title)
                data["youtube_description"] = yt.get("description", "")
            if tt:
                caption = tt.get("caption", title)
                hashtags = " ".join(tt.get("hashtags", []))
                data["tiktok_title"] = f"{caption} {hashtags}".strip()
            if ig:
                caption = ig.get("caption", title)
                hashtags = " ".join(ig.get("hashtags", []))
                data["instagram_caption"] = f"{caption}\n\n{hashtags}".strip()

            # Scheduling
            sched = clip_schedules.get(clip_id)
            if sched:
                scheduled_dt = self._schedule_to_datetime(sched, tz_name)
                data["scheduled_date"] = scheduled_dt.isoformat()
                data["timezone"] = tz_name

            # Upload the video file
            with open(short_path, "rb") as vf:
                files = {"video": (f"{clip_id}.mp4", vf, "video/mp4")}
                self.logger.info(f"  Uploading {clip_id} ({short_path.stat().st_size / 1e6:.1f} MB)...")
                response = httpx.post(
                    UPLOAD_POST_URL,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=300.0,
                )

            if response.status_code in (200, 202):
                resp_data = response.json()
                request_id = resp_data.get("request_id", resp_data.get("job_id", ""))
                results.append({
                    "clip_id": clip_id,
                    "status": "submitted",
                    "platforms": platforms,
                    "request_id": request_id,
                    "scheduled": data.get("scheduled_date"),
                })
                self.logger.info(f"  {clip_id} submitted (id: {request_id})")
            else:
                results.append({
                    "clip_id": clip_id,
                    "status": "failed",
                    "error": response.text,
                    "status_code": response.status_code,
                })
                self.logger.error(f"  {clip_id} failed: {response.status_code} {response.text}")

        # === Publish longform to YouTube ===
        longform_path = self.episode_dir / "longform.mp4"
        longform_result = None
        if longform_path.exists() and "youtube" in platforms:
            self.logger.info("Uploading longform to YouTube...")
            lf_title = longform_meta.get("title", "Podcast Episode")
            lf_desc = longform_meta.get("description", "")
            lf_tags = longform_meta.get("tags", [])

            data = {
                "user": user,
                "platform[0]": "youtube",
                "title": lf_title,
                "description": lf_desc,
                "youtube_title": lf_title,
                "youtube_description": lf_desc,
                "async_upload": "true",
            }
            if lf_tags:
                data["tags"] = ",".join(lf_tags)

            with open(longform_path, "rb") as vf:
                files = {"video": ("longform.mp4", vf, "video/mp4")}
                self.logger.info(f"  Longform: {longform_path.stat().st_size / 1e6:.0f} MB")
                response = httpx.post(
                    UPLOAD_POST_URL,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=600.0,
                )

            if response.status_code in (200, 202):
                resp_data = response.json()
                longform_result = {
                    "status": "submitted",
                    "platform": "youtube",
                    "request_id": resp_data.get("request_id", resp_data.get("job_id", "")),
                }
                self.logger.info(f"  Longform submitted")
            else:
                longform_result = {
                    "status": "failed",
                    "error": response.text,
                    "status_code": response.status_code,
                }
                self.logger.error(f"  Longform failed: {response.status_code}")

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
        day_offset = 0
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
