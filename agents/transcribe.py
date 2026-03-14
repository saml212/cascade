"""Transcribe agent — Deepgram Nova-3 via REST API.

Two modes: multichannel (H6E per-speaker tracks, perfect attribution) or
mono fallback (camera audio with diarize=true).
"""

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
        work_dir = self.episode_dir / "work"
        work_dir.mkdir(exist_ok=True)

        episode_data = self.load_json_safe("episode.json")
        channel_map = None
        multichannel = False

        # Try multichannel (H6E per-speaker tracks)
        if episode_data.get("audio_tracks") and episode_data.get("crop_config", {}).get("speakers"):
            audio_path, channel_map = self._prepare_multichannel_audio(episode_data)
            multichannel = audio_path is not None
            if not multichannel:
                self.logger.warning("Multichannel prep failed, falling back to mono")

        # Fallback: extract camera audio
        if not multichannel:
            audio_path = work_dir / "audio.m4a"
            if not audio_path.exists():
                self.logger.info("Extracting audio to m4a...")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(self.episode_dir / "source_merged.mp4"),
                     "-vn", "-c:a", "aac", "-b:a", "128k", str(audio_path)],
                    capture_output=True, text=True, check=True,
                )

        audio_size_mb = audio_path.stat().st_size / 1e6
        self.logger.info(f"Audio: {audio_size_mb:.1f} MB, mode={'multichannel' if multichannel else 'mono+diarize'}")

        # Deepgram API call
        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set in environment")

        tc = self.config.get("transcription", {})
        params = {
            "model": tc.get("model", "nova-3"),
            "language": tc.get("language", "en"),
            "utterances": "true",
            "smart_format": str(tc.get("smart_format", True)).lower(),
            "punctuate": "true",
        }
        if multichannel:
            params["multichannel"] = "true"
        else:
            params["diarize"] = "true"

        # Keyterms need repeated query params — build URL manually if present
        url = DEEPGRAM_URL
        keyterms = tc.get("keyterms", [])
        if keyterms:
            from urllib.parse import urlencode
            url = f"{DEEPGRAM_URL}?{urlencode(params)}&" + "&".join(f"keyterm={k}" for k in keyterms)
            params = None

        with open(audio_path, "rb") as f:
            audio_data = f.read()

        self.logger.info("Sending to Deepgram Nova-3...")
        resp = httpx.post(
            url, params=params,
            headers={"Authorization": f"Token {api_key}",
                     "Content-Type": "audio/wav" if multichannel else "audio/mp4"},
            content=audio_data, timeout=600.0,
        )
        resp.raise_for_status()
        raw = resp.json()

        self.save_json("transcript.json", raw)

        diarized = self._build_diarized_transcript(raw, multichannel, channel_map)
        self.save_json("diarized_transcript.json", diarized)

        self._generate_srt(raw, multichannel)

        utts = diarized["utterances"]
        return {
            "transcript_path": str(self.episode_dir / "transcript.json"),
            "diarized_path": str(self.episode_dir / "diarized_transcript.json"),
            "srt_path": str(self.episode_dir / "subtitles" / "transcript.srt"),
            "utterance_count": len(utts),
            "word_count": sum(len(u.get("words", [])) for u in utts),
            "audio_size_mb": round(audio_size_mb, 1),
            "mode": "multichannel" if multichannel else "diarized",
        }

    def _prepare_multichannel_audio(self, episode_data: dict):
        """Merge H6E per-speaker tracks into N-channel WAV. Returns (path, channel_map) or (None, None)."""
        speakers = episode_data["crop_config"]["speakers"][:4]
        sync = episode_data.get("audio_sync", {})
        offset, tempo = sync.get("offset_seconds", 0), sync.get("tempo_factor", 1.0)

        # Map track_number -> dest_path (existing files only)
        track_paths = {t["track_number"]: Path(t["dest_path"])
                       for t in episode_data.get("audio_tracks", [])
                       if t.get("track_number") is not None and Path(t["dest_path"]).exists()}

        channel_map, inputs, filters, labels = [], [], [], []
        for i, spk in enumerate(speakers):
            tn = spk.get("track")
            if not tn or tn not in track_paths:
                self.logger.warning(f"Speaker {i} track {tn} not found")
                return None, None
            channel_map.append({"index": i, "label": f"Speaker {i}", "track": tn})
            inputs += (["-ss", str(offset)] if offset >= 0 else []) + ["-i", str(track_paths[tn])]

            f = f"[{i}:a]aformat=channel_layouts=mono"
            if offset < 0:
                delay_ms = int(abs(offset) * 1000)
                f += f",adelay={delay_ms}|{delay_ms}"
            if abs(tempo - 1.0) > 1e-7:
                f += f",atempo={tempo:.8f}"
            filters.append(f + f"[s{i}]")
            labels.append(f"[s{i}]")

        n = len(channel_map)
        fc = "; ".join(filters) + f"; {''.join(labels)}amerge=inputs={n}[out]"
        video_dur = sync.get("video_duration") or episode_data.get("duration_seconds")
        output = self.episode_dir / "work" / "transcript_audio.wav"

        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", fc,
               "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "48000"]
        if video_dur:
            cmd += ["-t", str(video_dur)]
        cmd.append(str(output))

        self.logger.info(f"Merging {n} speaker tracks into multichannel WAV...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            self.logger.error(f"Multichannel merge failed: {result.stderr[-500:]}")
            return None, None
        return output, channel_map

    def _build_diarized_transcript(self, raw: dict, multichannel=False, channel_map=None) -> dict:
        """Build speaker-labeled utterances. Same output schema for both modes."""
        speaker_key = "channel" if multichannel else "speaker"
        utterances = []
        for utt in raw.get("results", {}).get("utterances", []):
            spk = utt.get(speaker_key, 0)
            utterances.append({
                "speaker": spk,
                "start": utt.get("start", 0),
                "end": utt.get("end", 0),
                "text": utt.get("transcript", ""),
                "confidence": utt.get("confidence", 0),
                "words": [
                    {"word": w.get("word", w.get("punctuated_word", "")),
                     "start": w.get("start", 0), "end": w.get("end", 0),
                     "confidence": w.get("confidence", 0), "speaker": spk}
                    for w in utt.get("words", [])
                ],
            })

        result = {"mode": "multichannel" if multichannel else "diarized", "utterances": utterances}
        if channel_map:
            result["speaker_map"] = channel_map
        return result

    def _generate_srt(self, raw: dict, multichannel=False):
        """Generate SRT from word-level data across all channels."""
        srt_dir = self.episode_dir / "subtitles"
        srt_dir.mkdir(exist_ok=True)
        srt_path = srt_dir / "transcript.srt"

        words = []
        for ch_idx, ch in enumerate(raw.get("results", {}).get("channels", [])):
            for alt in ch.get("alternatives", []):
                for w in alt.get("words", []):
                    if multichannel:
                        w = {**w, "speaker": ch_idx}
                    words.append(w)

        if multichannel:
            words.sort(key=lambda w: w.get("start", 0))
        if not words:
            srt_path.write_text("")
            return

        srt_lines = []
        for idx, i in enumerate(range(0, len(words), 5), 1):
            chunk = words[i:i + 5]
            text = " ".join(w.get("punctuated_word", w.get("word", "")) for w in chunk)
            srt_lines.append(
                f"{idx}\n{fmt_timecode(chunk[0]['start'])} --> {fmt_timecode(chunk[-1]['end'])}\n{text}\n"
            )
        srt_path.write_text("\n".join(srt_lines))
        self.logger.info(f"SRT: {len(srt_lines)} blocks")
