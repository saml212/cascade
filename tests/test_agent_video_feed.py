"""Safety and RSS contract tests for the dedicated video-feed agent."""

import io
import json
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from agents.video_feed import ITUNES_NS, VideoFeedAgent


def _config() -> dict:
    return {
        "platforms": {"video_podcast_rss": {"enabled": True}},
        "podcast": {
            "title": "The Local Podcast",
            "description": "A local Bay Area podcast",
            "author": "The Local Podcast",
            "artwork_url": "https://media.example/artwork.jpg",
            "language": "en",
            "category": "Society & Culture",
            "explicit": "false",
            "link": "https://example.com/show",
            "owner_email": "owner@example.com",
            "r2": {
                "bucket": "podcast",
                "public_url": "https://media.example",
            },
        },
    }


def _snapshot(*, qa=True, editorial=True, publish=True, video=True) -> dict:
    return {
        "quality": {
            "status": "passed" if qa else "stale",
            "current_revision": "sha256:quality",
            "report_revision": "sha256:quality" if qa else "sha256:old-quality",
        },
        "release_gate": {
            "safe": publish,
            "can_approve_publish": qa and editorial and video,
            "revision": "sha256:release",
            "blockers": [],
            "publish_plan": {
                "video_podcast_rss": {
                    "enabled": True,
                    "format": "video",
                    "destination_configured": True,
                    "channel_configured": True,
                    "episode_configured": True,
                }
            },
        },
        "approvals": {
            "editorial": {
                "current": editorial,
                "revision": "sha256:editorial",
            },
            "publish": {"current": publish, "revision": "sha256:release"},
        },
        "artifacts": {"release_video": {"ready": video}},
    }


@pytest.fixture
def agent(tmp_path):
    episode_dir = tmp_path / "episodes" / "ep_test"
    episode_dir.mkdir(parents=True)
    video = episode_dir / "upload_video.mp4"
    video.write_bytes(b"current video")
    episode = {
        "episode_id": "ep_test",
        "title": "A Full Episode Title",
        "description": "The reviewed episode description.",
        "video_explicit": False,
        "created_at": "2026-09-12T08:00:00+00:00",
        "publish_approval": {"revision": "sha256:release"},
    }
    (episode_dir / "episode.json").write_text(json.dumps(episode))
    stat = video.stat()
    (episode_dir / "render_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "clock": "source",
                "shorts": {},
                "longform": {
                    "path": "upload_video.mp4",
                    "render_mode": "speaker_cut",
                    "fingerprint": "render-fingerprint",
                    "output_duration_seconds": 120.4,
                    "output": {
                        "duration_seconds": 120.4,
                        "size_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    },
                },
            }
        )
    )
    return VideoFeedAgent(episode_dir, _config())


def _inputs(agent, snapshot=None) -> dict:
    with (
        patch(
            "agents.video_feed.quality_snapshot", return_value=snapshot or _snapshot()
        ),
        patch(
            "agents.video_feed.file_fingerprint",
            return_value={"id": "sha256:" + "a" * 64},
        ),
    ):
        return agent._current_inputs(require_publish_approval=True)


def _remote_feed(item_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Old channel</title>'
        f"{item_xml}</channel></rss>"
    ).encode()


def test_video_enclosure_author_guid_and_false_explicit(agent):
    inputs = _inputs(agent)
    xml, state = agent._build_feed(inputs, None)
    root = ET.fromstring(xml)
    channel = root.find("channel")
    item = channel.find("item")
    enclosure = item.find("enclosure")
    ns = {"itunes": ITUNES_NS}

    assert channel.findtext("itunes:author", namespaces=ns) == "The Local Podcast"
    assert item.findtext("itunes:author", namespaces=ns) == "The Local Podcast"
    assert channel.findtext("itunes:explicit", namespaces=ns) == "false"
    assert item.findtext("itunes:explicit", namespaces=ns) == "false"
    assert enclosure.attrib == {
        "url": "https://media.example/video/ep_test/render-fingerprint.mp4",
        "length": str(len(b"current video")),
        "type": "video/mp4",
    }
    assert item.findtext("guid") == "video:ep_test"
    assert item.findtext("pubDate") != "Sat, 12 Sep 2026 08:00:00 GMT"
    assert state["item_count"] == 1


