"""One catalogue for short-form metadata and Upload-Post fields."""

from __future__ import annotations

import math
from pathlib import Path

from lib.ffprobe import probe

SHORT_PLATFORM_SPECS = {
    "youtube": {
        "label": "YouTube Shorts",
        "metadata_fields": ("title", "description"),
        "upload_fields": {
            "title": "youtube_title",
            "description": "youtube_description",
            "first_comment": "youtube_first_comment",
        },
    },
    "tiktok": {
        "label": "TikTok",
        "metadata_fields": ("caption", "hashtags"),
        "required_fields": ("caption",),
        "upload_fields": {"text": "tiktok_title"},
    },
    "instagram": {
        "label": "Instagram Reels",
        "metadata_fields": ("caption", "hashtags"),
        "required_fields": ("caption",),
        "upload_fields": {"text": "instagram_title"},
    },
    "x": {
        "label": "X",
        "metadata_fields": ("text",),
        "upload_fields": {"text": "x_title"},
        "fixed_upload_fields": {"x_long_text_as_post": "true"},
    },
    "facebook": {
        "label": "Facebook Reels",
        "metadata_fields": ("title", "description"),
        "upload_fields": {
            "title": "facebook_title",
            "description": "facebook_description",
        },
        "fixed_upload_fields": {"facebook_media_type": "REELS"},
        "account_config": "account_username",
        "target": ("page_id", "facebook_page_id", True),
        "copy_limits": {
            "title": (255, "characters"),
            "description": (63206, "characters"),
        },
        "media": {
            "min_duration": 3,
            "max_duration": 90,
            "min_width": 540,
            "min_height": 960,
            "min_fps": 24,
            "max_fps": 60,
            "aspect": (9, 16),
            "codecs": ("h264",),
            "containers": ("mp4",),
            "pixel_formats": ("yuv420p",),
            "field_orders": ("progressive",),
            "audio_codecs": ("aac",),
            "audio_sample_rates": (48000,),
            "audio_channels": (2,),
            "min_audio_bitrate": 128_000,
        },
    },
    "threads": {
        "label": "Threads",
        "metadata_fields": ("text",),
        "upload_fields": {"text": "threads_title"},
        "account_config": "account_username",
        "copy_limits": {"text": (500, "utf8_bytes")},
        "media": {
            "max_bytes": 1_000_000_000,
            "max_duration": 300,
            "max_width": 1920,
            "min_fps": 23,
            "max_fps": 60,
            "codecs": ("h264", "hevc"),
            "containers": ("mp4", "mov"),
            "audio_codecs": ("aac",),
            "max_audio_sample_rate": 48_000,
            "audio_channels": (1, 2),
            "min_audio_bitrate": 128_000,
            "faststart": True,
            "edit_lists": False,
        },
    },
    "bluesky": {
        "label": "Bluesky",
        "metadata_fields": ("text",),
        "upload_fields": {"text": "bluesky_title"},
        "account_config": "account_username",
        "copy_limits": {"text": (300, "characters")},
        "media": {
            "max_bytes": 300_000_000,
            "min_duration": 1,
            "max_duration": 600,
            "min_width": 360,
            "max_width": 1920,
            "min_height": 360,
            "max_height": 1920,
            "min_fps": 10,
            "max_fps": 60,
            "codecs": ("h264",),
            "containers": ("mp4",),
        },
    },
    "linkedin": {
        "label": "LinkedIn",
        "metadata_fields": ("title", "description"),
        "upload_fields": {
            "title": "linkedin_title",
            "description": "linkedin_description",
        },
        "account_config": "account_username",
        "target": ("page_id", "target_linkedin_page_id", False),
        "copy_limits": {
            "title": (400, "utf16_units"),
            "description": (3000, "characters"),
        },
        "media": {
            "min_bytes": 75_000,
            "max_bytes": 500_000_000,
            "min_duration": 3,
            "max_duration": 1800,
            "min_width": 256,
            "max_width": 4096,
            "min_height": 144,
            "max_height": 2304,
            "min_aspect": 1 / 2.4,
            "max_aspect": 2.4,
            "min_fps": 10,
            "max_fps": 60,
            "min_bitrate": 192_000,
            "max_bitrate": 30_000_000,
            "codecs": ("h264",),
            "containers": ("mp4",),
        },
    },
    "pinterest": {
        "label": "Pinterest",
        "metadata_fields": ("title", "description"),
        "upload_fields": {
            "title": "pinterest_title",
            "description": "pinterest_description",
            "link": "pinterest_link",
        },
        "account_config": "account_username",
        "target": ("board_id", "pinterest_board_id", True),
        "copy_limits": {
            "title": (100, "characters"),
            "description": (800, "characters"),
            "link": (2048, "characters"),
        },
        "media": {
            "max_bytes": 2_000_000_000,
            "min_duration": 4,
            "max_duration": 900,
            "min_aspect": 0.5,
            "max_aspect": 1.91,
            "codecs": ("h264",),
            "containers": ("mp4", "mov", "m4v"),
        },
    },
}

