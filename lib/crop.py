"""Shared crop calculation — single source of truth for all render agents.

Formulas must match frontend/app.js redrawCropCanvas(). See comments there.
  speaker: crop_w = src_w / (2 * zoom)   — 16:9 half-frame per speaker
  wide:    crop_w = src_w / zoom   — 16:9 full-frame
  short:   crop_h = src_h / zoom   — 9:16 portrait
"""

import math


def _fallback(value, fallback):
    return fallback if value is None else value


def visual_crop_state(crop_config: dict, aspect: str) -> dict:
    """Return only crop values that can change pixels for one aspect ratio."""
    if aspect not in {"longform", "short"}:
        raise ValueError(f"Unknown crop aspect: {aspect!r}")
    configured = crop_config.get("speakers", [])
    if configured:
        speakers = []
        for speaker in configured:
            if aspect == "short":
                speakers.append(
                    {
                        "center_x": speaker.get("center_x"),
                        "center_y": speaker.get("center_y"),
                        "zoom": speaker.get("zoom", 1.0),
                    }
                )
            else:
                speakers.append(
                    {
                        "center_x": _fallback(
                            speaker.get("longform_center_x"), speaker.get("center_x")
                        ),
                        "center_y": _fallback(
                            speaker.get("longform_center_y"), speaker.get("center_y")
                        ),
                        "zoom": _fallback(
                            speaker.get("longform_zoom"), speaker.get("zoom", 1.0)
                        ),
                    }
                )
    else:
        speakers = [
            {
                "center_x": crop_config.get(f"speaker_{side}_center_x"),
                "center_y": crop_config.get(f"speaker_{side}_center_y"),
                "zoom": crop_config.get(
                    f"speaker_{side}_zoom", crop_config.get("zoom", 1.0)
                ),
            }
            for side in ("l", "r")
        ]
    state = {"speakers": speakers}
    if aspect == "longform":
        wide_zoom = _fallback(
            crop_config.get("wide_zoom"), crop_config.get("zoom", 1.0)
        )
        state["wide"] = {"zoom": wide_zoom}
        if isinstance(wide_zoom, (int, float)) and wide_zoom > 1.0:
            state["wide"].update(
                center_x=crop_config.get("wide_center_x"),
                center_y=crop_config.get("wide_center_y"),
            )
    return state


def speaker_crop_state(crop_config: dict) -> list[dict]:
    """Return crop fields that affect speaker-to-source assignment."""
    return [
        {
            key: value
            for key, value in speaker.items()
            if key
            not in {
                "center_x",
                "center_y",
                "longform_center_x",
                "longform_center_y",
                "longform_zoom",
                "volume",
                "zoom",
            }
        }
        for speaker in crop_config.get("speakers", [])
    ]


def compute_crop(src_w, src_h, cx, cy, zoom, mode):
    """Return (x, y, crop_w, crop_h) clamped to frame bounds.

    In speaker mode, zoom=1.0 selects one 16:9 half of a two-person frame.
    """
    if mode not in {"speaker", "wide", "short"}:
        raise ValueError(f"Unknown crop mode: {mode!r}")
    values = {"source width": src_w, "source height": src_h, "zoom": zoom}
    for label, value in values.items():
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{label} must be a positive finite number")
    for label, value in (("center x", cx), ("center y", cy)):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{label} must be a finite number")

    if mode == "speaker":
        crop_w = max(64, int(src_w / (2 * zoom)))
        crop_h = max(36, int(crop_w * 9 / 16))
    elif mode == "wide":
        crop_w = max(64, int(src_w / zoom))
        crop_h = max(36, int(crop_w * 9 / 16))
    elif mode == "short":
        crop_h = max(36, int(src_h / zoom))
        crop_w = max(64, int(crop_h * 9 / 16))
    crop_w = min(crop_w, src_w)
    crop_h = min(crop_h, src_h)
    x = max(0, min(cx - crop_w // 2, src_w - crop_w))
    y = max(0, min(cy - crop_h // 2, src_h - crop_h))
    return x, y, crop_w, crop_h


def resolve_speaker(speaker, src_w, src_h, crop_config, for_shorts=False):
    """Resolve a speaker label to (cx, cy, zoom, mode).

    Returns (cx, cy, zoom, mode) where mode is "speaker", "wide", or None.
    None means BOTH/NONE with zoom <= 1.0 (passthrough, no crop needed).

    Each speaker can have separate center + zoom values per aspect ratio:
      center_x, center_y       — shorts (9:16) anchor, required
      zoom                     — shorts (9:16) zoom
      longform_center_x/y      — longform (16:9) anchor, optional (falls back to shorts)
      longform_zoom            — longform (16:9) zoom, falls back to shorts zoom if not set
    """
    speakers = crop_config.get("speakers", [])

    # N-speaker mode (speaker_0, speaker_1, ...) and legacy L/R labels
    if speaker.startswith("speaker_") or speaker in ("L", "R"):
        if speaker in ("L", "R"):
            idx = 0 if speaker == "L" else 1
        else:
            suffix = speaker.removeprefix("speaker_")
            if not suffix.isdigit():
                raise ValueError(f"Invalid speaker label: {speaker!r}")
            idx = int(suffix)

        # Use speakers[] array if available
        if speakers and idx < len(speakers):
            spk = speakers[idx]
            if for_shorts:
                zoom = spk.get("zoom", 1.0)
                cx = spk["center_x"]
                cy = spk.get("center_y", src_h // 2)
            else:
                zoom = spk.get("longform_zoom")
                zoom = spk.get("zoom", 1.0) if zoom is None else zoom
                cx = spk.get("longform_center_x")
                cx = spk["center_x"] if cx is None else cx
                cy = spk.get("longform_center_y")
                cy = spk.get("center_y", src_h // 2) if cy is None else cy
            return cx, cy, zoom, "speaker"

        # Fallback: legacy speaker_l/speaker_r fields for 2-speaker setups
        if idx <= 1:
            prefix = "speaker_l" if idx == 0 else "speaker_r"
            cx = crop_config.get(f"{prefix}_center_x", src_w // 2)
            cy = crop_config.get(f"{prefix}_center_y", src_h // 2)
            zoom = crop_config.get(f"{prefix}_zoom", crop_config.get("zoom", 1.0))
            return cx, cy, zoom, "speaker"

        raise ValueError(
            f"Speaker index {idx} has no crop configuration; "
            f"configured speaker count is {len(speakers)}"
        )

    # BOTH/NONE — wide shot
    zoom = crop_config.get("wide_zoom", crop_config.get("zoom", 1.0))
    if zoom <= 1.0:
        return src_w // 2, src_h // 2, zoom, None  # passthrough
    cx = crop_config.get("wide_center_x", src_w // 2)
    cy = crop_config.get("wide_center_y", src_h // 2)
    return cx, cy, zoom, "wide"
