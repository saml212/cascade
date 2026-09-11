"""Fill missing release copy without replacing editorial metadata."""

from __future__ import annotations

import copy
import json

from agents.base import BaseAgent
from lib.generation import generate_structured

PLATFORM_FIELDS = {
    "youtube": ("title", "description"),
    "tiktok": ("caption", "hashtags"),
    "instagram": ("caption", "hashtags"),
    "x": ("text",),
}

_PLATFORM_SCHEMAS = {
    "youtube": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["title", "description"],
        "additionalProperties": False,
    },
    "tiktok": {
        "type": "object",
        "properties": {
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["caption", "hashtags"],
        "additionalProperties": False,
    },
    "instagram": {
        "type": "object",
        "properties": {
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["caption", "hashtags"],
        "additionalProperties": False,
    },
    "x": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}

_CLIP_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        **_PLATFORM_SCHEMAS,
    },
    "required": ["id", *_PLATFORM_SCHEMAS],
    "additionalProperties": False,
}

_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "longform": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "description", "tags"],
            "additionalProperties": False,
        },
        "clips": {"type": "array", "items": _CLIP_METADATA_SCHEMA},
    },
    "required": ["longform", "clips"],
    "additionalProperties": False,
}


def _has_fields(value: object, fields: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(value.get(field) for field in fields)


class MetadataGenAgent(BaseAgent):
    name = "metadata_gen"

    def execute(self) -> dict:
        episode = self.load_json_safe("episode.json")
        clips_data = self.load_json("clips.json")
        all_clips = clips_data.get("clips", [])
        clips = [
            clip
            for clip in all_clips
            if clip.get("selection_status") != "rejected"
            and clip.get("status") != "rejected"
        ]
        existing = self.load_json_safe("metadata/metadata.json")
        metadata = self._existing_metadata(episode, clips, existing)
        enabled = self._enabled_platforms()
        missing = self._missing_copy(metadata, enabled)

        provenance: dict
        if missing:
            diarized = self.load_json("diarized_transcript.json")
            if diarized.get("clock") != "source":
                raise ValueError("A source-clock canonical transcript is required")
            generated, provenance = self._generate(
                episode,
                clips,
                diarized,
                metadata,
                missing,
            )
            metadata = self._merge_missing(metadata, generated)
            remaining = self._missing_copy(metadata, enabled)
            if remaining:
                raise ValueError(f"Generated metadata is incomplete: {remaining}")
        else:
            provenance = {
                "provider": "existing_editorial",
                "generated": False,
            }

        metadata["generation"] = provenance
        if existing.get("schedule"):
            metadata["schedule"] = existing["schedule"]
        else:
            metadata.setdefault("schedule", [])
        self.save_json("metadata/metadata.json", metadata)
        self._sync_missing_clip_metadata(clips_data, metadata)
        self._fill_missing_longform(episode, metadata.get("longform", {}))
        return {
            "metadata_path": str(self.episode_dir / "metadata" / "metadata.json"),
            "longform_title": metadata.get("longform", {}).get("title", ""),
            "clip_metadata_count": len(metadata.get("clips", [])),
            "generated": bool(missing),
            "generation": provenance,
        }

    def _enabled_platforms(self) -> list[str]:
        settings = self.config.get("platforms", {})
        return [
            platform
            for platform in PLATFORM_FIELDS
            if settings.get(platform, {}).get("enabled")
        ]

    @staticmethod
    def _existing_metadata(
        episode: dict, clips: list[dict], existing: dict
    ) -> dict:
        existing_longform = existing.get("longform", {})
        longform = {
            field: episode.get(field) or existing_longform.get(field)
            for field in ("title", "description", "tags")
        }
        existing_by_id = {
            str(item["id"]): item
            for item in existing.get("clips", [])
            if isinstance(item, dict) and item.get("id")
        }
        resolved = []
        for clip in clips:
            clip_id = str(clip["id"])
            stored = dict(existing_by_id.get(clip_id, {}))
            inline = clip.get("metadata") or {}
            for platform, value in inline.items():
                if value:
                    stored[platform] = value
            stored["id"] = clip_id
            resolved.append(stored)
        return {"longform": longform, "clips": resolved}

    @staticmethod
    def _missing_copy(metadata: dict, enabled: list[str]) -> list[dict]:
        missing: list[dict] = []
        longform = metadata.get("longform", {})
        longform_fields = [
            field for field in ("title", "description") if not longform.get(field)
        ]
        if longform_fields:
            missing.append({"scope": "longform", "fields": longform_fields})
        for clip in metadata.get("clips", []):
            for platform in enabled:
                if not _has_fields(clip.get(platform), PLATFORM_FIELDS[platform]):
                    missing.append(
                        {
                            "scope": "clip",
                            "clip_id": clip["id"],
                            "platform": platform,
                            "fields": list(PLATFORM_FIELDS[platform]),
                        }
                    )
        return missing

    def _generate(
        self,
        episode: dict,
        clips: list[dict],
        diarized: dict,
        current: dict,
        missing: list[dict],
    ) -> tuple[dict, dict]:
        podcast = self.config.get("podcast", {})
        summaries = []
        for clip in clips:
            summaries.append(
                {
                    "id": clip["id"],
                    "title": clip.get("title", ""),
                    "hook_text": clip.get("hook_text", ""),
                    "start_seconds": clip.get("start_seconds", clip.get("start")),
                    "end_seconds": clip.get("end_seconds", clip.get("end")),
                    "transcript_excerpt": self._get_excerpt(
                        diarized,
                        float(clip.get("start_seconds", clip.get("start"))),
                        float(clip.get("end_seconds", clip.get("end"))),
                    ),
                    "locked_editorial_copy": clip.get("metadata") or {},
                }
            )
        prompt = f"""Fill only the missing podcast release-copy fields listed below.

MISSING FIELDS:
{json.dumps(missing, indent=2)}

EPISODE:
{json.dumps({
    "guest_name": episode.get("guest_name", ""),
    "title": episode.get("title", ""),
    "description": episode.get("description", ""),
    "youtube_longform_url": episode.get("youtube_longform_url", ""),
    "spotify_longform_url": episode.get("spotify_longform_url", ""),
    "podcast_title": podcast.get("title", "The Local Podcast"),
    "channel_handle": podcast.get("channel_handle", ""),
}, indent=2)}

CLIPS AND SOURCE-CLOCK TRANSCRIPT EVIDENCE:
{json.dumps(summaries, indent=2)}

CURRENT LOCKED METADATA:
{json.dumps(current, indent=2)}

Return the complete schema. Existing nonempty copy is locked: reproduce it exactly.
Ground every new factual claim in the supplied transcript excerpt. Do not invent a
quotation, achievement, identity, place, or event. Do not claim a published episode,
live URL, or link when the corresponding URL above is empty. Keep YouTube titles under
100 characters and X text under 280 characters. Use platform-appropriate copy."""
        generated, provenance = generate_structured(
            self.config,
            task="podcast_release_metadata",
            instructions=(
                "You are a careful podcast copy editor. The transcript and locked "
                "editorial fields are authoritative. Return only schema-valid JSON."
            ),
            prompt=prompt,
            schema=_METADATA_SCHEMA,
            max_output_tokens=16384,
        )
        provenance = {
            **provenance,
            "clock": "source",
            "raw_transcript_sha256": diarized.get("provenance", {}).get(
                "raw_transcript_sha256"
            ),
            "corrections_fingerprint": diarized.get("provenance", {}).get(
                "corrections_fingerprint"
            ),
        }
        return generated, provenance

    @staticmethod
    def _merge_missing(current: dict, generated: dict) -> dict:
        merged = {
            "longform": dict(current.get("longform", {})),
            "clips": [dict(item) for item in current.get("clips", [])],
        }
        for field, value in generated.get("longform", {}).items():
            if not merged["longform"].get(field) and value:
                merged["longform"][field] = value
        generated_by_id = {
            str(item["id"]): item
            for item in generated.get("clips", [])
            if isinstance(item, dict) and item.get("id")
        }
        for clip in merged["clips"]:
            candidate = generated_by_id.get(str(clip["id"]), {})
            for platform, fields in PLATFORM_FIELDS.items():
                if not _has_fields(clip.get(platform), fields):
                    value = candidate.get(platform)
                    if value:
                        clip[platform] = value
        return merged

    def _sync_missing_clip_metadata(self, clips_data: dict, metadata: dict) -> None:
        original = copy.deepcopy(clips_data)
        all_clips = clips_data.get("clips", [])
        by_id = {
            str(item["id"]): item
            for item in metadata.get("clips", [])
            if isinstance(item, dict) and item.get("id")
        }
        changed = False
        for clip in all_clips:
            generated = by_id.get(str(clip.get("id")), {})
            inline = clip.get("metadata")
            if not isinstance(inline, dict):
                inline = {}
            for platform in PLATFORM_FIELDS:
                if not inline.get(platform) and generated.get(platform):
                    if clip.get("metadata") is not inline:
                        clip["metadata"] = inline
                    inline[platform] = generated[platform]
                    changed = True
        if not changed:
            return
        if self.load_json("clips.json") != original:
            raise ValueError("clips.json changed during metadata generation; retry")
        updated = dict(clips_data)
        updated["clips"] = all_clips
        self.save_json("clips.json", updated)

    def _fill_missing_longform(self, episode: dict, longform: dict) -> None:
        original = dict(episode)
        changed = False
        for field in ("title", "description", "tags"):
            if not episode.get(field) and longform.get(field):
                episode[field] = longform[field]
                changed = True
        if changed:
            if self.load_json_safe("episode.json") != original:
                raise ValueError("episode.json changed during metadata generation; retry")
            self.save_json("episode.json", episode)

    @staticmethod
    def _get_excerpt(diarized: dict, start: float, end: float) -> str:
        words = [
            word
            for utterance in diarized.get("utterances", [])
            for word in utterance.get("words", [])
            if start <= (float(word["start"]) + float(word["end"])) / 2 < end
        ]
        words.sort(
            key=lambda word: (float(word["start"]), float(word["end"]))
        )
        return " ".join(
            f"{word.get('punctuated_word') or word.get('word', '')}"
            f"{' [?]' if word.get('suspect') else ''}"
            for word in words
        ).strip()
