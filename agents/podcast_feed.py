"""Podcast feed agent — extract audio, generate RSS feed, upload to Cloudflare R2.

Inputs:
    - selected/base audio or canonical upload_video.mp4, episode.json
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

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from agents.base import BaseAgent
from agents.qa import canonical_release_metadata, quality_snapshot
from lib.atomic_write import atomic_write_json
from lib.audio_mix import audio_selection_settings, selected_audio_source
from lib.delivery_video import read_render_manifest, render_artifact_state
from lib.ffprobe import file_fingerprint

PODCAST_AUDIO_PROOF_SCHEMA = "cascade.podcast-audio/v1"
PODCAST_AUDIO_ENCODE_VERSION = 3
PODCAST_AUDIO_PROCESSING_KEYS = (
    "audio_enhance",
    "audio_enhance_mode",
    "audio_target_lufs",
    "audio_target_lra",
    "audio_target_tp",
    "audio_highpass_hz",
    "audio_per_speaker_leveling",
    "audio_per_speaker_dynaudnorm",
    "audio_denoise_model",
    "use_hardware_accel",
)


def _file_identity(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _episode_data(episode_dir: Path, episode: dict | None) -> dict:
    if episode is not None:
        return episode
    try:
        return json.loads((episode_dir / "episode.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def podcast_source_fingerprint(
    episode_dir: str | Path,
    episode: dict,
    config: dict | None = None,
) -> str:
    """Fingerprint every current input that can change prepared podcast audio."""
    episode_dir = Path(episode_dir)
    paths = [episode_dir / "source_merged.mp4"]
    for track in episode.get("audio_tracks", []):
        value = track.get("dest_path") or track.get("path")
        if value:
            paths.append(Path(value))
    inputs = [_file_identity(path) for path in sorted(set(paths)) if path.exists()]
    payload = {
        "episode": {
            "audio_selection": audio_selection_settings(episode),
            "audio_tracks": episode.get("audio_tracks"),
            "duration_seconds": episode.get("duration_seconds"),
            "longform_edits": episode.get("longform_edits"),
            "source_properties": episode.get("source_properties"),
        },
        "inputs": inputs,
    }
    selected_audio = selected_audio_source(episode_dir, episode, config)
    if selected_audio is not None:
        payload["selected_audio"] = _file_identity(selected_audio)
    if config is not None:
        payload["processing"] = {
            key: config.get("processing", {}).get(key)
            for key in PODCAST_AUDIO_PROCESSING_KEYS
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def podcast_audio_input(
    episode_dir: str | Path,
    episode: dict | None = None,
    config: dict | None = None,
) -> dict | None:
    """Resolve the only media sources allowed to produce the podcast MP3."""
    episode_dir = Path(episode_dir)
    episode = _episode_data(episode_dir, episode)
    selected = selected_audio_source(episode_dir, episode, config)
    if selected is not None:
        return {"path": selected, "kind": "selected_repair", "clock": "source"}

    base_mix = episode_dir / "work" / "audio_mix.wav"
    if base_mix.is_file() and base_mix.stat().st_size > 0:
        try:
            mix_fingerprint = base_mix.with_suffix(".fingerprint").read_text().strip()
        except OSError:
            mix_fingerprint = None
        return {
            "path": base_mix,
            "kind": "base_mix",
            "clock": "source",
            "mix_fingerprint": mix_fingerprint,
        }

    # A canonical render is an edited-clock fallback. A legacy longform.mp4 is
    # deliberately excluded because it is not a release candidate.
    video = episode_dir / "upload_video.mp4"
    record = read_render_manifest(episode_dir).get("longform", {})
    if record.get("path") != video.name:
        return None
    state = render_artifact_state(
        episode_dir,
        video,
        record,
        expected_fingerprint=record.get("fingerprint"),
        expected_mode="speaker_cut",
    )
    if not state["current"]:
        return None
    return {
        "path": video,
        "kind": "canonical_longform",
        "clock": "edited",
        "render_fingerprint": record["fingerprint"],
        "requires_release_gate": True,
    }


def podcast_audio_fingerprint(
    episode_dir: str | Path,
    episode: dict | None = None,
    config: dict | None = None,
    *,
    input_record: dict | None = None,
) -> str | None:
    """Fingerprint the source, edit timeline, and fixed podcast encode policy."""
    episode_dir = Path(episode_dir)
    episode = _episode_data(episode_dir, episode)
    input_record = input_record or podcast_audio_input(episode_dir, episode, config)
    if input_record is None:
        return None
    path = Path(input_record["path"])
    payload = {
        "version": PODCAST_AUDIO_ENCODE_VERSION,
        "source_fingerprint": podcast_source_fingerprint(episode_dir, episode, config),
        "input": {
            "kind": input_record["kind"],
            "clock": input_record["clock"],
            "identity": _file_identity(path),
            "mix_fingerprint": input_record.get("mix_fingerprint"),
            "render_fingerprint": input_record.get("render_fingerprint"),
        },
        "edits": (
            episode.get("longform_edits", [])
            if input_record["clock"] == "source"
            else []
        ),
        "encoding": {"codec": "libmp3lame", "bitrate": "192k", "sample_rate": 48000},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def current_podcast_audio(
    episode_dir: str | Path,
    episode: dict | None = None,
    config: dict | None = None,
    audio_path: str | Path | None = None,
    *,
    release_gate_safe: bool = False,
    verify_content: bool = True,
) -> Path | None:
    """Return an exact current MP3; video fallbacks also require a safe release gate."""
    episode_dir = Path(episode_dir)
    episode = _episode_data(episode_dir, episode)
    audio_path = Path(audio_path or episode_dir / "podcast_audio.mp3")
    input_record = podcast_audio_input(episode_dir, episode, config)
    if (
        input_record
        and input_record.get("requires_release_gate")
        and not release_gate_safe
    ):
        return None
    expected = podcast_audio_fingerprint(
        episode_dir, episode, config, input_record=input_record
    )
    if expected is None:
        return None
    try:
        proof = json.loads(audio_path.with_suffix(".fingerprint").read_text())
        recorded_output = proof["output"]
        stat = audio_path.stat()
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError, TypeError):
        return None
    if (
        proof.get("schema") != PODCAST_AUDIO_PROOF_SCHEMA
        or proof.get("fingerprint") != expected
        or Path(recorded_output.get("path", "")).resolve() != audio_path.resolve()
        or recorded_output.get("size_bytes") != stat.st_size
        or recorded_output.get("mtime_ns") != stat.st_mtime_ns
    ):
        return None
    if not verify_content:
        return audio_path
    try:
        actual_output = file_fingerprint(audio_path)
    except OSError:
        return None
    return audio_path if actual_output == recorded_output.get("fingerprint") else None


class PodcastFeedAgent(BaseAgent):
    name = "podcast_feed"

    def _publication_inputs(self) -> dict:
        """Validate the approved release and all RSS inputs before any upload."""
        episode = self.load_json("episode.json")
        episode_id = episode.get("episode_id", self.episode_dir.name)
        gate = quality_snapshot(self.episode_dir, config=self.config)["release_gate"]
        if not gate.get("safe"):
            reasons = "; ".join(
                item.get("message", "")
                for item in gate.get("blockers", [])
                if item.get("message")
            )
            detail = f" — {reasons}" if reasons else ""
            raise RuntimeError(
                f"release gate blocked ({gate.get('status', 'unknown')}){detail}"
            )

        if (
            self.config.get("platforms", {}).get("podcast_rss", {}).get("enabled")
            is not True
        ):
            raise RuntimeError("platforms.podcast_rss.enabled is not true")

        podcast_cfg = self.config.get("podcast", {})
        r2_cfg = podcast_cfg.get("r2", {})
        bucket = r2_cfg.get("bucket", "")
        public_url = r2_cfg.get("public_url", "").rstrip("/")
        if not bucket:
            raise RuntimeError("podcast.r2.bucket not set in config.toml")
        if not public_url:
            raise RuntimeError("podcast.r2.public_url not set in config.toml")

        missing_channel = [
            field
            for field in (
                "title",
                "description",
                "author",
                "artwork_url",
                "link",
                "owner_email",
            )
            if not podcast_cfg.get(field)
        ]
        if missing_channel:
            raise RuntimeError(
                "Podcast config is missing: {}".format(", ".join(missing_channel))
            )

        metadata = canonical_release_metadata(self.episode_dir, episode, [])
        longform_metadata = metadata.get("longform", {})
        ep_title = longform_metadata.get("title")
        ep_description = longform_metadata.get("description")
        missing_episode = [
            field
            for field, value in (
                ("title", ep_title),
                ("description", ep_description),
            )
            if not value
        ]
        if missing_episode:
            raise RuntimeError(
                "Episode metadata is missing: {}".format(", ".join(missing_episode))
            )
        return {
            "episode": episode,
            "episode_id": episode_id,
            "podcast": podcast_cfg,
            "bucket": bucket,
            "public_url": public_url,
            "title": ep_title,
            "description": ep_description,
        }

    def execute(self) -> dict:
        inputs = self._publication_inputs()
        episode = inputs["episode"]
        episode_id = inputs["episode_id"]
        podcast_cfg = inputs["podcast"]
        bucket = inputs["bucket"]
        public_url = inputs["public_url"]

        audio_path = current_podcast_audio(
            self.episode_dir, episode, self.config, release_gate_safe=True
        )
        if audio_path is None:
            raise RuntimeError(
                "Podcast MP3 is missing or stale. Prepare it with "
                f"POST /api/episodes/{episode_id}/delivery/prepare before publishing."
            )
        audio_size = audio_path.stat().st_size
        audio_duration = self._get_duration(audio_path)
        self.logger.info("Audio: %.1f MB, %d seconds", audio_size / 1e6, audio_duration)

        # Build and validate the complete feed before the first network mutation.
        self.logger.info("Building RSS feed with all episodes...")
        episodes_root = self.episode_dir.parent  # Parent dir contains all episodes
        all_episodes = self._collect_all_episodes(episodes_root, podcast_cfg)
        audio_key = f"audio/{episode_id}.mp3"
        audio_url = f"{public_url}/{audio_key}"

        # Update/add the current episode's podcast data
        current_ep = {
            "episode_id": episode_id,
            "title": inputs["title"],
            "description": inputs["description"],
            "audio_url": audio_url,
            "audio_size": audio_size,
            "duration_seconds": int(audio_duration),
            "pub_date": episode.get(
                "created_at", datetime.now(timezone.utc).isoformat()
            ),
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

        feed_url = f"{public_url}/feed.xml"
        feed_xml = self._build_feed_xml(podcast_cfg, all_episodes, feed_url=feed_url)
        self._require_r2_credentials()

        # Write feed locally for reference
        local_feed = self.episode_dir / "feed.xml"
        local_feed.write_text(feed_xml, encoding="utf-8")

        self.logger.info("Uploading MP3 to Cloudflare R2...")
        self._upload_file_to_r2(
            bucket, audio_path, audio_key, content_type="audio/mpeg"
        )
        self.logger.info(f"MP3 uploaded: {audio_url}")

        self._upload_to_r2(
            bucket,
            "feed.xml",
            feed_xml.encode("utf-8"),
            content_type="application/rss+xml; charset=utf-8",
        )
        self.logger.info(f"Feed uploaded: {feed_url}")

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

    def prepare_local_audio(self, video_path=None, audio_path=None):
        """Create the upload-ready MP3 without uploading or changing the feed.

        ``video_path`` remains accepted for old callers but is never used to
        select legacy longform.mp4. Source resolution is selected repair, base
        mix, then a manifest-backed canonical upload_video.mp4.
        """
        audio_path = Path(audio_path or self.episode_dir / "podcast_audio.mp3")
        episode = self.load_json_safe("episode.json")
        input_record = podcast_audio_input(self.episode_dir, episode, self.config)
        if input_record is None:
            raise FileNotFoundError(
                "No selected/base audio or current canonical upload_video.mp4 found "
                f"in {self.episode_dir}"
            )
        source_path = Path(input_record["path"])
        fingerprint = podcast_audio_fingerprint(
            self.episode_dir,
            episode,
            self.config,
            input_record=input_record,
        )
        if (
            current_podcast_audio(self.episode_dir, episode, self.config, audio_path)
            is not None
        ):
            self.logger.info("podcast_audio.mp3 is current, skipping export")
            return audio_path

        self.logger.info(f"Encoding podcast MP3 from {source_path.name}...")
        edits = (
            episode.get("longform_edits", [])
            if input_record["clock"] == "source"
            else []
        )
        self._extract_audio(source_path, audio_path, edits=edits)
        output_fingerprint = file_fingerprint(audio_path)
        atomic_write_json(
            audio_path.with_suffix(".fingerprint"),
            {
                "schema": PODCAST_AUDIO_PROOF_SCHEMA,
                "fingerprint": fingerprint,
                "source_fingerprint": podcast_source_fingerprint(
                    self.episode_dir, episode, self.config
                ),
                "source": {
                    "kind": input_record["kind"],
                    "clock": input_record["clock"],
                    **_file_identity(source_path),
                },
                "output": {
                    "path": str(audio_path.resolve()),
                    "size_bytes": output_fingerprint["size_bytes"],
                    "mtime_ns": output_fingerprint["mtime_ns"],
                    "fingerprint": output_fingerprint,
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return audio_path

    def _extract_audio(self, video_path, audio_path, *, edits=None):
        # type: (Path, Path) -> None
        temp_path = audio_path.with_name(audio_path.name + ".tmp.mp3")
        audio_filter_args = ["-map", "0:a:0"]
        if edits:
            from lib.delivery_video import build_keep_intervals

            intervals = build_keep_intervals(self._get_duration(video_path), edits)
            chains = []
            labels = []
            for index, (start, end) in enumerate(intervals):
                chains.append(
                    f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]"
                )
                labels.append(f"[a{index}]")
            chains.append(f"{''.join(labels)}concat=n={len(intervals)}:v=0:a=1[outa]")
            audio_filter_args = ["-filter_complex", ";".join(chains), "-map", "[outa]"]
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            *audio_filter_args,
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-id3v2_version",
            "3",
            str(temp_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg audio export failed: %s" % exc) from exc
        if result.returncode != 0:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg audio export failed: %s" % result.stderr[-500:])
        temp_path.replace(audio_path)

    def _get_duration(self, audio_path):
        # type: (Path) -> float
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(audio_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        return float(info.get("format", {}).get("duration", 0))

    # ---- Cloudflare R2 REST API helpers ----

    def _require_r2_credentials(self):
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
        return account_id, api_token

    def _upload_to_r2(self, bucket, key, data, content_type="application/octet-stream"):
        # type: (str, str, bytes, str) -> None
        """Upload bytes to Cloudflare R2 via the Cloudflare REST API.

        Uses: PUT /client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects/{key}
        """
        import httpx

        account_id, api_token = self._require_r2_credentials()

        url = (
            "https://api.cloudflare.com/client/v4/accounts/%s/r2/buckets/%s/objects/%s"
            % (
                account_id,
                bucket,
                key,
            )
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

    def _upload_file_to_r2(
        self, bucket, local_path, key, content_type="application/octet-stream"
    ):
        # type: (str, Path, str, str) -> None
        """Upload a file to R2 by reading it into memory."""
        data = Path(local_path).read_bytes()
        self._upload_to_r2(bucket, key, data, content_type)

    # ---- Episode collection ----

    def _collect_all_episodes(self, episodes_root, podcast_cfg):
        # type: (Path, dict) -> List[Dict]
        """Scan all episode directories for podcast_feed.json to build the full feed."""
        episodes = []

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

                    episodes.append(
                        {
                            "episode_id": data.get("episode_id", ep_dir.name),
                            "title": ep_data.get("episode_name", "")
                            or ep_data.get("title", "")
                            or ep_dir.name,
                            "description": ep_data.get("episode_description", "")
                            or self._get_episode_description(ep_data),
                            "audio_url": data.get("audio_url", ""),
                            "audio_size": data.get("audio_size_bytes", 0),
                            "duration_seconds": data.get("duration_seconds", 0),
                            "pub_date": ep_data.get("created_at", ""),
                        }
                    )
                except (json.JSONDecodeError, KeyError):
                    self.logger.warning(
                        "Skipping malformed podcast_feed.json in %s" % ep_dir.name
                    )
                    continue

        return episodes

    def _get_episode_description(self, episode):
        # type: (dict) -> str
        """Extract a description from episode metadata if available."""
        # Try metadata/metadata.json for the longform description
        if episode:
            ep_id = episode.get("episode_id", "")
            if ep_id:
                meta_path = (
                    self.episode_dir.parent / ep_id / "metadata" / "metadata.json"
                )
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

    def _build_feed_xml(self, podcast_cfg, episodes, *, feed_url=""):
        # type: (dict, List[Dict], str) -> str
        """Generate an Apple Podcasts + Spotify compliant RSS XML feed."""
        # Canonical iTunes namespace — itunes.com NOT itunes.apple.com.
        # Spotify validators are strict; apple.com fails their ingester.
        ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
        CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
        ATOM_NS = "http://www.w3.org/2005/Atom"

        rss = Element("rss")
        rss.set("version", "2.0")
        rss.set("xmlns:itunes", ITUNES_NS)
        rss.set("xmlns:content", CONTENT_NS)
        rss.set("xmlns:atom", ATOM_NS)

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

        # Canonical feed self-link — lets podcatchers discover the feed URL for updates
        if feed_url:
            atom_link = SubElement(channel, "atom:link")
            atom_link.set("href", feed_url)
            atom_link.set("rel", "self")
            atom_link.set("type", "application/rss+xml")

        # lastBuildDate — required by many validators
        self._add_text_element(channel, "lastBuildDate", formatdate(usegmt=True))

        itunes_author = SubElement(channel, "itunes:author")
        itunes_author.text = author

        itunes_image = SubElement(channel, "itunes:image")
        itunes_image.set("href", artwork_url)

        itunes_category = SubElement(channel, "itunes:category")
        itunes_category.set("text", category)

        itunes_explicit = SubElement(channel, "itunes:explicit")
        itunes_explicit.text = explicit

        # episodic type — Spotify treats newest as latest (not a serial sequence)
        itunes_type = SubElement(channel, "itunes:type")
        itunes_type.text = "episodic"

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

            ep_title = ep.get("title", "")
            self._add_text_element(item, "title", ep_title)
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

            # itunes:episodeType — pipeline only produces full episodes
            item_ep_type = SubElement(item, "itunes:episodeType")
            item_ep_type.text = "full"

            # itunes:title — used in Apple Podcasts episode listings
            item_itunes_title = SubElement(item, "itunes:title")
            item_itunes_title.text = ep_title

        # Serialize via minidom for pretty-printing
        rough_string = tostring(rss, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        xml_str = reparsed.toprettyxml(indent="  ", encoding=None)

        # Normalise declaration to exactly: <?xml version="1.0" encoding="UTF-8"?>
        # (minidom may produce version="1.0" without encoding, or with extra whitespace)
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
