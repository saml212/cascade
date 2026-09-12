"""Tests for the podcast_feed agent's RSS XML generation.

These tests pin the structural requirements of the feed XML so future edits
can't silently break Apple Podcasts / Spotify ingestion. They do NOT cover
the audio extraction or R2 upload paths — those need ffmpeg and network and
are integration-tested in the pipeline harness.

The `_build_feed_xml` method is called directly with hand-built episode
dicts so the test is hermetic.
"""

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.podcast_feed import (
    PodcastFeedAgent,
    current_podcast_audio,
    podcast_audio_input,
    podcast_source_fingerprint,
)

# Namespaces used to query elements from the produced XML
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
NS = {"itunes": ITUNES, "atom": ATOM}


def _publish_config():
    return {
        "platforms": {"podcast_rss": {"enabled": True}},
        "podcast": {
            "title": "The Local",
            "description": "Bay Area conversations.",
            "author": "Sam Larson",
            "artwork_url": "https://media.example.invalid/artwork.jpg",
            "link": "https://example.invalid",
            "owner_email": "sam@example.invalid",
            "r2": {
                "bucket": "test-bucket",
                "public_url": "https://media.example.invalid",
            },
        },
    }


def _prepare_current_audio(agent):
    mix = agent.episode_dir / "work" / "audio_mix.wav"
    mix.parent.mkdir(exist_ok=True)
    mix.write_bytes(b"wav")

    def finish_to_output(command, **_kwargs):
        Path(command[-1]).write_bytes(b"prepared mp3")
        return MagicMock(returncode=0, stderr="")

    with patch("agents.podcast_feed.subprocess.run", side_effect=finish_to_output):
        return agent.prepare_local_audio()


@pytest.fixture
def agent(tmp_path):
    """Construct an agent without running execute(). We only call helpers."""
    ep_dir = tmp_path / "episodes" / "ep_test"
    ep_dir.mkdir(parents=True)
    return PodcastFeedAgent(ep_dir, config={})


@pytest.fixture
def podcast_cfg():
    return {
        "title": "The Local",
        "description": "Bay Area conversations.",
        "author": "Sam Larson",
        "artwork_url": "https://example.r2.dev/artwork.jpg",
        "language": "en",
        "category": "Society & Culture",
        "explicit": "false",
        "link": "https://example.com",
        "owner_email": "sam@example.com",
    }


@pytest.fixture
def episodes():
    """Two episodes, oldest first — caller is expected to sort, but the
    fixture is intentionally unsorted so tests that assert ordering are
    actually meaningful."""
    return [
        {
            "episode_id": "ep_old",
            "title": "Older episode",
            "description": "Older description text.",
            "audio_url": "https://example.r2.dev/audio/ep_old.mp3",
            "audio_size": 12345678,
            "duration_seconds": 2800,
            "pub_date": "2026-04-01T12:00:00+00:00",
        },
        {
            "episode_id": "ep_new",
            "title": "Newer episode",
            "description": "Newer description text.",
            "audio_url": "https://example.r2.dev/audio/ep_new.mp3",
            "audio_size": 23456789,
            "duration_seconds": 3300,
            "pub_date": "2026-04-10T12:00:00+00:00",
        },
    ]


@pytest.fixture
def feed_xml(agent, podcast_cfg, episodes):
    # Sort newest-first the way execute() does
    sorted_eps = sorted(episodes, key=lambda e: e.get("pub_date", ""), reverse=True)
    return agent._build_feed_xml(
        podcast_cfg,
        sorted_eps,
        feed_url="https://example.r2.dev/feed.xml",
    )


@pytest.fixture
def root(feed_xml):
    return ET.fromstring(feed_xml)


@pytest.fixture
def channel(root):
    ch = root.find("channel")
    assert ch is not None, "RSS feed missing <channel>"
    return ch


class TestNamespaces:
    # ElementTree's parser consumes xmlns:* attributes into Clark-notation
    # element tags rather than leaving them on the root element, so we check
    # the raw XML string for namespace declarations.

    def test_itunes_namespace_is_canonical(self, feed_xml):
        # Apple's canonical namespace is itunes.com, NOT itunes.apple.com.
        # Spotify validators are particularly fussy about this.
        assert 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' in feed_xml
        assert "itunes.apple.com" not in feed_xml

    def test_atom_namespace_declared(self, feed_xml):
        assert 'xmlns:atom="http://www.w3.org/2005/Atom"' in feed_xml

    def test_content_namespace_declared(self, feed_xml):
        assert "xmlns:content=" in feed_xml

    def test_rss_version_2(self, root):
        assert root.attrib.get("version") == "2.0"


