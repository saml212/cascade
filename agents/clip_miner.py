"""Find source-clock short-form candidates from the canonical transcript."""

from __future__ import annotations

import json
import math
import re

from agents.base import BaseAgent
from lib.atomic_write import atomic_write_json
from lib.generation import generate_structured

_EPISODE_INFO_SCHEMA = {
    "type": "object",
    "properties": {
        "guest_name": {"type": "string"},
        "guest_title": {"type": "string"},
        "episode_title": {"type": "string"},
        "episode_description": {"type": "string"},
    },
    "required": [
        "guest_name",
        "guest_title",
        "episode_title",
        "episode_description",
    ],
    "additionalProperties": False,
}
_CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "start_seconds": {"type": "number"},
        "end_seconds": {"type": "number"},
        "title": {"type": "string"},
        "hook_text": {"type": "string"},
        "compelling_reason": {"type": "string"},
        "virality_score": {"type": "number"},
    },
    "required": [
        "start_seconds",
        "end_seconds",
        "title",
        "hook_text",
        "compelling_reason",
        "virality_score",
    ],
    "additionalProperties": False,
}
_CLIP_MINER_SCHEMA = {
    "type": "object",
    "properties": {
        "episode_info": _EPISODE_INFO_SCHEMA,
        "clips": {"type": "array", "items": _CLIP_SCHEMA},
    },
    "required": ["episode_info", "clips"],
    "additionalProperties": False,
}


def _normalized(value: str) -> str:
    return re.sub(r"[^\w']+", " ", value.casefold()).strip()


