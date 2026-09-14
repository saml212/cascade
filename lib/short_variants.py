"""Identity and review state for optional short-video variants."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from lib.atomic_write import atomic_write_json
from lib.delivery_video import render_artifact_state
from lib.ffprobe import file_fingerprint, scan_identity
from lib.timeline import Timeline

BACKGROUND_VARIANT_ID = "background_motion_v1"
BACKGROUND_VARIANT_MODE = "speaker_cut_short_background_motion_v1"
BACKGROUND_LAYOUT_VERSION = "portrait-over-motion/v3"
DEFAULT_BACKGROUND_ASSET_ID = "original_block_parkour_v1"

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
    if variant_id != BACKGROUND_VARIANT_ID:
        raise KeyError(f"Unknown short variant: {variant_id}")


def background_variant_output(episode_dir: Path, clip_id: str) -> Path:
    return (
        Path(episode_dir) / "short_variants" / BACKGROUND_VARIANT_ID / f"{clip_id}.mp4"
    )


def _record_path(episode_dir: Path, clip_id: str) -> Path:
    return background_variant_output(episode_dir, clip_id).with_suffix(".json")


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
    """Resolve one explicitly identified local asset and its checked provenance."""
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
    return {
        "asset_id": asset_id,
        "path": path,
        "content_revision": content["content_revision"],
        "manifest_revision": _json_revision(manifest),
        "scan_identity": content["scan_identity"],
        "provenance": provenance,
    }


def background_variant_fingerprint(
    base_record: dict,
    base_identity: dict,
    asset: dict,
    encoding: dict,
) -> str:
    return _json_revision(
        {
            "variant_id": BACKGROUND_VARIANT_ID,
            "layout": BACKGROUND_LAYOUT_VERSION,
            "base": {
                "render_fingerprint": base_record.get("fingerprint"),
                "scan_identity": base_identity,
            },
            "asset": {
                key: asset[key]
                for key in (
                    "asset_id",
                    "content_revision",
                    "manifest_revision",
                    "scan_identity",
                )
            },
            "encoding": encoding,
        }
    )


def variant_record(episode_dir: Path, clip_id: str) -> dict:
    try:
        value = json.loads(_record_path(episode_dir, clip_id).read_text())
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
    asset: dict,
    encoding: dict,
    captions: dict,
) -> dict:
    output = background_variant_output(episode_dir, clip_id)
    output_content = file_content_identity(output)
    output_identity = output_content["scan_identity"]
    record = {
        "variant_id": BACKGROUND_VARIANT_ID,
        "path": str(output.relative_to(episode_dir)),
        "render_mode": BACKGROUND_VARIANT_MODE,
        "layout_version": BACKGROUND_LAYOUT_VERSION,
        "fingerprint": fingerprint,
        "clip_source_intervals": [list(item) for item in timeline.keep_intervals],
        "output_duration_seconds": round(timeline.duration, 3),
        "base_render": {
            "fingerprint": base_record["fingerprint"],
            "scan_identity": base_identity,
        },
        "asset": {
            key: asset[key]
            for key in (
                "asset_id",
                "content_revision",
                "manifest_revision",
                "scan_identity",
                "provenance",
            )
        },
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
    atomic_write_json(_record_path(episode_dir, clip_id), record)
    return record


def background_variant_state(
    episode_dir: Path,
    clip_id: str,
    *,
    base_record: dict | None,
    encoding: dict,
) -> tuple[dict, dict]:
    """Return a recorded variant and shared render-artifact state."""
    output = background_variant_output(episode_dir, clip_id)
    record = variant_record(episode_dir, clip_id)
    expected = None
    stale_detail = None
    if base_record:
        try:
            base_path = Path(episode_dir) / "shorts" / f"{clip_id}.mp4"
            base_identity = _scan(base_path)
            recorded_base = record.get("base_render")
            recorded_asset = record.get("asset")
            recorded_output = record.get("output")
            if (
                record.get("variant_id") != BACKGROUND_VARIANT_ID
                or record.get("layout_version") != BACKGROUND_LAYOUT_VERSION
                or record.get("encoding") != encoding
                or not isinstance(recorded_base, dict)
                or not isinstance(recorded_asset, dict)
                or not isinstance(recorded_output, dict)
                or not _SHA256.fullmatch(
                    str(recorded_output.get("content_revision", ""))
                )
            ):
                raise TypeError("The variant manifest is malformed")
            asset_id = recorded_asset.get("asset_id")
            if not isinstance(asset_id, str):
                raise TypeError("The variant manifest has no asset identity")
            asset = load_background_asset(asset_id)
            if base_record.get("fingerprint") != recorded_base.get(
                "fingerprint"
            ) or base_identity != recorded_base.get("scan_identity"):
                stale_detail = (
                    "The canonical base short changed after this variant was rendered."
                )
            elif any(
                asset[key] != recorded_asset.get(key)
                for key in (
                    "asset_id",
                    "content_revision",
                    "manifest_revision",
                    "scan_identity",
                )
            ):
                stale_detail = (
                    "The background asset changed after this variant was rendered."
                )
            else:
                expected = background_variant_fingerprint(
                    base_record,
                    base_identity,
                    asset,
                    encoding,
                )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            stale_detail = str(exc)

    render = render_artifact_state(
        Path(episode_dir),
        output,
        record,
        expected_fingerprint=expected,
        expected_mode=BACKGROUND_VARIANT_MODE,
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
    episode_dir: Path, clip_id: str, expected_record: dict, revision: str
) -> dict | None:
    # The route holds this variant's render_output_lock through this write.
    current = variant_record(episode_dir, clip_id)
    output_identity = (
        current.get("output", {}).get("scan_identity")
        if isinstance(current.get("output"), dict)
        else None
    )
    try:
        live_output_identity = _scan(background_variant_output(episode_dir, clip_id))
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
    atomic_write_json(_record_path(episode_dir, clip_id), updated)
    return updated
