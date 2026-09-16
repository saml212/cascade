"""Release approval is bound to the exact external publication plan."""

import json

import pytest

from agents.qa import (
    current_funnel_urls,
    current_publish_plan,
    episode_hub_url,
    release_revision,
)


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


def test_required_short_variant_is_bound_to_release_plan(tmp_path):
    config = _config()
    config["platforms"]["x"]["enabled"] = True
    episode = _episode()
    environment = {"UPLOAD_POST_USER": "account-a"}
    original = release_revision(
        tmp_path, episode, config=config, environment=environment
    )

    config["platforms"]["x"]["required_short_variant_id"] = "satisfying_motion_v1"
    plan = current_publish_plan(config, episode, environment=environment)

    assert plan["upload_post"]["required_short_variants"] == {
        "x": "satisfying_motion_v1"
    }
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != original
    )


def test_absent_required_short_variant_preserves_publish_plan_shape():
    plan = current_publish_plan(
        _config(), _episode(), environment={"UPLOAD_POST_USER": "account-a"}
    )

    assert "required_short_variants" not in plan["upload_post"]


def test_required_short_variant_must_be_a_trimmed_nonempty_id():
    config = _config()
    config["platforms"]["x"] = {
        "enabled": True,
        "required_short_variant_id": " speaker_panels_v1 ",
    }

    with pytest.raises(TypeError, match="required_short_variant_id"):
        current_publish_plan(config, _episode(), environment={})


def test_required_short_variant_must_name_a_supported_variant():
    config = _config()
    config["platforms"]["x"] = {
        "enabled": True,
        "required_short_variant_id": "unknown_variant",
    }

    with pytest.raises(ValueError, match="unknown short variant"):
        current_publish_plan(config, _episode(), environment={})


def test_disabled_expansion_destinations_do_not_change_existing_publish_plan(tmp_path):
    config = _config()
    episode = _episode()
    environment = {"UPLOAD_POST_USER": "account-a"}
    original = current_publish_plan(config, episode, environment=environment)
    for destination in (
        "facebook",
        "threads",
        "bluesky",
        "linkedin",
        "pinterest",
    ):
        config["platforms"][destination] = {
            "enabled": False,
            "account_username": "dormant-account",
            "page_id": "dormant-page",
            "board_id": "dormant-board",
        }

    assert current_publish_plan(config, episode, environment=environment) == original


def test_episode_url_template_replaces_legacy_hub_and_changes_release(tmp_path):
    config = _config()
    episode = _episode()
    environment = {"UPLOAD_POST_USER": "account-a"}
    legacy_revision = release_revision(
        tmp_path, episode, config=config, environment=environment
    )

    assert episode_hub_url(config, "ep/a b") == (
        "https://private.invalid/media/links/episodes/ep%2Fa%20b.html"
    )

    config["podcast"]["links"] = {
        "episode_url_template": "https://thelocalpod.link/#{episode_id}"
    }

    assert episode_hub_url(config, "ep/a b") == "https://thelocalpod.link/#ep%2Fa%20b"
    assert (
        current_publish_plan(config, episode, environment=environment)["upload_post"][
            "short_copy"
        ]["episode_hub_url"]
        == "https://thelocalpod.link/#ep_private"
    )
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != legacy_revision
    )


@pytest.mark.parametrize(
    "template",
    (
        "http://thelocalpod.link/#{episode_id}",
        "https://thelocalpod.link/#episodes",
        "https://thelocalpod.link/#{episode_id}/{other}",
        "https://thelocalpod.link:bad/#{episode_id}",
        "https://thelocalpod.link:99999/#{episode_id}",
        "https://./#{episode_id}",
        "https://thelocalpod.link/\x00{episode_id}",
    ),
)
def test_episode_url_template_rejects_unsafe_or_ambiguous_values(template):
    config = _config()
    config["podcast"]["links"] = {"episode_url_template": template}

    with pytest.raises(ValueError, match="episode_url_template"):
        episode_hub_url(config, "ep_private")


def test_enabled_expansion_account_and_target_are_release_bound(tmp_path):
    config = _config()
    episode = _episode()
    environment = {"UPLOAD_POST_USER": "account-a"}
    before = release_revision(tmp_path, episode, config=config, environment=environment)
    config["platforms"]["facebook"] = {
        "enabled": True,
        "account_username": "opaque-account-id",
        "page_id": "page-a",
    }

    plan = current_publish_plan(config, episode, environment=environment)["upload_post"]
    assert plan["destination_bindings"] == {
        "facebook": {
            "account_username": "opaque-account-id",
            "page_id": "page-a",
        }
    }
    approved = release_revision(
        tmp_path, episode, config=config, environment=environment
    )
    assert approved != before

    config["platforms"]["facebook"]["page_id"] = "page-b"
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != approved
    )


