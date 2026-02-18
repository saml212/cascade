"""Clip miner agent — use Claude to identify the best short-form clips from the transcript."""

import json
import os
from pathlib import Path

from agents.base import BaseAgent


class ClipMinerAgent(BaseAgent):
    name = "clip_miner"

    def execute(self) -> dict:
        diarized = self.load_json("diarized_transcript.json")
        segments_data = self.load_json("segments.json")
        stitch_data = self.load_json("stitch.json")

        total_duration = stitch_data["duration_seconds"]
        clip_count = self.config.get("processing", {}).get("clip_count", 10)
        clip_min = self.config.get("processing", {}).get("clip_min_seconds", 30)
        clip_max = self.config.get("processing", {}).get("clip_max_seconds", 90)

        # Format transcript for Claude
        transcript_text = self._format_transcript(diarized)

        # Call Claude
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        model = self.config.get("clip_mining", {}).get("llm_model", "claude-opus-4-6")
        temperature = self.config.get("clip_mining", {}).get("llm_temperature", 0.3)

        # Extract guest/episode info from early transcript
        episode_info = self._extract_episode_info(client, model, diarized)
        self.save_json("episode_info.json", episode_info)

        # Update episode.json with extracted info
        episode_file = self.episode_dir / "episode.json"
        if episode_file.exists():
            with open(episode_file) as f:
                episode = json.load(f)
            episode["guest_name"] = episode_info.get("guest_name", "")
            episode["guest_title"] = episode_info.get("guest_title", "")
            episode["episode_name"] = episode_info.get("episode_title", "")
            episode["episode_description"] = episode_info.get("episode_description", "")
            with open(episode_file, "w") as f:
                json.dump(episode, f, indent=2, default=str)

        prompt = f"""You are an expert podcast clip editor. Analyze this transcript and identify the {clip_count} best clips for short-form video (YouTube Shorts, TikTok, Instagram Reels).

Each clip should be {clip_min}-{clip_max} seconds long and should:
- Have a strong hook in the first 3 seconds
- Tell a complete micro-story or make a compelling point
- Be emotionally engaging, funny, surprising, or deeply insightful
- End on a strong note (punchline, revelation, call-to-action)

The total episode duration is {total_duration:.1f} seconds.

TRANSCRIPT (with timestamps and speaker labels):
{transcript_text}

Return EXACTLY a JSON array of {clip_count} clips. Each clip must have these fields:
- "start_seconds": number (start time in seconds)
- "end_seconds": number (end time in seconds)
- "title": string (catchy title, max 60 chars)
- "hook_text": string (the opening hook line)
- "compelling_reason": string (why this clip will perform well)
- "virality_score": number (1-10, how viral this clip could be)

Return ONLY the JSON array, no other text."""

        self.logger.info(f"Calling {model} for clip mining...")
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )

        # Parse response
        response_text = response.content[0].text.strip()
        # Handle markdown code blocks
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

        clips = json.loads(response_text)

        # Snap clip boundaries to silence
        clips = self._snap_to_silence(clips, segments_data)

        # Determine dominant speaker per clip
        segments = segments_data.get("segments", [])
        for i, clip in enumerate(clips):
            clip["id"] = f"clip_{i+1:02d}"
            clip["rank"] = i + 1
            clip["duration"] = round(clip["end_seconds"] - clip["start_seconds"], 1)
            clip["speaker"] = self._get_dominant_speaker(
                clip["start_seconds"], clip["end_seconds"], segments
            )
            clip["status"] = "pending"
            clip["manual"] = False

        result = {
            "clips": clips,
            "clip_count": len(clips),
            "model_used": model,
        }
        self.save_json("clips.json", result)
        return result

    def _format_transcript(self, diarized: dict) -> str:
        lines = []
        for utt in diarized.get("utterances", []):
            speaker = utt.get("speaker", "?")
            start = utt.get("start", 0)
            end = utt.get("end", 0)
            text = utt.get("text", "")
            lines.append(f"[{start:.1f}s - {end:.1f}s] Speaker {speaker}: {text}")
        return "\n".join(lines)

    def _snap_to_silence(self, clips: list, segments_data: dict) -> list:
        """Snap clip boundaries to nearest low-energy point."""
        tolerance = self.config.get("clip_mining", {}).get(
            "boundary_snap_tolerance_seconds", 3.0
        )

        # Try to load RMS data for silence detection
        rms_path = self.episode_dir / "work" / "rms_data.json"
        if not rms_path.exists():
            return clips

        try:
            with open(rms_path) as f:
                rms_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return clips

        frame_sec = rms_data.get("frame_seconds", 0.1)
        left_rms = rms_data.get("left_rms_db", [])
        right_rms = rms_data.get("right_rms_db", [])

        if not left_rms or not right_rms:
            return clips

        import numpy as np

        # Combined energy
        combined = np.array(left_rms) + np.array(right_rms)

        for clip in clips:
            for key in ("start_seconds", "end_seconds"):
                t = clip[key]
                # Search window
                lo = max(0, int((t - tolerance) / frame_sec))
                hi = min(len(combined), int((t + tolerance) / frame_sec))
                if lo >= hi:
                    continue
                # Find minimum energy frame in window
                window = combined[lo:hi]
                min_idx = lo + int(np.argmin(window))
                clip[key] = round(min_idx * frame_sec, 2)

        return clips

    def _extract_episode_info(self, client, model: str, diarized: dict) -> dict:
        """Extract guest name and episode info from the first ~3 minutes of transcript."""
        # Get utterances from first 3 minutes
        early_utterances = []
        for utt in diarized.get("utterances", []):
            if utt.get("start", 0) > 180:
                break
            early_utterances.append(utt)

        if not early_utterances:
            return {"guest_name": "", "guest_title": "", "episode_title": "", "episode_description": ""}

        early_text = "\n".join(
            f"Speaker {utt.get('speaker', '?')}: {utt.get('text', '')}"
            for utt in early_utterances
        )

        prompt = f"""Analyze this podcast transcript opening and extract guest information.

TRANSCRIPT (first ~3 minutes):
{early_text}

Return a JSON object with:
- "guest_name": full name of the guest (empty string if not mentioned)
- "guest_title": who they are / what they do (e.g. "Nuclear Navy Officer", empty string if unknown)
- "episode_title": suggested episode title (default to guest name if available)
- "episode_description": 2-3 sentence description of the episode topic and guest

Return ONLY the JSON object, no other text."""

        self.logger.info("Extracting guest/episode info from transcript...")
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

        try:
            info = json.loads(response_text)
        except json.JSONDecodeError:
            self.logger.warning("Failed to parse episode info response, using empty defaults")
            info = {"guest_name": "", "guest_title": "", "episode_title": "", "episode_description": ""}

        return info

    def _get_dominant_speaker(
        self, start: float, end: float, segments: list
    ) -> str:
        """Determine dominant speaker for a time range from segments."""
        speaker_time = {}
        for seg in segments:
            seg_start = seg["start"]
            seg_end = seg["end"]
            overlap_start = max(start, seg_start)
            overlap_end = min(end, seg_end)
            if overlap_start < overlap_end:
                speaker = seg["speaker"]
                speaker_time[speaker] = speaker_time.get(speaker, 0) + (
                    overlap_end - overlap_start
                )

        if not speaker_time:
            return "BOTH"

        return max(speaker_time, key=speaker_time.get)