class TestRequiredChannelTags:
    """Tags Apple Podcasts requires at the channel level."""

    def test_title(self, channel):
        assert channel.findtext("title") == "The Local"

    def test_description(self, channel):
        assert channel.findtext("description") == "Bay Area conversations."

    def test_link(self, channel):
        assert channel.findtext("link") == "https://example.com"

    def test_language(self, channel):
        assert channel.findtext("language") == "en"

    def test_itunes_explicit(self, channel):
        assert channel.findtext("itunes:explicit", namespaces=NS) == "false"

    def test_itunes_author(self, channel):
        assert channel.findtext("itunes:author", namespaces=NS) == "Sam Larson"

    def test_itunes_image_href(self, channel):
        img = channel.find("itunes:image", namespaces=NS)
        assert img is not None
        assert img.attrib.get("href") == "https://example.r2.dev/artwork.jpg"

    def test_itunes_category(self, channel):
        cat = channel.find("itunes:category", namespaces=NS)
        assert cat is not None
        assert cat.attrib.get("text") == "Society & Culture"

    def test_itunes_owner(self, channel):
        owner = channel.find("itunes:owner", namespaces=NS)
        assert owner is not None
        assert owner.findtext("itunes:name", namespaces=NS) == "Sam Larson"
        assert owner.findtext("itunes:email", namespaces=NS) == "sam@example.com"


class TestRecommendedChannelTags:
    """Tags strongly recommended by Apple/Spotify but not strictly required."""

    def test_atom_self_link(self, channel):
        atom_link = channel.find("atom:link", namespaces=NS)
        assert atom_link is not None, (
            "Missing <atom:link rel='self'> — without this, podcatchers cannot "
            "discover the canonical feed URL for refetching updates."
        )
        assert atom_link.attrib.get("rel") == "self"
        assert atom_link.attrib.get("type") == "application/rss+xml"
        assert atom_link.attrib.get("href") == "https://example.r2.dev/feed.xml"

    def test_itunes_type_episodic(self, channel):
        # Setting this explicitly to "episodic" ensures Spotify treats the
        # newest episode as the latest, not as part of a serial sequence.
        assert channel.findtext("itunes:type", namespaces=NS) == "episodic"

    def test_last_build_date_present(self, channel):
        lbd = channel.findtext("lastBuildDate")
        assert lbd, "Missing <lastBuildDate>"


class TestEpisodeOrdering:
    def test_newest_episode_first(self, channel):
        items = channel.findall("item")
        assert len(items) == 2
        # Newest-first ordering
        assert items[0].findtext("title") == "Newer episode"
        assert items[1].findtext("title") == "Older episode"


class TestRequiredItemTags:
    """Required + recommended tags at the <item> level."""

    @pytest.fixture
    def first_item(self, channel):
        items = channel.findall("item")
        return items[0]

    def test_title(self, first_item):
        assert first_item.findtext("title") == "Newer episode"

    def test_description(self, first_item):
        assert first_item.findtext("description") == "Newer description text."

    def test_enclosure_required_attrs(self, first_item):
        enc = first_item.find("enclosure")
        assert enc is not None
        # All three attrs are required by Apple's validator
        assert enc.attrib.get("url") == "https://example.r2.dev/audio/ep_new.mp3"
        assert enc.attrib.get("length") == "23456789"
        assert enc.attrib.get("type") == "audio/mpeg"

    def test_guid_stable_not_permalink(self, first_item):
        guid = first_item.find("guid")
        assert guid is not None
        # Stable GUIDs (isPermaLink=false) prevent re-ingestion as new episodes
        # if the audio_url ever changes.
        assert guid.attrib.get("isPermaLink") == "false"
        assert guid.text == "ep_new"

    def test_pub_date_rfc2822(self, first_item):
        pub = first_item.findtext("pubDate")
        assert pub
        # RFC 2822 dates end with a timezone abbreviation like "GMT" or "+0000"
        assert "2026" in pub
        assert ("GMT" in pub) or ("+0000" in pub) or ("UTC" in pub)

    def test_itunes_duration(self, first_item):
        # Apple accepts seconds as a plain integer string.
        assert first_item.findtext("itunes:duration", namespaces=NS) == "3300"

    def test_itunes_explicit(self, first_item):
        assert first_item.findtext("itunes:explicit", namespaces=NS) == "false"

    def test_itunes_episode_type_full(self, first_item):
        # Pipeline only renders "full" episodes; trailers/bonuses would need
        # an explicit flag in episode.json. Pinning this so future code can't
        # accidentally drop the field.
        assert first_item.findtext("itunes:episodeType", namespaces=NS) == "full"

    def test_itunes_title_present(self, first_item):
        # itunes:title is the un-prefixed episode title used in Apple Podcasts
        # episode listings. It should mirror <title> for now.
        assert first_item.findtext("itunes:title", namespaces=NS) == "Newer episode"


