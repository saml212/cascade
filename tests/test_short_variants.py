"""Currentness and approval guards for optional short-video variants."""

import hashlib
import json
import os

import pytest

from lib.short_variants import (
    BACKGROUND_VARIANT_ID,
    background_variant_fingerprint,
    background_variant_state,
    file_content_identity,
    load_background_asset,
    record_background_variant,
    save_background_variant_approval,
    variant_record,
)
from lib.timeline import Timeline


def _asset(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    root.mkdir()
    media = root / "motion.mp4"
    media.write_bytes(b"silent motion")
    manifest = {
        "asset_id": "motion_v1",
        "file": media.name,
        "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
        "origin": "test generator",
        "license": "original",
    }
    (root / "motion.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("CASCADE_BACKGROUND_ASSETS_DIR", str(root))
    return load_background_asset("motion_v1", verify_content=True)


def _record(tmp_path, monkeypatch):
    episode_dir = tmp_path / "episode"
    base = episode_dir / "shorts" / "clip_01.mp4"
    output = episode_dir / "short_variants" / BACKGROUND_VARIANT_ID / "clip_01.mp4"
    captions = (
        episode_dir
        / "subtitles"
        / "short_variants"
        / BACKGROUND_VARIANT_ID
        / "clip_01.ass"
    )
    for path, content in (
        (base, b"base pixels and audio"),
        (output, b"variant pixels and base audio"),
        (captions, b"captions"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    asset = _asset(tmp_path, monkeypatch)
    base_record = {"fingerprint": "sha256:base"}
    base_content = file_content_identity(base)
    encoding = {"video_bitrate": "10M", "audio_bitrate": "192k"}
    fingerprint = background_variant_fingerprint(
        base_record,
        base_content["scan_identity"],
        asset,
        encoding,
    )
    record = record_background_variant(
        episode_dir,
        "clip_01",
        fingerprint=fingerprint,
        timeline=Timeline.from_edits(1),
        media={"duration_seconds": 1, "width": 1080, "height": 1920},
        base_record=base_record,
        base_identity=base_content["scan_identity"],
        asset=asset,
        encoding=encoding,
        captions={
            "path": str(captions.relative_to(episode_dir)),
            "format": "ass",
            "burned_in": True,
        },
    )
    return episode_dir, base_record, encoding, record, output


def test_asset_verification_hashes_content_and_rejects_escape(tmp_path, monkeypatch):
    asset = _asset(tmp_path, monkeypatch)
    assert asset["content_revision"].startswith("sha256:")
    asset["path"].write_bytes(b"changed motion")
    try:
        load_background_asset("motion_v1", verify_content=True)
    except ValueError as exc:
        assert "recorded SHA-256" in str(exc)
    else:
        raise AssertionError("content replacement was accepted")

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    root = tmp_path / "assets"
    (root / "escape.json").write_text(
        json.dumps(
            {
                "asset_id": "escape",
                "path": str(outside),
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        )
    )
    try:
        load_background_asset("escape", verify_content=True)
    except ValueError as exc:
        assert "inside the asset directory" in str(exc)
    else:
        raise AssertionError("asset path escape was accepted")


def test_same_path_variant_replacement_is_stale_and_cannot_be_approved(
    tmp_path, monkeypatch
):
    episode_dir, base_record, encoding, record, output = _record(tmp_path, monkeypatch)
    _, current = background_variant_state(
        episode_dir, "clip_01", base_record=base_record, encoding=encoding
    )
    assert current["current"] is True

    stat = output.stat()
    output.write_bytes(b"X" * stat.st_size)
    os.utime(output, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    _, changed = background_variant_state(
        episode_dir, "clip_01", base_record=base_record, encoding=encoding
    )

    assert changed["current"] is False
    assert changed["reason_code"] == "artifact_changed"
    assert (
        save_background_variant_approval(
            episode_dir, "clip_01", record, "sha256:review"
        )
        is None
    )


def test_non_mapping_variant_sidecar_fails_closed(tmp_path):
    state = tmp_path / "short_variants" / BACKGROUND_VARIANT_ID / "clip_01.json"
    state.parent.mkdir(parents=True)
    state.write_text("[]")
    assert variant_record(tmp_path, "clip_01") == {}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("variant_id", "wrong", "verification_inputs_unavailable"),
        ("layout_version", [], "verification_inputs_unavailable"),
        ("output", {}, "artifact_changed"),
    ),
)
def test_malformed_nested_variant_identity_is_never_current(
    tmp_path, monkeypatch, field, value, reason
):
    episode_dir, base_record, encoding, record, output = _record(tmp_path, monkeypatch)
    record[field] = value
    output.with_suffix(".json").write_text(json.dumps(record))

    _, state = background_variant_state(
        episode_dir, "clip_01", base_record=base_record, encoding=encoding
    )

    assert state["current"] is False
    assert state["reason_code"] == reason


@pytest.mark.parametrize(
    "layout_version",
    (
        "portrait-over-motion/v1",
        "portrait-over-motion/v2",
        "portrait-over-motion/v3",
    ),
)
def test_previous_layout_has_specific_stale_reason(
    tmp_path, monkeypatch, layout_version
):
    episode_dir, base_record, encoding, record, output = _record(tmp_path, monkeypatch)
    record["layout_version"] = layout_version
    output.with_suffix(".json").write_text(json.dumps(record))

    _, state = background_variant_state(
        episode_dir, "clip_01", base_record=base_record, encoding=encoding
    )

    assert state["current"] is False
    assert state["reason_code"] == "verification_inputs_unavailable"
    assert state["detail"] == (
        f"Background layout {layout_version} is out of date; re-render this variant."
    )


def test_recorded_variant_path_cannot_redirect_review(tmp_path, monkeypatch):
    episode_dir, base_record, encoding, record, output = _record(tmp_path, monkeypatch)
    record["path"] = "short_variants/elsewhere.mp4"
    output.with_suffix(".json").write_text(json.dumps(record))

    _, state = background_variant_state(
        episode_dir, "clip_01", base_record=base_record, encoding=encoding
    )

    assert state["current"] is False
    assert state["reason_code"] == "variant_path_changed"
