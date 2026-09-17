"""Deterministic gameplay playback-window policy."""

from copy import deepcopy

import pytest

from lib.gameplay_playback import (
    GAMEPLAY_PLAYBACK_POLICY_VERSION,
    GAMEPLAY_PLAYBACK_TAIL_GUARD_SECONDS,
    require_resolved_gameplay_playback,
    resolve_gameplay_asset_playback,
)


def _asset(**updates):
    asset = {
        "asset_id": "licensed_gameplay_v1",
        "content_revision": "sha256:" + "a" * 64,
        "manifest_revision": "sha256:" + "b" * 64,
        "playback_start_seconds": 5.25,
        "provenance": {
            "license": "commercial-license",
            "usage": "composited podcast shorts only",
        },
    }
    asset.update(updates)
    return asset


def _resolve(asset, *, clip_id="clip_01", clip_duration=36.67, source_duration=180):
    return resolve_gameplay_asset_playback(
        asset,
        episode_id="ep_2026-09-16_120000",
        clip_id=clip_id,
        variant_id="gameplay_surround_v1",
        clip_duration_seconds=clip_duration,
        source_duration_seconds=source_duration,
    )


def test_playback_window_is_stable_per_clip_and_preserves_rights():
    source = _asset()
    before = deepcopy(source)

    first = _resolve(source)
    repeated = _resolve(source)
    another_clip = _resolve(source, clip_id="clip_02")

    assert first == repeated
    assert source == before
    assert first["provenance"] == source["provenance"]
    assert first["playback_loop"] is False
    assert first["playback_start_seconds"] >= source["playback_start_seconds"]
    assert (
        first["playback_start_seconds"]
        + first["playback_policy"]["clip_duration_seconds"]
        + GAMEPLAY_PLAYBACK_TAIL_GUARD_SECONDS
        <= first["playback_policy"]["source_duration_seconds"]
    )
    assert first["playback_policy"]["wrap_required"] is False
    assert first["playback_policy"]["version"] == GAMEPLAY_PLAYBACK_POLICY_VERSION
    assert first["playback_start_seconds"] != another_clip["playback_start_seconds"]
    assert (
        first["playback_policy"]["selection_key_revision"]
        != (another_clip["playback_policy"]["selection_key_revision"])
    )
    require_resolved_gameplay_playback(first)


def test_short_source_fails_unless_manifest_explicitly_permits_looping():
    with pytest.raises(ValueError, match="does not permit looping"):
        _resolve(_asset(playback_start_seconds=1), clip_duration=12, source_duration=10)

    resolved = _resolve(
        _asset(playback_start_seconds=0, playback_loop=True),
        clip_duration=12,
        source_duration=10,
    )

    assert resolved["playback_policy"]["loop_enabled"] is True
    assert resolved["playback_policy"]["wrap_required"] is True
    assert 0 <= resolved["playback_start_seconds"] <= 9.75

    with pytest.raises(ValueError, match="before its manifest start"):
        _resolve(
            _asset(playback_start_seconds=1, playback_loop=True),
            clip_duration=12,
            source_duration=10,
        )


def test_exact_millisecond_boundary_validates_without_float_drift():
    resolved = _resolve(
        _asset(playback_start_seconds=506.454),
        clip_duration=18.808,
        source_duration=525.512,
    )

    assert resolved["playback_start_seconds"] == 506.454
    assert resolved["playback_policy"]["wrap_required"] is False
    require_resolved_gameplay_playback(resolved)


@pytest.mark.parametrize(
    ("updates", "match"),
    (
        ({"playback_loop": "yes"}, "playback_loop"),
        ({"playback_start_seconds": float("nan")}, "manifest playback start"),
        ({"playback_start_seconds": 180}, "starts beyond"),
    ),
)
def test_invalid_manifest_playback_inputs_fail_closed(updates, match):
    with pytest.raises((TypeError, ValueError), match=match):
        _resolve(_asset(**updates))


def test_resolved_policy_cannot_be_detached_from_start_or_loop_decision():
    resolved = _resolve(_asset())
    moved = deepcopy(resolved)
    moved["playback_start_seconds"] += 0.001
    with pytest.raises(ValueError, match="does not match its policy"):
        require_resolved_gameplay_playback(moved)

    loop_changed = deepcopy(resolved)
    loop_changed["playback_loop"] = True
    with pytest.raises(ValueError, match="loop setting"):
        require_resolved_gameplay_playback(loop_changed)

    fake_wrap = deepcopy(resolved)
    fake_wrap["playback_loop"] = True
    fake_wrap["playback_policy"]["loop_enabled"] = True
    fake_wrap["playback_policy"]["wrap_required"] = True
    with pytest.raises(ValueError, match="invalid wrap decision"):
        require_resolved_gameplay_playback(fake_wrap)
