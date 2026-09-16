"""Currentness and approval guards for optional short-video variants."""

import hashlib
import json
import os
from copy import deepcopy

import pytest

from agents.qa import clip_review_revision, short_distribution_state
from lib.crop import visual_crop_state
from lib.short_variants import (
    BACKGROUND_VARIANT_ID,
    BACKGROUND_VARIANT_IDS,
    CONTAIN_BLUR_FIT_MODE,
    DEFAULT_BACKGROUND_ASSET_ID,
    GAMEPLAY_SURROUND_ASSET_SET_ID,
    GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION,
    GAMEPLAY_SURROUND_LAYOUT_VERSION,
    GAMEPLAY_SURROUND_RENDER_PLAN,
    GAMEPLAY_SURROUND_VARIANT_ID,
    GTA_DRIVING_ASSET_ID,
    GTA_DRIVING_VARIANT_ID,
    MINECRAFT_PARKOUR_ASSET_ID,
    MINECRAFT_PARKOUR_VARIANT_ID,
    SATISFYING_BACKGROUND_ASSET_ID,
    SATISFYING_VARIANT_ID,
    SUBWAY_SURFERS_ASSET_ID,
    SUBWAY_SURFERS_VARIANT_ID,
    background_variant_fingerprint,
    background_variant_label,
    background_variant_output,
    background_variant_state,
    default_background_asset_id,
    file_content_identity,
    gameplay_surround_caption_context_revision,
    load_background_asset,
    load_background_variant_asset,
    record_background_variant,
    require_background_variant_asset,
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


def _gameplay_asset_set(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    root.mkdir(exist_ok=True)
    for asset_id in (
        SUBWAY_SURFERS_ASSET_ID,
        GTA_DRIVING_ASSET_ID,
        MINECRAFT_PARKOUR_ASSET_ID,
    ):
        media = root / f"{asset_id}.mp4"
        media.write_bytes(f"silent {asset_id}".encode())
        (root / f"{asset_id}.json").write_text(
            json.dumps(
                {
                    "asset_id": asset_id,
                    "file": media.name,
                    "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                    "origin": "test generator",
                    "license": "original",
                    "playback_start_seconds": 1.0,
                    "focus_x": 0.5,
                    "focus_y": 0.5,
                    "fit_mode": (
                        "stretch" if asset_id == SUBWAY_SURFERS_ASSET_ID else "crop"
                    ),
                }
            )
        )
    monkeypatch.setenv("CASCADE_BACKGROUND_ASSETS_DIR", str(root))
    return load_background_variant_asset(
        GAMEPLAY_SURROUND_VARIANT_ID, verify_content=True
    )


def _write_gameplay_caption_context(episode_dir):
    episode = {
        "crop_config": {
            "speakers": [
                {
                    "label": "Host",
                    "center_x": 400,
                    "center_y": 520,
                    "zoom": 1.2,
                    "longform_center_x": 400,
                    "longform_center_y": 500,
                    "longform_zoom": 1,
                },
                {
                    "label": "Guest",
                    "center_x": 1500,
                    "center_y": 520,
                    "zoom": 1.2,
                    "longform_center_x": 1500,
                    "longform_center_y": 500,
                    "longform_zoom": 1,
                },
            ]
        }
    }
    diarized = {
        "clock": "source",
        "speaker_map": [{"index": 5, "logical_track": 1, "mapping_confidence": 1.0}],
    }
    segments = {
        "clock": "source",
        "track_mapping": [
            {"speaker": "speaker_0", "person": "Host", "logical_track": 1},
            {"speaker": "speaker_1", "person": "Guest", "logical_track": 2},
        ],
    }
    for filename, document in (
        ("episode.json", episode),
        ("diarized_transcript.json", diarized),
        ("segments.json", segments),
    ):
        (episode_dir / filename).write_text(json.dumps(document))
    return episode, diarized, segments


@pytest.mark.parametrize(
    ("variant_id", "asset_id", "label"),
    (
        (
            MINECRAFT_PARKOUR_VARIANT_ID,
            MINECRAFT_PARKOUR_ASSET_ID,
            "Minecraft parkour",
        ),
        (
            SUBWAY_SURFERS_VARIANT_ID,
            SUBWAY_SURFERS_ASSET_ID,
            "Subway Surfers",
        ),
        (GTA_DRIVING_VARIANT_ID, GTA_DRIVING_ASSET_ID, "GTA driving"),
    ),
)
def test_gameplay_variants_bind_stable_asset_identity(variant_id, asset_id, label):
    assert default_background_asset_id(variant_id) == asset_id
    assert background_variant_label(variant_id) == label
    require_background_variant_asset(variant_id, asset_id)
    with pytest.raises(KeyError, match=f"{variant_id} requires {asset_id}"):
        require_background_variant_asset(variant_id, "another_asset")


def test_gameplay_surround_binds_all_assets_and_layout(tmp_path, monkeypatch):
    asset_set = _gameplay_asset_set(tmp_path, monkeypatch)

    assert default_background_asset_id(GAMEPLAY_SURROUND_VARIANT_ID) == (
        GAMEPLAY_SURROUND_ASSET_SET_ID
    )
    assert [item["role"] for item in asset_set["assets"]] == [
        "subway",
        "gta",
        "minecraft",
    ]
    assert [item["asset_id"] for item in asset_set["assets"]] == [
        SUBWAY_SURFERS_ASSET_ID,
        GTA_DRIVING_ASSET_ID,
        MINECRAFT_PARKOUR_ASSET_ID,
    ]
    assert asset_set["render_plan"] == GAMEPLAY_SURROUND_RENDER_PLAN
    require_background_variant_asset(
        GAMEPLAY_SURROUND_VARIANT_ID, GAMEPLAY_SURROUND_ASSET_SET_ID
    )


def test_gameplay_surround_currentness_binds_assets_and_effective_caption_context(
    tmp_path, monkeypatch
):
    episode_dir = tmp_path / "episode"
    base = episode_dir / "shorts" / "clip_01.mp4"
    output = background_variant_output(
        episode_dir, "clip_01", GAMEPLAY_SURROUND_VARIANT_ID
    )
    for path, content in ((base, b"base"), (output, b"surround")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (episode_dir / "captions.ass").write_text("[Script Info]\n")
    _write_gameplay_caption_context(episode_dir)
    caption_context_revision = gameplay_surround_caption_context_revision(episode_dir)
    asset_set = _gameplay_asset_set(tmp_path, monkeypatch)
    base_record = {"fingerprint": "sha256:base"}
    base_identity = file_content_identity(base)["scan_identity"]
    encoding = {"video_bitrate": "10M", "audio_bitrate": "192k"}
    fingerprint = background_variant_fingerprint(
        base_record,
        base_identity,
        asset_set,
        encoding,
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
        caption_context_revision=caption_context_revision,
    )
    changed_focus = deepcopy(asset_set)
    changed_focus["assets"][1]["focus_x"] = 0.625
    assert (
        background_variant_fingerprint(
            base_record,
            base_identity,
            changed_focus,
            encoding,
            variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
            caption_context_revision=caption_context_revision,
        )
        != fingerprint
    )
    changed_fit = deepcopy(asset_set)
    changed_fit["assets"][0]["fit_mode"] = CONTAIN_BLUR_FIT_MODE
    assert (
        background_variant_fingerprint(
            base_record,
            base_identity,
            changed_fit,
            encoding,
            variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
            caption_context_revision=caption_context_revision,
        )
        != fingerprint
    )
    from lib import short_variants as short_variants_module

    with monkeypatch.context() as patch_context:
        patch_context.setattr(
            short_variants_module,
            "GAMEPLAY_SURROUND_LAYOUT_VERSION",
            "gameplay-surround/test-change",
        )
        assert (
            background_variant_fingerprint(
                base_record,
                base_identity,
                asset_set,
                encoding,
                variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
                caption_context_revision=caption_context_revision,
            )
            != fingerprint
        )
    record = record_background_variant(
        episode_dir,
        "clip_01",
        fingerprint=fingerprint,
        timeline=Timeline.from_edits(1),
        media={"duration_seconds": 1, "width": 1080, "height": 1920},
        base_record=base_record,
        base_identity=base_identity,
        asset=asset_set,
        encoding=encoding,
        captions={
            "path": "captions.ass",
            "format": "ass",
            "burned_in": True,
            "placement_policy": GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION,
            "context_revision": caption_context_revision,
        },
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
    )

    assert record["layout_version"] == GAMEPLAY_SURROUND_LAYOUT_VERSION
    assert record["captions"]["placement_policy"] == (
        GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION
    )
    assert record["captions"]["context_revision"] == caption_context_revision
    assert record["asset"]["render_plan"] == GAMEPLAY_SURROUND_RENDER_PLAN
    assert len(record["asset"]["assets"]) == 3
    _, current = background_variant_state(
        episode_dir,
        "clip_01",
        base_record=base_record,
        encoding=encoding,
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
    )
    assert current["current"] is True

    transcript_path = episode_dir / "diarized_transcript.json"
    transcript = json.loads(transcript_path.read_text())
    transcript["speaker_map"][0]["mapping_confidence"] = 0.9
    transcript_path.write_text(json.dumps(transcript))
    segments_path = episode_dir / "segments.json"
    segments = json.loads(segments_path.read_text())
    segments["track_mapping"][0]["person"] = "Renamed host"
    segments_path.write_text(json.dumps(segments))
    assert gameplay_surround_caption_context_revision(episode_dir) == (
        caption_context_revision
    )
    _, still_current = background_variant_state(
        episode_dir,
        "clip_01",
        base_record=base_record,
        encoding=encoding,
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
    )
    assert still_current["current"] is True

    segments["track_mapping"][0]["logical_track"] = 2
    segments["track_mapping"][1]["logical_track"] = 1
    segments_path.write_text(json.dumps(segments))
    _, rebound = background_variant_state(
        episode_dir,
        "clip_01",
        base_record=base_record,
        encoding=encoding,
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
    )
    assert rebound["current"] is False
    assert "speaker bindings or panel anchors changed" in rebound["detail"]

    _, _, original_segments = _write_gameplay_caption_context(episode_dir)
    assert json.loads(segments_path.read_text()) == original_segments
    episode_path = episode_dir / "episode.json"
    original_episode = json.loads(episode_path.read_text())
    reordered_episode = deepcopy(original_episode)
    reordered_episode["crop_config"]["speakers"][0]["longform_center_x"] = 1600
    reordered_episode["crop_config"]["speakers"][1]["longform_center_x"] = 300
    assert visual_crop_state(
        original_episode["crop_config"], "short"
    ) == visual_crop_state(reordered_episode["crop_config"], "short")
    episode_path.write_text(json.dumps(reordered_episode))
    _, reordered = background_variant_state(
        episode_dir,
        "clip_01",
        base_record=base_record,
        encoding=encoding,
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
    )
    assert reordered["current"] is False
    assert "speaker bindings or panel anchors changed" in reordered["detail"]
    episode_path.write_text(json.dumps(original_episode))

    manifest = tmp_path / "assets" / f"{GTA_DRIVING_ASSET_ID}.json"
    changed = json.loads(manifest.read_text())
    changed["description"] = "manifest changed"
    manifest.write_text(json.dumps(changed))
    _, stale = background_variant_state(
        episode_dir,
        "clip_01",
        base_record=base_record,
        encoding=encoding,
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
    )
    assert stale["current"] is False
    assert "asset or its render plan changed" in stale["detail"]


def test_caption_policy_invalidates_only_gameplay_surround(tmp_path, monkeypatch):
    ordinary_asset = _asset(tmp_path, monkeypatch)
    gameplay_assets = _gameplay_asset_set(tmp_path, monkeypatch)
    base_record = {"fingerprint": "sha256:base"}
    base_identity = {"device": 1, "inode": 2, "size_bytes": 3, "mtime_ns": 4}
    encoding = {"video_bitrate": "10M", "audio_bitrate": "192k"}
    caption_context_revision = "sha256:" + "a" * 64
    ordinary = background_variant_fingerprint(
        base_record, base_identity, ordinary_asset, encoding
    )
    assert (
        background_variant_fingerprint(
            base_record,
            base_identity,
            ordinary_asset,
            encoding,
            caption_context_revision="sha256:gameplay-only",
        )
        == ordinary
    )
    gameplay = background_variant_fingerprint(
        base_record,
        base_identity,
        gameplay_assets,
        encoding,
        variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
        caption_context_revision=caption_context_revision,
    )
    from lib import short_variants as short_variants_module

    monkeypatch.setattr(
        short_variants_module,
        "GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION",
        "source-speaker-panel/test-change",
    )

    assert (
        background_variant_fingerprint(
            base_record, base_identity, ordinary_asset, encoding
        )
        == ordinary
    )
    assert (
        background_variant_fingerprint(
            base_record,
            base_identity,
            gameplay_assets,
            encoding,
            variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
            caption_context_revision=caption_context_revision,
        )
        != gameplay
    )

    with pytest.raises(ValueError, match="valid caption context revision"):
        background_variant_fingerprint(
            base_record,
            base_identity,
            gameplay_assets,
            encoding,
            variant_id=GAMEPLAY_SURROUND_VARIANT_ID,
        )


def test_variant_artifact_paths_are_isolated(tmp_path):
    outputs = {
        variant_id: background_variant_output(tmp_path, "clip_01", variant_id)
        for variant_id in BACKGROUND_VARIANT_IDS
    }

    assert len(set(outputs.values())) == len(BACKGROUND_VARIANT_IDS)
    assert outputs[BACKGROUND_VARIANT_ID] == (
        tmp_path / "short_variants" / "background_motion_v1" / "clip_01.mp4"
    )
    assert outputs[SATISFYING_VARIANT_ID] == (
        tmp_path / "short_variants" / "satisfying_motion_v1" / "clip_01.mp4"
    )
    assert (
        default_background_asset_id(BACKGROUND_VARIANT_ID)
        == DEFAULT_BACKGROUND_ASSET_ID
    )
    assert (
        default_background_asset_id(SATISFYING_VARIANT_ID)
        == SATISFYING_BACKGROUND_ASSET_ID
    )
    assert outputs[MINECRAFT_PARKOUR_VARIANT_ID].parent.name == (
        MINECRAFT_PARKOUR_VARIANT_ID
    )
    assert outputs[SUBWAY_SURFERS_VARIANT_ID].parent.name == SUBWAY_SURFERS_VARIANT_ID
    assert outputs[GTA_DRIVING_VARIANT_ID].parent.name == GTA_DRIVING_VARIANT_ID


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


def test_asset_manifest_rejects_unknown_fit_mode(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    root.mkdir()
    media = root / "motion.mp4"
    media.write_bytes(b"silent motion")
    (root / "motion.json").write_text(
        json.dumps(
            {
                "asset_id": "motion_v1",
                "file": media.name,
                "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "fit_mode": "follow-subject",
            }
        )
    )
    monkeypatch.setenv("CASCADE_BACKGROUND_ASSETS_DIR", str(root))

    with pytest.raises(ValueError, match="invalid fit_mode"):
        load_background_asset("motion_v1")


@pytest.mark.parametrize("fit_mode", ["crop", "stretch", CONTAIN_BLUR_FIT_MODE])
def test_asset_manifest_accepts_supported_fit_modes(tmp_path, monkeypatch, fit_mode):
    root = tmp_path / "assets"
    root.mkdir()
    media = root / "motion.mp4"
    media.write_bytes(b"silent motion")
    (root / "motion.json").write_text(
        json.dumps(
            {
                "asset_id": "motion_v1",
                "file": media.name,
                "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "fit_mode": fit_mode,
            }
        )
    )
    monkeypatch.setenv("CASCADE_BACKGROUND_ASSETS_DIR", str(root))

    assert load_background_asset("motion_v1")["fit_mode"] == fit_mode


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


def test_missing_distribution_selection_defaults_to_exact_base_approval(tmp_path):
    clip = {"id": "clip_01", "status": "approved"}
    base_record = {
        "fingerprint": "sha256:base",
        "output": {"content_revision": "sha256:base-pixels"},
    }
    revision = clip_review_revision(clip, base_record)
    clip.update(
        approved_render_fingerprint=base_record["fingerprint"],
        approved_revision=revision,
    )

    state = short_distribution_state(
        tmp_path,
        {},
        {},
        clip,
        base_record,
    )

    assert state["version"] == "base"
    assert state["variant_id"] is None
    assert state["current"] is True
    assert state["approval_current"] is True
    assert state["revision"] == revision
    assert state["path"] == "shorts/clip_01.mp4"


def test_selected_variant_requires_its_own_current_pixels_and_approval(
    tmp_path, monkeypatch
):
    episode_dir, base_record, encoding, record, output = _record(tmp_path, monkeypatch)
    clip = {
        "id": "clip_01",
        "status": "pending",
        "distribution_variant_id": BACKGROUND_VARIANT_ID,
    }
    revision = clip_review_revision(clip, record)
    assert save_background_variant_approval(episode_dir, "clip_01", record, revision)

    from agents import qa

    monkeypatch.setattr(qa, "render_config_for_episode", lambda *_args: {})
    monkeypatch.setattr(qa, "get_video_encoding_policy", lambda *_args: encoding)
    selected = short_distribution_state(
        episode_dir,
        {},
        {},
        clip,
        base_record,
    )

    assert selected["version"] == BACKGROUND_VARIANT_ID
    assert selected["current"] is True
    assert selected["approval_current"] is True
    assert selected["revision"] == revision
    assert selected["path"].endswith(f"/{BACKGROUND_VARIANT_ID}/clip_01.mp4")

    stat = output.stat()
    output.write_bytes(b"X" * stat.st_size)
    os.utime(output, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replaced = short_distribution_state(
        episode_dir,
        {},
        {},
        clip,
        base_record,
    )
    assert replaced["current"] is False
    assert replaced["approval_current"] is False
