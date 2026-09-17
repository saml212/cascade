"""Tests for the shared short-destination catalogue and local preflight."""

from copy import deepcopy

import pytest

from lib.short_distribution import (
    EXPANSION_DESTINATIONS,
    PLATFORM_COPY_FIELDS,
    SHORT_DESTINATIONS,
    configured_destination_bindings,
    upload_fields,
    valid_destination_bindings,
    validate_destination_copy,
    validate_destination_media,
)


def _probe() -> dict:
    return {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "60.0",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1080,
                "height": 1920,
                "pix_fmt": "yuv420p",
                "field_order": "progressive",
                "avg_frame_rate": "30000/1001",
                "bit_rate": "10000000",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "bit_rate": "192000",
            },
        ],
    }


def _media_file(tmp_path, *, edit_list=False):
    path = tmp_path / "short.mp4"
    atoms = b"ftyp....moov" + (b"edts" if edit_list else b"") + b"....mdat"
    path.write_bytes(atoms + b"0" * 100_000)
    return path


def test_catalogue_preserves_original_order_and_copy_fields():
    assert SHORT_DESTINATIONS[:4] == ("youtube", "tiktok", "instagram", "x")
    assert set(SHORT_DESTINATIONS[4:]) == EXPANSION_DESTINATIONS
    assert PLATFORM_COPY_FIELDS["facebook"] == ("title", "description")
    assert PLATFORM_COPY_FIELDS["threads"] == ("text",)


def test_final_copy_limits_count_provider_units_without_truncating():
    valid = {
        "threads": {"text": "🙂" * 125},
        "bluesky": {"text": "b" * 300},
        "linkedin": {"title": "🙂" * 200, "description": "d" * 3000},
        "pinterest": {"title": "p" * 100, "description": "d" * 800},
    }
    assert validate_destination_copy(valid) == []

    invalid = deepcopy(valid)
    invalid["threads"]["text"] += "🙂"
    invalid["bluesky"]["text"] += "b"
    invalid["linkedin"]["title"] += "🙂"
    invalid["pinterest"]["description"] += "d"
    assert validate_destination_copy(invalid) == [
        "threads.text exceeds 500 utf8_bytes",
        "bluesky.text exceeds 300 characters",
        "linkedin.title exceeds 400 utf16_units",
        "pinterest.description exceeds 800 characters",
    ]


def test_configured_bindings_require_explicit_accounts_and_native_targets():
    config = {
        "platforms": {
            "facebook": {
                "account_username": "fb-account",
                "page_id": "fb-page",
            },
            "threads": {"account_username": "threads-account"},
            "linkedin": {"account_username": "li-account", "page_id": ""},
            "pinterest": {
                "account_username": "pin-account",
                "board_id": "pin-board",
            },
        }
    }
    bindings = configured_destination_bindings(
        config, ["facebook", "threads", "linkedin", "pinterest"]
    )
    assert bindings == {
        "facebook": {
            "account_id": "fb-account",
            "target_kind": "page",
            "target_id": "fb-page",
        },
        "threads": {
            "account_id": "threads-account",
            "target_kind": "account",
            "target_id": "threads-account",
        },
        "linkedin": {
            "account_id": "li-account",
            "target_kind": "personal",
            "target_id": "li-account",
        },
        "pinterest": {
            "account_id": "pin-account",
            "target_kind": "board",
            "target_id": "pin-board",
        },
    }
    assert valid_destination_bindings(bindings, list(bindings))
    assert valid_destination_bindings(None, ["youtube", "tiktok"])

    with pytest.raises(ValueError, match="facebook.page_id"):
        configured_destination_bindings(
            {"platforms": {"facebook": {"account_username": "fb-account"}}},
            ["facebook"],
        )
    malformed = deepcopy(bindings)
    malformed["threads"]["target_kind"] = "page"
    assert not valid_destination_bindings(malformed, list(bindings))
    assert not valid_destination_bindings(None, ["facebook"])


def test_upload_fields_map_copy_and_fixed_options():
    assert upload_fields("facebook", {"title": "Title", "description": "Body"}) == {
        "facebook_title": "Title",
        "facebook_description": "Body",
        "facebook_media_type": "REELS",
    }
    assert upload_fields("threads", {"text": "Post"}) == {"threads_title": "Post"}
    assert upload_fields("x", {"text": "Post"}) == {
        "x_title": "Post",
        "x_long_text_as_post": "true",
    }


def test_current_motion_media_passes_all_expansion_limits(tmp_path, monkeypatch):
    path = _media_file(tmp_path)
    monkeypatch.setattr("lib.short_distribution.probe", lambda _path: _probe())
    assert validate_destination_media(path, list(EXPANSION_DESTINATIONS)) == []


@pytest.mark.parametrize(
    ("destination", "mutation", "expected"),
    [
        ("facebook", ("format", "duration", "nan"), "invalid duration"),
        ("facebook", ("video", "avg_frame_rate", "nan/1"), "invalid fps"),
        ("facebook", ("video", "width", 0), "invalid width"),
        ("facebook", ("video", "width", 1081), "requires 9:16"),
        ("facebook", ("video", "height", 1800), "requires 9:16"),
        ("threads", ("video", "width", 2000), "width <= 1920"),
        ("linkedin", ("video", "bit_rate", "nan"), "invalid bitrate"),
        ("linkedin", ("video", "bit_rate", None), "bitrate is unavailable"),
        ("linkedin", ("video", "bit_rate", "191999"), "bitrate >= 192000"),
        ("pinterest", ("video", "height", 2400), "aspect >= 0.5"),
        ("bluesky", ("video", "codec_name", "vp9"), "does not support vp9"),
    ],
)
def test_media_preflight_rejects_hard_limit_violations(
    tmp_path, monkeypatch, destination, mutation, expected
):
    path = _media_file(tmp_path)
    data = _probe()
    section, field, value = mutation
    target = data["format"] if section == "format" else data["streams"][0]
    target[field] = value
    monkeypatch.setattr("lib.short_distribution.probe", lambda _path: data)
    assert expected in "; ".join(validate_destination_media(path, [destination]))


def test_threads_rejects_edit_lists(tmp_path, monkeypatch):
    path = _media_file(tmp_path, edit_list=True)
    monkeypatch.setattr("lib.short_distribution.probe", lambda _path: _probe())
    assert "does not accept MP4 edit lists" in "; ".join(
        validate_destination_media(path, ["threads"])
    )