SHORT_DESTINATIONS = tuple(SHORT_PLATFORM_SPECS)
EXPANSION_DESTINATIONS = frozenset(
    name for name, spec in SHORT_PLATFORM_SPECS.items() if spec.get("account_config")
)
PLATFORM_COPY_FIELDS = {
    name: tuple(spec.get("required_fields", spec["metadata_fields"]))
    for name, spec in SHORT_PLATFORM_SPECS.items()
}
PLATFORM_METADATA_FIELDS = {
    name: tuple(spec["metadata_fields"]) for name, spec in SHORT_PLATFORM_SPECS.items()
}


def metadata_schema(platform: str) -> dict:
    """Return the structured-generation schema for one platform's stored copy."""
    fields = PLATFORM_METADATA_FIELDS[platform]
    return {
        "type": "object",
        "properties": {
            field: (
                {"type": "array", "items": {"type": "string"}}
                if field == "hashtags"
                else {"type": "string"}
            )
            for field in fields
        },
        "required": list(fields),
        "additionalProperties": False,
    }


def copy_length(value: str, unit: str) -> int:
    """Measure provider-specific text units without silently truncating copy."""
    if unit == "utf8_bytes":
        return len(value.encode("utf-8"))
    if unit == "utf16_units":
        return len(value.encode("utf-16-le")) // 2
    return len(value)


def validate_destination_copy(copy: dict) -> list[str]:
    """Return final-copy violations for expansion destinations."""
    issues = []
    for platform, fields in copy.items():
        limits = SHORT_PLATFORM_SPECS.get(platform, {}).get("copy_limits", {})
        for field, (maximum, unit) in limits.items():
            value = fields.get(field) if isinstance(fields, dict) else None
            if isinstance(value, str) and copy_length(value, unit) > maximum:
                issues.append(f"{platform}.{field} exceeds {maximum} {unit}")
    return issues


def configured_destination_bindings(
    config: dict, destinations: list[str]
) -> dict[str, dict[str, str]]:
    """Resolve explicit expansion-account targets, rejecting implicit routing."""
    configured = config.get("platforms", {})
    bindings = {}
    for platform in destinations:
        spec = SHORT_PLATFORM_SPECS[platform]
        account_key = spec.get("account_config")
        if not account_key:
            continue
        settings = configured.get(platform, {})
        account = settings.get(account_key)
        if (
            not isinstance(account, str)
            or not account.strip()
            or account != account.strip()
        ):
            raise ValueError(f"platforms.{platform}.{account_key} is required")
        binding = {
            "account_id": account,
            "target_kind": "account",
            "target_id": account,
        }
        target = spec.get("target")
        if target:
            config_key, _provider_key, required = target
            value = settings.get(config_key)
            if required and (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"platforms.{platform}.{config_key} is required")
            if value not in (None, ""):
                if not isinstance(value, str) or value != value.strip():
                    raise ValueError(f"platforms.{platform}.{config_key} is invalid")
                binding = {
                    "account_id": account,
                    "target_kind": ("board" if platform == "pinterest" else "page"),
                    "target_id": value,
                }
            elif platform == "linkedin":
                binding["target_kind"] = "personal"
        bindings[platform] = binding
    return bindings


def valid_destination_bindings(bindings: object, destinations: list[str]) -> bool:
    """Validate the normalized account/target binding stored in a receipt."""
    expansion = sorted(set(destinations) & EXPANSION_DESTINATIONS)
    if not expansion:
        return bindings is None
    if not isinstance(bindings, dict) or sorted(bindings) != expansion:
        return False
    expected_kinds = {
        "facebook": {"page"},
        "threads": {"account"},
        "bluesky": {"account"},
        "linkedin": {"personal", "page"},
        "pinterest": {"board"},
    }
    for platform in expansion:
        binding = bindings[platform]
        if (
            not isinstance(binding, dict)
            or set(binding) != {"account_id", "target_kind", "target_id"}
            or any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in binding.values()
            )
            or binding["target_kind"] not in expected_kinds[platform]
            or (
                binding["target_kind"] in {"account", "personal"}
                and binding["target_id"] != binding["account_id"]
            )
        ):
            return False
    return True


def _frame_rate(stream: dict) -> float:
    value = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1")
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0.0


