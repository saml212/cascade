"""Publish approved shorts and YouTube longform through Upload-Post.

The current release gate is checked before any external request. Scheduled
shorts are reserved against the Upload-Post calendar and local episode
receipts; the calendar lock serializes Cascade publishers on this filesystem.
"""

import fcntl
import json
import os
import subprocess
from contextlib import ExitStack, contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from agents.base import BaseAgent
from agents.qa import (
    canonical_release_metadata,
    current_funnel_urls,
    publication_identity,
    quality_snapshot,
    release_metadata_issues,
)
from lib.delivery_video import render_output_lock

UPLOAD_POST_URL = "https://api.upload-post.com/api/upload"
SCHEDULE_URL = "https://api.upload-post.com/api/uploadposts/schedule"
RECORDED_STATES = {
    "submitted",
    "published",
    "already_submitted",
    "partial_failure",
    "unknown",
}
SLOT_HOURS = {"morning": 9, "afternoon": 14, "evening": 18}


@contextmanager
def publication_lock(episodes_dir):
    """Serialize remote scheduling with receipt-bound distribution changes."""
    with (Path(episodes_dir) / ".publish-schedule.lock").open("a+") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _build_first_comment(youtube_url, spotify_url="", channel_handle=""):
    lines = [f"Full episode: {youtube_url}"]
    if spotify_url:
        lines.append(f"Listen on Spotify: {spotify_url}")
    if channel_handle:
        lines.append(channel_handle)
    return "\n".join(lines)


