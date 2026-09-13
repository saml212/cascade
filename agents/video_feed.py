"""Publish a dedicated Apple-compatible video RSS feed without touching audio RSS."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

import boto3
import httpx
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from agents.base import BaseAgent
from agents.qa import normalize_podcast_explicit, quality_snapshot
from lib.delivery_video import read_render_manifest
from lib.ffprobe import file_fingerprint

VIDEO_FEED_SCHEMA = "cascade.video-podcast-feed/v1"
VIDEO_FEED_PLAN_SCHEMA = "cascade.video-podcast-feed-plan/v1"
VIDEO_FEED_KEY = "feed-video.xml"
VIDEO_MEDIA_PREFIX = "video"
VIDEO_CONTENT_TYPE = "video/mp4"
MAX_REMOTE_FEED_BYTES = 5 * 1024 * 1024
MULTIPART_CHUNK_BYTES = 64 * 1024 * 1024

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)


def _itunes(tag: str) -> str:
    return f"{{{ITUNES_NS}}}{tag}"


def _atom(tag: str) -> str:
    return f"{{{ATOM_NS}}}{tag}"


def _text(parent: ET.Element, tag: str, value: object) -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = str(value)
    return element


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _not_found(error: ClientError) -> bool:
    response = getattr(error, "response", {})
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _sha256_value(fingerprint: dict) -> str:
    value = str(fingerprint.get("id", ""))
    if not value.startswith("sha256:"):
        raise RuntimeError("Current video has no full SHA-256 fingerprint")
    return value.removeprefix("sha256:")


def _format_pub_date(value: object) -> str:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return format_datetime(parsed.astimezone(timezone.utc), usegmt=True)
        except ValueError:
            try:
                return format_datetime(
                    parsedate_to_datetime(raw).astimezone(timezone.utc), usegmt=True
                )
            except (TypeError, ValueError):
                pass
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def _item_timestamp(item: ET.Element) -> datetime:
    try:
        value = parsedate_to_datetime(item.findtext("pubDate", ""))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


@contextmanager
def _exclusive_feed_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class VideoFeedAgent(BaseAgent):
    """Publish one immutable video object and the separate video RSS feed."""

    name = "video_feed"

    def _current_inputs(self, *, require_publish_approval: bool) -> dict:
        episode = self.load_json("episode.json")
        snapshot = quality_snapshot(self.episode_dir, config=self.config)
        quality = snapshot.get("quality", {})
        gate = snapshot.get("release_gate", {})
        approvals = snapshot.get("approvals", {})
        artifacts = snapshot.get("artifacts", {})
        video_plan = gate.get("publish_plan", {}).get("video_podcast_rss", {})

        if video_plan.get("enabled") is not True:
            raise RuntimeError("platforms.video_podcast_rss.enabled is not true")
        if video_plan.get("format") != "video":
            raise RuntimeError("Approved publication plan is not a video RSS plan")
        if not video_plan.get("destination_configured"):
            raise RuntimeError("Video podcast R2 destination is not configured")
        if not video_plan.get("channel_configured"):
            raise RuntimeError("Video podcast channel metadata is incomplete")
        if not video_plan.get("episode_configured"):
            raise RuntimeError("Video podcast episode metadata is incomplete")
        if quality.get("status") != "passed" or quality.get(
            "report_revision"
        ) != quality.get("current_revision"):
            raise RuntimeError("Current QA report has not passed")
        if approvals.get("editorial", {}).get("current") is not True:
            raise RuntimeError("Current longform editorial approval is required")
        if artifacts.get("release_video", {}).get("ready") is not True:
            raise RuntimeError("Current canonical upload video is required")
        if gate.get("can_approve_publish") is not True:
            reasons = "; ".join(
                str(item.get("message", ""))
                for item in gate.get("blockers", [])
                if item.get("message")
            )
            raise RuntimeError(f"Release prerequisites are not satisfied: {reasons}")
        if require_publish_approval:
            approval = episode.get("publish_approval")
            if (
                approvals.get("publish", {}).get("current") is not True
                or not isinstance(approval, dict)
                or approval.get("revision") != gate.get("revision")
                or gate.get("safe") is not True
            ):
                raise RuntimeError(
                    "Current revision-bound publish approval is required"
                )

        podcast = self.config.get("podcast", {})
        r2 = podcast.get("r2", {})
        title = episode.get("title") or episode.get("episode_name", "")
        description = episode.get("description") or episode.get(
            "episode_description", ""
        )
        explicit = normalize_podcast_explicit(
            episode.get("video_explicit", podcast.get("explicit", "false"))
        )
        channel_explicit = normalize_podcast_explicit(podcast.get("explicit", "false"))
        if explicit is None or channel_explicit is None:
            raise RuntimeError("Podcast explicit value must be true or false")

        video = self.episode_dir / "upload_video.mp4"
        record = read_render_manifest(self.episode_dir).get("longform", {})
        try:
            stat = video.stat()
            recorded_output = record["output"]
            render_fingerprint = str(record["fingerprint"])
            duration = float(
                recorded_output.get("duration_seconds")
                or record["output_duration_seconds"]
            )
        except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("Current video render proof is incomplete") from exc
        if (
            record.get("path") != video.name
            or record.get("render_mode") != "speaker_cut"
            or not render_fingerprint
            or recorded_output.get("size_bytes") != stat.st_size
            or recorded_output.get("mtime_ns") != stat.st_mtime_ns
            or stat.st_size <= 0
            or duration <= 0
        ):
            raise RuntimeError(
                "Current video render proof does not match upload_video.mp4"
            )

        content_fingerprint = file_fingerprint(video)
        content_sha256 = _sha256_value(content_fingerprint)
        episode_id = str(episode.get("episode_id") or self.episode_dir.name)
        prior_receipt = self.load_json_safe("video_feed.json")
        prior_pub_date = None
        if (
            prior_receipt.get("schema") == VIDEO_FEED_SCHEMA
            and prior_receipt.get("status") == "published"
            and prior_receipt.get("episode_id") == episode_id
        ):
            prior_pub_date = prior_receipt.get("episode", {}).get(
                "pub_date"
            ) or prior_receipt.get("published_at")
        public_url = str(r2.get("public_url", "")).rstrip("/")
        object_key = f"{VIDEO_MEDIA_PREFIX}/{episode_id}/{render_fingerprint}.mp4"
        return {
            "episode": episode,
            "episode_id": episode_id,
            "title": str(title),
            "description": str(description),
            "explicit": explicit,
            "pub_date": prior_pub_date,
            "guid": f"video:{episode_id}",
            "podcast": podcast,
            "channel_explicit": channel_explicit,
            "bucket": str(r2.get("bucket", "")),
            "public_url": public_url,
            "feed_url": f"{public_url}/{VIDEO_FEED_KEY}",
            "object_key": object_key,
            "video_url": f"{public_url}/{object_key}",
            "video_path": video,
            "video_size": stat.st_size,
            "duration_seconds": round(duration),
            "render_fingerprint": render_fingerprint,
            "content_sha256": content_sha256,
            "quality_revision": quality.get("current_revision"),
            "editorial_revision": approvals.get("editorial", {}).get("revision"),
            "release_revision": gate.get("revision"),
            "publish_approval_current": approvals.get("publish", {}).get("current")
            is True,
        }

    def _r2_client(self):
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
        api_token = os.getenv("CLOUDFLARE_API_TOKEN", "")
        if not account_id or not api_token:
            raise RuntimeError("Cloudflare R2 credentials are not configured")
        response = httpx.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30.0,
        )
        response.raise_for_status()
        try:
            token_id = str(response.json()["result"]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Cloudflare token verification returned no token ID"
            ) from exc
        return boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=token_id,
            aws_secret_access_key=hashlib.sha256(api_token.encode()).hexdigest(),
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"mode": "standard", "max_attempts": 5},
            ),
        )

    def _head_video_object(self, client, inputs: dict) -> dict | None:
        try:
            head = client.head_object(Bucket=inputs["bucket"], Key=inputs["object_key"])
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise RuntimeError("Could not inspect immutable video object") from exc
        metadata = {
            str(key).lower(): str(value)
            for key, value in (head.get("Metadata") or {}).items()
        }
        actual_type = str(head.get("ContentType", "")).split(";", 1)[0].lower()
        if (
            int(head.get("ContentLength", -1)) != inputs["video_size"]
            or actual_type != VIDEO_CONTENT_TYPE
            or metadata.get("sha256") != inputs["content_sha256"]
            or metadata.get("cascade-render-fingerprint")
            != inputs["render_fingerprint"]
        ):
            raise RuntimeError(
                "Immutable video object exists but does not match the current render"
            )
        return {
            "status": "ready",
            "etag": str(head.get("ETag", "")).strip('"'),
            "size_bytes": int(head["ContentLength"]),
            "content_type": actual_type,
            "sha256": metadata["sha256"],
            "render_fingerprint": metadata["cascade-render-fingerprint"],
        }

    def _ensure_video_object(self, client, inputs: dict) -> tuple[dict, bool]:
        current = self._head_video_object(client, inputs)
        if current is not None:
            return current, True
        transfer = TransferConfig(
            multipart_threshold=MULTIPART_CHUNK_BYTES,
            multipart_chunksize=MULTIPART_CHUNK_BYTES,
            max_concurrency=2,
            use_threads=True,
        )
        client.upload_file(
            str(inputs["video_path"]),
            inputs["bucket"],
            inputs["object_key"],
            ExtraArgs={
                "ContentType": VIDEO_CONTENT_TYPE,
                "Metadata": {
                    "sha256": inputs["content_sha256"],
                    "cascade-render-fingerprint": inputs["render_fingerprint"],
                },
            },
            Config=transfer,
        )
        uploaded = self._head_video_object(client, inputs)
        if uploaded is None:
            raise RuntimeError("Video upload completed without a readable object")
        return uploaded, False

    def _remote_feed(self, client, bucket: str) -> tuple[bytes | None, str | None]:
        try:
            response = client.get_object(Bucket=bucket, Key=VIDEO_FEED_KEY)
        except ClientError as exc:
            if _not_found(exc):
                return None, None
            raise RuntimeError("Could not read the existing video feed") from exc
        length = int(response.get("ContentLength", 0))
        if length > MAX_REMOTE_FEED_BYTES:
            raise RuntimeError("Existing video feed is unexpectedly large")
        body = response["Body"]
        try:
            data = body.read(MAX_REMOTE_FEED_BYTES + 1)
        finally:
            body.close()
        if len(data) > MAX_REMOTE_FEED_BYTES:
            raise RuntimeError("Existing video feed is unexpectedly large")
        return data, str(response.get("ETag", "")).strip('"') or None

    def _historical_items(self, remote_feed: bytes | None) -> list[ET.Element]:
        if remote_feed is None:
            return []
        try:
            root = ET.fromstring(remote_feed)
            channel = root.find("channel")
        except ET.ParseError as exc:
            raise RuntimeError("Existing video feed is malformed") from exc
        if channel is None:
            raise RuntimeError("Existing video feed has no channel")
        items = [copy.deepcopy(item) for item in channel.findall("item")]
        guids = [str(item.findtext("guid", "")).strip() for item in items]
        if any(not guid for guid in guids) or len(set(guids)) != len(guids):
            raise RuntimeError("Existing video feed has missing or duplicate GUIDs")
        return items

    @staticmethod
    def _matches_current_item(item: ET.Element, inputs: dict) -> bool:
        if str(item.findtext("guid", "")).strip() == inputs["guid"]:
            return True
        enclosure = item.find("enclosure")
        url = enclosure.get("url", "") if enclosure is not None else ""
        return f"/{VIDEO_MEDIA_PREFIX}/{inputs['episode_id']}/" in url

    def _current_item(
        self, inputs: dict, *, guid: str | None = None, pub_date: str | None = None
    ) -> ET.Element:
        item = ET.Element("item")
        _text(item, "title", inputs["title"])
        _text(item, "description", inputs["description"])
        enclosure = ET.SubElement(item, "enclosure")
        enclosure.set("url", inputs["video_url"])
        enclosure.set("length", str(inputs["video_size"]))
        enclosure.set("type", VIDEO_CONTENT_TYPE)
        guid_element = _text(item, "guid", guid or inputs["guid"])
        guid_element.set("isPermaLink", "false")
        _text(item, "pubDate", pub_date or _format_pub_date(inputs["pub_date"]))
        _text(item, _itunes("duration"), inputs["duration_seconds"])
        _text(item, _itunes("explicit"), inputs["explicit"])
        _text(item, _itunes("episodeType"), "full")
        _text(item, _itunes("title"), inputs["title"])
        _text(item, _itunes("author"), inputs["podcast"]["author"])
        return item

    def _build_feed(
        self, inputs: dict, remote_feed: bytes | None
    ) -> tuple[bytes, dict]:
        historical = self._historical_items(remote_feed)
        previous_current = next(
            (item for item in historical if self._matches_current_item(item, inputs)),
            None,
        )
        retained = [
            item for item in historical if not self._matches_current_item(item, inputs)
        ]
        prior_guid = (
            str(previous_current.findtext("guid", "")).strip()
            if previous_current is not None
            else None
        )
        prior_pub_date = (
            previous_current.findtext("pubDate")
            if previous_current is not None
            else None
        )
        current = self._current_item(inputs, guid=prior_guid, pub_date=prior_pub_date)
        items = [current, *retained]
        items.sort(key=_item_timestamp, reverse=True)

        rss = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(rss, "channel")
        podcast = inputs["podcast"]
        _text(channel, "title", podcast["title"])
        _text(channel, "link", podcast["link"])
        _text(channel, "description", podcast["description"])
        _text(channel, "language", podcast.get("language", "en"))
        self_link = ET.SubElement(channel, _atom("link"))
        self_link.set("href", inputs["feed_url"])
        self_link.set("rel", "self")
        self_link.set("type", "application/rss+xml")
        _text(channel, "lastBuildDate", _format_pub_date(None))
        _text(channel, _itunes("author"), podcast["author"])
        artwork = ET.SubElement(channel, _itunes("image"))
        artwork.set("href", podcast["artwork_url"])
        category = ET.SubElement(channel, _itunes("category"))
        category.set("text", podcast.get("category", "Society & Culture"))
        _text(channel, _itunes("explicit"), inputs["channel_explicit"])
        _text(channel, _itunes("type"), "episodic")
        owner = ET.SubElement(channel, _itunes("owner"))
        _text(owner, _itunes("name"), podcast["author"])
        _text(owner, _itunes("email"), podcast["owner_email"])
        channel.extend(items)

        serialized = ET.tostring(rss, encoding="utf-8")
        pretty = minidom.parseString(serialized).toprettyxml(
            indent="  ", encoding="UTF-8"
        )
        parsed = ET.fromstring(pretty)
        output_items = parsed.find("channel").findall("item")
        output_guids = [str(item.findtext("guid", "")).strip() for item in output_items]
        if (
            len(output_items) != len(retained) + 1
            or any(not guid for guid in output_guids)
            or len(set(output_guids)) != len(output_guids)
        ):
            raise RuntimeError("Generated video feed did not preserve unique entries")
        return pretty, {
            "previous_item_count": len(historical),
            "item_count": len(output_items),
            "guid": str(current.findtext("guid", "")),
            "pub_date": str(current.findtext("pubDate", "")),
            "replaced_existing": previous_current is not None,
        }

    @staticmethod
    def _identity(inputs: dict) -> tuple:
        return (
            inputs["release_revision"],
            inputs["editorial_revision"],
            inputs["quality_revision"],
            inputs["render_fingerprint"],
            inputs["content_sha256"],
            inputs["video_size"],
            inputs["title"],
            inputs["description"],
            inputs["explicit"],
        )

    def _manifest(
        self,
        inputs: dict,
        object_state: dict | None,
        feed_state: dict,
        remote_etag: str | None,
    ) -> dict:
        return {
            "schema": VIDEO_FEED_PLAN_SCHEMA,
            "episode_id": inputs["episode_id"],
            "release_revision": inputs["release_revision"],
            "editorial_revision": inputs["editorial_revision"],
            "quality_revision": inputs["quality_revision"],
            "publish_approval_current": inputs["publish_approval_current"],
            "episode": {
                "guid": feed_state["guid"],
                "title": inputs["title"],
                "description": inputs["description"],
                "explicit": inputs["explicit"],
                "pub_date": feed_state["pub_date"],
                "duration_seconds": inputs["duration_seconds"],
            },
            "video": {
                "path": str(inputs["video_path"]),
                "object_key": inputs["object_key"],
                "url": inputs["video_url"],
                "content_type": VIDEO_CONTENT_TYPE,
                "size_bytes": inputs["video_size"],
                "render_fingerprint": inputs["render_fingerprint"],
                "sha256": inputs["content_sha256"],
                "remote": object_state,
            },
            "feed": {
                "key": VIDEO_FEED_KEY,
                "url": inputs["feed_url"],
                "remote_etag": remote_etag,
                **feed_state,
            },
        }

    def prepare(self) -> dict:
        """Build a local dry run from authoritative remote history."""
        inputs = self._current_inputs(require_publish_approval=False)
        client = self._r2_client()
        remote_feed, remote_etag = self._remote_feed(client, inputs["bucket"])
        object_state = self._head_video_object(client, inputs)
        feed_xml, feed_state = self._build_feed(inputs, remote_feed)
        manifest = self._manifest(inputs, object_state, feed_state, remote_etag)
        manifest.update(
            status="prepared",
            dry_run=True,
            prepared_at=datetime.now(timezone.utc).isoformat(),
        )
        _atomic_write(self.episode_dir / "feed-video.preview.xml", feed_xml)
        _atomic_write(
            self.episode_dir / "video_feed_plan.json",
            json.dumps(manifest, indent=2).encode(),
        )
        return manifest

    def execute(self) -> dict:
        """Publish only after revalidating media, QA, approvals, and remote history."""
        lock_path = self.episode_dir.parent / ".video-feed.lock"
        with _exclusive_feed_lock(lock_path):
            inputs = self._current_inputs(require_publish_approval=True)
            approved_identity = self._identity(inputs)
            client = self._r2_client()
            object_state, reused = self._ensure_video_object(client, inputs)

            current = self._current_inputs(require_publish_approval=True)
            if self._identity(current) != approved_identity:
                raise RuntimeError("Release inputs changed while video publication ran")
            remote_feed, remote_etag = self._remote_feed(client, current["bucket"])
            feed_xml, feed_state = self._build_feed(current, remote_feed)

            final = self._current_inputs(require_publish_approval=True)
            if self._identity(final) != approved_identity:
                raise RuntimeError(
                    "Release inputs changed before video feed publication"
                )
            self._head_video_object(client, final)
            put_args = {
                "Bucket": final["bucket"],
                "Key": VIDEO_FEED_KEY,
                "Body": feed_xml,
                "ContentType": "application/rss+xml; charset=utf-8",
            }
            if remote_etag:
                put_args["IfMatch"] = f'"{remote_etag}"'
            else:
                put_args["IfNoneMatch"] = "*"
            client.put_object(
                **put_args,
            )

            _atomic_write(self.episode_dir / VIDEO_FEED_KEY, feed_xml)
            result = self._manifest(final, object_state, feed_state, remote_etag)
            result.update(
                schema=VIDEO_FEED_SCHEMA,
                status="published",
                dry_run=False,
                video_object_reused=reused,
                published_at=datetime.now(timezone.utc).isoformat(),
            )
            return result