class TestSerializationFormat:
    def test_xml_declaration_utf8(self, feed_xml):
        first_line = feed_xml.split("\n", 1)[0]
        assert first_line == '<?xml version="1.0" encoding="UTF-8"?>'

    def test_parses_as_xml(self, feed_xml):
        # Round-trip: parsing must succeed without exception.
        ET.fromstring(feed_xml)

    def test_special_chars_escaped(self, agent, podcast_cfg, episodes):
        # If a title contains XML special chars, they must be escaped, not
        # silently mangled.
        episodes[0]["title"] = "Q&A with <Sam>"
        xml_str = agent._build_feed_xml(
            podcast_cfg,
            episodes,
            feed_url="https://example.r2.dev/feed.xml",
        )
        # If escaping is broken, ET.fromstring will raise.
        root = ET.fromstring(xml_str)
        # And the round-tripped text must equal the original.
        titles = [it.findtext("title") for it in root.find("channel").findall("item")]
        assert "Q&A with <Sam>" in titles


class TestLocalAudioExport:
    def test_prefers_lossless_mix_and_uses_atomic_output(self, agent, tmp_path):
        mix = agent.episode_dir / "work" / "audio_mix.wav"
        mix.parent.mkdir()
        mix.write_bytes(b"wav")
        video = agent.episode_dir / "longform.mp4"
        video.write_bytes(b"video")
        output = agent.episode_dir / "podcast_audio.mp3"

        def finish_encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"mp3")
            return MagicMock(returncode=0, stderr="")

        with patch(
            "agents.podcast_feed.subprocess.run", side_effect=finish_encode
        ) as run:
            result = agent.prepare_local_audio(video, output)

        cmd = run.call_args.args[0]
        assert str(mix) in cmd
        assert cmd[cmd.index("-ar") + 1] == "48000"
        assert cmd[-1].endswith(".tmp.mp3")
        assert result == output
        assert output.read_bytes() == b"mp3"
        proof = json.loads(output.with_suffix(".fingerprint").read_text())
        assert proof["source_fingerprint"] == podcast_source_fingerprint(
            agent.episode_dir, {}, {}
        )

    def test_current_export_is_reused(self, agent):
        mix = agent.episode_dir / "work" / "audio_mix.wav"
        mix.parent.mkdir()
        mix.write_bytes(b"wav")
        video = agent.episode_dir / "longform.mp4"
        video.write_bytes(b"video")
        output = agent.episode_dir / "podcast_audio.mp3"

        def finish_encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"mp3")
            return MagicMock(returncode=0, stderr="")

        with patch(
            "agents.podcast_feed.subprocess.run", side_effect=finish_encode
        ) as run:
            assert agent.prepare_local_audio(video, output) == output
            assert agent.prepare_local_audio(video, output) == output
        assert run.call_count == 1

    def test_saved_edits_are_applied_to_export(self, agent):
        mix = agent.episode_dir / "work" / "audio_mix.wav"
        mix.parent.mkdir()
        mix.write_bytes(b"wav")
        video = agent.episode_dir / "longform.mp4"
        video.write_bytes(b"video")
        (agent.episode_dir / "episode.json").write_text(
            '{"longform_edits":[{"type":"trim_start","seconds":104}]}'
        )
        output = agent.episode_dir / "podcast_audio.mp3"

        def run_command(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return MagicMock(stdout='{"format":{"duration":"1000"}}')
            Path(cmd[-1]).write_bytes(b"mp3")
            return MagicMock(returncode=0, stderr="")

        with patch(
            "agents.podcast_feed.subprocess.run", side_effect=run_command
        ) as run:
            agent.prepare_local_audio(video, output)
        ffmpeg_cmd = run.call_args_list[-1].args[0]
        graph = ffmpeg_cmd[ffmpeg_cmd.index("-filter_complex") + 1]
        assert "atrim=start=104.0:end=1000.0" in graph

    def test_failed_export_does_not_replace_existing_file(self, agent):
        video = agent.episode_dir / "longform.mp4"
        video.write_bytes(b"video")
        output = agent.episode_dir / "podcast_audio.mp3"
        output.write_bytes(b"known-good")
        video.touch()

        with patch("agents.podcast_feed.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stderr="bad input")
            with pytest.raises(RuntimeError, match="audio export failed"):
                agent._extract_audio(video, output)

        assert output.read_bytes() == b"known-good"
        assert not output.with_name(output.name + ".tmp.mp3").exists()

    def test_replaced_same_stat_output_is_reencoded(self, agent):
        mix = agent.episode_dir / "work" / "audio_mix.wav"
        mix.parent.mkdir()
        mix.write_bytes(b"wav")
        output = agent.episode_dir / "podcast_audio.mp3"

        def finish_encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"good")
            return MagicMock(returncode=0, stderr="")

        with patch(
            "agents.podcast_feed.subprocess.run", side_effect=finish_encode
        ) as run:
            agent.prepare_local_audio(audio_path=output)
            recorded = output.stat()
            output.write_bytes(b"evil")
            os.utime(output, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))
            assert current_podcast_audio(agent.episode_dir, {}, {}) is None
            agent.prepare_local_audio(audio_path=output)

        assert run.call_count == 2
        assert output.read_bytes() == b"good"

    def test_legacy_longform_is_never_an_audio_source(self, agent):
        (agent.episode_dir / "longform.mp4").write_bytes(b"legacy")

        assert podcast_audio_input(agent.episode_dir, {}, {}) is None
        with pytest.raises(FileNotFoundError, match="selected/base audio"):
            agent.prepare_local_audio()

    def test_manifest_backed_canonical_video_is_edited_clock_fallback(self, agent):
        video = agent.episode_dir / "upload_video.mp4"
        video.write_bytes(b"canonical")
        stat = video.stat()
        (agent.episode_dir / "render_manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "clock": "source",
                    "shorts": {},
                    "longform": {
                        "path": video.name,
                        "render_mode": "speaker_cut",
                        "fingerprint": "render-fingerprint",
                        "output": {
                            "size_bytes": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                        },
                    },
                }
            )
        )
        (agent.episode_dir / "episode.json").write_text(
            json.dumps({"longform_edits": [{"type": "trim_start", "seconds": 10}]})
        )

        def finish_encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"mp3")
            return MagicMock(returncode=0, stderr="")

        with patch(
            "agents.podcast_feed.subprocess.run", side_effect=finish_encode
        ) as run:
            agent.prepare_local_audio()

        command = run.call_args.args[0]
        assert str(video) in command
        assert "-filter_complex" not in command
        assert current_podcast_audio(agent.episode_dir, None, {}) is None
        assert (
            current_podcast_audio(agent.episode_dir, None, {}, release_gate_safe=True)
            == agent.episode_dir / "podcast_audio.mp3"
        )