def _parse_time(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("datetime has no UTC offset")
    return result


def _parse_remote_schedule_time(value: str) -> datetime:
    """Parse Upload-Post calendar dates, whose offsetless values are UTC."""
    value = value.strip().replace("Z", "+00:00")
    result = datetime.fromisoformat(value)
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(microsecond=0)


class PublishAgent(BaseAgent):
    name = "publish"

    def run(self) -> dict:
        """Keep distribution selection locked until publish.json is durable."""
        with publication_lock(self.episode_dir.parent):
            self._publication_lock_held = True
            try:
                return super().run()
            finally:
                self._publication_lock_held = False

    def execute(self) -> dict:
        episode = self.load_json_safe("episode.json")
        snapshot = quality_snapshot(self.episode_dir, config=self.config)
        gate = snapshot["release_gate"]
        if not gate["safe"]:
            reasons = "; ".join(item["message"] for item in gate["blockers"])
            raise RuntimeError(f"release gate blocked — {reasons}")

        api_key = os.getenv("UPLOAD_POST_API_KEY")
        user = os.getenv("UPLOAD_POST_USER", "")
        if not api_key:
            raise RuntimeError("UPLOAD_POST_API_KEY not set in environment")
        if not user:
            raise RuntimeError("UPLOAD_POST_USER not set in environment")

        clips = self.load_json("clips.json").get("clips", [])
        approved = [clip for clip in clips if clip.get("status") == "approved"]
        metadata = canonical_release_metadata(self.episode_dir, episode, clips)
        issues = release_metadata_issues(metadata, approved, self.config)
        if issues:
            issue = issues[0]
            subject = (
                f"{issue['clip_id']} {issue['platform']} copy"
                if issue["scope"] == "clip"
                else f"longform {issue['platform']} copy"
            )
            raise RuntimeError(f"{subject} is missing: {', '.join(issue['fields'])}")

        platform_config = self.config.get("platforms", {})
        platforms = [
            name
            for name in ("youtube", "tiktok", "instagram", "x")
            if platform_config.get(name, {}).get("enabled")
        ]
        if not platforms:
            raise RuntimeError("No platforms enabled in config")

        revision = gate["revision"]
        try:
            previous = self.load_json("publish.json")
        except FileNotFoundError:
            previous = {}
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Cannot inspect the current publish receipt; nothing was submitted"
            ) from error
        if not isinstance(previous, dict):
            raise TypeError(
                "Cannot inspect the current publish receipt; nothing was submitted"
            )
        previous_shorts = self._validated_previous_shorts(previous)
        funnel_urls = current_funnel_urls(
            episode,
            episode_dir=self.episode_dir,
            editorial_revision_value=snapshot["approvals"]["editorial"]["revision"],
            quality_revision_value=snapshot["quality"]["current_revision"],
        )
        longform_revision = snapshot["approvals"]["editorial"]["revision"]
        self._bind_legacy_funnel_urls(episode, funnel_urls, longform_revision)
        youtube_url = funnel_urls["youtube"]
        short_metadata = {
            str(item["id"]): item
            for item in metadata.get("clips", [])
            if isinstance(item, dict) and item.get("id")
        }
        short_versions = gate.get("short_versions")
        if not isinstance(short_versions, dict) or any(
            not isinstance(short_versions.get(str(clip.get("id", ""))), dict)
            or short_versions[str(clip.get("id", ""))].get("current") is not True
            or short_versions[str(clip.get("id", ""))].get("approval_current")
            is not True
            for clip in approved
        ):
            raise RuntimeError(
                "The selected short versions are unavailable or unapproved"
            )
        with self._publication_output_locks(approved, short_versions):
            live_snapshot = quality_snapshot(self.episode_dir, config=self.config)
            live_gate = live_snapshot["release_gate"]
            if (
                live_gate.get("safe") is not True
                or live_gate.get("revision") != revision
                or live_snapshot.get("approvals", {})
                .get("editorial", {})
                .get("revision")
                != longform_revision
            ):
                raise RuntimeError(
                    "Release media, copy, or approvals changed after publication "
                    "started; nothing was submitted"
                )
            longform = self._publish_longform(
                episode,
                metadata.get("longform", {}),
                previous.get("longform"),
                revision,
                longform_revision,
                youtube_url,
                platforms,
                api_key,
                user,
            )
            if "youtube" in platforms and not youtube_url:
                state = (
                    longform.get("status") if isinstance(longform, dict) else "failed"
                )
                if state in {
                    "submitted",
                    "unknown",
                    "published",
                    "already_submitted",
                }:
                    publish_status = "waiting_for_longform_publication"
                    reason = (
                        "The YouTube longform submission is awaiting a public URL."
                        if state != "unknown"
                        else "The YouTube longform submission outcome is unknown."
                    )
                    next_action = (
                        f"Poll POST /api/episodes/{self.episode_dir.name}/"
                        "check-upload-urls, then rerun publish after it records the "
                        "current public URL."
                    )
                else:
                    publish_status = "longform_failed"
                    reason = "The YouTube longform submission failed."
                    next_action = (
                        "Resolve the reported longform error and rerun the current "
                        "approved publish job."
                    )
                result = self._result(
                    [],
                    longform,
                    platforms,
                    revision,
                    user,
                    reason,
                    previous_shorts,
                )
                result.update(
                    publish_status=publish_status,
                    action_required=True,
                    next_action=next_action,
                )
                return result
            shorts = self._publish_shorts(
                approved,
                short_versions,
                short_metadata,
                metadata.get("schedule", []),
                previous_shorts,
                episode,
                platforms,
                youtube_url,
                funnel_urls["spotify"],
                revision,
                api_key,
                user,
            )
        return self._result(
            shorts,
            longform,
            platforms,
            revision,
            user,
            previous_shorts=previous_shorts,
        )

    @contextmanager
    def _publication_output_locks(self, clips, short_versions):
        paths = {self.episode_dir / "upload_video.mp4"}
        for clip in clips:
            clip_id = str(clip.get("id", ""))
            paths.add(self.episode_dir / "shorts" / f"{clip_id}.mp4")
            version = short_versions.get(clip_id, {})
            if version.get("variant_id") is not None:
                relative_path = version.get("path")
                if not isinstance(relative_path, str):
                    raise RuntimeError(
                        f"The selected short version for {clip_id} has no media path"
                    )
                paths.add(self.episode_dir / relative_path)
        with ExitStack() as stack:
            for path in sorted(paths):
                stack.enter_context(render_output_lock(path))
            yield

    def _publish_longform(
        self,
        episode,
        metadata,
        previous,
        revision,
        longform_revision,
        youtube_url,
        platforms,
        api_key,
        user,
    ):
        if "youtube" not in platforms:
            return None
        identity = self._identity(longform_revision, "longform")
        legacy_identity = self._identity(revision, "longform")
        raw_url = episode.get("youtube_longform_url")
        if (
            raw_url
            and episode.get("youtube_longform_url_source") == "upload_post_receipt"
        ):
            recorded_revision = episode.get("youtube_longform_url_release_revision")
            recorded_editorial_revision = episode.get(
                "youtube_longform_url_editorial_revision"
            )
            recorded_identity = episode.get("youtube_longform_url_external_id")
            valid_identities = {
                publication_identity(self.episode_dir.name, bound_revision, "longform")
                for bound_revision in (
                    recorded_revision,
                    recorded_editorial_revision,
                )
                if isinstance(bound_revision, str) and bound_revision
            }
            if recorded_identity not in valid_identities:
                raise RuntimeError(
                    "The captured YouTube URL does not belong to a valid release identity"
                )
        if youtube_url:
            return {
                "status": "already_submitted",
                "platform": "youtube",
                "youtube_longform_url": youtube_url,
                "external_id": identity,
                "idempotency_key": identity,
                "editorial_revision": longform_revision,
            }
        if (
            isinstance(previous, dict)
            and previous.get("external_id") in {identity, legacy_identity}
            and previous.get("status") in RECORDED_STATES
        ):
            return {
                **previous,
                "editorial_revision": longform_revision,
                "reused_receipt": True,
            }

        path = self.episode_dir / "upload_video.mp4"
        if not path.exists():
            raise RuntimeError(
                "Current YouTube longform is missing; shorts were not submitted"
            )
        title = metadata.get("title", "Podcast Episode")
        command = self._base_command(
            path, title, ["youtube"], identity, api_key, user, 1200
        )
        command += [
            "-F",
            f"youtube_title={title}",
            "-F",
            f"youtube_description={metadata.get('description', '')}",
        ]
        if metadata.get("tags"):
            command += ["-F", f"tags={','.join(metadata['tags'])}"]
        command += ["-X", "POST", UPLOAD_POST_URL]
        self.logger.info("Uploading longform to YouTube...")
        result = self._submit(command, 1200, identity)
        result.update(
            platform="youtube",
            editorial_revision=longform_revision,
        )
        return result

    def _bind_legacy_funnel_urls(
        self, episode: dict, funnel_urls: dict, longform_revision: str
    ) -> None:
        changed = False
        if (
            funnel_urls["youtube"]
            and episode.get("youtube_longform_url_source")
            in {"supplied", "upload_post_receipt"}
            and episode.get("youtube_longform_url_editorial_revision") is None
        ):
            episode["youtube_longform_url_editorial_revision"] = longform_revision
            changed = True
        if (
            funnel_urls["spotify"]
            and episode.get("spotify_longform_url_editorial_revision") is None
        ):
            episode["spotify_longform_url_source"] = "supplied"
            episode["spotify_longform_url_editorial_revision"] = longform_revision
            changed = True
        if changed:
            self.save_json("episode.json", episode)

    def _publish_shorts(
        self,
        clips,
        short_versions,
        metadata,
        schedule,
        previous,
        episode,
        platforms,
        youtube_url,
        spotify_url,
        revision,
        api_key,
        user,
    ):
        previous = previous if isinstance(previous, list) else []
        results = []
        pending = []
        for clip in clips:
            clip_id = str(clip.get("id", ""))
            version = short_versions[clip_id]
            identity = self._identity(revision, "short", clip_id)
            recorded_for_clip = [
                item
                for item in previous
                if isinstance(item, dict)
                and str(item.get("clip_id", "")) == clip_id
                and item.get("status") in RECORDED_STATES
            ]
            receipt = next(
                (
                    item
                    for item in recorded_for_clip
                    if item.get("external_id") == identity
                ),
                None,
            )
            if receipt is None and recorded_for_clip:
                raise RuntimeError(
                    f"A historical publication receipt exists for {clip_id}; "
                    "create an explicit re-release identity before submitting it again"
                )
            if receipt and not self._receipt_matches_version(receipt, version):
                raise RuntimeError(
                    f"The recorded receipt for {clip_id} does not match its "
                    "selected version; nothing was submitted"
                )
            if receipt and receipt.get("status") != "unknown":
                results.append({**receipt, "reused_receipt": True})
            else:
                pending.append((clip, version, identity, receipt))
        if not pending:
            return results

        tz_name = self.get_config("schedule", "timezone", default="America/Los_Angeles")
        weekday_limit = int(
            self.get_config("schedule", "shorts_per_day_weekday", default=1)
        )
        weekend_limit = int(
            self.get_config("schedule", "shorts_per_day_weekend", default=2)
        )
        if not 1 <= weekday_limit <= 3 or not 1 <= weekend_limit <= 3:
            raise RuntimeError("Shorts-per-day limits must be between 1 and 3")
        reference = self._schedule_reference(episode, tz_name)

        with self._schedule_lock():
            occupied = self._occupied_schedule(api_key, user)
            remaining = []
            for clip, version, identity, uncertain in pending:
                matches = [
                    item for item in occupied if item.get("external_id") == identity
                ]
                existing = next(
                    (item for item in matches if item["source"] == "upload-post"),
                    matches[0] if matches else None,
                )
                if existing:
                    results.append(
                        self._scheduled_receipt(
                            clip,
                            version,
                            identity,
                            existing,
                            platforms,
                            tz_name,
                        )
                    )
                elif uncertain:
                    results.append({**uncertain, "reused_receipt": True})
                else:
                    remaining.append((clip, version, identity))

            if not schedule:
                schedule = self._generate_schedule(
                    [clip for clip, _, _ in remaining],
                    weekday_limit,
                    weekend_limit,
                    tz_name=tz_name,
                    reference=reference,
                    occupied=occupied,
                )
            schedule_by_clip = {}
            for entry in schedule if isinstance(schedule, list) else []:
                clip_id = entry.get("clip_id") if isinstance(entry, dict) else None
                if clip_id is not None:
                    clip_id = str(clip_id)
                    if clip_id in schedule_by_clip:
                        raise RuntimeError(
                            f"Schedule has duplicate entries for {clip_id}; "
                            "no shorts were submitted"
                        )
                    schedule_by_clip[clip_id] = entry
            unscheduled = [
                str(clip.get("id", ""))
                for clip, _, _ in remaining
                if str(clip.get("id", "")) not in schedule_by_clip
            ]
            if unscheduled:
                raise RuntimeError(
                    "Approved clips are missing schedule entries: "
                    + ", ".join(unscheduled)
                )

            plans = []
            reservations = list(occupied)
            for clip, version, identity in remaining:
                clip_id = str(clip.get("id", ""))
                entry = schedule_by_clip.get(clip_id)
                scheduled_at = (
                    self._schedule_to_datetime(entry, tz_name, reference=reference)
                    if entry
                    else None
                )
                if scheduled_at:
                    if _instant(scheduled_at) <= _instant(reference):
                        raise RuntimeError(
                            f"Schedule for {clip_id} is not in the future; "
                            "no shorts were submitted"
                        )
                    if _instant(scheduled_at) > _instant(
                        reference + timedelta(days=365)
                    ):
                        raise RuntimeError(
                            f"Schedule for {clip_id} is more than 365 days away; "
                            "no shorts were submitted"
                        )
                    self._reserve(
                        reservations,
                        scheduled_at,
                        identity,
                        clip_id,
                        tz_name,
                        weekday_limit,
                        weekend_limit,
                    )
                plans.append((clip, version, identity, scheduled_at))

            for index, (clip, version, identity, scheduled_at) in enumerate(plans, 1):
                clip_id = str(clip.get("id", ""))
                self.report_progress(index, len(plans), f"Uploading {clip_id}")
                result = self._submit_short(
                    clip,
                    metadata.get(clip_id, {}),
                    platforms,
                    scheduled_at,
                    youtube_url,
                    spotify_url,
                    version,
                    identity,
                    api_key,
                    user,
                )
                if result:
                    results.append(result)
        return results

    @staticmethod
    def _receipt_matches_version(receipt: dict, version: dict) -> bool:
        identity_fields = {
            "version",
            "variant_id",
            "render_fingerprint",
            "approval_revision",
        }
        present = identity_fields.intersection(receipt)
        if not present:
            return (
                version.get("version") == "base" and version.get("variant_id") is None
            )
        if present != identity_fields:
            return False
        expected = {
            "version": version.get("version"),
            "variant_id": version.get("variant_id"),
            "render_fingerprint": version.get("render_fingerprint"),
            "approval_revision": version.get("revision"),
        }
        return all(receipt.get(key) == value for key, value in expected.items())

    @staticmethod
    def _validated_previous_shorts(previous: dict) -> list[dict]:
        receipts = previous.get("shorts", [])
        if not isinstance(receipts, list) or any(
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("clip_id"), str)
            or not receipt["clip_id"]
            or receipt.get("status") not in RECORDED_STATES | {"failed"}
            for receipt in receipts
        ):
            raise RuntimeError(
                "Cannot inspect prior short publication receipts; nothing was submitted"
            )
        return receipts

    def _submit_short(
        self,
        clip,
        metadata,
        platforms,
        scheduled_at,
        youtube_url,
        spotify_url,
        version,
        identity,
        api_key,
        user,
    ):
        clip_id = str(clip.get("id", ""))
        path = self.episode_dir / version["path"]
        if not path.exists():
            self.logger.warning("Short not found: %s", clip_id)
            return None
        title = clip.get("title", f"Clip {clip_id}")
        command = self._base_command(
            path, title, platforms, identity, api_key, user, 600
        )

        youtube = metadata.get("youtube", {})
        if youtube:
            command += ["-F", f"youtube_title={youtube.get('title', title)}"]
            command += [
                "-F",
                f"youtube_description={youtube.get('description', '')}",
            ]
        tiktok = metadata.get("tiktok", {})
        if tiktok:
            caption = tiktok.get("caption", title)
            hashtags = " ".join(tiktok.get("hashtags", []))
            command += [
                "-F",
                f"tiktok_title={f'{caption} {hashtags}'.strip()}",
            ]
        instagram = metadata.get("instagram", {})
        if instagram:
            caption = instagram.get("caption", title)
            hashtags = " ".join(instagram.get("hashtags", []))
            command += [
                "-F",
                f"instagram_title={f'{caption}\n\n{hashtags}'.strip()}",
            ]
        x_copy = metadata.get("x", {})
        if x_copy:
            command += ["-F", f"x_title={x_copy.get('text', title)}"]
            command += ["-F", "x_long_text_as_post=true"]

        tz_name = self.get_config("schedule", "timezone", default="America/Los_Angeles")
        if scheduled_at:
            command += [
                "-F",
                f"scheduled_date={scheduled_at.replace(tzinfo=None).isoformat()}",
                "-F",
                f"timezone={tz_name}",
            ]
        if youtube_url and "youtube" in platforms:
            comment = _build_first_comment(
                youtube_url,
                spotify_url,
                self.config.get("podcast", {}).get("channel_handle", ""),
            )
            command += ["-F", f"youtube_first_comment={comment}"]
        command += ["-X", "POST", UPLOAD_POST_URL]

        result = self._submit(command, 600, identity)
        result.update(
            clip_id=clip_id,
            platforms=platforms,
            scheduled=scheduled_at is not None,
            version=version["version"],
            variant_id=version["variant_id"],
            render_fingerprint=version["render_fingerprint"],
            approval_revision=version["revision"],
        )
        if scheduled_at:
            result.update(
                scheduled_date=scheduled_at.isoformat(),
                timezone=tz_name,
            )
        return result

    @staticmethod
    def _base_command(path, title, platforms, identity, api_key, user, timeout):
        command = [
            "curl",
            "-sS",
            "--max-time",
            str(timeout),
            "-H",
            f"Authorization: Apikey {api_key}",
            "-H",
            f"Idempotency-Key: {identity}",
            "-F",
            f"video=@{path}",
            "-F",
            f"user={user}",
            "-F",
            f"title={title}",
            "-F",
            f"request_id={identity}",
            "-F",
            f"external_id={identity}",
            "-F",
            "async_upload=true",
        ]
        for platform in platforms:
            command += ["-F", f"platform[]={platform}"]
        return command

    @staticmethod
    def _submit(command, timeout, identity):
        base = {
            "external_id": identity,
            "idempotency_key": identity,
            "request_id": identity,
        }
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                **base,
                "status": "unknown",
                "error": f"Upload timed out ({timeout}s)",
            }
        except OSError as error:
            return {**base, "status": "failed", "error": str(error)}
        if process.returncode:
            return {
                **base,
                "status": "unknown",
                "error": f"curl error: {process.stderr[:500]}",
                "stdout": process.stdout,
            }
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError:
            return {
                **base,
                "status": "unknown",
                "error": (
                    f"non-JSON response from Upload-Post: {process.stdout[:200]}"
                ),
            }

        receipt = {**base, "response": response}
        if response.get("job_id"):
            receipt["job_id"] = response["job_id"]
        if response.get("request_id") and response["request_id"] != identity:
            receipt["server_request_id"] = response["request_id"]
        if response.get("success") is False or (
            response.get("error")
            and not response.get("request_id")
            and not response.get("job_id")
        ):
            return {
                **receipt,
                "status": "failed",
                "error": response.get("error") or response.get("message"),
            }
        platform_results = response.get("results")
        if isinstance(platform_results, dict):
            failures = {
                platform: value
                for platform, value in platform_results.items()
                if isinstance(value, dict) and value.get("success") is False
            }
            requested = {
                value.removeprefix("platform[]=")
                for value in command
                if value.startswith("platform[]=")
            }
            for platform in requested - platform_results.keys():
                failures[platform] = {
                    "success": False,
                    "error": "Upload-Post omitted this requested platform",
                }
            if failures:
                return {
                    **receipt,
                    "status": "partial_failure",
                    "error": (
                        "Upload-Post reported platform failures: "
                        + ", ".join(sorted(failures))
                    ),
                    "platform_failures": failures,
                }
            return {**receipt, "status": "published"}
        request_id = response.get("request_id", response.get("job_id"))
        if not request_id:
            return {
                **receipt,
                "status": "unknown",
                "error": response.get("error") or response.get("message"),
            }
        return {**receipt, "status": "submitted"}

    def _remote_schedule(self, api_key, user):
        command = [
            "curl",
            "-sS",
            "--fail-with-body",
            "--max-time",
            "30",
            "-H",
            f"Authorization: Apikey {api_key}",
            "--get",
            "--data-urlencode",
            f"profile_username={user}",
            SCHEDULE_URL,
        ]
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            if process.returncode:
                raise ValueError(process.stderr.strip() or process.stdout.strip())
            remote = json.loads(process.stdout).get("scheduled_posts")
            if not isinstance(remote, list):
                raise TypeError("scheduled_posts is missing")
        except (
            OSError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            raise RuntimeError(
                "Cannot inspect Upload-Post schedule; no shorts were submitted: "
                f"{error}"
            ) from error
        return remote

    def _occupied_schedule(self, api_key, user):
        remote = self._remote_schedule(api_key, user)
        records = []
        for item in remote:
            if not isinstance(item, dict):
                raise TypeError(
                    "Cannot inspect Upload-Post schedule; a job is not an object"
                )
            try:
                scheduled_at = _parse_remote_schedule_time(item["scheduled_date"])
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    "Cannot inspect Upload-Post schedule; a job has no valid time"
                ) from error
            records.append(
                {
                    "scheduled_at": scheduled_at,
                    "external_id": item.get("external_id"),
                    "job_id": item.get("job_id"),
                    "status": "submitted",
                    "source": "upload-post",
                }
            )

        for path in sorted(self.episode_dir.parent.glob("*/publish.json")):
            try:
                publish = json.loads(path.read_text())
                if not isinstance(publish, dict):
                    raise TypeError("top-level value is not an object")
                shorts = publish.get("shorts", [])
                if not isinstance(shorts, list):
                    raise TypeError("shorts is not a list")
            except (OSError, json.JSONDecodeError, TypeError) as error:
                raise RuntimeError(
                    f"Cannot inspect local publish receipt {path}; "
                    "no shorts were submitted"
                ) from error
            if publish.get("profile_username") not in (None, user):
                continue
            for item in shorts:
                if not isinstance(item, dict):
                    raise TypeError(
                        f"Cannot inspect local publish receipt {path}; "
                        "no shorts were submitted"
                    )
                if item.get("status") not in RECORDED_STATES or not item.get(
                    "scheduled"
                ):
                    continue
                try:
                    scheduled_at = _parse_time(item["scheduled_date"])
                except (AttributeError, KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(
                        f"Cannot inspect local publish receipt {path}; "
                        "a scheduled short has no valid time"
                    ) from error
                external_id = item.get("external_id")
                if external_id and any(
                    remote_item.get("external_id") == external_id
                    and _instant(remote_item["scheduled_at"]) == _instant(scheduled_at)
                    for remote_item in records
                    if remote_item["source"] == "upload-post"
                ):
                    continue
                records.append(
                    {
                        "scheduled_at": scheduled_at,
                        "external_id": external_id,
                        "job_id": item.get("job_id")
                        or item.get("server_request_id")
                        or (
                            item.get("request_id")
                            if not item.get("idempotency_key")
                            else None
                        ),
                        "status": item.get("status"),
                        "source": str(path),
                    }
                )

        unique = {}
        for index, item in enumerate(records):
            job_id = item.get("job_id")
            key = ("job", job_id) if job_id else ("record", index)
            unique.setdefault(key, item)
        return list(unique.values())

    @contextmanager
    def _schedule_lock(self):
        if getattr(self, "_publication_lock_held", False):
            yield
            return
        with publication_lock(self.episode_dir.parent):
            yield

    @staticmethod
    def _schedule_reference(episode, tz_name):
        value = (episode.get("publish_approval") or {}).get("approved_at")
        if not isinstance(value, str):
            raise TypeError("Publish approval needs an approved_at time")
        try:
            zone = ZoneInfo(tz_name)
            approved = _parse_time(value).astimezone(zone)
            now = datetime.now(zone)
            return max((approved, now), key=_instant)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "Publish approval has an invalid approved_at time"
            ) from error

    def _identity(self, revision, kind, clip_id=""):
        return publication_identity(self.episode_dir.name, revision, kind, clip_id)

    @staticmethod
    def _scheduled_receipt(clip, version, identity, existing, platforms, tz_name):
        scheduled_at = existing["scheduled_at"].astimezone(ZoneInfo(tz_name))
        receipt = {
            "clip_id": str(clip.get("id", "")),
            "status": existing.get("status", "submitted"),
            "platforms": platforms,
            "request_id": identity,
            "external_id": identity,
            "idempotency_key": identity,
            "scheduled": True,
            "scheduled_date": scheduled_at.isoformat(),
            "timezone": tz_name,
            "version": version["version"],
            "variant_id": version["variant_id"],
            "render_fingerprint": version["render_fingerprint"],
            "approval_revision": version["revision"],
            "reused_receipt": True,
            "receipt_source": existing["source"],
        }
        if existing.get("job_id"):
            receipt["job_id"] = existing["job_id"]
        return receipt

    @staticmethod
    def _reserve(
        reservations,
        scheduled_at,
        identity,
        clip_id,
        tz_name,
        weekday_limit,
        weekend_limit,
    ):
        zone = ZoneInfo(tz_name)
        local = scheduled_at.astimezone(zone)
        same_day = [
            item
            for item in reservations
            if item["scheduled_at"].astimezone(zone).date() == local.date()
        ]
        limit = weekend_limit if local.weekday() >= 4 else weekday_limit
        exact = any(
            _instant(item["scheduled_at"]) == _instant(scheduled_at)
            for item in same_day
        )
        if exact or len(same_day) >= limit:
            raise RuntimeError(
                f"Schedule collision for {clip_id} at {local.isoformat()}; "
                "no shorts were submitted"
            )
        reservations.append(
            {
                "scheduled_at": scheduled_at,
                "external_id": identity,
                "source": "current-release",
            }
        )

    def _generate_schedule(
        self,
        clips,
        weekday_per_day,
        weekend_per_day,
        *,
        tz_name="America/Los_Angeles",
        reference=None,
        occupied=(),
    ):
        zone = ZoneInfo(tz_name)
        reference = (reference or datetime.now(zone)).astimezone(zone)
        reservations = list(occupied)
        schedule = []
        clip_index = 0
        day_offset = 1
        while clip_index < len(clips):
            date = reference.date() + timedelta(days=day_offset)
            limit = weekend_per_day if date.weekday() >= 4 else weekday_per_day
            same_day = [
                item
                for item in reservations
                if item["scheduled_at"].astimezone(zone).date() == date
            ]
            slots = (
                ["morning"]
                if limit == 1
                else ["morning", "evening"]
                if limit == 2
                else ["morning", "afternoon", "evening"]
            )
            remaining = max(0, limit - len(same_day))
            for slot in slots:
                candidate = datetime.combine(
                    date, time(hour=SLOT_HOURS[slot]), tzinfo=zone
                )
                if not remaining or clip_index >= len(clips):
                    break
                if any(
                    _instant(item["scheduled_at"]) == _instant(candidate)
                    for item in same_day
                ):
                    continue
                schedule.append(
                    {
                        "clip_id": clips[clip_index].get("id"),
                        "platform": "all",
                        "day_offset": day_offset,
                        "time_slot": slot,
                    }
                )
                record = {"scheduled_at": candidate, "source": "generated"}
                reservations.append(record)
                same_day.append(record)
                clip_index += 1
                remaining -= 1
            day_offset += 1
        return schedule

    @staticmethod
    def _schedule_to_datetime(sched, tz_name, *, reference=None):
        zone = ZoneInfo(tz_name)
        if sched.get("scheduled_date"):
            supplied = _parse_time(sched["scheduled_date"])
            local = supplied.astimezone(zone)
            if supplied.utcoffset() != local.utcoffset():
                raise ValueError(
                    f"scheduled_date offset does not match timezone {tz_name}"
                )
            wall_time = local.replace(tzinfo=None)
            if (
                wall_time.replace(tzinfo=zone, fold=0).utcoffset()
                != wall_time.replace(tzinfo=zone, fold=1).utcoffset()
            ):
                raise ValueError(
                    f"scheduled_date is ambiguous or nonexistent in {tz_name}"
                )
            return local
        reference = reference or datetime.now(zone)
        if reference.tzinfo is None:
            raise ValueError("schedule reference must include a UTC offset")
        day_offset = int(sched.get("day_offset", 0))
        if day_offset < 0:
            raise ValueError("day_offset cannot be negative")
        slot = sched.get("time_slot", "morning")
        if slot not in SLOT_HOURS:
            raise ValueError(f"unknown time_slot: {slot}")
        date = reference.astimezone(zone).date() + timedelta(days=day_offset)
        hour = SLOT_HOURS[slot]
        return datetime.combine(date, time(hour=hour), tzinfo=zone)

    @staticmethod
    def _receipt_key(receipt):
        for field in ("external_id", "idempotency_key", "request_id", "job_id"):
            value = receipt.get(field) if isinstance(receipt, dict) else None
            if isinstance(value, str) and value:
                return field, value
        return None

    @classmethod
    def _merge_short_receipts(cls, current, previous):
        current = [item for item in current if isinstance(item, dict)]
        previous = previous if isinstance(previous, list) else []
        current_keys = {
            key for item in current if (key := cls._receipt_key(item)) is not None
        }
        history = []
        for item in previous:
            if not isinstance(item, dict):
                continue
            key = cls._receipt_key(item)
            if key is not None and key in current_keys:
                continue
            history.append({**item, "historical_receipt": True})
        return current + history

    @classmethod
    def _result(
        cls,
        shorts,
        longform,
        platforms,
        revision,
        user,
        deferred_reason=None,
        previous_shorts=None,
    ):
        active_shorts = shorts
        result = {
            "shorts": cls._merge_short_receipts(shorts, previous_shorts),
            "longform": longform,
            "shorts_submitted": sum(
                item.get("status") == "submitted" and not item.get("reused_receipt")
                for item in active_shorts
            ),
            "shorts_reused": sum(
                bool(item.get("reused_receipt")) for item in active_shorts
            ),
            "shorts_published": sum(
                item.get("status") == "published" for item in active_shorts
            ),
            "shorts_failed": sum(
                item.get("status") in {"failed", "partial_failure"}
                for item in active_shorts
            ),
            "shorts_unknown": sum(
                item.get("status") == "unknown" for item in active_shorts
            ),
            "platforms": platforms,
            "release_revision": revision,
            "profile_username": user,
        }
        if deferred_reason:
            result["shorts_deferred"] = True
            result["shorts_deferred_reason"] = deferred_reason
        elif result["shorts_failed"]:
            result.update(
                publish_status="shorts_failed",
                action_required=True,
                next_action=(
                    "Inspect each platform result and use Upload-Post's status or "
                    "failed-platform retry workflow. The full batch is not "
                    "automatically resubmitted."
                ),
            )
        elif result["shorts_unknown"]:
            result.update(
                publish_status="shorts_status_unknown",
                action_required=True,
                next_action=(
                    "Poll Upload-Post with the saved request_id or job_id before "
                    "attempting another submission."
                ),
            )
        elif active_shorts and all(
            item.get("reused_receipt") for item in active_shorts
        ):
            result["publish_status"] = "already_submitted"
        elif active_shorts and result["shorts_published"] == len(active_shorts):
            result["publish_status"] = "published"
        else:
            result["publish_status"] = "submitted"
        return result
