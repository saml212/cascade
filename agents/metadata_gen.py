"""Metadata generation agent — generate per-platform metadata and publish schedule via Claude."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.base import BaseAgent


class MetadataGenAgent(BaseAgent):
    name = "metadata_gen"

    def execute(self) -> dict:
        clips_data = self.load_json("clips.json")
        diarized = self.load_json("diarized_transcript.json")
        clips = clips_data.get("clips", [])

        # Load episode info for cross-references
        episode_info = {}
        try:
            episode_info = self.load_json("episode_info.json")
        except (FileNotFoundError, Exception):
            pass

        guest_name = episode_info.get("guest_name", "")
        guest_title = episode_info.get("guest_title", "")
        episode_description = episode_info.get("episode_description", "")

        # Load podcast config
        podcast_config = self.config.get("podcast", {})
        podcast_title = podcast_config.get("title", "The Local Podcast")
        channel_handle = podcast_config.get("channel_handle", "@local")

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        model = self.config.get("clip_mining", {}).get("llm_model", "claude-opus-4-6")

        # Build context: clips + transcript excerpts
        clip_summaries = []
        for clip in clips:
            # Get transcript excerpt for this clip
            excerpt = self._get_excerpt(diarized, clip["start_seconds"], clip["end_seconds"])
            clip_summaries.append({
                "id": clip["id"],
                "title": clip["title"],
                "hook_text": clip["hook_text"],
                "duration": clip["duration"],
                "virality_score": clip["virality_score"],
                "transcript_excerpt": excerpt[:500],
            })

        # Build guest/episode context block
        guest_context = ""
        if guest_name:
            guest_context = f"""
EPISODE CONTEXT:
- Guest: {guest_name}{f' — {guest_title}' if guest_title else ''}
- Podcast: {podcast_title} (channel: {channel_handle})
- Episode description: {episode_description}

IMPORTANT RULES:
- Longform title MUST follow this format: "{guest_name} | {podcast_title}"
- Longform description should mention who {guest_name} is and what they do
- ALL short-form clip metadata MUST include a reference to the full episode channel ({channel_handle})
- Include "{guest_name}" in clip captions/descriptions where natural
- YouTube Shorts descriptions should include "Full episode on {channel_handle}"
- TikTok captions should include "{channel_handle}"
- Instagram captions should include "Full ep on {channel_handle}"
"""
        else:
            guest_context = f"""
EPISODE CONTEXT:
- Podcast: {podcast_title} (channel: {channel_handle})

IMPORTANT RULES:
- ALL short-form clip metadata MUST include a reference to the full episode channel ({channel_handle})
- YouTube Shorts descriptions should include "Full episode on {channel_handle}"
- TikTok captions should include "{channel_handle}"
- Instagram captions should include "Full ep on {channel_handle}"
"""

        prompt = f"""You are a social media strategist for a podcast. Generate metadata for these clips and the longform episode.
{guest_context}
CLIPS:
{json.dumps(clip_summaries, indent=2)}

Generate a JSON object with these sections:

1. "longform": object with:
   - "title": YouTube episode title (max 100 chars{f', format: "{guest_name} | {podcast_title}"' if guest_name else ''})
   - "description": YouTube description (2-3 paragraphs, include timestamps, call to action)
   - "tags": array of 10-15 relevant tags

2. "clips": array (one per clip, same order) each with:
   - "id": clip ID
   - "youtube": object with "title" (max 100 chars with #Shorts), "description" (2-3 lines, include "Full episode on {channel_handle}")
   - "tiktok": object with "caption" (max 150 chars with hashtags inline, include {channel_handle}), "hashtags" (array of 5-8 hashtags)
   - "instagram": object with "caption" (max 200 chars with CTA, include "Full ep on {channel_handle}"), "hashtags" (array of 10 hashtags)
   - "linkedin": object with "title" (professional tone, max 100 chars), "description" (1-2 paragraphs, insight-driven)
   - "x": object with "text" (max 280 chars including hashtags, punchy and engaging)
   - "facebook": object with "title" (max 100 chars), "description" (conversational, 1-2 paragraphs)
   - "threads": object with "text" (max 500 chars, conversational tone)
   - "pinterest": object with "title" (max 100 chars, keyword-rich), "description" (2-3 sentences, searchable)
   - "bluesky": object with "text" (max 300 chars, casual tone)

3. "schedule": array of publish slots, each with:
   - "clip_id": which clip
   - "platform": "youtube" | "tiktok" | "instagram" | "linkedin" | "x" | "facebook" | "threads" | "pinterest" | "bluesky"
   - "day_offset": days from today (0 = today)
   - "time_slot": "morning" | "afternoon" | "evening"

Schedule rules: 1 clip/day Mon-Thu, 2 clips/day Fri-Sun. Stagger platforms. Prioritize YouTube, TikTok, Instagram first, then rotate LinkedIn, X, Facebook, Threads, Pinterest, Bluesky.

Return ONLY the JSON object, no other text."""

        self.logger.info("Generating metadata via Claude...")
        response = client.messages.create(
            model=model,
            max_tokens=16384,
            temperature=0.4,
            messages=[{"role": "user", "content": prompt}],
        )

        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Metadata response truncated (hit max_tokens). "
                f"Used {response.usage.output_tokens} output tokens."
            )

        response_text = response.content[0].text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

        metadata = json.loads(response_text)

        # Save metadata
        metadata_dir = self.episode_dir / "metadata"
        metadata_dir.mkdir(exist_ok=True)
        self.save_json("metadata/metadata.json", metadata)

        return {
            "metadata_path": str(metadata_dir / "metadata.json"),
            "longform_title": metadata.get("longform", {}).get("title", ""),
            "clip_metadata_count": len(metadata.get("clips", [])),
            "schedule_entries": len(metadata.get("schedule", [])),
        }

    def _get_excerpt(self, diarized: dict, start: float, end: float) -> str:
        lines = []
        for utt in diarized.get("utterances", []):
            utt_start = utt.get("start", 0)
            utt_end = utt.get("end", 0)
            if utt_end > start and utt_start < end:
                lines.append(utt.get("text", ""))
        return " ".join(lines)
