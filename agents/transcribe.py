"""Transcribe agent — Deepgram Nova-3 transcription with diarization via REST API.

Inputs:
    - source_merged.mp4
Outputs:
    - transcript.json (raw Deepgram response)
    - diarized_transcript.json (speaker-labeled utterances with word timestamps)
    - subtitles/transcript.srt (full-episode SRT)
    - work/audio.m4a (compact audio for upload)
Dependencies:
    - ffmpeg (audio extraction), httpx (Deepgram REST API)
Config:
    - transcription.model, transcription.language, transcription.diarize
    - transcription.smart_format, transcription.utterances
Environment:
    - DEEPGRAM_API_KEY
"""

import json
import os
import subprocess
from pathlib import Path

import httpx

from agents.base import BaseAgent
from lib.srt import fmt_timecode

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


class TranscribeAgent(BaseAgent):
    name = "transcribe"

    def execute(self) -> dict:
        merged_path = self.episode_dir / "source_merged.mp4"
        work_dir = self.episode_dir / "work"
        work_dir.mkdir(exist_ok=True)

        # Extract audio to compact m4a for upload
        audio_path = work_dir / "audio.m4a"
        if not audio_path.exists():
            self.logger.info("Extracting audio to m4a...")
            cmd = [
                "ffmpeg", "-y",
                "-i", str(merged_path),
                "-vn", "-c:a", "aac", "-b:a", "128k",
                str(audio_path),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=True)

        audio_size_mb = audio_path.stat().st_size / 1e6
        self.logger.info(f"Audio file: {audio_size_mb:.1f} MB")

        # Call Deepgram REST API directly
        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set in environment")

        tc = self.config.get("transcription", {})
        params = {
            "model": tc.get("model", "nova-3"),
            "language": tc.get("language", "en"),
            "diarize": str(tc.get("diarize", True)).lower(),
            "utterances": str(tc.get("utterances", True)).lower(),
            "smart_format": str(tc.get("smart_format", True)).lower(),
            "punctuate": "true",
        }

        with open(audio_path, "rb") as f:
            audio_data = f.read()

        self.logger.info("Sending to Deepgram Nova-3 (this may take a few minutes)...")
        response = httpx.post(
            DEEPGRAM_URL,
            params=params,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/mp4",
            },
            content=audio_data,
            timeout=600.0,
        )
        response.raise_for_status()
        raw_response = response.json()

        # Save raw transcript
        self.save_json("transcript.json", raw_response)
        self.logger.info("Raw transcript saved")

        # Build diarized transcript
        diarized = self._build_diarized_transcript(raw_response)
        self.save_json("diarized_transcript.json", diarized)

        # Generate SRT subtitles
        self._generate_srt(raw_response)

        utterance_count = len(diarized.get("utterances", []))
        word_count = sum(len(u.get("words", [])) for u in diarized.get("utterances", []))

        return {
            "transcript_path": str(self.episode_dir / "transcript.json"),
            "diarized_path": str(self.episode_dir / "diarized_transcript.json"),
            "srt_path": str(self.episode_dir / "subtitles" / "transcript.srt"),
            "utterance_count": utterance_count,
            "word_count": word_count,
            "audio_size_mb": round(audio_size_mb, 1),
        }

    def _build_diarized_transcript(self, raw: dict) -> dict:
        """Extract speaker-labeled utterances with word timestamps."""
        utterances = []
        raw_utterances = raw.get("results", {}).get("utterances", [])

        for utt in raw_utterances:
            utterances.append({
                "speaker": utt.get("speaker", 0),
                "start": utt.get("start", 0),
                "end": utt.get("end", 0),
                "text": utt.get("transcript", ""),
                "confidence": utt.get("confidence", 0),
                "words": [
                    {
                        "word": w.get("word", w.get("punctuated_word", "")),
                        "start": w.get("start", 0),
                        "end": w.get("end", 0),
                        "confidence": w.get("confidence", 0),
                        "speaker": w.get("speaker", 0),
                    }
                    for w in utt.get("words", [])
                ],
            })

        return {"utterances": utterances}

    def _generate_srt(self, raw: dict):
        """Generate SRT subtitle file from transcript."""
        srt_dir = self.episode_dir / "subtitles"
        srt_dir.mkdir(exist_ok=True)
        srt_path = srt_dir / "transcript.srt"

        # Extract words from channels
        words = []
        for ch in raw.get("results", {}).get("channels", []):
            for alt in ch.get("alternatives", []):
                words.extend(alt.get("words", []))

        if not words:
            with open(srt_path, "w") as f:
                f.write("")
            return

        # Group words into ~5-word subtitle blocks
        srt_lines = []
        idx = 1
        i = 0
        while i < len(words):
            chunk = words[i : i + 5]
            start_time = chunk[0].get("start", 0)
            end_time = chunk[-1].get("end", 0)
            text = " ".join(
                w.get("punctuated_word", w.get("word", "")) for w in chunk
            )

            srt_lines.append(
                f"{idx}\n"
                f"{fmt_timecode(start_time)} --> {fmt_timecode(end_time)}\n"
                f"{text}\n"
            )
            idx += 1
            i += 5

        with open(srt_path, "w") as f:
            f.write("\n".join(srt_lines))

        self.logger.info(f"SRT generated with {idx - 1} blocks")