class TestPublishingGate:
    def _agent(self, tmp_path, *, config=None, episode=None):
        episode_dir = tmp_path / "episodes" / "ep_test"
        episode_dir.mkdir(parents=True)
        (episode_dir / "episode.json").write_text(
            json.dumps(
                episode
                or {
                    "episode_id": "ep_test",
                    "title": "Episode title",
                    "description": "Episode description",
                    "created_at": "2026-09-11T00:00:00+00:00",
                }
            )
        )
        return PodcastFeedAgent(episode_dir, config or _publish_config())

    def test_blocked_release_never_reaches_local_or_network_work(self, tmp_path):
        agent = self._agent(tmp_path)
        blocked = {
            "release_gate": {
                "safe": False,
                "status": "approval_required",
                "blockers": [{"message": "Approve this exact release revision."}],
            }
        }
        with (
            pytest.raises(RuntimeError, match="release gate blocked"),
            patch("agents.podcast_feed.quality_snapshot", return_value=blocked) as gate,
            patch("agents.podcast_feed.current_podcast_audio") as current_audio,
            patch.object(agent, "_upload_file_to_r2") as upload_audio,
            patch.object(agent, "_upload_to_r2") as upload_feed,
        ):
            agent.execute()

        gate.assert_called_once_with(agent.episode_dir, config=agent.config)
        current_audio.assert_not_called()
        upload_audio.assert_not_called()
        upload_feed.assert_not_called()
        assert not (agent.episode_dir / "feed.xml").exists()

    def test_disabled_rss_refuses_even_with_safe_release_snapshot(self, tmp_path):
        config = _publish_config()
        config["platforms"]["podcast_rss"]["enabled"] = False
        agent = self._agent(tmp_path, config=config)
        with (
            pytest.raises(RuntimeError, match="podcast_rss.enabled"),
            patch(
                "agents.podcast_feed.quality_snapshot",
                return_value={"release_gate": {"safe": True, "status": "ready"}},
            ),
            patch.object(agent, "_upload_file_to_r2") as upload_audio,
            patch.object(agent, "_upload_to_r2") as upload_feed,
        ):
            agent.execute()

        upload_audio.assert_not_called()
        upload_feed.assert_not_called()

    def test_stale_mp3_is_rejected_before_network(self, tmp_path):
        agent = self._agent(tmp_path)
        output = _prepare_current_audio(agent)
        recorded = output.stat()
        output.write_bytes(b"replaced mp3")
        os.utime(output, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))

        with (
            pytest.raises(RuntimeError, match="missing or stale"),
            patch(
                "agents.podcast_feed.quality_snapshot",
                return_value={"release_gate": {"safe": True, "status": "ready"}},
            ),
            patch.object(agent, "_upload_file_to_r2") as upload_audio,
            patch.object(agent, "_upload_to_r2") as upload_feed,
        ):
            agent.execute()

        upload_audio.assert_not_called()
        upload_feed.assert_not_called()

    def test_metadata_and_config_are_validated_before_network(self, tmp_path):
        config = _publish_config()
        config["podcast"]["artwork_url"] = ""
        agent = self._agent(tmp_path, config=config)
        with (
            pytest.raises(RuntimeError, match="artwork_url"),
            patch(
                "agents.podcast_feed.quality_snapshot",
                return_value={"release_gate": {"safe": True, "status": "ready"}},
            ),
            patch.object(agent, "_upload_file_to_r2") as upload_audio,
            patch.object(agent, "_upload_to_r2") as upload_feed,
        ):
            agent.execute()

        upload_audio.assert_not_called()
        upload_feed.assert_not_called()

    def test_episode_copy_is_validated_before_audio_or_network(self, tmp_path):
        agent = self._agent(
            tmp_path,
            episode={"episode_id": "ep_test", "title": "Episode", "description": ""},
        )
        with (
            pytest.raises(
                RuntimeError, match="Episode metadata is missing: description"
            ),
            patch(
                "agents.podcast_feed.quality_snapshot",
                return_value={"release_gate": {"safe": True, "status": "ready"}},
            ),
            patch("agents.podcast_feed.current_podcast_audio") as current_audio,
            patch.object(agent, "_upload_file_to_r2") as upload_audio,
            patch.object(agent, "_upload_to_r2") as upload_feed,
        ):
            agent.execute()

        current_audio.assert_not_called()
        upload_audio.assert_not_called()
        upload_feed.assert_not_called()

    def test_current_approved_audio_and_feed_upload_in_order(
        self, tmp_path, monkeypatch
    ):
        agent = self._agent(tmp_path)
        audio = _prepare_current_audio(agent)
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
        events = []

        def upload_audio(bucket, path, key, content_type):
            assert path == audio
            events.append(("audio", bucket, key, content_type))

        def upload_feed(bucket, key, data, content_type):
            assert b"Episode title" in data
            events.append(("feed", bucket, key, content_type))

        with (
            patch(
                "agents.podcast_feed.quality_snapshot",
                return_value={"release_gate": {"safe": True, "status": "ready"}},
            ),
            patch.object(agent, "_get_duration", return_value=120.5),
            patch.object(agent, "_collect_all_episodes", return_value=[]),
            patch.object(agent, "_upload_file_to_r2", side_effect=upload_audio),
            patch.object(agent, "_upload_to_r2", side_effect=upload_feed),
        ):
            result = agent.execute()

        assert [event[0] for event in events] == ["audio", "feed"]
        assert result["episode_id"] == "ep_test"
        assert result["duration_seconds"] == 120
