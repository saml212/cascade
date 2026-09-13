"""Release approval is bound to the exact external publication plan."""

import json

from agents.qa import current_publish_plan, release_revision


def _config() -> dict:
    return {
        "platforms": {
            "youtube": {"enabled": True},
            "tiktok": {"enabled": False},
            "instagram": {"enabled": False},
            "x": {"enabled": False},
            "podcast_rss": {"enabled": False},
            "video_podcast_rss": {"enabled": False},
        },
        "schedule": {
            "timezone": "America/Los_Angeles",
            "shorts_per_day_weekday": 1,
            "shorts_per_day_weekend": 2,
        },
        "podcast": {
            "title": "Private show title",
            "description": "Private description",
            "author": "Private author",
            "artwork_url": "https://private.invalid/art.jpg",
            "link": "https://private.invalid/show",
            "channel_handle": "@private-channel",
            "r2": {
                "bucket": "private-bucket",
                "public_url": "https://private.invalid/media/",
            },
        },
    }


def _episode() -> dict:
    return {
        "episode_id": "ep_private",
        "title": "Private episode title",
        "description": "Private episode description",
        "created_at": "2026-01-01T00:00:00+00:00",
        "youtube_longform_url": "https://private.invalid/watch",
    }


def test_upload_post_destination_and_account_change_release_revision(tmp_path):
    config = _config()
    episode = _episode()
    approved = release_revision(
        tmp_path,
        episode,
        config=config,
        environment={"UPLOAD_POST_USER": "account-a"},
    )

    config["platforms"]["tiktok"]["enabled"] = True
    assert (
        release_revision(
            tmp_path,
            episode,
            config=config,
            environment={"UPLOAD_POST_USER": "account-a"},
        )
        != approved
    )

    config["platforms"]["tiktok"]["enabled"] = False
    assert (
        release_revision(
            tmp_path,
            episode,
            config=config,
            environment={"UPLOAD_POST_USER": "account-b"},
        )
        != approved
    )


def test_upload_post_receipt_does_not_invalidate_approved_batch(tmp_path):
    config = _config()
    episode = _episode()
    episode.pop("youtube_longform_url")
    environment = {"UPLOAD_POST_USER": "account-a"}
    approved = release_revision(
        tmp_path, episode, config=config, environment=environment
    )

    episode["youtube_longform_url"] = "https://youtube.invalid/receipt"
    episode["youtube_longform_url_source"] = "upload_post_receipt"
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        == approved
    )

    episode["youtube_longform_url"] = "https://youtube.invalid/supplied"
    episode["youtube_longform_url_source"] = "supplied"
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != approved
    )


def test_credentials_are_not_stored_or_bound_to_release_revision(tmp_path):
    config = _config()
    episode = _episode()
    first_environment = {
        "UPLOAD_POST_USER": "private-account",
        "UPLOAD_POST_API_KEY": "first-secret",
        "CLOUDFLARE_API_TOKEN": "first-token",
    }
    second_environment = {
        **first_environment,
        "UPLOAD_POST_API_KEY": "second-secret",
        "CLOUDFLARE_API_TOKEN": "second-token",
    }

    first = release_revision(
        tmp_path, episode, config=config, environment=first_environment
    )
    second = release_revision(
        tmp_path, episode, config=config, environment=second_environment
    )
    serialized = json.dumps(
        current_publish_plan(config, episode, environment=first_environment)
    )

    assert first == second
    assert "private-account" not in serialized
    assert "first-secret" not in serialized
    assert "first-token" not in serialized


def test_rss_destination_channel_and_account_change_release_revision(tmp_path):
    config = _config()
    config["platforms"]["podcast_rss"]["enabled"] = True
    episode = _episode()
    environment = {"UPLOAD_POST_USER": "account", "CLOUDFLARE_ACCOUNT_ID": "a"}
    approved = release_revision(
        tmp_path, episode, config=config, environment=environment
    )

    config["podcast"]["r2"]["bucket"] = "another-bucket"
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != approved
    )

    config["podcast"]["r2"]["bucket"] = "private-bucket"
    config["podcast"]["title"] = "Another show"
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != approved
    )

    config["podcast"]["title"] = "Private show title"
    environment["CLOUDFLARE_ACCOUNT_ID"] = "b"
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != approved
    )


def test_video_rss_destination_is_explicit_and_revision_bound(tmp_path):
    config = _config()
    episode = _episode()
    environment = {"UPLOAD_POST_USER": "account", "CLOUDFLARE_ACCOUNT_ID": "a"}
    before = release_revision(tmp_path, episode, config=config, environment=environment)

    config["platforms"]["video_podcast_rss"]["enabled"] = True
    plan = current_publish_plan(config, episode, environment=environment)
    video = plan["video_podcast_rss"]
    assert video["enabled"] is True
    assert video["format"] == "video"
    assert video["feed_key"] == "feed-video.xml"
    assert video["media_prefix"] == "video"
    assert video["enclosure_type"] == "video/mp4"
    assert video["episode_configured"] is True
    approved = release_revision(
        tmp_path, episode, config=config, environment=environment
    )
    assert approved != before

    config["podcast"]["r2"]["bucket"] = "another-video-bucket"
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != approved
    )

    config["podcast"]["r2"]["bucket"] = "private-bucket"
    episode["video_explicit"] = True
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != approved
    )


def test_disabled_publication_destinations_ignore_dormant_configuration(tmp_path):
    config = _config()
    config["platforms"]["youtube"]["enabled"] = False
    episode = _episode()
    approved = release_revision(
        tmp_path,
        episode,
        config=config,
        environment={"UPLOAD_POST_USER": "account-a"},
    )

    config["podcast"]["title"] = "Dormant edit"
    assert (
        release_revision(
            tmp_path,
            episode,
            config=config,
            environment={"UPLOAD_POST_USER": "account-b"},
        )
        == approved
    )