def test_remote_history_and_existing_guid_pubdate_are_preserved(agent):
    inputs = _inputs(agent)
    remote = _remote_feed(
        """
        <item><title>Historical</title><description>Keep me</description>
          <enclosure url="https://media.example/video/ep_old/old.mp4" length="9" type="video/mp4" />
          <guid isPermaLink="false">video:ep_old</guid>
          <pubDate>Wed, 01 Apr 2026 12:00:00 GMT</pubDate>
        </item>
        <item><title>Prior current title</title><description>Old copy</description>
          <enclosure url="https://media.example/video/ep_test/old-render.mp4" length="8" type="video/mp4" />
          <guid isPermaLink="false">stable-existing-guid</guid>
          <pubDate>Thu, 02 Apr 2026 12:00:00 GMT</pubDate>
        </item>
        """
    )

    xml, state = agent._build_feed(inputs, remote)
    root = ET.fromstring(xml)
    items = root.find("channel").findall("item")
    by_guid = {item.findtext("guid"): item for item in items}

    assert set(by_guid) == {"video:ep_old", "stable-existing-guid"}
    assert by_guid["video:ep_old"].findtext("title") == "Historical"
    assert by_guid["stable-existing-guid"].findtext("title") == inputs["title"]
    assert (
        by_guid["stable-existing-guid"].findtext("pubDate")
        == "Thu, 02 Apr 2026 12:00:00 GMT"
    )
    assert state == {
        "previous_item_count": 2,
        "item_count": 2,
        "guid": "stable-existing-guid",
        "pub_date": "Thu, 02 Apr 2026 12:00:00 GMT",
        "replaced_existing": True,
    }


def test_receipt_date_is_reused_when_remote_item_is_absent(agent):
    (agent.episode_dir / "video_feed.json").write_text(
        json.dumps(
            {
                "schema": "cascade.video-podcast-feed/v1",
                "status": "published",
                "episode_id": "ep_test",
                "published_at": "2026-09-13T20:00:00+00:00",
            }
        )
    )
    inputs = _inputs(agent)

    xml, _ = agent._build_feed(inputs, None)

    item = ET.fromstring(xml).find("channel").find("item")
    assert item.findtext("pubDate") == "Sun, 13 Sep 2026 20:00:00 GMT"


def test_current_staged_object_is_reused_by_exact_identity(agent):
    inputs = _inputs(agent)
    client = MagicMock()
    client.head_object.return_value = {
        "ContentLength": inputs["video_size"],
        "ContentType": "video/mp4",
        "ETag": '"multipart-etag"',
        "Metadata": {
            "sha256": inputs["content_sha256"],
            "cascade-render-fingerprint": inputs["render_fingerprint"],
        },
    }

    state, reused = agent._ensure_video_object(client, inputs)

    assert reused is True
    assert state["sha256"] == "a" * 64
    client.head_object.assert_called_once_with(
        Bucket="podcast", Key="video/ep_test/render-fingerprint.mp4"
    )
    client.upload_file.assert_not_called()