def test_youtube_audience_declaration_is_explicit_and_approval_bound(tmp_path):
    config = _config()
    episode = _episode()
    environment = {"UPLOAD_POST_USER": "account-a"}

    initial = release_revision(
        tmp_path, episode, config=config, environment=environment
    )
    assert current_publish_plan(config, episode, environment=environment)[
        "upload_post"
    ]["youtube"] == {"self_declared_made_for_kids": False}

    config["platforms"]["youtube"]["self_declared_made_for_kids"] = True
    assert (
        release_revision(tmp_path, episode, config=config, environment=environment)
        != initial
    )

    config["platforms"]["youtube"]["self_declared_made_for_kids"] = "false"
    with pytest.raises(TypeError, match="must be true or false"):
        current_publish_plan(config, episode, environment=environment)


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


def test_selected_variant_identity_changes_release_but_default_base_does_not(
    tmp_path,
):
    config = _config()
    episode = _episode()
    clips_path = tmp_path / "clips.json"
    clip = {"id": "clip_01", "status": "approved"}
    clips_path.write_text(json.dumps({"clips": [clip]}))
    base = release_revision(tmp_path, episode, config=config, environment={})

    clip["distribution_variant_id"] = None
    clips_path.write_text(json.dumps({"clips": [clip]}))
    assert release_revision(tmp_path, episode, config=config, environment={}) == base

    clip["distribution_variant_id"] = "background_motion_v1"
    clips_path.write_text(json.dumps({"clips": [clip]}))
    record_path = tmp_path / "short_variants" / "background_motion_v1" / "clip_01.json"
    record_path.parent.mkdir(parents=True)
    record = {
        "fingerprint": "sha256:variant-render",
        "output": {
            "content_revision": "sha256:variant-pixels",
            "scan_identity": {"size_bytes": 10, "mtime_ns": 20},
        },
        "asset": {
            "asset_id": "motion",
            "content_revision": "sha256:motion-pixels",
            "manifest_revision": "sha256:motion-manifest",
        },
        "approval": {"revision": "sha256:variant-review"},
    }
    record_path.write_text(json.dumps(record))

    selected = release_revision(tmp_path, episode, config=config, environment={})
    assert selected != base
    record["output"]["content_revision"] = "sha256:replacement-pixels"
    record_path.write_text(json.dumps(record))
    assert (
        release_revision(tmp_path, episode, config=config, environment={}) != selected
    )


def test_supplied_funnel_url_is_bound_to_longform_revision(tmp_path):
    episode = {
        "youtube_longform_url": "https://youtube.invalid/current",
        "youtube_longform_url_source": "supplied",
        "youtube_longform_url_editorial_revision": "sha256:longform-a",
    }

    current = current_funnel_urls(
        episode,
        episode_dir=tmp_path,
        editorial_revision_value="sha256:longform-a",
        quality_revision_value="sha256:quality-a",
    )
    remastered = current_funnel_urls(
        episode,
        episode_dir=tmp_path,
        editorial_revision_value="sha256:longform-b",
        quality_revision_value="sha256:quality-b",
    )

    assert current["youtube"] == "https://youtube.invalid/current"
    assert remastered["youtube"] == ""


def test_source_less_old_urls_are_not_reused_for_rebuilt_release(tmp_path):
    episode = {
        "youtube_longform_url": "https://youtube.invalid/old",
        "spotify_longform_url": "https://spotify.invalid/old",
    }

    assert current_funnel_urls(
        episode,
        episode_dir=tmp_path,
        editorial_revision_value="sha256:rebuilt-longform",
        quality_revision_value="sha256:rebuilt-quality",
    ) == {"youtube": "", "spotify": ""}


def test_current_legacy_supplied_urls_migrate_only_with_matching_release_proof(
    tmp_path,
):
    episode = {
        "youtube_longform_url": "https://youtube.invalid/current",
        "youtube_longform_url_source": "supplied",
        "spotify_longform_url": "https://spotify.invalid/current",
        "publish_approval": {"revision": "sha256:published-release"},
    }
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / "qa.json").write_text(
        json.dumps(
            {
                "quality_revision": "sha256:current-quality",
                "release_revision": "sha256:published-release",
                "editorial_revision": "sha256:current-longform",
            }
        )
    )
    (tmp_path / "publish.json").write_text(
        json.dumps({"release_revision": "sha256:published-release"})
    )

    migrated = current_funnel_urls(
        episode,
        episode_dir=tmp_path,
        editorial_revision_value="sha256:current-longform",
        quality_revision_value="sha256:current-quality",
    )
    stale = current_funnel_urls(
        episode,
        episode_dir=tmp_path,
        editorial_revision_value="sha256:remastered-longform",
        quality_revision_value="sha256:remastered-quality",
    )

    assert migrated == {
        "youtube": "https://youtube.invalid/current",
        "spotify": "https://spotify.invalid/current",
    }
    assert stale == {"youtube": "", "spotify": ""}


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