def validate_destination_media(path: str | Path, destinations: list[str]) -> list[str]:
    """Validate a short once against selected expansion-platform hard limits."""
    selected = [name for name in destinations if name in EXPANSION_DESTINATIONS]
    if not selected:
        return []
    media_path = Path(path)
    try:
        size = media_path.stat().st_size
        data = probe(media_path)
        video = next(
            item
            for item in data.get("streams", [])
            if item.get("codec_type") == "video"
        )
        audio = next(
            item
            for item in data.get("streams", [])
            if item.get("codec_type") == "audio"
        )
        duration = float(data.get("format", {}).get("duration"))
        width, height = int(video["width"]), int(video["height"])
        fps = _frame_rate(video)
        video_bitrate = (
            float(video["bit_rate"])
            if video.get("bit_rate") not in (None, "")
            else None
        )
    except (KeyError, OSError, StopIteration, TypeError, ValueError) as exc:
        return [f"media cannot be inspected: {exc}"]

    values = {
        "bytes": size,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "aspect": width / height if height else 0,
        "bitrate": video_bitrate,
    }
    invalid = [
        name
        for name in ("bytes", "duration", "width", "height", "fps", "aspect")
        if not isinstance(values[name], (int, float))
        or not math.isfinite(values[name])
        or values[name] <= 0
    ]
    if invalid:
        return ["media has invalid " + ", ".join(invalid)]
    if values["bitrate"] is not None and (
        not math.isfinite(values["bitrate"]) or values["bitrate"] <= 0
    ):
        return ["media has invalid bitrate"]
    try:
        audio_sample_rate = int(audio["sample_rate"])
        audio_channels = int(audio["channels"])
        audio_bitrate = (
            int(audio["bit_rate"]) if audio.get("bit_rate") not in (None, "") else None
        )
    except (KeyError, TypeError, ValueError):
        return ["media has invalid audio stream metadata"]
    if (
        audio_sample_rate <= 0
        or audio_channels <= 0
        or (audio_bitrate is not None and audio_bitrate <= 0)
    ):
        return ["media has invalid audio stream metadata"]
    format_names = set(str(data.get("format", {}).get("format_name", "")).split(","))
    atom_data = None
    issues = []
    for platform in selected:
        limits = SHORT_PLATFORM_SPECS[platform]["media"]
        for name, value in values.items():
            if value is None:
                if f"max_{name}" in limits or f"min_{name}" in limits:
                    issues.append(f"{platform} {name} is unavailable")
                continue
            minimum = limits.get(f"min_{name}")
            maximum = limits.get(f"max_{name}")
            if minimum is not None and value < minimum:
                issues.append(f"{platform} requires {name} >= {minimum}")
            if maximum is not None and value > maximum:
                issues.append(f"{platform} requires {name} <= {maximum}")
        aspect = limits.get("aspect")
        if aspect is not None:
            aspect_width, aspect_height = aspect
            if width * aspect_height != height * aspect_width:
                issues.append(f"{platform} requires 9:16 video")
        if video.get("codec_name") not in limits.get("codecs", ()):
            issues.append(
                f"{platform} does not support {video.get('codec_name')} video"
            )
        if limits.get("containers") and not format_names.intersection(
            limits["containers"]
        ):
            issues.append(f"{platform} does not support the media container")
        if (
            limits.get("pixel_formats")
            and video.get("pix_fmt") not in limits["pixel_formats"]
        ):
            issues.append(f"{platform} does not support {video.get('pix_fmt')} pixels")
        if (
            limits.get("field_orders")
            and video.get("field_order") not in limits["field_orders"]
        ):
            issues.append(f"{platform} requires progressive video")
        if (
            limits.get("audio_codecs")
            and audio.get("codec_name") not in limits["audio_codecs"]
        ):
            issues.append(f"{platform} does not support the audio codec")
        if (
            limits.get("audio_sample_rates")
            and audio_sample_rate not in limits["audio_sample_rates"]
        ):
            issues.append(f"{platform} does not support the audio sample rate")
        if (
            limits.get("max_audio_sample_rate") is not None
            and audio_sample_rate > limits["max_audio_sample_rate"]
        ):
            issues.append(f"{platform} audio sample rate is too high")
        if (
            limits.get("audio_channels")
            and audio_channels not in limits["audio_channels"]
        ):
            issues.append(f"{platform} does not support the audio channel count")
        if limits.get("min_audio_bitrate") is not None and (
            audio_bitrate is None or audio_bitrate < limits["min_audio_bitrate"]
        ):
            issues.append(f"{platform} audio bitrate is unavailable or too low")
        if limits.get("faststart") or limits.get("edit_lists") is False:
            if atom_data is None:
                try:
                    with media_path.open("rb") as media:
                        atom_data = media.read(4 * 1024 * 1024)
                except OSError:
                    atom_data = b""
            if limits.get("faststart") and not (
                0 <= atom_data.find(b"moov") < atom_data.find(b"mdat")
            ):
                issues.append(f"{platform} requires a fast-start MP4")
            if limits.get("edit_lists") is False and b"edts" in atom_data:
                issues.append(f"{platform} does not accept MP4 edit lists")
    return issues


def upload_fields(platform: str, copy: dict) -> dict[str, str]:
    """Map exact reviewed destination copy to Upload-Post multipart fields."""
    spec = SHORT_PLATFORM_SPECS[platform]
    result = {
        provider_field: copy[key]
        for key, provider_field in spec["upload_fields"].items()
        if isinstance(copy.get(key), str) and copy[key]
    }
    result.update(spec.get("fixed_upload_fields", {}))
    return result
