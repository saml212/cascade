"""Identity and review state for optional short-video variants."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from lib.ass import caption_ranges_use_target, resolve_caption_speaker_targets
from lib.atomic_write import atomic_write_json
from lib.caption_speaker_overrides import (
    apply_current_caption_speaker_overrides,
    caption_speaker_overrides_path,
)
from lib.clips import load_clips
from lib.crop import visual_crop_state
from lib.delivery_video import render_artifact_state
from lib.ffprobe import file_fingerprint, probe, scan_identity
from lib.gameplay_playback import (
    require_resolved_gameplay_playback,
    resolve_gameplay_asset_playback,
)
from lib.timeline import Timeline

BACKGROUND_VARIANT_ID = "background_motion_v1"
SATISFYING_VARIANT_ID = "satisfying_motion_v1"
MINECRAFT_PARKOUR_VARIANT_ID = "minecraft_parkour_v1"
SUBWAY_SURFERS_VARIANT_ID = "subway_surfers_v1"
GTA_DRIVING_VARIANT_ID = "gta_driving_v1"
GAMEPLAY_SURROUND_VARIANT_ID = "gameplay_surround_v1"
SPEAKER_PANELS_VARIANT_ID = "speaker_panels_v1"
BACKGROUND_VARIANT_MODE = "speaker_cut_short_background_motion_v1"
BACKGROUND_LAYOUT_VERSION = "portrait-over-motion/v4"
GAMEPLAY_SURROUND_VARIANT_MODE = "podcast_gameplay_surround_v1"
GAMEPLAY_SURROUND_LAYOUT_VERSION = "gameplay-surround/v1"
GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION = "source-speaker-panel/v1"
SPEAKER_PANELS_VARIANT_MODE = "podcast_speaker_panels_v1"
SPEAKER_PANELS_LAYOUT_VERSION = "speaker-panels/v1"
SPEAKER_PANEL_CAPTION_CONTEXT_VERSION = "speaker-panel-effective/v1"
GAMEPLAY_SURROUND_CAPTION_CONTEXT_VERSION = SPEAKER_PANEL_CAPTION_CONTEXT_VERSION
DEFAULT_BACKGROUND_ASSET_ID = "original_block_parkour_v1"
SATISFYING_BACKGROUND_ASSET_ID = "mixkit-47347"
MINECRAFT_PARKOUR_ASSET_ID = "spicy_sauce_minecraft_12_v1"
SUBWAY_SURFERS_ASSET_ID = "orbitalncg_subway_surfers_12_v1"
GTA_DRIVING_ASSET_ID = "orbitalncg_gta_driving_15_v1"
GAMEPLAY_SURROUND_ASSET_SET_ID = "gameplay_surround_assets_v1"
GAMEPLAY_SURROUND_ASSETS = (
    ("subway", SUBWAY_SURFERS_ASSET_ID),
    ("gta", GTA_DRIVING_ASSET_ID),
    ("minecraft", MINECRAFT_PARKOUR_ASSET_ID),
)
CONTAIN_BLUR_FIT_MODE = "contain_blur_v1"
BACKGROUND_FIT_MODES = frozenset({"crop", "stretch", CONTAIN_BLUR_FIT_MODE})
GAMEPLAY_SURROUND_RENDER_PLAN = {
    "canvas": [1080, 1920],
    "upper_height": 1216,
    "bottom_height": 704,
    "left_width": 270,
    "podcast_width": 540,
    "right_width": 270,
    "podcast_header_height": 72,
    "panel_border": 6,
    "asset_roles": [role for role, _ in GAMEPLAY_SURROUND_ASSETS],
    "brand": "thelocalpod.link",
    "brand_font_size": 36,
    "brand_y": 1286,
}
SPEAKER_PANELS_RENDER_PLAN = {
    "canvas": [1080, 1920],
    "header_height": 72,
    "panels_height": 1776,
    "footer_height": 72,
    "panel_width": 1080,
    "panel_border": 6,
    "title": "THE LOCAL PODCAST",
    "title_font_size": 28,
    "brand": "thelocalpod.link",
    "brand_font_size": 26,
}
CLEAN_NEUTRAL_HEADER_POLICY = {
    "version": "clean-neutral-header/v2",
    "font_size_px": 48,
    "background_box": [0, 0, 1080, 72],
}
BASE_SHORT_VERSION = "base"
DISTRIBUTION_VARIANT_FIELD = "distribution_variant_id"
DISTRIBUTION_RELEASE_FIELD = "distribution_release"

_VARIANT_LABELS = {
    BACKGROUND_VARIANT_ID: "Motion background",
    SATISFYING_VARIANT_ID: "Satisfying footage",
    MINECRAFT_PARKOUR_VARIANT_ID: "Minecraft parkour",
    SUBWAY_SURFERS_VARIANT_ID: "Subway Surfers",
    GTA_DRIVING_VARIANT_ID: "GTA driving",
    GAMEPLAY_SURROUND_VARIANT_ID: "Gameplay surround",
    SPEAKER_PANELS_VARIANT_ID: "Clean speaker panels",
}
_VARIANT_ASSETS = {
    BACKGROUND_VARIANT_ID: DEFAULT_BACKGROUND_ASSET_ID,
    SATISFYING_VARIANT_ID: SATISFYING_BACKGROUND_ASSET_ID,
    MINECRAFT_PARKOUR_VARIANT_ID: MINECRAFT_PARKOUR_ASSET_ID,
    SUBWAY_SURFERS_VARIANT_ID: SUBWAY_SURFERS_ASSET_ID,
    GTA_DRIVING_VARIANT_ID: GTA_DRIVING_ASSET_ID,
    GAMEPLAY_SURROUND_VARIANT_ID: GAMEPLAY_SURROUND_ASSET_SET_ID,
    SPEAKER_PANELS_VARIANT_ID: None,
}
BACKGROUND_VARIANT_IDS = tuple(_VARIANT_LABELS)
GAMEPLAY_VARIANT_IDS = frozenset(
    {
        MINECRAFT_PARKOUR_VARIANT_ID,
        SUBWAY_SURFERS_VARIANT_ID,
        GTA_DRIVING_VARIANT_ID,
        GAMEPLAY_SURROUND_VARIANT_ID,
    }
)
SPEAKER_PANEL_VARIANT_IDS = frozenset(
    {GAMEPLAY_SURROUND_VARIANT_ID, SPEAKER_PANELS_VARIANT_ID}
)

_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def background_assets_dir() -> Path:
    configured = os.getenv("CASCADE_BACKGROUND_ASSETS_DIR")
    if configured:
        return Path(configured).expanduser()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Cascade"
        / "background-assets"
    )


def require_background_variant(variant_id: str) -> None:
    if variant_id not in BACKGROUND_VARIANT_IDS:
        raise KeyError(f"Unknown short variant: {variant_id}")


def background_variant_label(variant_id: str) -> str:
    require_background_variant(variant_id)
    return _VARIANT_LABELS[variant_id]


def default_background_asset_id(variant_id: str) -> str | None:
    require_background_variant(variant_id)
    return _VARIANT_ASSETS[variant_id]


def require_background_variant_asset(variant_id: str, asset_id: str | None) -> None:
    require_background_variant(variant_id)
    expected_asset_id = _VARIANT_ASSETS[variant_id]
    if expected_asset_id is None:
        if asset_id is not None:
            raise KeyError(f"{variant_id} does not use a background asset")
        return
    if variant_id != BACKGROUND_VARIANT_ID and asset_id != expected_asset_id:
        raise KeyError(f"{variant_id} requires {expected_asset_id}")


def background_variant_asset_ids(variant_id: str) -> tuple[str, ...]:
    """Return the immutable media asset IDs required by a variant."""
    require_background_variant(variant_id)
    if variant_id == SPEAKER_PANELS_VARIANT_ID:
        return ()
    if variant_id == GAMEPLAY_SURROUND_VARIANT_ID:
        return tuple(asset_id for _, asset_id in GAMEPLAY_SURROUND_ASSETS)
    return (_VARIANT_ASSETS[variant_id],)


def selected_short_variant_id(clip: dict) -> str | None:
    """Return the explicit distribution variant; missing means canonical base."""
    variant_id = clip.get(DISTRIBUTION_VARIANT_FIELD)
    if variant_id is None:
        return None
    if not isinstance(variant_id, str):
        raise KeyError(f"Unknown short variant: {variant_id}")
    require_background_variant(variant_id)
    return variant_id


def distribution_release_revision(
    *,
    request_id: str,
    actor: str,
    reason: str,
    variant_id: str | None,
    target_revision: str,
    render_fingerprint: str,
    receipt_history_revision: str,
    unresolved_history_acknowledgement: dict | None = None,
) -> str:
    inputs = {
        "request_id": request_id,
        "actor": actor,
        "reason": reason,
        "variant_id": variant_id,
        "target_revision": target_revision,
        "render_fingerprint": render_fingerprint,
        "receipt_history_revision": receipt_history_revision,
    }
    if unresolved_history_acknowledgement is not None:
        inputs["unresolved_history_acknowledgement"] = (
            unresolved_history_acknowledgement
        )
    return _json_revision(inputs)


def background_variant_output(
    episode_dir: Path,
    clip_id: str,
    variant_id: str = BACKGROUND_VARIANT_ID,
) -> Path:
    require_background_variant(variant_id)
    return Path(episode_dir) / "short_variants" / variant_id / f"{clip_id}.mp4"


def _record_path(
    episode_dir: Path,
    clip_id: str,
    variant_id: str = BACKGROUND_VARIANT_ID,
) -> Path:
    return background_variant_output(episode_dir, clip_id, variant_id).with_suffix(
        ".json"
    )


def _scan(path: Path) -> dict:
    identity = scan_identity(path)
    if identity is None:
        raise OSError(f"{path} changed while its identity was read")
    return identity


def file_content_identity(path: Path, expected_revision: str | None = None) -> dict:
    """Hash stable bytes and retain the filesystem identity used for the read."""
    before = _scan(path)
    revision = file_fingerprint(path)["id"]
    after = _scan(path)
    if before != after:
        raise OSError(f"{path} changed while its content was read")
    if expected_revision is not None and revision != expected_revision:
        raise ValueError(f"{path.name} does not match its recorded SHA-256")
    return {"content_revision": revision, "scan_identity": after}


def _json_revision(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _asset_file(manifest_path: Path, manifest: dict, root: Path) -> Path:
    configured = manifest.get("file") or manifest.get("path")
    if not isinstance(configured, str) or not configured:
        raise ValueError("Background asset manifest has no media path")
    path = Path(configured).expanduser()
    path = (manifest_path.parent / path if not path.is_absolute() else path).resolve(
        strict=True
    )
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Background asset media must stay inside the asset directory"
        ) from exc
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("Background asset media is missing or empty")
    return path


def load_background_asset(asset_id: str, *, verify_content: bool = False) -> dict:
    """Resolve one explicitly identified local asset and its checked provenance.

    Gameplay-surround asset manifests may omit ``fit_mode`` for the default
    aspect-fill crop. Their panels may select ``crop``, ``stretch``, or
    ``contain_blur_v1``. The latter keeps the complete source frame proportional
    over a darkened, blurred aspect-fill copy so a narrow panel has no empty
    bars.
    """
    if not _ASSET_ID.fullmatch(asset_id):
        raise KeyError(f"Unknown background asset: {asset_id}")
    root = background_assets_dir().resolve(strict=True)
    matches = []
    for manifest_path in root.rglob("*.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(manifest, dict) and manifest.get("asset_id") == asset_id:
            matches.append((manifest_path, manifest))
    if len(matches) != 1:
        raise KeyError(f"Unknown background asset: {asset_id}")

    manifest_path, manifest = matches[0]
    path = _asset_file(manifest_path, manifest, root)
    declared_sha = str(manifest.get("sha256", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", declared_sha):
        raise ValueError("Background asset manifest has no valid SHA-256")
    content_revision = f"sha256:{declared_sha}"
    content = (
        file_content_identity(path, content_revision)
        if verify_content
        else {
            "content_revision": content_revision,
            "scan_identity": _scan(path),
        }
    )

    provenance = {
        key: manifest[key]
        for key in (
            "title",
            "description",
            "origin",
            "source_url",
            "license",
            "license_url",
            "license_verified_at",
            "license_summary",
            "usage",
        )
        if manifest.get(key) is not None
    }
    playback = {}
    for key, default, minimum, maximum in (
        ("playback_start_seconds", 0.0, 0.0, None),
        ("focus_x", 0.5, 0.0, 1.0),
        ("focus_y", 0.5, 0.0, 1.0),
    ):
        if key not in manifest:
            continue
        value = manifest.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < minimum
            or (maximum is not None and value > maximum)
        ):
            raise ValueError(f"Background asset manifest has invalid {key}")
        playback[key] = float(value)
    fit_mode = manifest.get("fit_mode")
    if fit_mode is not None:
        if fit_mode not in BACKGROUND_FIT_MODES:
            raise ValueError("Background asset manifest has invalid fit_mode")
        playback["fit_mode"] = fit_mode
    if "playback_loop" in manifest:
        playback_loop = manifest.get("playback_loop")
        if not isinstance(playback_loop, bool):
            raise ValueError("Background asset manifest has invalid playback_loop")
        playback["playback_loop"] = playback_loop
    return {
        "asset_id": asset_id,
        "path": path,
        "content_revision": content["content_revision"],
        "manifest_revision": _json_revision(manifest),
        "scan_identity": content["scan_identity"],
        "provenance": provenance,
        **playback,
    }


def load_background_variant_asset(
    variant_id: str, *, verify_content: bool = False
) -> dict | None:
    """Load the fixed asset, or fixed multi-asset set, required by a variant."""
    require_background_variant(variant_id)
    if variant_id == SPEAKER_PANELS_VARIANT_ID:
        return None
    if variant_id != GAMEPLAY_SURROUND_VARIANT_ID:
        return load_background_asset(
            default_background_asset_id(variant_id), verify_content=verify_content
        )
    assets = []
    for role, asset_id in GAMEPLAY_SURROUND_ASSETS:
        assets.append(
            {
                "role": role,
                **load_background_asset(asset_id, verify_content=verify_content),
            }
        )
    return {
        "asset_id": GAMEPLAY_SURROUND_ASSET_SET_ID,
        "assets": assets,
        "render_plan": GAMEPLAY_SURROUND_RENDER_PLAN,
    }


def resolve_gameplay_variant_playback(
    asset: dict,
    *,
    episode_id: str,
    clip_id: str,
    variant_id: str,
    clip_duration_seconds: float,
    source_durations: dict[str, float],
) -> dict:
    """Resolve every gameplay source to a deterministic per-clip window."""
    if variant_id not in GAMEPLAY_VARIANT_IDS:
        raise ValueError(f"{variant_id} is not a gameplay variant")
    if not isinstance(asset, dict) or not isinstance(source_durations, dict):
        raise TypeError("Gameplay playback resolution needs assets and durations")
    require_background_variant_asset(variant_id, asset.get("asset_id"))
    resolved = deepcopy(asset)
    media_assets = (
        resolved.get("assets")
        if variant_id == GAMEPLAY_SURROUND_VARIANT_ID
        else [resolved]
    )
    if not isinstance(media_assets, list) or not media_assets:
        raise TypeError("Gameplay playback resolution needs source assets")
    resolved_assets = []
    for media_asset in media_assets:
        if not isinstance(media_asset, dict):
            raise TypeError("Gameplay playback resolution needs source mappings")
        source_id = media_asset.get("asset_id")
        if not isinstance(source_id, str) or source_id not in source_durations:
            raise ValueError(f"Gameplay source duration is missing for {source_id}")
        resolved_assets.append(
            resolve_gameplay_asset_playback(
                media_asset,
                episode_id=episode_id,
                clip_id=clip_id,
                variant_id=variant_id,
                clip_duration_seconds=clip_duration_seconds,
                source_duration_seconds=source_durations[source_id],
            )
        )
    if variant_id == GAMEPLAY_SURROUND_VARIANT_ID:
        resolved["assets"] = resolved_assets
        return resolved
    return resolved_assets[0]


def _require_gameplay_variant_playback(asset: dict, variant_id: str) -> None:
    if variant_id not in GAMEPLAY_VARIANT_IDS:
        return
    media_assets = (
        asset.get("assets") if variant_id == GAMEPLAY_SURROUND_VARIANT_ID else [asset]
    )
    if not isinstance(media_assets, list) or not media_assets:
        raise ValueError("Gameplay variant has no resolved source assets")
    for media_asset in media_assets:
        require_resolved_gameplay_playback(media_asset)


@lru_cache(maxsize=512)
def _probed_media_duration(identity: tuple) -> float:
    result = probe(Path(identity[0]))
    duration = float(result.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Media has no positive duration: {Path(identity[0]).name}")
    return duration


def _media_duration(path: Path) -> float:
    identity = _scan(path)
    return _probed_media_duration(
        (
            identity["resolved_path"],
            identity["device"],
            identity["inode"],
            identity["size_bytes"],
            identity["mtime_ns"],
            identity["ctime_ns"],
        )
    )


def _asset_record(asset: dict, *, include_provenance: bool) -> dict:
    keys = [
        "asset_id",
        "content_revision",
        "manifest_revision",
        "scan_identity",
        "playback_start_seconds",
        "playback_loop",
        "playback_policy",
        "focus_x",
        "focus_y",
        "fit_mode",
    ]
    if include_provenance:
        keys.append("provenance")
    recorded = {key: asset[key] for key in keys if key in asset}
    if "role" in asset:
        recorded["role"] = asset["role"]
    return recorded


def _variant_asset_record(
    asset: dict | None, *, include_provenance: bool
) -> dict | None:
    if asset is None:
        return None
    if asset.get("asset_id") != GAMEPLAY_SURROUND_ASSET_SET_ID:
        return _asset_record(asset, include_provenance=include_provenance)
    assets = asset.get("assets")
    if not isinstance(assets, list) or len(assets) != len(GAMEPLAY_SURROUND_ASSETS):
        raise TypeError("Gameplay surround requires its three fixed assets")
    identities = [
        (item.get("role"), item.get("asset_id"))
        for item in assets
        if isinstance(item, dict)
    ]
    if identities != list(GAMEPLAY_SURROUND_ASSETS):
        raise TypeError("Gameplay surround asset roles or identities are invalid")
    return {
        "asset_id": GAMEPLAY_SURROUND_ASSET_SET_ID,
        "assets": [
            _asset_record(item, include_provenance=include_provenance)
            for item in assets
        ],
        "render_plan": GAMEPLAY_SURROUND_RENDER_PLAN,
    }


def _recorded_asset_identity(recorded: dict | None) -> dict | None:
    if recorded is None:
        return None
    asset_id = recorded.get("asset_id")
    if asset_id != GAMEPLAY_SURROUND_ASSET_SET_ID:
        return {
            key: recorded[key]
            for key in (
                "asset_id",
                "content_revision",
                "manifest_revision",
                "scan_identity",
                "playback_start_seconds",
                "playback_loop",
                "playback_policy",
                "focus_x",
                "focus_y",
                "fit_mode",
            )
            if key in recorded
        }
    assets = recorded.get("assets")
    if not isinstance(assets, list) or any(
        not isinstance(item, dict) for item in assets
    ):
        raise TypeError("The gameplay surround asset manifest is malformed")
    return {
        "asset_id": asset_id,
        "assets": [
            {
                key: item[key]
                for key in (
                    "asset_id",
                    "content_revision",
                    "manifest_revision",
                    "scan_identity",
                    "role",
                    "playback_start_seconds",
                    "playback_loop",
                    "playback_policy",
                    "focus_x",
                    "focus_y",
                    "fit_mode",
                )
                if key in item
            }
            for item in assets
        ],
        "render_plan": recorded.get("render_plan"),
    }


def _variant_layout(variant_id: str) -> str:
    if variant_id == GAMEPLAY_SURROUND_VARIANT_ID:
        return GAMEPLAY_SURROUND_LAYOUT_VERSION
    if variant_id == SPEAKER_PANELS_VARIANT_ID:
        return SPEAKER_PANELS_LAYOUT_VERSION
    return BACKGROUND_LAYOUT_VERSION


def _variant_mode(variant_id: str) -> str:
    if variant_id == GAMEPLAY_SURROUND_VARIANT_ID:
        return GAMEPLAY_SURROUND_VARIANT_MODE
    if variant_id == SPEAKER_PANELS_VARIANT_ID:
        return SPEAKER_PANELS_VARIANT_MODE
    return BACKGROUND_VARIANT_MODE


def speaker_panel_caption_context_revision(
    episode_dir: Path,
    *,
    episode: dict | None = None,
    diarized: dict | None = None,
    segment_document: dict | None = None,
    caption_speaker_overrides: dict | None = None,
    neutral_header_policy: dict | None = None,
) -> str:
    """Fingerprint inputs unique to speaker-panel caption placement."""

    def load_document(filename: str) -> dict:
        try:
            document = json.loads((Path(episode_dir) / filename).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Speaker-panel caption context needs current {filename}"
            ) from exc
        if not isinstance(document, dict):
            raise TypeError(
                f"Speaker-panel caption context needs a mapping in {filename}"
            )
        return document

    episode = episode if episode is not None else load_document("episode.json")
    diarized = (
        diarized if diarized is not None else load_document("diarized_transcript.json")
    )
    segment_document = (
        segment_document
        if segment_document is not None
        else load_document("segments.json")
    )
    crop_config = episode.get("crop_config") or {}
    if not isinstance(crop_config, dict):
        raise TypeError("Speaker-panel caption context needs a crop mapping")
    speaker_targets = resolve_caption_speaker_targets(
        diarized, segment_document, crop_config
    )
    configured_speakers = crop_config.get("speakers", [])
    longform_speakers = (
        visual_crop_state(crop_config, "longform")["speakers"]
        if isinstance(configured_speakers, list) and len(configured_speakers) >= 2
        else []
    )
    state = {
        "version": SPEAKER_PANEL_CAPTION_CONTEXT_VERSION,
        "speaker_targets": [
            {"asr_speaker": source, "target": target}
            for source, target in sorted(speaker_targets.items())
        ],
        "longform_speakers": longform_speakers,
    }
    if caption_speaker_overrides is not None:
        state["caption_speaker_overrides"] = caption_speaker_overrides
    if neutral_header_policy is not None:
        if neutral_header_policy != CLEAN_NEUTRAL_HEADER_POLICY:
            raise ValueError("Unsupported clean neutral-header caption policy")
        state["neutral_header_policy"] = CLEAN_NEUTRAL_HEADER_POLICY
    return _json_revision(state)


def speaker_panel_neutral_header_policy(
    variant_id: str,
    diarized: dict,
    segment_document: dict,
    crop_config: dict,
    source_intervals: object,
) -> dict | None:
    """Select larger neutral captions only for affected clean-panel clips."""
    if variant_id != SPEAKER_PANELS_VARIANT_ID:
        return None
    if not isinstance(source_intervals, (list, tuple)):
        raise TypeError("Clean neutral-header policy needs clip source intervals")
    intervals = []
    for interval in source_intervals:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise TypeError(
                "Clean neutral-header policy has malformed source intervals"
            )
        start, end = float(interval[0]), float(interval[1])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("Clean neutral-header policy has invalid source intervals")
        intervals.append((start, end))
    if not intervals:
        raise ValueError("Clean neutral-header policy needs clip source intervals")
    speaker_targets = resolve_caption_speaker_targets(
        diarized, segment_document, crop_config
    )
    if caption_ranges_use_target(diarized, intervals, speaker_targets, "BOTH"):
        return deepcopy(CLEAN_NEUTRAL_HEADER_POLICY)
    return None


gameplay_surround_caption_context_revision = speaker_panel_caption_context_revision


def _current_speaker_panel_caption_state(
    episode_dir: Path,
    clip_id: str,
    variant_id: str,
    base_record: dict,
) -> tuple[str, dict | None, dict | None]:
    def load_document(filename: str) -> dict:
        try:
            value = json.loads((Path(episode_dir) / filename).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Speaker-panel caption context needs current {filename}"
            ) from exc
        if not isinstance(value, dict):
            raise TypeError(
                f"Speaker-panel caption context needs a mapping in {filename}"
            )
        return value

    episode = load_document("episode.json")
    diarized = load_document("diarized_transcript.json")
    segment_document = load_document("segments.json")
    crop_config = episode.get("crop_config") or {}
    if not isinstance(crop_config, dict):
        raise TypeError("Speaker-panel caption context needs a crop mapping")

    binding = None
    if caption_speaker_overrides_path(episode_dir, clip_id).exists():
        matching_clips = [
            clip for clip in load_clips(Path(episode_dir)) if clip.get("id") == clip_id
        ]
        if len(matching_clips) != 1:
            raise ValueError("Caption speaker overrides need one current matching clip")
        diarized, binding = apply_current_caption_speaker_overrides(
            Path(episode_dir),
            matching_clips[0],
            diarized,
            segment_document,
            crop_config,
        )
    neutral_header_policy = speaker_panel_neutral_header_policy(
        variant_id,
        diarized,
        segment_document,
        crop_config,
        base_record.get("clip_source_intervals"),
    )
    revision = speaker_panel_caption_context_revision(
        Path(episode_dir),
        episode=episode,
        diarized=diarized,
        segment_document=segment_document,
        caption_speaker_overrides=binding,
        neutral_header_policy=neutral_header_policy,
    )
    return revision, binding, neutral_header_policy


def require_speaker_panel_caption_context_revision(value: object) -> str:
    """Require the effective context bound to a speaker-panel render."""
    revision = str(value or "")
    if not _SHA256.fullmatch(revision):
        raise ValueError(
            "Speaker-panel variant requires a valid caption context revision"
        )
    return revision


def require_gameplay_caption_context_revision(value: object) -> str:
    revision = str(value or "")
    if not _SHA256.fullmatch(revision):
        raise ValueError("Gameplay surround requires a valid caption context revision")
    return revision


def background_variant_fingerprint(
    base_record: dict,
    base_identity: dict,
    asset: dict | None,
    encoding: dict,
    *,
    variant_id: str = BACKGROUND_VARIANT_ID,
    caption_context_revision: str | None = None,
) -> str:
    if variant_id != SPEAKER_PANELS_VARIANT_ID and not isinstance(asset, dict):
        raise TypeError("The short variant requires an asset record")
    asset_id = asset.get("asset_id") if isinstance(asset, dict) else None
    require_background_variant_asset(variant_id, asset_id)
    if isinstance(asset, dict):
        _require_gameplay_variant_playback(asset, variant_id)
    state = {
        "variant_id": variant_id,
        "layout": _variant_layout(variant_id),
        "base": {
            "render_fingerprint": base_record.get("fingerprint"),
            "scan_identity": base_identity,
        },
        "asset": _variant_asset_record(asset, include_provenance=False),
        "encoding": encoding,
    }
    if variant_id in SPEAKER_PANEL_VARIANT_IDS:
        caption_context_revision = (
            require_gameplay_caption_context_revision(caption_context_revision)
            if variant_id == GAMEPLAY_SURROUND_VARIANT_ID
            else require_speaker_panel_caption_context_revision(
                caption_context_revision
            )
        )
        state["caption_policy"] = {
            "version": GAMEPLAY_SURROUND_CAPTION_POLICY_VERSION,
            "context_revision": caption_context_revision,
        }
    if variant_id == SPEAKER_PANELS_VARIANT_ID:
        state["render_plan"] = SPEAKER_PANELS_RENDER_PLAN
    return _json_revision(state)


def variant_record(
    episode_dir: Path,
    clip_id: str,
    variant_id: str = BACKGROUND_VARIANT_ID,
) -> dict:
    try:
        value = json.loads(_record_path(episode_dir, clip_id, variant_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def record_background_variant(
    episode_dir: Path,
    clip_id: str,
    *,
    fingerprint: str,
    timeline: Timeline,
    media: dict,
    base_record: dict,
    base_identity: dict,
    asset: dict | None,
    encoding: dict,
    captions: dict,
    variant_id: str = BACKGROUND_VARIANT_ID,
) -> dict:
    if variant_id != SPEAKER_PANELS_VARIANT_ID and not isinstance(asset, dict):
        raise TypeError("The short variant requires an asset record")
    asset_id = asset.get("asset_id") if isinstance(asset, dict) else None
    require_background_variant_asset(variant_id, asset_id)
    if isinstance(asset, dict):
        _require_gameplay_variant_playback(asset, variant_id)
    output = background_variant_output(episode_dir, clip_id, variant_id)
    output_content = file_content_identity(output)
    output_identity = output_content["scan_identity"]
    record = {
        "variant_id": variant_id,
        "path": str(output.relative_to(episode_dir)),
        "render_mode": _variant_mode(variant_id),
        "layout_version": _variant_layout(variant_id),
        "fingerprint": fingerprint,
        "clip_source_intervals": [list(item) for item in timeline.keep_intervals],
        "output_duration_seconds": round(timeline.duration, 3),
        "base_render": {
            "fingerprint": base_record["fingerprint"],
            "scan_identity": base_identity,
        },
        "asset": _variant_asset_record(asset, include_provenance=True),
        **(
            {"render_plan": SPEAKER_PANELS_RENDER_PLAN}
            if variant_id == SPEAKER_PANELS_VARIANT_ID
            else {}
        ),
        "encoding": encoding,
        "captions": captions,
        "output": {
            **media,
            "scan_identity": output_identity,
            "content_revision": output_content["content_revision"],
            "size_bytes": output_identity["size_bytes"],
            "mtime_ns": output_identity["mtime_ns"],
        },
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    # The renderer holds this variant's render_output_lock through this write.
    atomic_write_json(_record_path(episode_dir, clip_id, variant_id), record)
    return record


def background_variant_state(
    episode_dir: Path,
    clip_id: str,
    *,
    base_record: dict | None,
    encoding: dict,
    variant_id: str = BACKGROUND_VARIANT_ID,
) -> tuple[dict, dict]:
    """Return a recorded variant and shared render-artifact state."""
    require_background_variant(variant_id)
    output = background_variant_output(episode_dir, clip_id, variant_id)
    record = variant_record(episode_dir, clip_id, variant_id)
    expected = None
    stale_detail = None
    if base_record:
        try:
            base_path = Path(episode_dir) / "shorts" / f"{clip_id}.mp4"
            base_identity = _scan(base_path)
            recorded_base = record.get("base_render")
            recorded_asset = record.get("asset")
            recorded_output = record.get("output")
            recorded_layout = record.get("layout_version")
            asset_free = variant_id == SPEAKER_PANELS_VARIANT_ID
            if (
                record.get("variant_id") != variant_id
                or (asset_free and "asset" not in record)
                or not isinstance(recorded_layout, str)
                or record.get("encoding") != encoding
                or not isinstance(recorded_base, dict)
                or (
                    recorded_asset is not None
                    if asset_free
                    else not isinstance(recorded_asset, dict)
                )
                or not isinstance(recorded_output, dict)
                or not _SHA256.fullmatch(
                    str(recorded_output.get("content_revision", ""))
                )
            ):
                raise TypeError("The variant manifest is malformed")
            expected_layout = _variant_layout(variant_id)
            if recorded_layout != expected_layout:
                if recorded_layout in {
                    "portrait-over-motion/v1",
                    "portrait-over-motion/v2",
                    "portrait-over-motion/v3",
                }:
                    raise ValueError(
                        f"Background layout {recorded_layout} is out of date; "
                        "re-render this variant."
                    )
                raise TypeError("The variant manifest is malformed")
            asset_id = (
                recorded_asset.get("asset_id")
                if isinstance(recorded_asset, dict)
                else None
            )
            if not asset_free and not isinstance(asset_id, str):
                raise TypeError("The variant manifest has no asset identity")
            require_background_variant_asset(variant_id, asset_id)
            asset = (
                None
                if asset_free
                else load_background_variant_asset(variant_id)
                if variant_id == GAMEPLAY_SURROUND_VARIANT_ID
                else load_background_asset(str(asset_id))
            )
            if variant_id in GAMEPLAY_VARIANT_IDS:
                clip_duration = _media_duration(base_path)
                source_durations = {
                    str(media_asset["asset_id"]): _media_duration(
                        Path(media_asset["path"])
                    )
                    for media_asset in (asset.get("assets", [asset]) if asset else [])
                }
                asset = resolve_gameplay_variant_playback(
                    asset,
                    episode_id=Path(episode_dir).name,
                    clip_id=clip_id,
                    variant_id=variant_id,
                    clip_duration_seconds=clip_duration,
                    source_durations=source_durations,
                )
            caption_speaker_overrides = None
            neutral_header_policy = None
            if variant_id in SPEAKER_PANEL_VARIANT_IDS:
                (
                    caption_context_revision,
                    caption_speaker_overrides,
                    neutral_header_policy,
                ) = _current_speaker_panel_caption_state(
                    episode_dir,
                    clip_id,
                    variant_id,
                    base_record,
                )
            else:
                caption_context_revision = None
            recorded_captions = record.get("captions")
            recorded_caption_context = (
                recorded_captions.get("context_revision")
                if isinstance(recorded_captions, dict)
                else None
            )
            recorded_caption_speaker_overrides = (
                recorded_captions.get("speaker_overrides")
                if isinstance(recorded_captions, dict)
                else None
            )
            recorded_neutral_header_policy = (
                recorded_captions.get("neutral_header_policy")
                if isinstance(recorded_captions, dict)
                else None
            )
            if base_record.get("fingerprint") != recorded_base.get(
                "fingerprint"
            ) or base_identity != recorded_base.get("scan_identity"):
                stale_detail = (
                    "The canonical base short changed after this variant was rendered."
                )
            elif _variant_asset_record(asset, include_provenance=False) != (
                _recorded_asset_identity(recorded_asset)
            ):
                stale_detail = (
                    "A gameplay surround asset or its render plan changed after "
                    "this variant was rendered."
                    if variant_id == GAMEPLAY_SURROUND_VARIANT_ID
                    else "The background asset changed after this variant was rendered."
                )
            elif variant_id in SPEAKER_PANEL_VARIANT_IDS and (
                recorded_caption_context != caption_context_revision
                or recorded_caption_speaker_overrides != caption_speaker_overrides
                or recorded_neutral_header_policy != neutral_header_policy
            ):
                stale_detail = (
                    "The speaker bindings or panel anchors changed for speaker-panel "
                    "captions, reviewed word attribution changed, or the neutral-caption "
                    "policy changed after this variant was rendered."
                )
            else:
                expected = background_variant_fingerprint(
                    base_record,
                    base_identity,
                    asset,
                    encoding,
                    variant_id=variant_id,
                    caption_context_revision=caption_context_revision,
                )
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            subprocess.CalledProcessError,
            TypeError,
            ValueError,
        ) as exc:
            stale_detail = str(exc)

    render = render_artifact_state(
        Path(episode_dir),
        output,
        record,
        expected_fingerprint=expected,
        expected_mode=_variant_mode(variant_id),
    )
    expected_path = str(output.relative_to(episode_dir))
    if render["playable"] and record.get("path") != expected_path:
        render.update(
            status="stale",
            current=False,
            reason_code="variant_path_changed",
            detail="The variant manifest does not identify its fixed output path.",
        )
    elif render["current"]:
        try:
            output_identity = _scan(output)
        except OSError:
            output_identity = None
        recorded_output = record.get("output")
        if not isinstance(
            recorded_output, dict
        ) or output_identity != recorded_output.get("scan_identity"):
            render.update(
                status="stale",
                current=False,
                reason_code="artifact_changed",
                detail="The variant file changed after its exact bytes were recorded.",
            )
    elif (
        stale_detail and render.get("reason_code") == "verification_inputs_unavailable"
    ):
        render["detail"] = stale_detail
    return record, render


def background_variant_approval_state(
    record: dict, render: dict, revision: str
) -> dict:
    approval = record.get("approval")
    if not isinstance(approval, dict):
        return {"status": "unapproved", "current": False, "revision": revision}
    current = render["current"] and approval.get("revision") == revision
    return {
        "status": "current" if current else "stale",
        "current": current,
        "revision": revision,
        "approved_at": approval.get("approved_at"),
    }


def save_background_variant_approval(
    episode_dir: Path,
    clip_id: str,
    expected_record: dict,
    revision: str,
    variant_id: str = BACKGROUND_VARIANT_ID,
) -> dict | None:
    # The route holds this variant's render_output_lock through this write.
    current = variant_record(episode_dir, clip_id, variant_id)
    output_identity = (
        current.get("output", {}).get("scan_identity")
        if isinstance(current.get("output"), dict)
        else None
    )
    try:
        live_output_identity = _scan(
            background_variant_output(episode_dir, clip_id, variant_id)
        )
    except OSError:
        live_output_identity = None
    if (
        current != expected_record
        or not output_identity
        or live_output_identity != output_identity
    ):
        return None
    updated = dict(current)
    updated["approval"] = {
        "revision": revision,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(_record_path(episode_dir, clip_id, variant_id), updated)
    return updated
