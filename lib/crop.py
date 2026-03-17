"""Shared crop calculation — single source of truth for all render agents.

Formulas must match frontend/app.js redrawCropCanvas(). See comments there.
  speaker: crop_w = src_w / zoom   — 16:9 (zoom=1 = full frame, zoom=2 = half)
  wide:    crop_w = src_w / zoom   — 16:9 full-frame
  short:   crop_h = src_h / zoom   — 9:16 portrait
"""


def compute_crop(src_w, src_h, cx, cy, zoom, mode):
    """Return (x, y, crop_w, crop_h) clamped to frame bounds.

    For "speaker" mode: zoom=1.0 gives full frame, zoom=2.0 gives half-frame.
    This avoids the crop-then-upscale quality loss at low zoom values.
    """
    if mode == "speaker":
        crop_w = max(64, int(src_w / zoom))
        crop_h = max(36, int(crop_w * 9 / 16))
    elif mode == "wide":
        crop_w = max(64, int(src_w / zoom))
        crop_h = max(36, int(crop_w * 9 / 16))
    elif mode == "short":
        crop_h = max(36, int(src_h / zoom))
        crop_w = max(64, int(crop_h * 9 / 16))
    else:
        raise ValueError(f"Unknown crop mode: {mode!r}")

    crop_w = min(crop_w, src_w)
    crop_h = min(crop_h, src_h)
    x = max(0, min(cx - crop_w // 2, src_w - crop_w))
    y = max(0, min(cy - crop_h // 2, src_h - crop_h))
    return x, y, crop_w, crop_h


def resolve_speaker(speaker, src_w, src_h, crop_config):
    """Resolve a speaker label to (cx, cy, zoom, mode).

    Returns (cx, cy, zoom, mode) where mode is "speaker", "wide", or None.
    None means BOTH/NONE with zoom <= 1.0 (passthrough, no crop needed).
    """
    speakers = crop_config.get("speakers", [])

    # N-speaker mode (speaker_0, speaker_1, ...) and legacy L/R labels
    if speaker.startswith("speaker_") or speaker in ("L", "R"):
        if speaker in ("L", "R"):
            idx = 0 if speaker == "L" else 1
        else:
            idx = int(speaker.split("_")[1])

        # Use speakers[] array if available
        if speakers and idx < len(speakers):
            spk = speakers[idx]
            return spk["center_x"], spk.get("center_y", src_h // 2), spk.get("zoom", 1.0), "speaker"

        # Fallback: legacy speaker_l/speaker_r fields for 2-speaker setups
        if idx <= 1:
            prefix = "speaker_l" if idx == 0 else "speaker_r"
            cx = crop_config.get(f"{prefix}_center_x", src_w // 2)
            cy = crop_config.get(f"{prefix}_center_y", src_h // 2)
            zoom = crop_config.get(f"{prefix}_zoom", crop_config.get("zoom", 1.0))
            return cx, cy, zoom, "speaker"

        return src_w // 2, src_h // 2, 1.0, "speaker"

    # BOTH/NONE — wide shot
    zoom = crop_config.get("wide_zoom", crop_config.get("zoom", 1.0))
    if zoom <= 1.0:
        return src_w // 2, src_h // 2, zoom, None  # passthrough
    cx = crop_config.get("wide_center_x", src_w // 2)
    cy = crop_config.get("wide_center_y", src_h // 2)
    return cx, cy, zoom, "wide"
