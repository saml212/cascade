"""Tests for the podcast_feed agent's RSS XML generation.

These tests pin the structural requirements of the feed XML so future edits
can't silently break Apple Podcasts / Spotify ingestion. They do NOT cover
the audio extraction or R2 upload paths — those need ffmpeg and network and
are integration-tested in the pipeline harness.

The `_build_feed_xml` method is called directly with hand-built episode
dicts so the test is hermetic.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agents.podcast_feed import PodcastFeedAgent


# Namespaces used to query elements from the produced XML
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
NS = {"itunes": ITUNES, "atom": ATOM}


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
            podcast_cfg, episodes, feed_url="https://example.r2.dev/feed.xml",
        )
        # If escaping is broken, ET.fromstring will raise.
        root = ET.fromstring(xml_str)
        # And the round-tripped text must equal the original.
        titles = [it.findtext("title") for it in root.find("channel").findall("item")]
        assert "Q&A with <Sam>" in titles
