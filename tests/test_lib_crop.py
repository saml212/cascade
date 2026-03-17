"""Tests for lib/crop.py — shared crop calculation and speaker resolution."""

import pytest
from lib.crop import compute_crop, resolve_speaker


# -- compute_crop ----------------------------------------------------------

@pytest.mark.parametrize("mode,src_w,src_h,zoom,exp_w,exp_h", [
    # Speaker: crop_w = src_w / (2 * zoom) — half-frame per speaker
    ("speaker", 3840, 2160, 1.0, 1920, 1080),
    ("speaker", 3840, 2160, 2.0, 960,  540),
    ("speaker", 1920, 1080, 1.0, 960,  540),
    # Wide: crop_w = src_w / zoom
    ("wide", 3840, 2160, 1.0, 3840, 2160),
    ("wide", 3840, 2160, 1.2, 3200, 1800),
    ("wide", 1920, 1080, 1.2, 1600, 900),
    # Short: crop_h = src_h / zoom
    ("short", 3840, 2160, 1.0, 1215, 2160),
    ("short", 3840, 2160, 2.0, 607,  1080),
])
def test_crop_dimensions(mode, src_w, src_h, zoom, exp_w, exp_h):
    _, _, w, h = compute_crop(src_w, src_h, src_w // 2, src_h // 2, zoom, mode)
    assert (w, h) == (exp_w, exp_h)


def test_wide_is_2x_speaker_at_same_zoom():
    _, _, w_wide, _ = compute_crop(3840, 2160, 1920, 1080, 1.5, "wide")
    _, _, w_spk, _ = compute_crop(3840, 2160, 1920, 1080, 1.5, "speaker")
    assert w_wide == 2 * w_spk


@pytest.mark.parametrize("cx,cy", [(0, 1080), (3840, 1080), (1920, 0), (1920, 2160)])
def test_clamped_to_frame(cx, cy):
    x, y, w, h = compute_crop(3840, 2160, cx, cy, 1.0, "speaker")
    assert x >= 0 and y >= 0 and x + w <= 3840 and y + h <= 2160


def test_extreme_zoom_minimum_dimensions():
    _, _, w, h = compute_crop(3840, 2160, 1920, 1080, 100.0, "speaker")
    assert w >= 64 and h >= 36


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        compute_crop(1920, 1080, 960, 540, 1.0, "invalid")


# -- UI formula consistency (JS must match Python) -------------------------

@pytest.mark.parametrize("src_w,src_h,zoom", [
    (3840, 2160, 1.0), (3840, 2160, 1.2), (3840, 2160, 2.0),
    (1920, 1080, 1.0), (1920, 1080, 1.5),
])
class TestUIMatch:
    def test_speaker(self, src_w, src_h, zoom):
        _, _, w, h = compute_crop(src_w, src_h, src_w // 2, src_h // 2, zoom, "speaker")
        assert w == max(64, int(src_w / (2 * zoom)))
        assert h == max(36, int(w * 9 / 16))

    def test_wide(self, src_w, src_h, zoom):
        _, _, w, h = compute_crop(src_w, src_h, src_w // 2, src_h // 2, zoom, "wide")
        assert w == min(src_w, max(64, int(src_w / zoom)))

    def test_short(self, src_w, src_h, zoom):
        _, _, w, h = compute_crop(src_w, src_h, src_w // 2, src_h // 2, zoom, "short")
        assert h == min(src_h, max(36, int(src_h / zoom)))


# -- resolve_speaker -------------------------------------------------------

def _n_speaker_config(n=3):
    return {
        "speakers": [
            {"label": f"Speaker {i}", "center_x": 400 * (i + 1), "center_y": 540, "zoom": 1.0 + i * 0.5}
            for i in range(n)
        ],
        "wide_center_x": 960, "wide_center_y": 540, "wide_zoom": 1.2,
    }


def test_resolve_speaker_n_mode():
    cfg = _n_speaker_config()
    cx, cy, zoom, mode = resolve_speaker("speaker_1", 1920, 1080, cfg)
    assert (cx, cy, zoom, mode) == (800, 540, 1.5, "speaker")


def test_resolve_speaker_out_of_range():
    cfg = _n_speaker_config(1)
    _, _, _, mode = resolve_speaker("speaker_5", 1920, 1080, cfg)
    assert mode == "speaker"  # still speaker mode, just defaults to center


def test_resolve_legacy_l_r():
    cfg = {"speaker_l_center_x": 300, "speaker_l_center_y": 540, "speaker_l_zoom": 1.5, "zoom": 1.0}
    cx, cy, zoom, mode = resolve_speaker("L", 1920, 1080, cfg)
    assert (cx, zoom, mode) == (300, 1.5, "speaker")


def test_resolve_legacy_l_uses_speakers_array():
    """When speakers[] exists, L maps to speakers[0]."""
    cfg = _n_speaker_config()
    cx, _, zoom, mode = resolve_speaker("L", 1920, 1080, cfg)
    assert (cx, zoom, mode) == (400, 1.0, "speaker")


def test_resolve_both_wide():
    cfg = _n_speaker_config()
    cx, cy, zoom, mode = resolve_speaker("BOTH", 1920, 1080, cfg)
    assert (cx, cy, zoom, mode) == (960, 540, 1.2, "wide")


def test_resolve_both_no_zoom_passthrough():
    cfg = {"wide_zoom": 1.0}
    _, _, _, mode = resolve_speaker("BOTH", 1920, 1080, cfg)
    assert mode is None