class ClipMinerAgent(BaseAgent):
    name = "clip_miner"

    def execute(self) -> dict:
        existing = self.load_json_safe("clips.json")
        if existing.get("clips"):
            return {
                "_status": "skipped",
                "reason": "clips.json already populated",
                "clip_count": len(existing["clips"]),
            }

        diarized, segments_data, total_duration = self._load_inputs()
        clip_count = int(self.get_config("processing", "clip_count", default=10))
        parsed, provenance = self._mine(
            diarized,
            total_duration,
            clip_count,
            exclusions=[],
        )
        clips = self._prepare_clips(
            parsed.get("clips", []),
            diarized,
            segments_data.get("segments", []),
            total_duration,
            exclusions=[],
        )
        episode_info = parsed["episode_info"]
        self.save_json("episode_info.json", episode_info)
        self._fill_missing_episode_info(episode_info)
        result = {
            "clips": clips,
            "clip_count": len(clips),
            "model_used": provenance["model"],
            "generation": self._generation_provenance(provenance, diarized),
        }
        self.save_json("clips.json", result)
        return result

    def generate_alternative(self, clip_id: str) -> dict:
        """Append one grounded alternative while preserving every stored candidate."""
        stored = self.load_json("clips.json")
        clips = stored.get("clips", [])
        rejected = next((clip for clip in clips if clip.get("id") == clip_id), None)
        if rejected is None:
            raise KeyError(f"Unknown clip: {clip_id}")

        diarized, segments_data, total_duration = self._load_inputs()
        exclusions = [
            (float(clip["start_seconds"]), float(clip["end_seconds"]))
            for clip in clips
            if clip.get("start_seconds") is not None
            and clip.get("end_seconds") is not None
        ]
        parsed, provenance = self._mine(
            diarized,
            total_duration,
            1,
            exclusions=exclusions,
        )
        alternatives = self._prepare_clips(
            parsed.get("clips", []),
            diarized,
            segments_data.get("segments", []),
            total_duration,
            exclusions=exclusions,
        )
        if not alternatives:
            raise RuntimeError("Generator did not return a non-overlapping alternative")
        if self.load_json("clips.json") != stored:
            raise ValueError("clips.json changed while generating an alternative; retry")

        rejected["status"] = "rejected"
        rejected["selection_status"] = "rejected"
        candidate = alternatives[0]
        candidate["id"] = self._next_clip_id(clips)
        candidate["rank"] = (
            max((int(clip.get("rank", 0)) for clip in clips), default=0) + 1
        )
        candidate["selection_status"] = "selected"
        candidate["alternative_for"] = clip_id
        clips.append(candidate)
        updated = dict(stored)
        updated.update(
            clips=clips,
            clip_count=len(clips),
            generation=self._generation_provenance(provenance, diarized),
        )
        atomic_write_json(self.episode_dir / "clips.json", updated)
        return {
            "rejected_clip_id": clip_id,
            "alternative": candidate,
            "generation": updated["generation"],
        }

    def _load_inputs(self) -> tuple[dict, dict, float]:
        diarized = self.load_json("diarized_transcript.json")
        if diarized.get("clock") != "source":
            raise ValueError("A source-clock canonical transcript is required")
        segments = self.load_json("segments.json")
        if segments.get("clock") != "source":
            raise ValueError("Source-clock speaker segments are required")
        stitch = self.load_json("stitch.json")
        duration = float(stitch["duration_seconds"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Episode duration must be positive and finite")
        return diarized, segments, duration

    def _mine(
        self,
        diarized: dict,
        total_duration: float,
        count: int,
        *,
        exclusions: list[tuple[float, float]],
    ) -> tuple[dict, dict]:
        preferred_min = self.get_config("processing", "clip_min_seconds", default=40)
        preferred_max = self.get_config("processing", "clip_max_seconds", default=75)
        excluded_text = (
            "\n".join(f"- {start:.3f}s to {end:.3f}s" for start, end in exclusions)
            or "- none"
        )
        prompt = f"""Choose up to {count} strong clips from this podcast transcript.

Episode duration: {total_duration:.3f} seconds.
Preferred duration: {preferred_min}-{preferred_max} seconds. A complete thought or
story may run 20-90 seconds; never cut a sentence merely to meet the preference.
Return fewer clips when the transcript does not contain {count} strong choices.
Use source timestamps exactly as supplied. Each hook_text must be exact spoken text
inside its clip. Ground titles and reasons in the transcript; do not invent quotes,
events, achievements, or publication links. Favor complete stories and strong endings.
Include Bay Area material when it is among the strongest content.

Do not overlap these existing candidate ranges:
{excluded_text}

SOURCE-CLOCK TRANSCRIPT:
{self._format_transcript(diarized)}"""
        return generate_structured(
            self.config,
            task="podcast_clip_candidates",
            instructions=(
                "Act as a careful podcast editor. Treat the supplied transcript as "
                "the only factual source and return the requested JSON schema."
            ),
            prompt=prompt,
            schema=_CLIP_MINER_SCHEMA,
            max_output_tokens=8192,
        )

    def _prepare_clips(
        self,
        generated: list[dict],
        diarized: dict,
        segments: list[dict],
        total_duration: float,
        *,
        exclusions: list[tuple[float, float]],
    ) -> list[dict]:
        clips = []
        for item in generated:
            clip = dict(item)
            start = float(clip["start_seconds"])
            end = float(clip["end_seconds"])
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or end > total_duration + 0.001
                or end - start < 5
                or end - start > 300
            ):
                raise ValueError(f"Generated clip has invalid bounds: {start}-{end}")
            if any(max(start, left) < min(end, right) for left, right in exclusions):
                continue

            excerpt = self._excerpt_words(diarized, start, end)
            if not excerpt:
                raise ValueError(
                    f"Generated clip {start}-{end} has no transcript words"
                )
            hook = str(clip.get("hook_text", "")).strip()
            if _normalized(hook) not in _normalized(excerpt):
                clip["proposed_hook_text"] = hook
                clip["hook_text"] = self._opening_phrase(excerpt)
                clip["hook_grounding"] = "replaced_with_canonical_transcript"
            else:
                clip["hook_grounding"] = "exact_canonical_transcript"
            clip.update(
                start_seconds=start,
                end_seconds=end,
                start=start,
                end=end,
                duration=round(end - start, 3),
                speaker=self._get_dominant_speaker(start, end, segments),
                status="pending",
                manual=False,
            )
            clips.append(clip)
        for index, clip in enumerate(clips, 1):
            clip.setdefault("id", f"clip_{index:02d}")
            clip.setdefault("rank", index)
        return clips

    @staticmethod
    def _generation_provenance(provenance: dict, diarized: dict) -> dict:
        transcript = diarized.get("provenance", {})
        return {
            **provenance,
            "clock": "source",
            "raw_transcript_sha256": transcript.get("raw_transcript_sha256"),
            "corrections_fingerprint": transcript.get("corrections_fingerprint"),
        }

    def _fill_missing_episode_info(self, info: dict) -> None:
        episode = self.load_json_safe("episode.json")
        if not episode:
            return
        for destination, source in (
            ("guest_name", "guest_name"),
            ("guest_title", "guest_title"),
            ("episode_name", "episode_title"),
            ("episode_description", "episode_description"),
        ):
            if not episode.get(destination) and info.get(source):
                episode[destination] = info[source]
        self.save_json("episode.json", episode)

    @staticmethod
    def _next_clip_id(clips: list[dict]) -> str:
        numbers = []
        for clip in clips:
            match = re.fullmatch(r"clip_(\d+)", str(clip.get("id", "")))
            if match:
                numbers.append(int(match.group(1)))
        return f"clip_{max(numbers, default=0) + 1:02d}"

    def _format_transcript(self, diarized: dict) -> str:
        lines = []
        for utterance in diarized.get("utterances", []):
            speaker = utterance.get("speaker", "?")
            start = float(utterance.get("start", 0))
            end = float(utterance.get("end", 0))
            text = utterance.get("text", "")
            lines.append(f"[{start:.3f}s-{end:.3f}s] Speaker {speaker}: {text}")
        return "\n".join(lines)

    @staticmethod
    def _excerpt_words(diarized: dict, start: float, end: float) -> str:
        words = [
            word
            for utterance in diarized.get("utterances", [])
            for word in utterance.get("words", [])
            if start <= (float(word["start"]) + float(word["end"])) / 2 < end
        ]
        words.sort(
            key=lambda word: (
                float(word.get("start", 0)),
                float(word.get("end", 0)),
                int(word.get("speaker", 0)),
            )
        )
        return " ".join(
            str(word.get("punctuated_word", word.get("word", ""))) for word in words
        )

    @staticmethod
    def _opening_phrase(excerpt: str) -> str:
        words = excerpt.split()
        chosen = []
        for word in words[:16]:
            chosen.append(word)
            if len(chosen) >= 5 and word.rstrip().endswith((".", "?", "!")):
                break
        return " ".join(chosen)

    def _snap_to_silence(self, clips: list, segments_data: dict) -> list:
        """Compatibility helper for explicitly requested energy-based snapping."""
        tolerance = self.get_config(
            "clip_mining", "boundary_snap_tolerance_seconds", default=3.0
        )
        import numpy as np

        left_path = self.episode_dir / "work" / "left_rms_db.npy"
        right_path = self.episode_dir / "work" / "right_rms_db.npy"
        meta_path = self.episode_dir / "work" / "rms_meta.json"
        if not left_path.exists() or not right_path.exists() or not meta_path.exists():
            return clips
        try:
            left_rms = np.load(str(left_path))
            right_rms = np.load(str(right_path))
            with meta_path.open() as source:
                frame_seconds = json.load(source).get("frame_seconds", 0.1)
        except (OSError, ValueError, json.JSONDecodeError):
            return clips
        combined = left_rms + right_rms
        if not len(combined):
            return clips
        for clip in clips:
            for key in ("start_seconds", "end_seconds"):
                value = float(clip[key])
                first = max(0, int((value - tolerance) / frame_seconds))
                last = min(len(combined), int((value + tolerance) / frame_seconds))
                if first < last:
                    clip[key] = round(
                        (first + int(np.argmin(combined[first:last]))) * frame_seconds,
                        2,
                    )
        return clips

    @staticmethod
    def _get_dominant_speaker(start: float, end: float, segments: list) -> str:
        speaker_time = {}
        for segment in segments:
            overlap = min(end, segment["end"]) - max(start, segment["start"])
            if overlap > 0:
                speaker = segment["speaker"]
                speaker_time[speaker] = speaker_time.get(speaker, 0) + overlap
        return max(speaker_time, key=speaker_time.get) if speaker_time else "BOTH"
