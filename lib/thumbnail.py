"""Deterministic podcast thumbnails built from real episode footage."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from lib.ffprobe import file_fingerprint, get_duration, media_fingerprint

THUMBNAIL_RENDER_VERSION = "source-frame-title/v1"
THUMBNAIL_SIZE = (1280, 720)
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int, font_path: str | Path | None = None) -> ImageFont.FreeTypeFont:
    candidates = ([str(font_path)] if font_path else []) + list(_FONT_CANDIDATES)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    raise FileNotFoundError("No usable TrueType font found for thumbnail rendering")


def _wrapped_headline(
    draw: ImageDraw.ImageDraw,
    headline: str,
    *,
    max_width: int,
    max_lines: int = 3,
    font_path: str | Path | None = None,
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    normalized = headline.strip().upper()
    words = normalized.split()
    if not words:
        raise ValueError("Thumbnail headline cannot be empty")

    forced_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    for size in range(96, 51, -2):
        font = _font(size, font_path)
        if len(forced_lines) > 1:
            if len(forced_lines) <= max_lines and all(
                draw.textbbox((0, 0), line, font=font, stroke_width=2)[2]
                <= max_width
                for line in forced_lines
            ):
                return forced_lines, font
            continue
        lines: list[str] = []
        current = ""
        for word in words:
            proposed = f"{current} {word}".strip()
            width = draw.textbbox((0, 0), proposed, font=font, stroke_width=2)[2]
            if current and width > max_width:
                lines.append(current)
                current = word
            else:
                current = proposed
        if current:
            lines.append(current)
        if len(lines) <= max_lines:
            return lines, font
    raise ValueError("Thumbnail headline is too long for the three-line title area")


def compose_thumbnail(
    frame: Image.Image,
    headline: str,
    *,
    podcast_name: str = "THE LOCAL PODCAST",
    font_path: str | Path | None = None,
    title_position: str = "top",
) -> Image.Image:
    """Compose a 16:9 thumbnail while preserving the source frame as imagery."""
    if title_position not in {"top", "bottom"}:
        raise ValueError("Thumbnail title_position must be 'top' or 'bottom'")
    canvas = ImageOps.fit(
        frame.convert("RGB"), THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS
    ).convert("RGBA")
    width, height = canvas.size

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    gradient_height = int(height * 0.62)
    for y in range(gradient_height):
        progress = y / max(1, gradient_height - 1)
        alpha = round(185 * (1 - progress) ** 1.7)
        overlay_draw.line((0, y, width, y), fill=(8, 20, 34, alpha))
    if title_position == "bottom":
        for y in range(height // 2, height):
            progress = (y - height // 2) / max(1, height // 2 - 1)
            alpha = round(175 * progress**1.5)
            overlay_draw.line((0, y, width, y), fill=(8, 20, 34, alpha))
    canvas = Image.alpha_composite(canvas, overlay)

    draw = ImageDraw.Draw(canvas)
    label_font = _font(28, font_path)
    label = podcast_name.strip().upper()
    label_box = draw.textbbox((0, 0), label, font=label_font)
    label_width = label_box[2] - label_box[0]
    draw.rounded_rectangle((64, 48, 96 + label_width, 94), 12, fill=(247, 177, 67, 255))
    draw.text((80, 54), label, font=label_font, fill=(10, 28, 43, 255))

    lines, title_font = _wrapped_headline(
        draw,
        headline,
        max_width=1060,
        font_path=font_path,
    )
    line_gap = 4
    line_height = title_font.getbbox("Ag")[3] - title_font.getbbox("Ag")[1]
    title_height = len(lines) * (line_height + line_gap) - line_gap
    title_y = 118 if title_position == "top" else height - title_height - 58
    draw.rounded_rectangle(
        (64, title_y + 8, 76, title_y + len(lines) * (line_height + line_gap) - 2),
        6,
        fill=(247, 177, 67, 255),
    )
    for line in lines:
        draw.text(
            (94, title_y),
            line,
            font=title_font,
            fill=(255, 255, 255, 255),
            stroke_width=3,
            stroke_fill=(8, 20, 34, 225),
        )
        title_y += line_height + line_gap

    return canvas.convert("RGB")


def _extract_source_frame(source: Path, destination: Path, at_seconds: float) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{at_seconds:.6f}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-y",
        str(destination),
    ]
    subprocess.run(command, capture_output=True, check=True)


def render_episode_thumbnail(
    source: str | Path,
    destination: str | Path,
    headline: str,
    *,
    at_seconds: float = 600.0,
    podcast_name: str = "THE LOCAL PODCAST",
    font_path: str | Path | None = None,
    title_position: str = "top",
) -> dict:
    """Render one reviewable thumbnail and return its complete provenance."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f"Episode source video not found: {source}")
    duration = get_duration(source)
    if duration <= 0:
        raise ValueError("Episode source duration must be positive")
    requested_seconds = float(at_seconds)
    if requested_seconds < 0:
        raise ValueError("Thumbnail frame time cannot be negative")
    frame_seconds = min(requested_seconds, max(0.0, duration - 0.1))

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="cascade-thumbnail-", dir=destination.parent
    ) as temporary_dir:
        temporary_dir = Path(temporary_dir)
        frame_path = temporary_dir / "source.png"
        output_path = temporary_dir / "thumbnail.jpg"
        _extract_source_frame(source, frame_path, frame_seconds)
        with Image.open(frame_path) as frame:
            thumbnail = compose_thumbnail(
                frame,
                headline,
                podcast_name=podcast_name,
                font_path=font_path,
                title_position=title_position,
            )
            thumbnail.save(output_path, format="JPEG", quality=92, optimize=True)
        os.replace(output_path, destination)

    return {
        "version": THUMBNAIL_RENDER_VERSION,
        "clock": "source",
        "source": media_fingerprint(source),
        "frame_seconds": frame_seconds,
        "headline": headline.strip().upper(),
        "podcast_name": podcast_name.strip().upper(),
        "title_position": title_position,
        "dimensions": {"width": THUMBNAIL_SIZE[0], "height": THUMBNAIL_SIZE[1]},
        "output": file_fingerprint(destination),
        "output_path": str(destination),
    }
