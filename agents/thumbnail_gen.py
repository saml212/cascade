"""Thumbnail generation agent — create caricature artwork for longform episodes.

Uses Claude to analyze the transcript and describe the podcast subjects,
then calls OpenAI's image generation API to create a caricature thumbnail.

Inputs:
    - diarized_transcript.json, episode.json (or episode_info.json)
Outputs:
    - thumbnail.png (1024x1024 square)
    - thumbnail_gen.json (prompt used, metadata)
Dependencies:
    - anthropic SDK (Claude API for prompt generation)
    - openai SDK (image generation)
Config:
    - clip_mining.llm_model (Claude model for prompt generation)
Environment:
    - ANTHROPIC_API_KEY
    - OPENAI_API_KEY
"""

import json
import os
from pathlib import Path

import httpx

from agents.base import BaseAgent


class ThumbnailGenAgent(BaseAgent):
    name = "thumbnail_gen"

    def execute(self) -> dict:
        diarized = self.load_json("diarized_transcript.json")
        episode_data = self.load_json_safe("episode.json")
        episode_info = self.load_json_safe("episode_info.json")

        # Merge episode context
        guest_name = episode_data.get("guest_name") or episode_info.get("guest_name", "")
        guest_title = episode_data.get("guest_title") or episode_info.get("guest_title", "")
        episode_name = episode_data.get("episode_name") or episode_info.get("episode_name", "")
        episode_description = episode_data.get("episode_description") or episode_info.get("episode_description", "")

        # Build a condensed transcript for Claude to analyze
        transcript_text = self._build_transcript_summary(diarized)

        # Step 1: Use Claude to generate an image prompt
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")

        import anthropic

        client = anthropic.Anthropic(api_key=anthropic_key)
        model = self.get_config("clip_mining", "llm_model", default="claude-sonnet-4-20250514")

        self.report_progress(1, 3, "Analyzing transcript for thumbnail concept")

        analysis_prompt = f"""You are a creative director creating a podcast episode thumbnail.

EPISODE CONTEXT:
- Guest: {guest_name}{f' — {guest_title}' if guest_title else ''}
- Episode name: {episode_name}
- Description: {episode_description}

TRANSCRIPT (condensed):
{transcript_text}

YOUR TASK:
1. Identify the podcast subject(s) — how many people are featured (1, 2, or 3)?
2. Based on the transcript, what are the key topics/activities discussed?
3. Generate a detailed image generation prompt for a FUN CARICATURE of the podcast subject(s).

The caricature should:
- Depict the subject(s) in an exaggerated, fun cartoon style
- Show them doing something related to what they discussed in the podcast
- Be fairly zoomed out to show full body/scene
- Be colorful and eye-catching
- Have a simple, clean background that complements the scene
- NOT include any text, titles, or logos
- Be in a square format (1:1 aspect ratio)

Return your response as JSON with these fields:
{{
    "num_subjects": 1 or 2 or 3,
    "subject_descriptions": ["brief physical/role description of each person"],
    "activity": "what they should be doing in the image",
    "image_prompt": "The complete, detailed prompt for the image generation model. Be very specific about the caricature style, poses, expressions, scene, colors, and composition."
}}

Return ONLY the JSON object, no other text."""

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            temperature=0.6,
            messages=[{"role": "user", "content": analysis_prompt}],
        )

        response_text = response.content[0].text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

        analysis = json.loads(response_text)
        image_prompt = analysis["image_prompt"]

        self.logger.info(
            "Thumbnail concept: %d subject(s), activity=%s",
            analysis.get("num_subjects", "?"),
            analysis.get("activity", "?"),
        )

        # Step 2: Generate the image via OpenAI and save directly
        self.report_progress(2, 3, "Generating caricature via OpenAI")

        thumbnail_path = self.episode_dir / "thumbnail.png"
        self._generate_image_openai(openai_key, image_prompt, thumbnail_path)

        self.report_progress(3, 3, "Thumbnail saved")

        # Save the analysis and prompt for potential re-generation
        self.save_json("thumbnail_gen.json", {
            "analysis": analysis,
            "image_prompt": image_prompt,
            "thumbnail_path": str(thumbnail_path),
        })

        return {
            "thumbnail_path": str(thumbnail_path),
            "num_subjects": analysis.get("num_subjects", 0),
            "image_prompt": image_prompt,
        }

    def _build_transcript_summary(self, diarized: dict) -> str:
        """Build a condensed transcript for analysis (~first 10 min + last 5 min)."""
        utterances = diarized.get("utterances", [])
        if not utterances:
            return "No transcript available."

        lines = []
        for utt in utterances:
            start = utt.get("start", 0)
            channel = utt.get("channel", utt.get("speaker", "?"))
            if isinstance(channel, int):
                channel = "L" if channel == 0 else "R"
            text = utt.get("text", "").strip()
            if text:
                lines.append(f"[{start:.0f}s] {channel}: {text}")

        full_text = "\n".join(lines)

        # Keep it under ~8K chars for the Claude prompt
        if len(full_text) <= 8000:
            return full_text

        # Take first 5000 chars + last 3000 chars
        first = full_text[:5000]
        last = full_text[-3000:]
        first = first[:first.rfind("\n")]
        last = last[last.find("\n") + 1:]
        return f"{first}\n\n... [middle of conversation omitted] ...\n\n{last}"

    def _generate_image_openai(self, api_key: str, prompt: str, dest: Path):
        """Call OpenAI's image generation API and save directly to dest."""
        import base64

        resp = httpx.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-image-1",
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
                "quality": "high",
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        image_data = data["data"][0]

        # gpt-image-1 returns b64_json by default
        if "b64_json" in image_data:
            img_bytes = base64.b64decode(image_data["b64_json"])
            with open(dest, "wb") as f:
                f.write(img_bytes)
        elif "url" in image_data:
            img_resp = httpx.get(image_data["url"], timeout=60, follow_redirects=True)
            img_resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(img_resp.content)
        else:
            raise RuntimeError(f"Unexpected OpenAI response format: {list(image_data.keys())}")

        self.logger.info("Saved thumbnail to %s (%d bytes)", dest, dest.stat().st_size)
