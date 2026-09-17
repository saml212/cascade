"""Deterministic, source-bounded playback windows for gameplay variants."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy

GAMEPLAY_PLAYBACK_POLICY_VERSION = "deterministic-per-clip/v1"
GAMEPLAY_PLAYBACK_TAIL_GUARD_SECONDS = 0.25
_MILLISECONDS_PER_SECOND = 1000
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _finite_seconds(value: object, *, label: str, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value <= 0)
    ):
        qualifier = "positive " if positive else "non-negative "
        raise ValueError(f"Gameplay playback needs a {qualifier}{label}")
    return float(value)


def _revision(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def resolve_gameplay_asset_playback(
    asset: dict,
    *,
    episode_id: str,
    clip_id: str,
    variant_id: str,
    clip_duration_seconds: float,
    source_duration_seconds: float,
) -> dict:
    """Return one asset with a stable playback window for this exact clip.

    ``playback_start_seconds`` in the source manifest is the earliest licensed or
    editorially usable point. A non-looping window is preferred. A source may
    wrap only when its manifest explicitly sets ``playback_loop`` to true.
    """
    if not isinstance(asset, dict):
        raise TypeError("Gameplay playback needs an asset mapping")
    asset_id = asset.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise ValueError("Gameplay playback needs an asset identity")
    if not all(isinstance(value, str) and value for value in (episode_id, clip_id)):
        raise ValueError("Gameplay playback needs an episode and clip identity")
    if not isinstance(variant_id, str) or not variant_id:
        raise ValueError("Gameplay playback needs a variant identity")

    clip_duration = _finite_seconds(
        clip_duration_seconds, label="clip duration", positive=True
    )
    source_duration = _finite_seconds(
        source_duration_seconds, label="source duration", positive=True
    )
    manifest_start = _finite_seconds(
        asset.get("playback_start_seconds", 0.0), label="manifest playback start"
    )
    loop_enabled = asset.get("playback_loop", False)
    if not isinstance(loop_enabled, bool):
        raise TypeError("Gameplay playback_loop must be true or false")

    source_ms = math.floor(source_duration * _MILLISECONDS_PER_SECOND)
    clip_ms = math.ceil(clip_duration * _MILLISECONDS_PER_SECOND)
    manifest_start_ms = math.ceil(manifest_start * _MILLISECONDS_PER_SECOND)
    tail_guard_ms = math.ceil(
        GAMEPLAY_PLAYBACK_TAIL_GUARD_SECONDS * _MILLISECONDS_PER_SECOND
    )
    if manifest_start_ms >= source_ms:
        raise ValueError(
            f"Gameplay asset {asset_id} starts beyond its {source_duration:.3f}s source"
        )

    non_looping_max_ms = source_ms - clip_ms - tail_guard_ms
    if non_looping_max_ms >= manifest_start_ms:
        maximum_start_ms = non_looping_max_ms
        wrap_required = False
    elif loop_enabled:
        maximum_start_ms = source_ms - tail_guard_ms
        if maximum_start_ms < manifest_start_ms:
            raise ValueError(
                f"Gameplay asset {asset_id} has no safe playback frame after its "
                "manifest start"
            )
        wrap_required = True
    else:
        available = max(0.0, source_duration - manifest_start)
        raise ValueError(
            f"Gameplay asset {asset_id} provides {available:.3f}s after its manifest "
            f"start, shorter than the {clip_duration:.3f}s clip and "
            f"{GAMEPLAY_PLAYBACK_TAIL_GUARD_SECONDS:.3f}s tail guard; its manifest "
            "does not permit looping"
        )

    selection_inputs = {
        "version": GAMEPLAY_PLAYBACK_POLICY_VERSION,
        "episode_id": episode_id,
        "clip_id": clip_id,
        "variant_id": variant_id,
        "asset_id": asset_id,
        "role": asset.get("role"),
        "content_revision": asset.get("content_revision"),
        "manifest_start_milliseconds": manifest_start_ms,
        "source_duration_milliseconds": source_ms,
        "clip_duration_milliseconds": clip_ms,
        "maximum_start_milliseconds": maximum_start_ms,
        "loop_enabled": loop_enabled,
        "wrap_required": wrap_required,
    }
    selection_revision = _revision(selection_inputs)
    choice = int(selection_revision.removeprefix("sha256:"), 16)
    span = maximum_start_ms - manifest_start_ms + 1
    resolved_start_ms = manifest_start_ms + choice % span
    resolved_start = resolved_start_ms / _MILLISECONDS_PER_SECOND

    resolved = deepcopy(asset)
    resolved["playback_start_seconds"] = resolved_start
    resolved["playback_loop"] = loop_enabled
    resolved["playback_policy"] = {
        "version": GAMEPLAY_PLAYBACK_POLICY_VERSION,
        "selection_key_revision": selection_revision,
        "manifest_start_seconds": manifest_start_ms / _MILLISECONDS_PER_SECOND,
        "resolved_start_seconds": resolved_start,
        "source_duration_seconds": source_ms / _MILLISECONDS_PER_SECOND,
        "clip_duration_seconds": clip_ms / _MILLISECONDS_PER_SECOND,
        "tail_guard_seconds": tail_guard_ms / _MILLISECONDS_PER_SECOND,
        "loop_enabled": loop_enabled,
        "wrap_required": wrap_required,
    }
    return resolved


def require_resolved_gameplay_playback(asset: dict) -> None:
    """Fail closed when a gameplay fingerprint omits its resolved window."""
    policy = asset.get("playback_policy") if isinstance(asset, dict) else None
    if not isinstance(policy, dict):
        raise TypeError("Gameplay asset has no resolved per-clip playback policy")
    if policy.get("version") != GAMEPLAY_PLAYBACK_POLICY_VERSION:
        raise ValueError("Gameplay asset playback policy is out of date")
    revision = policy.get("selection_key_revision")
    if not isinstance(revision, str) or not _SHA256.fullmatch(revision):
        raise ValueError("Gameplay asset has no valid playback selection revision")
    try:
        manifest_start = _finite_seconds(
            policy.get("manifest_start_seconds"), label="policy manifest start"
        )
        resolved = _finite_seconds(
            policy.get("resolved_start_seconds"), label="policy resolved start"
        )
        source_duration = _finite_seconds(
            policy.get("source_duration_seconds"),
            label="policy source duration",
            positive=True,
        )
        clip_duration = _finite_seconds(
            policy.get("clip_duration_seconds"),
            label="policy clip duration",
            positive=True,
        )
        tail_guard = _finite_seconds(
            policy.get("tail_guard_seconds"), label="policy tail guard"
        )
        recorded_start = _finite_seconds(
            asset.get("playback_start_seconds"), label="recorded playback start"
        )
    except ValueError as exc:
        raise ValueError("Gameplay asset playback policy has invalid timing") from exc
    if (
        recorded_start != resolved
        or resolved < manifest_start
        or resolved >= source_duration
    ):
        raise ValueError("Gameplay asset playback start does not match its policy")
    loop_enabled = policy.get("loop_enabled")
    wrap_required = policy.get("wrap_required")
    if (
        not isinstance(loop_enabled, bool)
        or asset.get("playback_loop") is not loop_enabled
    ):
        raise ValueError("Gameplay asset loop setting does not match its policy")
    if not isinstance(wrap_required, bool):
        raise TypeError("Gameplay asset playback policy has no wrap decision")
    if wrap_required and not loop_enabled:
        raise ValueError("Gameplay asset playback policy wraps without loop permission")
    if tail_guard != GAMEPLAY_PLAYBACK_TAIL_GUARD_SECONDS:
        raise ValueError("Gameplay asset playback tail guard is out of date")
    non_looping_window_exists = (
        manifest_start + clip_duration + tail_guard <= source_duration
    )
    if wrap_required == non_looping_window_exists:
        raise ValueError("Gameplay asset playback policy has an invalid wrap decision")
    if not wrap_required and resolved + clip_duration + tail_guard > source_duration:
        raise ValueError("Gameplay asset playback policy exceeds its source duration")
    if wrap_required and resolved + tail_guard > source_duration:
        raise ValueError("Gameplay asset playback wrap starts beyond its source tail")
