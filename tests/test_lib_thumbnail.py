"""Tests for deterministic real-footage thumbnails."""

import subprocess

from PIL import Image, ImageDraw

from lib.thumbnail import (
    THUMBNAIL_RENDER_VERSION,
    THUMBNAIL_SIZE,
    _wrapped_headline,
    compose_thumbnail,
    render_episode_thumbnail,
)


def test_compose_thumbnail_preserves_image_and_adds_title():
    frame = Image.new("RGB", (640, 360), (30, 140, 210))

    thumbnail = compose_thumbnail(frame, "Rewilding the Bay")

    assert thumbnail.size == THUMBNAIL_SIZE
    assert thumbnail.getpixel((1270, 710)) == (30, 140, 210)
    assert thumbnail.getpixel((80, 70)) != (30, 140, 210)


def test_explicit_line_break_preserves_an_editorial_phrase():
    frame = Image.new("RGB", (640, 360), (30, 140, 210))
    lines, _ = _wrapped_headline(
        ImageDraw.Draw(frame),
        "15 YEARS DRIVING\nTHE 38 GEARY",
        max_width=1060,
    )

    thumbnail = compose_thumbnail(
        frame,
        "15 YEARS DRIVING\nTHE 38 GEARY",
        title_position="bottom",
    )

    assert thumbnail.size == THUMBNAIL_SIZE
    assert lines == ["15 YEARS DRIVING", "THE 38 GEARY"]


def test_render_thumbnail_uses_clamped_source_clock_frame(tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "thumbnails" / "longform.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x248ecf:s=320x180:d=1:r=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(source),
        ],
        check=True,
    )

    result = render_episode_thumbnail(
        source,
        output,
        "A Life Around Surfing",
        at_seconds=600,
    )

    assert output.is_file()
    with Image.open(output) as rendered:
        assert rendered.size == THUMBNAIL_SIZE
    assert result["version"] == THUMBNAIL_RENDER_VERSION
    assert result["clock"] == "source"
    assert 0.8 <= result["frame_seconds"] < 1.0
    assert result["headline"] == "A LIFE AROUND SURFING"
    assert result["title_position"] == "top"
    assert result["source"]["id"].startswith("sha256:")
    assert result["output"]["id"].startswith("sha256:")