def test_missing_object_uses_bounded_multipart_upload(agent):
    inputs = _inputs(agent)
    missing = ClientError(
        {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )
    client = MagicMock()
    client.head_object.side_effect = [
        missing,
        {
            "ContentLength": inputs["video_size"],
            "ContentType": "video/mp4",
            "ETag": '"uploaded"',
            "Metadata": {
                "sha256": inputs["content_sha256"],
                "cascade-render-fingerprint": inputs["render_fingerprint"],
            },
        },
    ]

    _, reused = agent._ensure_video_object(client, inputs)

    assert reused is False
    _, bucket, key = client.upload_file.call_args.args
    assert (bucket, key) == ("podcast", "video/ep_test/render-fingerprint.mp4")
    kwargs = client.upload_file.call_args.kwargs
    assert kwargs["Config"].multipart_threshold == 64 * 1024 * 1024
    assert kwargs["Config"].multipart_chunksize == 64 * 1024 * 1024
    assert kwargs["Config"].max_concurrency == 2
    assert kwargs["ExtraArgs"]["ContentType"] == "video/mp4"


@pytest.mark.parametrize(
    "snapshot, message",
    [
        (_snapshot(qa=False), "QA report"),
        (_snapshot(editorial=False), "editorial approval"),
        (_snapshot(publish=False), "publish approval"),
        (_snapshot(video=False), "upload video"),
    ],
)
def test_guard_failures_prevent_publication(agent, snapshot, message):
    with (
        patch("agents.video_feed.quality_snapshot", return_value=snapshot),
        patch("agents.video_feed.file_fingerprint") as fingerprint,
        pytest.raises(RuntimeError, match=message),
    ):
        agent._current_inputs(require_publish_approval=True)
    fingerprint.assert_not_called()


def test_prepare_does_not_touch_audio_feed_files(agent):
    audio_feed = agent.episode_dir / "feed.xml"
    audio_receipt = agent.episode_dir / "podcast_feed.json"
    audio_feed.write_bytes(b"audio feed sentinel")
    audio_receipt.write_bytes(b"audio receipt sentinel")
    inputs = _inputs(agent)
    client = MagicMock()

    with (
        patch.object(agent, "_current_inputs", return_value=inputs),
        patch.object(agent, "_r2_client", return_value=client),
        patch.object(agent, "_remote_feed", return_value=(None, None)),
        patch.object(agent, "_head_video_object", return_value=None),
    ):
        result = agent.prepare()

    assert result["dry_run"] is True
    assert audio_feed.read_bytes() == b"audio feed sentinel"
    assert audio_receipt.read_bytes() == b"audio receipt sentinel"
    assert (agent.episode_dir / "feed-video.preview.xml").is_file()


def test_release_change_before_feed_put_aborts(agent):
    inputs = _inputs(agent)
    changed = {**inputs, "release_revision": "sha256:changed"}
    client = MagicMock()
    with (
        patch.object(agent, "_current_inputs", side_effect=[inputs, changed]),
        patch.object(agent, "_r2_client", return_value=client),
        patch.object(
            agent,
            "_ensure_video_object",
            return_value=({"status": "ready"}, True),
        ),
        pytest.raises(RuntimeError, match="changed while video publication ran"),
    ):
        agent.execute()
    client.put_object.assert_not_called()


@pytest.mark.parametrize(
    ("remote_feed", "remote_etag", "condition"),
    [
        (
            _remote_feed(
                """
                <item><title>Historical</title><description>Keep me</description>
                  <enclosure url="https://media.example/video/ep_old/old.mp4" length="9" type="video/mp4" />
                  <guid isPermaLink="false">video:ep_old</guid>
                  <pubDate>Wed, 01 Apr 2026 12:00:00 GMT</pubDate>
                </item>
                """
            ),
            "feed-etag",
            {"IfMatch": '"feed-etag"'},
        ),
        (None, None, {"IfNoneMatch": "*"}),
    ],
)
def test_feed_put_is_conditional_on_remote_history(
    agent, remote_feed, remote_etag, condition
):
    inputs = _inputs(agent)
    client = MagicMock()
    object_state = {"status": "ready"}
    with (
        patch.object(agent, "_current_inputs", return_value=inputs),
        patch.object(agent, "_r2_client", return_value=client),
        patch.object(agent, "_ensure_video_object", return_value=(object_state, True)),
        patch.object(agent, "_remote_feed", return_value=(remote_feed, remote_etag)),
        patch.object(agent, "_head_video_object", return_value=object_state),
    ):
        result = agent.execute()

    put_args = client.put_object.call_args.kwargs
    assert put_args["Key"] == "feed-video.xml"
    assert {key: put_args[key] for key in condition} == condition
    if remote_feed is not None:
        assert "video:ep_old" in put_args["Body"].decode()
    assert result["video_object_reused"] is True


def test_remote_feed_read_is_bounded(agent):
    client = MagicMock()
    body = io.BytesIO(b"<rss><channel /></rss>")
    client.get_object.return_value = {
        "ContentLength": len(body.getvalue()),
        "Body": body,
        "ETag": '"feed-etag"',
    }
    data, etag = agent._remote_feed(client, "podcast")
    assert data == b"<rss><channel /></rss>"
    assert etag == "feed-etag"
