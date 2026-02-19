"""Podcast feed agent — extract audio, generate RSS feed, upload to Cloudflare R2.

Inputs:
    - longform.mp4, episode.json
Outputs:
    - podcast_audio.mp3 (extracted audio)
    - feed.xml (RSS feed, also uploaded to R2)
    - podcast_feed.json (URLs, sizes, duration)
Dependencies:
    - ffmpeg (audio extraction), ffprobe (duration), httpx (R2 upload)
Config:
    - podcast.* (title, author, artwork, etc.)
    - podcast.r2.bucket, podcast.r2.public_url
Environment:
    - CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree.ElementTree import Element, SubElement, ElementTree, tostring
from xml.dom import minidom

from agents.base import BaseAgent


class PodcastFeedAgent(BaseAgent):
    name = "podcast_feed"

    def execute(self) -> dict:
        podcast_cfg = self.config.get("podcast", {})
        r2_cfg = podcast_cfg.get("r2", {})

        # Load episode metadata
        episode = self.load_json("episode.json")
        episode_id = episode.get("episode_id", self.episode_dir.name)

        # --- Step 1: Extract audio from longform video ---
        longform_path = self.episode_dir / "longform.mp4"
        audio_path = self.episode_dir / "podcast_audio.mp3"

        if not longform_path.exists():
            raise FileNotFoundError(
                "longform.mp4 not found in episode directory: %s" % self.episode_dir
            )

        if audio_path.exists():
            self.logger.info("podcast_audio.mp3 already exists, skipping extraction")
        else:
            self.logger.info("Extracting audio from longform.mp4...")
            self._extract_audio(longform_path, audio_path)

        audio_size = audio_path.stat().st_size
        audio_duration = self._get_duration(audio_path)
        self.logger.info(
            "Audio: %.1f MB, %d seconds"
            % (audio_size / 1e6, audio_duration)
        )

        # --- Step 2: Upload MP3 to R2 ---
        self.logger.info("Uploading MP3 to Cloudflare R2...")
        bucket = r2_cfg.get("bucket", "")
        public_url = r2_cfg.get("public_url", "").rstrip("/")

        if not bucket:
            raise RuntimeError("podcast.r2.bucket not set in config.toml")
        if not public_url:
            raise RuntimeError("podcast.r2.public_url not set in config.toml")

        audio_key = "audio/%s.mp3" % episode_id
        self._upload_file_to_r2(bucket, audio_path, audio_key, content_type="audio/mpeg")
        audio_url = "%s/%s" % (public_url, audio_key)
        self.logger.info("MP3 uploaded: %s" % audio_url)

        # --- Step 3: Build and upload RSS feed ---
        self.logger.info("Building RSS feed with all episodes...")
        episodes_root = self.episode_dir.parent  # Parent dir contains all episodes
        all_episodes = self._collect_all_episodes(episodes_root, podcast_cfg)

        # Require episode_name before publishing to RSS
        ep_title = episode.get("episode_name", "") or episode.get("title", "")
        if not ep_title:
            raise RuntimeError(
                "Episode name is required before publishing to podcast feed. "
                "Set it in the UI or episode.json."
            )

        # Update/add the current episode's podcast data
        current_ep = {
            "episode_id": episode_id,
            "title": ep_title,
            "description": episode.get("episode_description", "") or self._get_episode_description(episode),
            "audio_url": audio_url,
            "audio_size": audio_size,
            "duration_seconds": int(audio_duration),
            "pub_date": episode.get("created_at", datetime.now(timezone.utc).isoformat()),
        }

        # Replace existing entry for this episode or append
        found = False
        for i, ep in enumerate(all_episodes):
            if ep["episode_id"] == episode_id:
                all_episodes[i] = current_ep
                found = True
                break
        if not found:
            all_episodes.append(current_ep)

        # Sort by pub_date descending (newest first)
        all_episodes.sort(key=lambda e: e.get("pub_date", ""), reverse=True)

        feed_xml = self._build_feed_xml(podcast_cfg, all_episodes)

        # Write feed locally for reference
        local_feed = self.episode_dir / "feed.xml"
        local_feed.write_text(feed_xml, encoding="utf-8")

        # Upload feed.xml to R2
        self._upload_to_r2(
            bucket,
            "feed.xml",
            feed_xml.encode("utf-8"),
            content_type="application/rss+xml; charset=utf-8",
        )
        feed_url = "%s/feed.xml" % public_url
        self.logger.info("Feed uploaded: %s" % feed_url)

        # --- Step 4: Save podcast_feed.json in episode directory ---
        result = {
            "audio_url": audio_url,
            "feed_url": feed_url,
            "audio_size_bytes": audio_size,
            "duration_seconds": int(audio_duration),
            "episode_id": episode_id,
            "total_episodes_in_feed": len(all_episodes),
        }

        return result

    # ---- Audio extraction ----

    def _extract_audio(self, video_path, audio_path):
        # type: (Path, Path) -> None
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            "-ar", "44100",
            str(audio_path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            raise RuntimeError(
                "ffmpeg audio extraction failed: %s" % result.stderr[-500:]
            )

    def _get_duration(self, audio_path):
        # type: (Path) -> float
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(audio_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        return float(info.get("format", {}).get("duration", 0))

    # ---- Cloudflare R2 REST API helpers ----

    def _upload_to_r2(self, bucket, key, data, content_type="application/octet-stream"):
        # type: (str, str, bytes, str) -> None
        """Upload bytes to Cloudflare R2 via the Cloudflare REST API.

        Uses: PUT /client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects/{key}
        """
        import httpx

        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
        api_token = os.getenv("CLOUDFLARE_API_TOKEN", "")

        if not account_id:
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID not set in .env")
        if not api_token:
            raise RuntimeError(
                "CLOUDFLARE_API_TOKEN not set in .env — "
                "create one at https://dash.cloudflare.com/profile/api-tokens "
                "with Account > R2 Storage > Edit permission"
            )

        url = "https://api.cloudflare.com/client/v4/accounts/%s/r2/buckets/%s/objects/%s" % (
            account_id, bucket, key,
        )

        resp = httpx.put(
            url,
            content=data,
            headers={
                "Authorization": "Bearer %s" % api_token,
                "Content-Type": content_type,
            },
            timeout=600.0,
        )

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                "R2 upload failed (HTTP %d): %s" % (resp.status_code, resp.text[:500])
            )

    def _upload_file_to_r2(self, bucket, local_path, key, content_type="application/octet-stream"):
        # type: (str, Path, str, str) -> None
        """Upload a file to R2 by reading it into memory."""
        data = Path(local_path).read_bytes()
        self._upload_to_r2(bucket, key, data, content_type)

    # ---- Episode collection ----

    def _collect_all_episodes(self, episodes_root, podcast_cfg):
        # type: (Path, dict) -> List[Dict]
        """Scan all episode directories for podcast_feed.json to build the full feed."""
        episodes = []
        r2_public_url = podcast_cfg.get("r2", {}).get("public_url", "").rstrip("/")

        if not episodes_root.is_dir():
            return episodes

        for ep_dir in sorted(episodes_root.iterdir()):
            if not ep_dir.is_dir():
                continue

            # Skip the current episode (we'll add it fresh)
            if ep_dir == self.episode_dir:
                continue

            # Check for existing podcast_feed.json (from a prior run)
            feed_json = ep_dir / "podcast_feed.json"
            if feed_json.exists():
                try:
                    data = json.loads(feed_json.read_text())
                    ep_json_path = ep_dir / "episode.json"
                    ep_data = {}
                    if ep_json_path.exists():
                        ep_data = json.loads(ep_json_path.read_text())

                    episodes.append({
                        "episode_id": data.get("episode_id", ep_dir.name),
                        "title": ep_data.get("episode_name", "") or ep_data.get("title", "") or ep_dir.name,
                        "description": ep_data.get("episode_description", "") or self._get_episode_description(ep_data),
                        "audio_url": data.get("audio_url", ""),
                        "audio_size": data.get("audio_size_bytes", 0),
                        "duration_seconds": data.get("duration_seconds", 0),
                        "pub_date": ep_data.get("created_at", ""),
                    })
                except (json.JSONDecodeError, KeyError):
                    self.logger.warning("Skipping malformed podcast_feed.json in %s" % ep_dir.name)
                    continue

        return episodes

    def _get_episode_description(self, episode):
        # type: (dict) -> str
        """Extract a description from episode metadata if available."""
        # Try metadata/metadata.json for the longform description
        if episode:
            ep_id = episode.get("episode_id", "")
            if ep_id:
                meta_path = self.episode_dir.parent / ep_id / "metadata" / "metadata.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                        desc = meta.get("longform", {}).get("description", "")
                        if desc:
                            return desc
                    except (json.JSONDecodeError, KeyError):
                        pass

        return episode.get("title", "") if episode else ""

    # ---- RSS feed generation ----

    def _build_feed_xml(self, podcast_cfg, episodes):
        # type: (dict, List[Dict]) -> str
        """Generate an Apple Podcasts + Spotify compliant RSS XML feed."""
        ITUNES_NS = "http://www.itunes.apple.com/dtds/podcast-1.0.dtd"
        CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

        rss = Element("rss")
        rss.set("version", "2.0")
        rss.set("xmlns:itunes", ITUNES_NS)
        rss.set("xmlns:content", CONTENT_NS)

        channel = SubElement(rss, "channel")

        # Channel metadata from config
        title = podcast_cfg.get("title", "My Podcast")
        description = podcast_cfg.get("description", "")
        author = podcast_cfg.get("author", "")
        artwork_url = podcast_cfg.get("artwork_url", "")
        language = podcast_cfg.get("language", "en")
        category = podcast_cfg.get("category", "Technology")
        explicit = str(podcast_cfg.get("explicit", "false")).lower()
        link = podcast_cfg.get("link", "")

        self._add_text_element(channel, "title", title)
        self._add_text_element(channel, "link", link)
        self._add_text_element(channel, "description", description)
        self._add_text_element(channel, "language", language)

        itunes_author = SubElement(channel, "itunes:author")
        itunes_author.text = author

        itunes_image = SubElement(channel, "itunes:image")
        itunes_image.set("href", artwork_url)

        itunes_category = SubElement(channel, "itunes:category")
        itunes_category.set("text", category)

        itunes_explicit = SubElement(channel, "itunes:explicit")
        itunes_explicit.text = explicit

        itunes_owner = SubElement(channel, "itunes:owner")
        owner_name = SubElement(itunes_owner, "itunes:name")
        owner_name.text = author
        owner_email_val = podcast_cfg.get("owner_email", "")
        if owner_email_val:
            owner_email = SubElement(itunes_owner, "itunes:email")
            owner_email.text = owner_email_val

        # Episodes
        for ep in episodes:
            item = SubElement(channel, "item")

            self._add_text_element(item, "title", ep.get("title", ""))
            self._add_text_element(item, "description", ep.get("description", ""))

            enclosure = SubElement(item, "enclosure")
            enclosure.set("url", ep.get("audio_url", ""))
            enclosure.set("length", str(ep.get("audio_size", 0)))
            enclosure.set("type", "audio/mpeg")

            guid = SubElement(item, "guid")
            guid.set("isPermaLink", "false")
            guid.text = ep.get("episode_id", "")

            # Format pub_date as RFC 2822
            pub_date_str = ep.get("pub_date", "")
            pub_date = self._format_rfc2822(pub_date_str)
            self._add_text_element(item, "pubDate", pub_date)

            itunes_dur = SubElement(item, "itunes:duration")
            itunes_dur.text = str(ep.get("duration_seconds", 0))

            item_explicit = SubElement(item, "itunes:explicit")
            item_explicit.text = explicit

        # Pretty-print XML
        rough_string = tostring(rss, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        xml_str = reparsed.toprettyxml(indent="  ", encoding=None)

        # minidom adds an xml declaration; ensure it's UTF-8
        if not xml_str.startswith("<?xml"):
            xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str
        else:
            # Replace minidom's declaration with explicit UTF-8
            lines = xml_str.split("\n")
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
            xml_str = "\n".join(lines)

        return xml_str

    def _add_text_element(self, parent, tag, text):
        # type: (Element, str, str) -> Element
        el = SubElement(parent, tag)
        el.text = text
        return el

    def _format_rfc2822(self, iso_str):
        # type: (str) -> str
        """Convert an ISO 8601 datetime string to RFC 2822 format."""
        if not iso_str:
            return formatdate(usegmt=True)
        try:
            # Handle various ISO formats
            iso_str = iso_str.replace("Z", "+00:00")
            if "+" not in iso_str and iso_str.endswith("00:00"):
                pass
            # Python 3.9 compatible parsing
            if "T" in iso_str:
                # Strip timezone info for fromisoformat on 3.9
                base = iso_str.split("+")[0].split("Z")[0]
                dt = datetime.fromisoformat(base).replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(iso_str).replace(tzinfo=timezone.utc)
            return formatdate(dt.timestamp(), usegmt=True)
        except (ValueError, TypeError):
            return formatdate(usegmt=True)
