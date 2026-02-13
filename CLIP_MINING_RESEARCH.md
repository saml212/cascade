# Automated Podcast Clip Mining: State-of-the-Art Research

Comprehensive, implementation-ready strategies for automatically identifying and extracting
compelling 30-90 second clips from long-form podcast/interview content.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Audio Energy-Based Scoring](#2-audio-energy-based-scoring)
3. [Transcript-Based NLP Scoring](#3-transcript-based-nlp-scoring)
4. [LLM-Based Ranking (Claude)](#4-llm-based-ranking-claude)
5. [Engagement Prediction Features](#5-engagement-prediction-features)
6. [Speaker Change Dynamics](#6-speaker-change-dynamics)
7. [Silence/Pause Detection](#7-silencepause-detection)
8. [Combined Scoring Pipeline](#8-combined-scoring-pipeline)
9. [Analytics Feedback Loop](#9-analytics-feedback-loop)
10. [Scheduling Optimization](#10-scheduling-optimization)
11. [Bayesian Optimization for Scheduling](#11-bayesian-optimization-for-scheduling)

---

## 1. Architecture Overview

The overall pipeline processes a long-form episode through parallel scoring channels, then
fuses results into a ranked list of candidate clips.

```
┌─────────────────────────────────────────────────────────────────┐
│                     INPUT: Full Episode                         │
│                  (Audio + Transcript + Metadata)                │
└───────┬──────────────┬──────────────┬──────────────┬────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
┌──────────────┐┌──────────────┐┌──────────────┐┌──────────────┐
│ Audio Energy ││ NLP Scoring  ││ LLM Ranking  ││ Speaker      │
│ Analysis     ││ (Transcript) ││ (Claude)     ││ Dynamics     │
│              ││              ││              ││              │
│ - RMS energy ││ - Sentiment  ││ - Compelling ││ - Turn rate  │
│ - Pitch var  ││ - Quotability││   moments    ││ - Overlaps   │
│ - Laughter   ││ - Topic shift││ - Hook qual  ││ - Rapid      │
│ - Silence    ││ - Q&A pairs  ││ - Resolution ││   exchange   │
└──────┬───────┘└──────┬───────┘└──────┬───────┘└──────┬───────┘
       │               │               │               │
       └───────────────┴───────┬───────┴───────────────┘
                               ▼
                    ┌─────────────────────┐
                    │  Score Fusion Layer  │
                    │  (Weighted Combine)  │
                    └──────────┬──────────┘
                               ▼
                    ┌─────────────────────┐
                    │ Clip Candidate List  │
                    │ (Ranked, Deduplicated│
                    │  with boundaries)    │
                    └──────────┬──────────┘
                               ▼
                    ┌─────────────────────┐
                    │ Analytics Feedback   │
                    │ (Learn from perf.)   │
                    └─────────────────────┘
```

### Prerequisite: Transcription Pipeline

Use WhisperX for word-level timestamps with speaker diarization:

```python
import whisperx

# Step 1: Transcribe with Whisper
device = "cuda"  # or "cpu"
model = whisperx.load_model("large-v3", device, compute_type="float16")
audio = whisperx.load_audio("episode.mp3")
result = model.transcribe(audio, batch_size=16)

# Step 2: Align for word-level timestamps
model_a, metadata = whisperx.load_align_model(language_code="en", device=device)
result = whisperx.align(
    result["segments"], model_a, metadata, audio, device,
    return_char_alignments=False
)

# Step 3: Speaker diarization
diarize_model = whisperx.DiarizationPipeline(
    use_auth_token="YOUR_HF_TOKEN", device=device
)
diarize_segments = diarize_model(audio)
result = whisperx.assign_word_speakers(diarize_segments, result)

# Result format per segment:
# {
#   "start": 45.2,
#   "end": 48.7,
#   "text": "That completely changed how I think about it.",
#   "speaker": "SPEAKER_01",
#   "words": [
#     {"word": "That", "start": 45.2, "end": 45.4, "speaker": "SPEAKER_01"},
#     ...
#   ]
# }
```

---

## 2. Audio Energy-Based Scoring

Detect high-energy moments, laughter, vocal emphasis, and excitement from the raw audio.

### 2.1 RMS Energy Detection

```python
import librosa
import numpy as np

def compute_energy_scores(audio_path, sr=22050, hop_length=512, window_sec=30):
    """
    Compute RMS energy over sliding windows to find high-energy moments.
    Returns list of (start_sec, end_sec, energy_score) tuples.
    """
    y, sr = librosa.load(audio_path, sr=sr)

    # Compute frame-level RMS energy
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]

    # Convert to dB scale for better dynamic range
    rms_db = librosa.amplitude_to_db(rms, ref=np.max)

    # Compute spectral centroid (brightness ~ excitement)
    spectral_centroid = librosa.feature.spectral_centroid(
        y=y, sr=sr, hop_length=hop_length
    )[0]

    # Compute pitch variance (emotional speech has more pitch variation)
    pitches, magnitudes = librosa.piptrack(y=y, sr=sr, hop_length=hop_length)
    pitch_per_frame = []
    for t in range(pitches.shape[1]):
        index = magnitudes[:, t].argmax()
        pitch_per_frame.append(pitches[index, t])
    pitch_per_frame = np.array(pitch_per_frame)

    # Zero-crossing rate (higher in fricatives, laughter)
    zcr = librosa.feature.zero_crossing_rate(y=y, hop_length=hop_length)[0]

    # Sliding window scoring
    frames_per_window = int(window_sec * sr / hop_length)
    scores = []

    for start_frame in range(0, len(rms) - frames_per_window, frames_per_window // 2):
        end_frame = start_frame + frames_per_window

        window_rms = rms[start_frame:end_frame]
        window_centroid = spectral_centroid[start_frame:end_frame]
        window_pitch = pitch_per_frame[start_frame:min(end_frame, len(pitch_per_frame))]
        window_zcr = zcr[start_frame:end_frame]

        # Score components (normalize each 0-1)
        energy_score = np.percentile(window_rms, 90)  # Peak energy
        energy_variance = np.std(window_rms)           # Dynamic range
        brightness = np.mean(window_centroid)           # Spectral brightness
        pitch_var = np.std(window_pitch[window_pitch > 0]) if np.any(window_pitch > 0) else 0
        zcr_mean = np.mean(window_zcr)

        start_sec = start_frame * hop_length / sr
        end_sec = end_frame * hop_length / sr

        scores.append({
            "start": start_sec,
            "end": end_sec,
            "energy": float(energy_score),
            "energy_variance": float(energy_variance),
            "brightness": float(brightness),
            "pitch_variance": float(pitch_var),
            "zcr": float(zcr_mean),
        })

    return scores
```

### 2.2 Laughter Detection

Use the dedicated laughter-detection library (github.com/jrgillick/laughter-detection):

```python
# Using the laughter-detection library
# pip install laughter-detection
# Or use the simpler heuristic approach:

def detect_laughter_heuristic(audio_path, sr=22050, hop_length=512):
    """
    Heuristic laughter detection based on audio features.
    Laughter has: high ZCR, rhythmic energy bursts, mid-range pitch.
    """
    y, sr = librosa.load(audio_path, sr=sr)

    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    zcr = librosa.feature.zero_crossing_rate(y=y, hop_length=hop_length)[0]
    mfccs = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop_length, n_mfcc=13)

    # Laughter characteristics:
    # 1. Rhythmic energy bursts (high variance in short windows)
    # 2. High zero-crossing rate
    # 3. Specific MFCC patterns

    frame_duration = hop_length / sr
    laugh_frames = []

    burst_window = int(0.5 / frame_duration)  # 0.5 second windows

    for i in range(0, len(rms) - burst_window, burst_window // 4):
        chunk_rms = rms[i:i + burst_window]
        chunk_zcr = zcr[i:i + burst_window]

        # Rhythmic bursts: high energy variance in short window
        energy_periodicity = np.std(chunk_rms) / (np.mean(chunk_rms) + 1e-10)

        # High ZCR
        zcr_level = np.mean(chunk_zcr)

        # Combined heuristic
        if energy_periodicity > 0.5 and zcr_level > 0.05:
            start_sec = i * frame_duration
            laugh_frames.append({
                "start": start_sec,
                "end": start_sec + 0.5,
                "confidence": min(1.0, energy_periodicity * zcr_level * 20)
            })

    # Merge adjacent laughter events
    return merge_adjacent_events(laugh_frames, gap_threshold=1.0)


def merge_adjacent_events(events, gap_threshold=1.0):
    """Merge events that are close together."""
    if not events:
        return []
    merged = [events[0].copy()]
    for event in events[1:]:
        if event["start"] - merged[-1]["end"] < gap_threshold:
            merged[-1]["end"] = event["end"]
            merged[-1]["confidence"] = max(merged[-1]["confidence"], event["confidence"])
        else:
            merged.append(event.copy())
    return merged
```

### 2.3 Vocal Emphasis Detection

```python
def detect_emphasis(audio_path, transcript_segments, sr=22050):
    """
    Detect moments of vocal emphasis by comparing local features
    to speaker baselines.
    """
    y, sr = librosa.load(audio_path, sr=sr)

    emphasis_scores = []

    for seg in transcript_segments:
        start_sample = int(seg["start"] * sr)
        end_sample = int(seg["end"] * sr)
        segment_audio = y[start_sample:end_sample]

        if len(segment_audio) < sr * 0.5:  # Skip very short segments
            continue

        # Features that indicate emphasis
        rms = librosa.feature.rms(y=segment_audio)[0]
        pitches, mags = librosa.piptrack(y=segment_audio, sr=sr)

        # Emphasis markers:
        # 1. Sudden energy increase (> 1.5x local average)
        # 2. Pitch jump (speaker raises voice)
        # 3. Slower speech rate (deliberate emphasis)

        peak_energy = np.percentile(rms, 95)
        mean_energy = np.mean(rms)
        energy_ratio = peak_energy / (mean_energy + 1e-10)

        # Duration per word (slower = more deliberate)
        n_words = len(seg.get("text", "").split())
        duration = seg["end"] - seg["start"]
        words_per_sec = n_words / (duration + 0.01)

        emphasis_scores.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg.get("text", ""),
            "energy_ratio": float(energy_ratio),
            "words_per_sec": float(words_per_sec),
            # Lower words/sec + higher energy ratio = more emphatic
            "emphasis_score": float(energy_ratio * (1.0 / (words_per_sec + 0.1)))
        })

    return emphasis_scores
```

---

## 3. Transcript-Based NLP Scoring

### 3.1 Quotability Scoring

```python
from transformers import pipeline
import re

# Load models
sentiment_analyzer = pipeline("sentiment-analysis", model="cardiffnlp/twitter-roberta-base-sentiment-latest")
# For sentence embeddings:
from sentence_transformers import SentenceTransformer
embed_model = SentenceTransformer("all-MiniLM-L6-v2")


def score_quotability(text):
    """
    Score a text segment for quotability based on multiple features.
    Returns a dict of feature scores.
    """
    sentences = re.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]

    features = {}

    # 1. Sentiment intensity (strong positive or negative = more quotable)
    if sentences:
        sentiments = sentiment_analyzer(sentences[:10])  # Cap for speed
        intensities = [max(s["score"] for s in [sent]) for sent in sentiments]
        features["sentiment_intensity"] = max(intensities) if intensities else 0

    # 2. Rhetorical patterns (questions, lists, contrasts)
    features["has_question"] = 1.0 if "?" in text else 0.0
    features["has_contrast"] = 1.0 if any(w in text.lower() for w in [
        "but", "however", "instead", "actually", "on the other hand",
        "the truth is", "what people don't realize"
    ]) else 0.0
    features["has_emphasis_words"] = sum(1 for w in [
        "never", "always", "everything", "nothing", "absolutely",
        "fundamentally", "literally", "completely", "the most",
        "the biggest", "the worst", "the best", "game changer"
    ] if w in text.lower()) / 5.0  # Normalize

    # 3. Specificity (concrete details are more quotable)
    # Numbers, names, specific examples
    has_numbers = len(re.findall(r'\d+', text)) > 0
    has_quotes = '"' in text or "'" in text
    features["specificity"] = (0.5 if has_numbers else 0) + (0.5 if has_quotes else 0)

    # 4. Sentence structure (shorter, punchier sentences score higher)
    if sentences:
        avg_words = np.mean([len(s.split()) for s in sentences])
        features["punchiness"] = max(0, 1.0 - (avg_words - 10) / 20)  # Peak at ~10 words
    else:
        features["punchiness"] = 0

    # 5. First-person statements (personal stories/opinions)
    features["personal"] = 1.0 if any(w in text.lower().split()[:3] for w in [
        "i", "my", "we", "our"
    ]) else 0.0

    # Combined quotability score (weighted)
    quotability = (
        features["sentiment_intensity"] * 0.2 +
        features["has_question"] * 0.1 +
        features["has_contrast"] * 0.15 +
        features["has_emphasis_words"] * 0.15 +
        features["specificity"] * 0.1 +
        features["punchiness"] * 0.15 +
        features["personal"] * 0.15
    )

    features["quotability_score"] = quotability
    return features
```

### 3.2 Topic Shift Detection

```python
from sentence_transformers import SentenceTransformer
import numpy as np

embed_model = SentenceTransformer("all-MiniLM-L6-v2")


def detect_topic_shifts(segments, window_size=5, shift_threshold=0.4):
    """
    Detect topic shifts by measuring cosine similarity between
    adjacent windows of transcript segments.

    Topic shifts often mark the start of new, self-contained discussions
    that make good clip boundaries.
    """
    texts = [seg["text"] for seg in segments]
    embeddings = embed_model.encode(texts)

    shifts = []

    for i in range(window_size, len(embeddings) - window_size):
        # Average embedding of preceding window
        before = np.mean(embeddings[i - window_size:i], axis=0)
        # Average embedding of following window
        after = np.mean(embeddings[i:i + window_size], axis=0)

        # Cosine similarity
        similarity = np.dot(before, after) / (
            np.linalg.norm(before) * np.linalg.norm(after) + 1e-10
        )

        # Low similarity = topic shift
        shift_score = 1.0 - similarity

        if shift_score > shift_threshold:
            shifts.append({
                "segment_index": i,
                "timestamp": segments[i]["start"],
                "shift_score": float(shift_score),
                "text_before": texts[max(0, i-1)],
                "text_after": texts[i],
            })

    return shifts


def detect_question_answer_pairs(segments):
    """
    Find Q&A pairs where a question is followed by a substantive answer.
    These are natural clip candidates.
    """
    qa_pairs = []

    for i in range(len(segments) - 1):
        text = segments[i]["text"].strip()

        # Detect questions
        is_question = (
            text.endswith("?") or
            text.lower().startswith(("what", "why", "how", "when", "where", "who",
                                      "do you", "can you", "would you", "is it",
                                      "tell me", "explain"))
        )

        if is_question:
            # Find the answer: collect subsequent segments from different speaker
            answer_segments = []
            answer_speaker = None

            for j in range(i + 1, min(i + 20, len(segments))):
                seg = segments[j]

                if answer_speaker is None:
                    # First response segment
                    if seg.get("speaker") != segments[i].get("speaker"):
                        answer_speaker = seg.get("speaker")
                        answer_segments.append(seg)
                elif seg.get("speaker") == answer_speaker:
                    answer_segments.append(seg)
                elif seg.get("speaker") == segments[i].get("speaker"):
                    # Questioner speaks again -- end of answer
                    break

            if answer_segments:
                answer_text = " ".join(s["text"] for s in answer_segments)
                answer_duration = answer_segments[-1]["end"] - answer_segments[0]["start"]

                # Score: longer, more substantive answers are better clips
                word_count = len(answer_text.split())

                qa_pairs.append({
                    "question_start": segments[i]["start"],
                    "question_end": segments[i]["end"],
                    "question_text": text,
                    "answer_start": answer_segments[0]["start"],
                    "answer_end": answer_segments[-1]["end"],
                    "answer_text": answer_text,
                    "answer_duration": answer_duration,
                    "answer_word_count": word_count,
                    # Good Q&A clips are 20-90 seconds with substantive answers
                    "qa_score": min(1.0, word_count / 100) * (
                        1.0 if 20 <= answer_duration <= 90 else 0.5
                    )
                })

    return qa_pairs
```

### 3.3 Semantic Clustering for Segment Coherence

```python
from sklearn.cluster import AgglomerativeClustering


def find_coherent_segments(segments, min_duration=20, max_duration=90):
    """
    Use hierarchical clustering on sentence embeddings to find
    self-contained topical segments that work as standalone clips.
    """
    texts = [seg["text"] for seg in segments]
    embeddings = embed_model.encode(texts)

    # Agglomerative clustering with distance threshold
    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=0.8,
        metric="cosine",
        linkage="average"
    )
    labels = clustering.fit_predict(embeddings)

    # Group consecutive segments by cluster
    coherent_blocks = []
    current_label = labels[0]
    block_start = 0

    for i in range(1, len(labels)):
        if labels[i] != current_label:
            block_segments = segments[block_start:i]
            duration = block_segments[-1]["end"] - block_segments[0]["start"]

            if min_duration <= duration <= max_duration:
                block_text = " ".join(s["text"] for s in block_segments)

                # Score coherence: how similar are all segments to each other?
                block_embeddings = embeddings[block_start:i]
                mean_emb = np.mean(block_embeddings, axis=0)
                coherence = np.mean([
                    np.dot(e, mean_emb) / (np.linalg.norm(e) * np.linalg.norm(mean_emb))
                    for e in block_embeddings
                ])

                coherent_blocks.append({
                    "start": block_segments[0]["start"],
                    "end": block_segments[-1]["end"],
                    "duration": duration,
                    "text": block_text,
                    "coherence_score": float(coherence),
                    "n_segments": len(block_segments),
                })

            current_label = labels[i]
            block_start = i

    return sorted(coherent_blocks, key=lambda x: x["coherence_score"], reverse=True)
```

---

## 4. LLM-Based Ranking (Claude)

This is the highest-signal scoring channel. It uses Claude to read the transcript and
identify the most compelling moments with reasoning.

### 4.1 Transcript Formatting for LLM Consumption

The format matters. Here is the optimal structure:

```python
def format_transcript_for_llm(segments, episode_title="", episode_description=""):
    """
    Format transcript with timestamps and speaker labels for LLM consumption.

    Key principles:
    - Include timestamps every ~30 seconds (not every word -- too noisy)
    - Use speaker names/labels consistently
    - Include episode context for relevance scoring
    - Keep total token count manageable (split into chunks if needed)
    """
    formatted_lines = []

    # Episode context header
    if episode_title:
        formatted_lines.append(f"EPISODE: {episode_title}")
    if episode_description:
        formatted_lines.append(f"DESCRIPTION: {episode_description}")
    formatted_lines.append("")
    formatted_lines.append("TRANSCRIPT:")
    formatted_lines.append("(Format: [MM:SS] SPEAKER: text)")
    formatted_lines.append("")

    current_speaker = None
    current_block = []
    block_start_time = 0

    for seg in segments:
        speaker = seg.get("speaker", "UNKNOWN")

        # Start new block on speaker change or every 30 seconds
        if (speaker != current_speaker or
            seg["start"] - block_start_time > 30):

            if current_block:
                timestamp = format_timestamp(block_start_time)
                text = " ".join(current_block)
                formatted_lines.append(f"[{timestamp}] {current_speaker}: {text}")

            current_speaker = speaker
            current_block = [seg["text"]]
            block_start_time = seg["start"]
        else:
            current_block.append(seg["text"])

    # Flush last block
    if current_block:
        timestamp = format_timestamp(block_start_time)
        text = " ".join(current_block)
        formatted_lines.append(f"[{timestamp}] {current_speaker}: {text}")

    return "\n".join(formatted_lines)


def format_timestamp(seconds):
    """Convert seconds to MM:SS format."""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"
```

**Example output:**

```
EPISODE: The Future of AI with Dr. Jane Smith
DESCRIPTION: Dr. Smith discusses breakthrough research in language models...

TRANSCRIPT:
(Format: [MM:SS] SPEAKER: text)

[00:00] HOST: Welcome back to the show. Today I'm thrilled to have Dr. Jane Smith...
[00:15] HOST: Before we dive in, can you tell us about your background?
[00:22] GUEST: Sure. So I spent about fifteen years in academic research, mostly at MIT...
[00:48] GUEST: ...and that's when everything changed. We realized the scaling laws were
  fundamentally different from what anyone had predicted.
[01:05] HOST: Wait, what do you mean by that? Can you give us a specific example?
[01:12] GUEST: Absolutely. So in 2022, we ran this experiment...
```

### 4.2 The Clip Identification Prompt

This is the core prompt structure. It uses a multi-stage approach for best results.

```python
CLIP_IDENTIFICATION_SYSTEM_PROMPT = """You are an expert content editor who specializes in
identifying the most compelling, shareable moments from long-form podcast conversations.

Your task is to identify segments that would work as standalone short-form clips
(30-90 seconds) for platforms like YouTube Shorts, TikTok, and Instagram Reels.

A great clip has these qualities:
1. HOOK: Starts with something that immediately grabs attention within the first 3 seconds
   - A surprising statement, bold claim, or provocative question
   - NOT a slow buildup or context-setting
2. SELF-CONTAINED: Makes sense without watching the full episode
   - The viewer should understand the context without prior knowledge
3. EMOTIONAL ARC: Has a beginning, middle, and resolution within the clip
   - Sets up tension/curiosity, develops it, and resolves it
4. VALUE: Delivers a specific insight, story, or moment worth sharing
   - Practical advice, counterintuitive wisdom, funny moment, emotional story
5. SHAREABILITY: Would make someone want to send it to a friend
   - "You HAVE to hear what this person said about X"

Bad clips:
- Start with "So..." or "Um..." or "Well, you know..."
- Require context from earlier in the conversation
- Are just generic advice without specificity
- End mid-thought or without resolution
- Are too abstract or academic"""


CLIP_IDENTIFICATION_USER_PROMPT = """Analyze this podcast transcript and identify the
{n_clips} most compelling moments that would work as standalone short-form clips
(30-90 seconds each).

{transcript}

For each clip, provide:
1. The exact start and end timestamps
2. A catchy title (max 60 characters) that would work as a video title
3. The hook -- the first sentence that grabs attention
4. Why this moment is compelling (1-2 sentences)
5. A virality score from 1-10 with brief justification
6. Which platform(s) it would work best on (YouTube Shorts / TikTok / Instagram Reels)
7. Any content warnings or sensitivities

IMPORTANT: Prefer clips that START with a strong hook. If a great moment has a weak
opening, suggest trimming to where the hook begins. The first 3 seconds determine
whether someone keeps watching.

Rank clips from most to least compelling."""
```

### 4.3 Structured Output with Claude API

```python
import anthropic
from pydantic import BaseModel
from typing import List, Optional


class ClipCandidate(BaseModel):
    start_timestamp: str       # "MM:SS" format
    end_timestamp: str         # "MM:SS" format
    start_seconds: float       # Precise start in seconds
    end_seconds: float         # Precise end in seconds
    duration_seconds: float    # Clip duration
    title: str                 # Catchy title for the clip (max 60 chars)
    hook_text: str             # The opening line that grabs attention
    compelling_reason: str     # Why this moment works as a clip
    virality_score: int        # 1-10 score
    virality_justification: str
    best_platforms: List[str]  # ["youtube_shorts", "tiktok", "instagram_reels"]
    content_warnings: Optional[List[str]]
    speaker_names: List[str]   # Who speaks in this clip
    topic_tags: List[str]      # For categorization


class ClipAnalysis(BaseModel):
    episode_title: str
    total_duration_minutes: float
    clips: List[ClipCandidate]
    overall_episode_quality: int  # 1-10
    episode_themes: List[str]


def identify_clips_with_claude(
    transcript: str,
    episode_title: str = "",
    episode_description: str = "",
    n_clips: int = 10,
    model: str = "claude-sonnet-4-5-20250514"
):
    """
    Use Claude to identify the best clip candidates from a transcript.
    Uses structured outputs for guaranteed JSON schema compliance.
    """
    client = anthropic.Anthropic()

    formatted_transcript = format_transcript_for_llm(
        transcript, episode_title, episode_description
    )

    user_message = CLIP_IDENTIFICATION_USER_PROMPT.format(
        n_clips=n_clips,
        transcript=formatted_transcript
    )

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=CLIP_IDENTIFICATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "episode_title": {"type": "string"},
                        "total_duration_minutes": {"type": "number"},
                        "overall_episode_quality": {"type": "integer"},
                        "episode_themes": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "clips": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "start_timestamp": {"type": "string"},
                                    "end_timestamp": {"type": "string"},
                                    "start_seconds": {"type": "number"},
                                    "end_seconds": {"type": "number"},
                                    "duration_seconds": {"type": "number"},
                                    "title": {"type": "string"},
                                    "hook_text": {"type": "string"},
                                    "compelling_reason": {"type": "string"},
                                    "virality_score": {"type": "integer"},
                                    "virality_justification": {"type": "string"},
                                    "best_platforms": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "content_warnings": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "speaker_names": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "topic_tags": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    }
                                },
                                "required": [
                                    "start_timestamp", "end_timestamp",
                                    "start_seconds", "end_seconds",
                                    "duration_seconds", "title", "hook_text",
                                    "compelling_reason", "virality_score",
                                    "virality_justification", "best_platforms",
                                    "speaker_names", "topic_tags"
                                ],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": [
                        "episode_title", "total_duration_minutes",
                        "overall_episode_quality", "episode_themes", "clips"
                    ],
                    "additionalProperties": False
                }
            }
        }
    )

    import json
    return json.loads(response.content[0].text)
```

### 4.4 Handling Long Transcripts (Chunking Strategy)

For episodes longer than ~45 minutes, the transcript may exceed context limits.
Use a two-pass approach:

```python
def identify_clips_long_episode(segments, max_chunk_minutes=30, overlap_minutes=2):
    """
    Two-pass approach for long episodes:
    Pass 1: Process overlapping chunks, get candidates from each
    Pass 2: De-duplicate and re-rank all candidates together
    """
    total_duration = segments[-1]["end"]
    chunk_duration = max_chunk_minutes * 60
    overlap = overlap_minutes * 60

    all_candidates = []

    # Pass 1: Chunk and identify
    start = 0
    while start < total_duration:
        end = min(start + chunk_duration, total_duration)

        chunk_segments = [
            s for s in segments
            if s["start"] >= start and s["end"] <= end
        ]

        if chunk_segments:
            chunk_transcript = format_transcript_for_llm(chunk_segments)
            candidates = identify_clips_with_claude(
                chunk_transcript,
                n_clips=5  # Fewer per chunk
            )
            all_candidates.extend(candidates.get("clips", []))

        start += chunk_duration - overlap

    # Pass 2: De-duplicate overlapping candidates
    deduplicated = deduplicate_clips(all_candidates, overlap_threshold=10)

    # Pass 3: Re-rank with Claude using just the candidates
    return rerank_candidates(deduplicated, segments)


def deduplicate_clips(candidates, overlap_threshold=10):
    """Remove clips that overlap significantly, keeping higher-scored ones."""
    sorted_candidates = sorted(candidates, key=lambda x: x["virality_score"], reverse=True)
    kept = []

    for candidate in sorted_candidates:
        is_duplicate = False
        for existing in kept:
            overlap = min(candidate["end_seconds"], existing["end_seconds"]) - \
                      max(candidate["start_seconds"], existing["start_seconds"])
            if overlap > overlap_threshold:
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append(candidate)

    return kept
```

### 4.5 Alternative Prompt: Iterative Refinement

For higher quality, use a two-step prompt:

```python
STEP1_PROMPT = """Read this podcast transcript carefully. List every moment that made
you think "that's interesting" or "I'd want to share that." Be generous -- list 20-30
moments with their timestamps and a one-line description of why they caught your
attention. Include moments that are: surprising, funny, emotional, insightful,
controversial, or contain a great story.

{transcript}"""

STEP2_PROMPT = """Here are the interesting moments you identified:

{moments_list}

Now narrow this down to the {n_clips} BEST moments that would work as standalone
30-90 second clips for social media. For each, evaluate:

1. Does it have a strong opening hook (first 3 seconds)?
2. Does it make sense without context from the rest of the episode?
3. Does it have a complete arc (setup -> development -> payoff)?
4. Would someone share this with a friend?

For each selected clip, provide the structured output with timestamps, title, hook,
and virality score."""
```

---

## 5. Engagement Prediction Features

Based on research into what makes short-form content go viral.

### 5.1 Feature Definitions

```python
from dataclasses import dataclass
from typing import List

@dataclass
class EngagementFeatures:
    """Features that correlate with viral short-form content performance."""

    # Hook Quality (first 3 seconds) -- most important predictor
    hook_type: str           # "question", "bold_claim", "story_start", "surprise", "conflict"
    hook_specificity: float  # 0-1: how specific vs generic the opener is
    hook_pattern_match: bool # Matches known viral hook patterns

    # Emotional Arc
    sentiment_range: float    # Max - min sentiment across clip (wider = more engaging)
    sentiment_trajectory: str # "positive_build", "negative_to_positive", "tension_release"
    emotional_peak_position: float  # Where in the clip (0-1) the peak occurs (ideal: 0.6-0.8)

    # Content Value
    novelty_score: float      # How unique/surprising is the insight?
    actionability: float      # Can the viewer DO something with this info?
    relatability: float       # Would most people connect with this?
    controversy_level: float  # Mild controversy drives engagement (sweet spot: 0.3-0.6)

    # Resolution
    has_punchline: bool       # Ends with a clear conclusion or punchline
    has_callback: bool        # References something set up earlier in the clip
    ends_clean: bool          # Ends at a natural sentence boundary

    # Technical
    duration_seconds: float   # Ideal: 45-75 seconds for most platforms
    speech_clarity: float     # 0-1: how clearly are speakers speaking?
    background_noise: float   # 0-1: how much noise? (lower = better)
    n_speakers: int           # 1-2 speakers is ideal for short clips


def compute_engagement_prediction(clip_features: EngagementFeatures) -> float:
    """
    Predict engagement score based on feature weights learned from
    historical performance data. These weights are starting points;
    the analytics feedback loop will optimize them.
    """
    score = 0.0

    # Hook quality: 30% of total score
    hook_weights = {
        "question": 0.7, "bold_claim": 0.9, "story_start": 0.8,
        "surprise": 1.0, "conflict": 0.85
    }
    hook_score = hook_weights.get(clip_features.hook_type, 0.5)
    hook_score *= clip_features.hook_specificity
    score += hook_score * 0.30

    # Emotional arc: 25% of total score
    arc_score = clip_features.sentiment_range * 0.4
    if clip_features.sentiment_trajectory == "negative_to_positive":
        arc_score += 0.3  # Redemption arcs perform well
    elif clip_features.sentiment_trajectory == "tension_release":
        arc_score += 0.25
    # Emotional peak in the right spot
    peak_pos = clip_features.emotional_peak_position
    arc_score += 0.3 * (1.0 - abs(peak_pos - 0.7))  # Ideal peak at 70%
    score += min(1.0, arc_score) * 0.25

    # Content value: 25% of total score
    value_score = (
        clip_features.novelty_score * 0.35 +
        clip_features.actionability * 0.25 +
        clip_features.relatability * 0.25 +
        # Inverted U for controversy: sweet spot around 0.4
        (1.0 - abs(clip_features.controversy_level - 0.4) * 2) * 0.15
    )
    score += value_score * 0.25

    # Resolution: 15% of total score
    resolution_score = (
        (0.4 if clip_features.has_punchline else 0.0) +
        (0.2 if clip_features.has_callback else 0.0) +
        (0.4 if clip_features.ends_clean else 0.0)
    )
    score += resolution_score * 0.15

    # Duration penalty: 5% of total score
    duration = clip_features.duration_seconds
    if 45 <= duration <= 75:
        duration_score = 1.0
    elif 30 <= duration <= 90:
        duration_score = 0.7
    else:
        duration_score = 0.3
    score += duration_score * 0.05

    return score
```

### 5.2 Known Viral Hook Patterns

```python
VIRAL_HOOK_PATTERNS = [
    # Pattern: "The X that nobody talks about"
    {"regex": r"(?:nobody|no one|people don't)\s+(?:talks?|knows?|realizes?|understands?)",
     "type": "hidden_knowledge", "weight": 0.9},

    # Pattern: Specific number + surprising claim
    {"regex": r"\d+\s+(?:percent|%|years?|times?|people|million|billion)",
     "type": "data_surprise", "weight": 0.85},

    # Pattern: "I was wrong about X" / admission
    {"regex": r"(?:I was wrong|I used to think|I made the mistake|biggest mistake)",
     "type": "vulnerability", "weight": 0.9},

    # Pattern: Direct contradiction of common belief
    {"regex": r"(?:actually|contrary to|opposite of|myth that|wrong about)",
     "type": "contrarian", "weight": 0.95},

    # Pattern: "Here's what happened" (story hook)
    {"regex": r"(?:here's what happened|so there I was|picture this|imagine)",
     "type": "story_hook", "weight": 0.85},

    # Pattern: Bold superlative claim
    {"regex": r"(?:the (?:most|best|worst|biggest|single)|never in my life|"
              r"changed everything|game.?changer)",
     "type": "superlative", "weight": 0.8},

    # Pattern: Direct address / question to viewer
    {"regex": r"(?:have you ever|did you know|let me ask you|think about this)",
     "type": "direct_address", "weight": 0.75},
]

def score_hook_pattern(text):
    """Score the opening text against known viral hook patterns."""
    import re
    best_match = {"type": "none", "weight": 0.0}

    # Only check first 2 sentences
    first_sentences = ". ".join(text.split(".")[:2])

    for pattern in VIRAL_HOOK_PATTERNS:
        if re.search(pattern["regex"], first_sentences, re.IGNORECASE):
            if pattern["weight"] > best_match["weight"]:
                best_match = pattern

    return best_match
```

---

## 6. Speaker Change Dynamics

Rapid back-and-forth exchanges often signal engaging, high-energy content.

### 6.1 Turn Rate Analysis

```python
def analyze_speaker_dynamics(segments, window_sec=60, step_sec=15):
    """
    Analyze speaker change patterns to find high-energy conversational moments.

    High turn rates indicate:
    - Heated discussion / debate
    - Rapid-fire Q&A
    - Excited agreement / building on ideas
    - Humor / banter

    Returns windows scored by speaker dynamics.
    """
    results = []
    total_duration = segments[-1]["end"]

    for window_start in np.arange(0, total_duration - window_sec, step_sec):
        window_end = window_start + window_sec

        # Get segments in this window
        window_segs = [
            s for s in segments
            if s["start"] >= window_start and s["end"] <= window_end
        ]

        if len(window_segs) < 2:
            continue

        # Count speaker changes
        speaker_changes = 0
        speakers_in_window = set()
        turn_durations = []
        current_turn_start = window_segs[0]["start"]

        for i in range(1, len(window_segs)):
            speakers_in_window.add(window_segs[i].get("speaker", "unknown"))

            if window_segs[i].get("speaker") != window_segs[i-1].get("speaker"):
                speaker_changes += 1
                turn_durations.append(window_segs[i]["start"] - current_turn_start)
                current_turn_start = window_segs[i]["start"]

        # Add final turn
        turn_durations.append(window_end - current_turn_start)

        # Metrics
        turns_per_minute = speaker_changes / (window_sec / 60)
        avg_turn_duration = np.mean(turn_durations) if turn_durations else window_sec
        turn_duration_variance = np.std(turn_durations) if len(turn_durations) > 1 else 0

        # Score: high turns/min + short avg turn + multiple speakers = engaging
        dynamics_score = (
            min(1.0, turns_per_minute / 15) * 0.4 +       # Normalize to ~15 turns/min max
            max(0, 1.0 - avg_turn_duration / 30) * 0.3 +   # Shorter turns = higher score
            min(1.0, len(speakers_in_window) / 3) * 0.15 + # More speakers = more dynamic
            min(1.0, turn_duration_variance / 10) * 0.15    # Varied turn lengths = more natural
        )

        results.append({
            "start": float(window_start),
            "end": float(window_end),
            "speaker_changes": speaker_changes,
            "turns_per_minute": float(turns_per_minute),
            "avg_turn_duration": float(avg_turn_duration),
            "n_speakers": len(speakers_in_window),
            "dynamics_score": float(dynamics_score),
        })

    return sorted(results, key=lambda x: x["dynamics_score"], reverse=True)
```

### 6.2 Interruption and Overlap Detection

```python
def detect_interruptions(segments, overlap_threshold=0.3):
    """
    Detect interruptions where a new speaker starts before the previous
    speaker finishes. Interruptions often signal exciting moments.
    """
    interruptions = []

    for i in range(1, len(segments)):
        prev = segments[i - 1]
        curr = segments[i]

        if prev.get("speaker") != curr.get("speaker"):
            overlap = prev["end"] - curr["start"]

            if overlap > overlap_threshold:
                interruptions.append({
                    "timestamp": curr["start"],
                    "interrupted_speaker": prev.get("speaker"),
                    "interrupting_speaker": curr.get("speaker"),
                    "overlap_duration": float(overlap),
                    "interrupted_text": prev["text"],
                    "interrupting_text": curr["text"],
                })

    return interruptions
```

---

## 7. Silence/Pause Detection

Silences serve as natural segment boundaries and can also indicate dramatic moments.

### 7.1 Silence Detection with Librosa

```python
def detect_silences(audio_path, sr=22050, top_db=35, min_silence_sec=0.5):
    """
    Detect silent regions using librosa.effects.split().

    Returns:
    - silence_regions: list of (start, end, duration) for each silence
    - speech_regions: list of (start, end) for non-silent regions
    """
    y, sr = librosa.load(audio_path, sr=sr)

    # Split on silence: returns array of [start_sample, end_sample] for non-silent regions
    non_silent_intervals = librosa.effects.split(y, top_db=top_db)

    silences = []
    speech_regions = []

    for i, (start, end) in enumerate(non_silent_intervals):
        start_sec = start / sr
        end_sec = end / sr
        speech_regions.append({"start": start_sec, "end": end_sec})

        # Gap between this speech region and the next
        if i < len(non_silent_intervals) - 1:
            silence_start = end / sr
            silence_end = non_silent_intervals[i + 1][0] / sr
            silence_duration = silence_end - silence_start

            if silence_duration >= min_silence_sec:
                silences.append({
                    "start": silence_start,
                    "end": silence_end,
                    "duration": silence_duration,
                    # Classify the silence
                    "type": classify_silence(silence_duration),
                })

    return silences, speech_regions


def classify_silence(duration):
    """Classify silence by duration and likely meaning."""
    if duration < 1.0:
        return "breath_pause"      # Natural speech pause
    elif duration < 2.0:
        return "thought_pause"     # Speaker thinking / for emphasis
    elif duration < 4.0:
        return "dramatic_pause"    # Deliberate dramatic effect
    elif duration < 8.0:
        return "topic_boundary"    # Likely topic transition
    else:
        return "segment_break"     # Major section break
```

### 7.2 Using Silences as Clip Boundaries

```python
def find_clip_boundaries(silences, target_duration=60, tolerance=15):
    """
    Use silence regions to find natural clip boundaries.
    Clips should start and end at silence boundaries for clean cuts.
    """
    boundaries = []

    # Use topic_boundary and dramatic_pause silences as potential boundaries
    boundary_silences = [
        s for s in silences
        if s["type"] in ("topic_boundary", "segment_break", "dramatic_pause")
    ]

    # Find pairs of boundaries that create clips of target duration
    for i, start_silence in enumerate(boundary_silences):
        for end_silence in boundary_silences[i + 1:]:
            duration = end_silence["start"] - start_silence["end"]

            if target_duration - tolerance <= duration <= target_duration + tolerance:
                boundaries.append({
                    "start": start_silence["end"],   # Start after the silence
                    "end": end_silence["start"],     # End before the silence
                    "duration": duration,
                    "start_silence_type": start_silence["type"],
                    "end_silence_type": end_silence["type"],
                })

    return boundaries
```

---

## 8. Combined Scoring Pipeline

Fuse all signals into a single ranked list.

### 8.1 Score Fusion

```python
from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class ClipScore:
    start: float
    end: float

    # Individual channel scores (0-1 normalized)
    audio_energy_score: float = 0.0
    laughter_score: float = 0.0
    emphasis_score: float = 0.0
    quotability_score: float = 0.0
    topic_coherence_score: float = 0.0
    qa_pair_score: float = 0.0
    llm_virality_score: float = 0.0
    speaker_dynamics_score: float = 0.0
    engagement_prediction_score: float = 0.0
    boundary_quality_score: float = 0.0

    # Metadata
    title: str = ""
    hook_text: str = ""
    text_preview: str = ""


def compute_fused_score(clip: ClipScore, weights: Optional[Dict[str, float]] = None) -> float:
    """
    Compute weighted fusion of all scoring channels.

    Default weights prioritize LLM scoring (most accurate) and engagement
    prediction (most correlated with actual performance).

    Weights should be tuned over time using the analytics feedback loop.
    """
    if weights is None:
        weights = {
            "llm_virality":        0.30,  # LLM judgment is the strongest signal
            "engagement_pred":     0.20,  # Engagement prediction model
            "quotability":         0.12,  # NLP-based quotability
            "audio_energy":        0.08,  # Audio energy peaks
            "speaker_dynamics":    0.08,  # Conversational energy
            "topic_coherence":     0.07,  # Self-contained topic
            "emphasis":            0.05,  # Vocal emphasis
            "laughter":            0.04,  # Laughter detection
            "qa_pair":             0.03,  # Q&A structure
            "boundary_quality":    0.03,  # Clean start/end boundaries
        }

    fused = (
        clip.llm_virality_score       * weights["llm_virality"] +
        clip.engagement_prediction_score * weights["engagement_pred"] +
        clip.quotability_score        * weights["quotability"] +
        clip.audio_energy_score       * weights["audio_energy"] +
        clip.speaker_dynamics_score   * weights["speaker_dynamics"] +
        clip.topic_coherence_score    * weights["topic_coherence"] +
        clip.emphasis_score           * weights["emphasis"] +
        clip.laughter_score           * weights["laughter"] +
        clip.qa_pair_score            * weights["qa_pair"] +
        clip.boundary_quality_score   * weights["boundary_quality"]
    )

    return fused


def run_full_pipeline(audio_path, segments, episode_title="", episode_description=""):
    """
    Run the complete clip mining pipeline.
    Returns a ranked list of clip candidates with fused scores.
    """
    import librosa

    # 1. Audio analysis
    energy_scores = compute_energy_scores(audio_path)
    laughter_events = detect_laughter_heuristic(audio_path)
    emphasis_scores = detect_emphasis(audio_path, segments)
    silences, speech_regions = detect_silences(audio_path)

    # 2. NLP analysis
    # Score each segment for quotability
    for seg in segments:
        seg["quotability"] = score_quotability(seg["text"])

    topic_shifts = detect_topic_shifts(segments)
    qa_pairs = detect_question_answer_pairs(segments)
    coherent_blocks = find_coherent_segments(segments)

    # 3. Speaker dynamics
    dynamics = analyze_speaker_dynamics(segments)
    interruptions = detect_interruptions(segments)

    # 4. LLM ranking (the most expensive but highest signal step)
    transcript_text = format_transcript_for_llm(
        segments, episode_title, episode_description
    )
    llm_clips = identify_clips_with_claude(
        transcript_text, episode_title, episode_description
    )

    # 5. Find natural boundaries
    clip_boundaries = find_clip_boundaries(silences)

    # 6. Fuse scores
    # Start with LLM-identified clips as the primary candidates
    candidates = []
    for llm_clip in llm_clips.get("clips", []):
        clip = ClipScore(
            start=llm_clip["start_seconds"],
            end=llm_clip["end_seconds"],
            title=llm_clip["title"],
            hook_text=llm_clip["hook_text"],
            llm_virality_score=llm_clip["virality_score"] / 10.0,
        )

        # Overlay audio scores for this time range
        clip.audio_energy_score = get_score_for_range(
            energy_scores, clip.start, clip.end, "energy"
        )
        clip.laughter_score = get_laughter_score_for_range(
            laughter_events, clip.start, clip.end
        )
        clip.speaker_dynamics_score = get_score_for_range(
            dynamics, clip.start, clip.end, "dynamics_score"
        )

        # NLP scores
        clip_segments = [s for s in segments if s["start"] >= clip.start and s["end"] <= clip.end]
        if clip_segments:
            clip.quotability_score = np.mean([
                s.get("quotability", {}).get("quotability_score", 0) for s in clip_segments
            ])

        # Boundary quality
        clip.boundary_quality_score = score_boundary_quality(
            clip.start, clip.end, silences
        )

        # Compute fused score
        clip.fused_score = compute_fused_score(clip)
        candidates.append(clip)

    # Sort by fused score
    candidates.sort(key=lambda c: c.fused_score, reverse=True)

    return candidates


# Helper functions for score fusion
def get_score_for_range(scored_windows, start, end, score_key):
    """Get the max score from scored windows overlapping with the given range."""
    overlapping = [
        w[score_key] for w in scored_windows
        if w["start"] < end and w["end"] > start
    ]
    return max(overlapping) if overlapping else 0.0

def get_laughter_score_for_range(laughter_events, start, end):
    """Score based on laughter events in the range."""
    laughs = [e for e in laughter_events if start <= e["start"] <= end]
    if not laughs:
        return 0.0
    return min(1.0, sum(e["confidence"] for e in laughs) / 3.0)

def score_boundary_quality(start, end, silences):
    """Score how cleanly the clip starts and ends relative to silences."""
    start_quality = 0.0
    end_quality = 0.0
    for s in silences:
        # How close is clip start to a silence boundary?
        if abs(s["end"] - start) < 0.5:
            start_quality = 1.0
        elif abs(s["end"] - start) < 1.5:
            start_quality = max(start_quality, 0.7)
        # How close is clip end to a silence start?
        if abs(s["start"] - end) < 0.5:
            end_quality = 1.0
        elif abs(s["start"] - end) < 1.5:
            end_quality = max(end_quality, 0.7)
    return (start_quality + end_quality) / 2.0
```

---

## 9. Analytics Feedback Loop

Use real performance data to improve clip selection over time.

### 9.1 YouTube Analytics API Integration

```python
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
import json
from datetime import datetime, timedelta

SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def get_youtube_analytics_service():
    """Authenticate and return YouTube Analytics API service."""
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secrets.json", SCOPES
    )
    credentials = flow.run_local_server(port=8080)

    analytics = build("youtubeAnalytics", "v2", credentials=credentials)
    youtube = build("youtube", "v3", credentials=credentials)

    return analytics, youtube


def get_video_performance(analytics, youtube, video_id, days_back=30):
    """
    Get comprehensive performance metrics for a specific video.
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Basic metrics
    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                "likes,dislikes,comments,shares,subscribersGained",
        filters=f"video=={video_id}",
    ).execute()

    metrics = {}
    if response.get("rows"):
        row = response["rows"][0]
        headers = [h["name"] for h in response["columnHeaders"]]
        metrics = dict(zip(headers, row))

    # Audience retention curve
    retention = analytics.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="audienceWatchRatio,relativeRetentionPerformance",
        dimensions="elapsedVideoTimeRatio",
        filters=f"video=={video_id}",
        sort="elapsedVideoTimeRatio",
    ).execute()

    retention_curve = []
    if retention.get("rows"):
        for row in retention["rows"]:
            retention_curve.append({
                "elapsed_ratio": row[0],
                "audience_watch_ratio": row[1],
                "relative_retention": row[2],
            })

    # Traffic sources
    traffic = analytics.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        filters=f"video=={video_id}",
        sort="-views",
    ).execute()

    traffic_sources = {}
    if traffic.get("rows"):
        for row in traffic["rows"]:
            traffic_sources[row[0]] = {"views": row[1], "watch_time": row[2]}

    return {
        "video_id": video_id,
        "metrics": metrics,
        "retention_curve": retention_curve,
        "traffic_sources": traffic_sources,
    }


def get_shorts_performance_batch(analytics, youtube, channel_id, days_back=30):
    """
    Get performance for all Shorts (videos under 60 seconds) in the channel.
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Get all videos from channel
    videos = []
    request = youtube.search().list(
        part="snippet",
        channelId=channel_id,
        type="video",
        order="date",
        maxResults=50,
    )
    response = request.execute()

    for item in response.get("items", []):
        video_id = item["id"]["videoId"]

        # Check if it's a Short (under 60 seconds)
        video_details = youtube.videos().list(
            part="contentDetails,statistics",
            id=video_id
        ).execute()

        if video_details.get("items"):
            details = video_details["items"][0]
            duration = details["contentDetails"]["duration"]  # ISO 8601

            # Parse duration and check if < 60 seconds
            # (simplified -- use isodate library for proper parsing)
            if "M" not in duration or duration.startswith("PT0M"):
                perf = get_video_performance(analytics, youtube, video_id, days_back)
                perf["title"] = item["snippet"]["title"]
                perf["published_at"] = item["snippet"]["publishedAt"]
                videos.append(perf)

    return videos
```

### 9.2 TikTok Analytics Integration

```python
# TikTok Content Posting API and analytics
# Note: TikTok's official API requires Business account approval.
# For most use cases, use the TikTok Creator Tools built-in analytics
# or third-party APIs like SocialKit.

import requests

def get_tiktok_video_analytics(access_token, video_ids):
    """
    Get TikTok video analytics using the Content Posting API.
    Requires approved developer access.
    """
    url = "https://open.tiktokapis.com/v2/video/query/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Fields available through the API
    fields = [
        "id", "title", "video_description", "duration",
        "create_time", "share_url",
        "like_count", "comment_count", "share_count", "view_count",
    ]

    response = requests.post(url, headers=headers, json={
        "filters": {"video_ids": video_ids},
        "fields": fields,
    })

    return response.json()


# Alternative: Use web scraping for basic metrics (use responsibly)
# Or use third-party tools like SocialKit API:
def get_tiktok_via_socialkit(api_key, video_url):
    """Third-party API for TikTok video analytics."""
    response = requests.get(
        "https://api.socialkit.dev/tiktok/video",
        params={"url": video_url},
        headers={"Authorization": f"Bearer {api_key}"}
    )
    return response.json()
```

### 9.3 Feedback Loop: Learning from Performance

```python
import json
from pathlib import Path

class PerformanceFeedbackLoop:
    """
    Track clip performance and use it to improve future selection.

    Stores: (clip_features, platform, actual_performance) tuples.
    Uses this data to:
    1. Adjust scoring weights
    2. Learn which features predict performance per platform
    3. Identify content patterns that work for this specific channel
    """

    def __init__(self, data_path="clip_performance.json"):
        self.data_path = Path(data_path)
        self.records = self._load()

    def _load(self):
        if self.data_path.exists():
            return json.loads(self.data_path.read_text())
        return []

    def _save(self):
        self.data_path.write_text(json.dumps(self.records, indent=2))

    def record_clip(self, clip_features: dict, platform: str,
                     published_at: str, video_id: str):
        """Record a published clip with its features."""
        self.records.append({
            "clip_features": clip_features,
            "platform": platform,
            "published_at": published_at,
            "video_id": video_id,
            "performance": None,  # Filled in later
            "performance_updated_at": None,
        })
        self._save()

    def update_performance(self, video_id: str, performance: dict):
        """
        Update performance metrics for a published clip.

        performance dict should contain:
        - views: int
        - likes: int
        - comments: int
        - shares: int
        - avg_watch_percentage: float (0-100)
        - retention_at_3s: float (0-1)  -- critical metric
        - retention_at_50pct: float (0-1)
        - ctr: float  -- click-through rate if applicable
        """
        for record in self.records:
            if record["video_id"] == video_id:
                record["performance"] = performance
                record["performance_updated_at"] = datetime.now().isoformat()
                break
        self._save()

    def compute_engagement_score(self, performance: dict) -> float:
        """
        Compute a single engagement score from performance metrics.
        This is the target variable we want to predict and maximize.
        """
        if not performance:
            return 0.0

        views = performance.get("views", 0)
        likes = performance.get("likes", 0)
        comments = performance.get("comments", 0)
        shares = performance.get("shares", 0)
        avg_watch_pct = performance.get("avg_watch_percentage", 0)
        retention_3s = performance.get("retention_at_3s", 0)

        # Engagement rate (interaction / views)
        if views > 0:
            engagement_rate = (likes + comments * 3 + shares * 5) / views
        else:
            engagement_rate = 0

        # Combined score emphasizing retention and shares
        score = (
            min(1.0, engagement_rate * 10) * 0.3 +
            retention_3s * 0.25 +
            (avg_watch_pct / 100) * 0.25 +
            min(1.0, shares / max(views * 0.01, 1)) * 0.2
        )

        return score

    def learn_optimal_weights(self):
        """
        Use historical performance data to learn optimal scoring weights.
        Simple approach: correlate each feature with engagement score.
        """
        completed_records = [
            r for r in self.records if r["performance"] is not None
        ]

        if len(completed_records) < 20:
            return None  # Not enough data yet

        import numpy as np
        from scipy import stats

        feature_names = [
            "llm_virality", "engagement_pred", "quotability",
            "audio_energy", "speaker_dynamics", "topic_coherence",
            "emphasis", "laughter", "qa_pair", "boundary_quality"
        ]

        # Build feature matrix and target vector
        X = []
        y = []

        for record in completed_records:
            features = record["clip_features"]
            performance = record["performance"]

            feature_vec = [features.get(f, 0) for f in feature_names]
            engagement = self.compute_engagement_score(performance)

            X.append(feature_vec)
            y.append(engagement)

        X = np.array(X)
        y = np.array(y)

        # Compute correlations
        correlations = {}
        for i, name in enumerate(feature_names):
            corr, p_value = stats.pearsonr(X[:, i], y)
            correlations[name] = {
                "correlation": float(corr),
                "p_value": float(p_value),
                "significant": p_value < 0.05,
            }

        # Convert correlations to weights (positive correlations only)
        raw_weights = {
            name: max(0, info["correlation"])
            for name, info in correlations.items()
        }

        # Normalize to sum to 1
        total = sum(raw_weights.values())
        if total > 0:
            optimal_weights = {k: v / total for k, v in raw_weights.items()}
        else:
            optimal_weights = None

        return {
            "correlations": correlations,
            "optimal_weights": optimal_weights,
            "n_samples": len(completed_records),
        }
```

---

## 10. Scheduling Optimization

### 10.1 Platform-Specific Optimal Posting Times

Based on 2025-2026 research across millions of posts:

```python
# Best posting times by platform (all times in local audience timezone)
POSTING_WINDOWS = {
    "youtube_shorts": {
        # Peak: afternoon and evening when mobile usage spikes
        "best_hours": [11, 12, 14, 15, 16, 20, 21],
        "best_days": ["friday", "saturday", "sunday"],
        "worst_days": ["monday"],
        "notes": "Shorts peak 2-4 PM and 8-11 PM. Tuesday 11 AM is a sweet spot.",
        "frequency_cap": 2,  # Max shorts per day for optimal algorithm treatment
        "min_gap_hours": 4,  # Minimum hours between posts
    },
    "tiktok": {
        # Peak: early afternoon through late evening
        "best_hours": [13, 14, 15, 16, 19, 20, 21, 22],
        "best_days": ["wednesday", "thursday", "friday"],
        "worst_days": ["saturday"],
        "notes": "Engagement picks up 1 PM+. Wednesday is top day. Saturday is worst.",
        "frequency_cap": 3,  # TikTok rewards higher frequency
        "min_gap_hours": 3,
    },
    "instagram_reels": {
        # Peak: morning commute and evening
        "best_hours": [7, 8, 9, 10, 11, 19, 20, 21],
        "best_days": ["tuesday", "wednesday", "thursday"],
        "worst_days": ["sunday"],
        "notes": "Morning commute (7-9 AM) and evening (7-9 PM) are prime.",
        "frequency_cap": 1,  # Instagram penalizes over-posting more than others
        "min_gap_hours": 8,
    },
}


def generate_posting_schedule(
    clips: list,
    platforms: list,
    start_date: datetime,
    days_ahead: int = 14,
):
    """
    Generate an optimal posting schedule across platforms.

    Strategy:
    - Spread clips across platforms (don't post same clip everywhere on same day)
    - Respect frequency caps per platform
    - Post at optimal hours for each platform
    - Stagger same clip across platforms by 24-48 hours
    """
    schedule = []
    clip_queue = list(enumerate(clips))  # (index, clip) pairs

    for day_offset in range(days_ahead):
        current_date = start_date + timedelta(days=day_offset)
        day_name = current_date.strftime("%A").lower()

        for platform in platforms:
            config = POSTING_WINDOWS[platform]

            # Skip worst days
            if day_name in config.get("worst_days", []):
                continue

            # Get clips for today (up to frequency cap)
            posts_today = [s for s in schedule
                          if s["date"] == current_date.date() and s["platform"] == platform]

            if len(posts_today) >= config["frequency_cap"]:
                continue

            # Pick best available hour
            available_hours = config["best_hours"].copy()
            for post in posts_today:
                # Remove hours too close to existing posts
                available_hours = [
                    h for h in available_hours
                    if abs(h - post["hour"]) >= config["min_gap_hours"]
                ]

            if not available_hours or not clip_queue:
                continue

            # Prefer best day + best hour combinations
            is_best_day = day_name in config.get("best_days", [])
            hour = available_hours[0] if is_best_day else available_hours[len(available_hours)//2]

            clip_idx, clip = clip_queue.pop(0)

            schedule.append({
                "date": current_date.date(),
                "hour": hour,
                "platform": platform,
                "clip_index": clip_idx,
                "clip_title": clip.get("title", f"Clip {clip_idx}"),
                "is_best_day": is_best_day,
                "priority": "high" if is_best_day else "normal",
            })

    return schedule
```

### 10.2 A/B Testing Framework

```python
import random
from collections import defaultdict

class ClipABTester:
    """
    A/B test different aspects of clip presentation:
    - Title variations
    - Thumbnail styles
    - Clip start/end point variations
    - Posting times
    - Caption styles
    """

    def __init__(self):
        self.experiments = {}
        self.results = defaultdict(list)

    def create_experiment(self, experiment_id: str, variants: list,
                          metric: str = "engagement_rate"):
        """
        Create an A/B test experiment.

        Example:
        create_experiment("title_style", [
            {"name": "question_hook", "title_template": "{topic}?"},
            {"name": "bold_claim", "title_template": "The truth about {topic}"},
            {"name": "numbered", "title_template": "{n} things about {topic}"},
        ])
        """
        self.experiments[experiment_id] = {
            "variants": variants,
            "metric": metric,
            "assignments": {},  # clip_id -> variant
            "created_at": datetime.now().isoformat(),
        }

    def assign_variant(self, experiment_id: str, clip_id: str):
        """Randomly assign a clip to an experiment variant."""
        experiment = self.experiments[experiment_id]
        variant = random.choice(experiment["variants"])
        experiment["assignments"][clip_id] = variant
        return variant

    def record_result(self, experiment_id: str, clip_id: str, metric_value: float):
        """Record the performance metric for a clip in an experiment."""
        experiment = self.experiments[experiment_id]
        variant = experiment["assignments"].get(clip_id)
        if variant:
            self.results[experiment_id].append({
                "variant": variant["name"],
                "metric": metric_value,
                "clip_id": clip_id,
            })

    def analyze_experiment(self, experiment_id: str):
        """
        Analyze experiment results using Bayesian inference.
        Returns probability that each variant is the best.
        """
        from scipy import stats

        results = self.results[experiment_id]
        variants = self.experiments[experiment_id]["variants"]

        # Group results by variant
        variant_results = defaultdict(list)
        for r in results:
            variant_results[r["variant"]].append(r["metric"])

        analysis = {}
        for variant in variants:
            name = variant["name"]
            values = variant_results.get(name, [])

            if len(values) < 5:
                analysis[name] = {
                    "n_samples": len(values),
                    "status": "insufficient_data",
                    "mean": np.mean(values) if values else 0,
                }
                continue

            analysis[name] = {
                "n_samples": len(values),
                "mean": float(np.mean(values)),
                "std": float(np.std(values)),
                "ci_95": (
                    float(np.mean(values) - 1.96 * np.std(values) / np.sqrt(len(values))),
                    float(np.mean(values) + 1.96 * np.std(values) / np.sqrt(len(values))),
                ),
            }

        # Pairwise comparisons
        variant_names = list(variant_results.keys())
        if len(variant_names) >= 2:
            for i in range(len(variant_names)):
                for j in range(i + 1, len(variant_names)):
                    a = variant_results[variant_names[i]]
                    b = variant_results[variant_names[j]]
                    if len(a) >= 5 and len(b) >= 5:
                        t_stat, p_value = stats.ttest_ind(a, b)
                        analysis[f"{variant_names[i]}_vs_{variant_names[j]}"] = {
                            "t_statistic": float(t_stat),
                            "p_value": float(p_value),
                            "significant": p_value < 0.05,
                            "winner": variant_names[i] if np.mean(a) > np.mean(b)
                                      else variant_names[j],
                        }

        return analysis
```

---

## 11. Bayesian Optimization for Scheduling

Use Thompson Sampling (a multi-armed bandit approach) to continuously optimize
posting schedules without running traditional A/B tests.

### 11.1 Thompson Sampling for Posting Time Optimization

```python
import numpy as np
from scipy.stats import beta as beta_dist
from collections import defaultdict
from datetime import datetime
import json


class ThompsonSamplingScheduler:
    """
    Multi-armed bandit scheduler using Thompson Sampling.

    Each "arm" is a (platform, day_of_week, time_slot) combination.
    The reward is a normalized engagement metric.

    Thompson Sampling naturally balances exploration vs exploitation:
    - Arms with high uncertainty get explored
    - Arms with proven high performance get exploited
    - No need to pre-define test/control groups
    """

    def __init__(self, platforms=None, time_slots=None, prior_alpha=1, prior_beta=1):
        """
        Initialize with uniform prior (Beta(1,1)).

        prior_alpha, prior_beta: prior parameters for the Beta distribution.
        Beta(1,1) = uniform prior (no initial assumption)
        Beta(2,2) = weak prior toward 0.5
        """
        self.platforms = platforms or ["youtube_shorts", "tiktok", "instagram_reels"]
        self.time_slots = time_slots or list(range(6, 24))  # 6 AM to 11 PM
        self.days = ["monday", "tuesday", "wednesday", "thursday",
                     "friday", "saturday", "sunday"]

        # Beta distribution parameters for each arm
        # arm_key: (platform, day, hour)
        self.arms = {}
        for platform in self.platforms:
            for day in self.days:
                for hour in self.time_slots:
                    key = (platform, day, hour)
                    self.arms[key] = {
                        "alpha": prior_alpha,  # successes + prior
                        "beta": prior_beta,    # failures + prior
                        "n_trials": 0,
                        "total_reward": 0.0,
                    }

        self.history = []

    def select_time_slot(self, platform: str, available_days: list = None,
                          n_slots: int = 1) -> list:
        """
        Select the best time slot(s) using Thompson Sampling.

        For each available arm, sample from its Beta distribution.
        Select the arm(s) with the highest sampled values.
        """
        if available_days is None:
            available_days = self.days

        # Sample from each arm's posterior
        candidates = []
        for day in available_days:
            for hour in self.time_slots:
                key = (platform, day, hour)
                arm = self.arms[key]

                # Thompson sample: draw from Beta(alpha, beta)
                sampled_value = np.random.beta(arm["alpha"], arm["beta"])

                candidates.append({
                    "key": key,
                    "platform": platform,
                    "day": day,
                    "hour": hour,
                    "sampled_value": sampled_value,
                    "mean": arm["alpha"] / (arm["alpha"] + arm["beta"]),
                    "n_trials": arm["n_trials"],
                    "uncertainty": self._compute_uncertainty(arm),
                })

        # Sort by sampled value (this is the Thompson Sampling selection)
        candidates.sort(key=lambda x: x["sampled_value"], reverse=True)

        return candidates[:n_slots]

    def update(self, platform: str, day: str, hour: int, reward: float):
        """
        Update the arm's Beta distribution after observing a reward.

        reward: float between 0 and 1 (normalized engagement score).

        For Beta-Bernoulli, we'd use binary success/failure.
        For continuous rewards, we use a soft update:
        - reward > threshold: increment alpha (success)
        - reward <= threshold: increment beta (failure)

        Or use proportional update for smoother learning.
        """
        key = (platform, day, hour)
        arm = self.arms[key]

        # Proportional update (smoother than binary)
        # Scale reward to get meaningful updates
        arm["alpha"] += reward
        arm["beta"] += (1.0 - reward)
        arm["n_trials"] += 1
        arm["total_reward"] += reward

        self.history.append({
            "platform": platform,
            "day": day,
            "hour": hour,
            "reward": reward,
            "timestamp": datetime.now().isoformat(),
        })

    def _compute_uncertainty(self, arm):
        """Compute uncertainty (variance) of the arm's distribution."""
        a, b = arm["alpha"], arm["beta"]
        return (a * b) / ((a + b) ** 2 * (a + b + 1))

    def get_best_schedule(self, platform: str, n_posts_per_week: int = 7):
        """
        Generate the current best-known schedule based on posterior means.
        Unlike select_time_slot (which explores), this exploits.
        """
        slots = []
        for day in self.days:
            for hour in self.time_slots:
                key = (platform, day, hour)
                arm = self.arms[key]
                mean = arm["alpha"] / (arm["alpha"] + arm["beta"])
                slots.append({
                    "day": day,
                    "hour": hour,
                    "expected_reward": mean,
                    "confidence": 1.0 / (1.0 + self._compute_uncertainty(arm) * 100),
                    "n_trials": arm["n_trials"],
                })

        slots.sort(key=lambda x: x["expected_reward"], reverse=True)

        # Pick top N slots, ensuring at most 2 per day
        selected = []
        day_counts = defaultdict(int)
        for slot in slots:
            if day_counts[slot["day"]] < 2 and len(selected) < n_posts_per_week:
                selected.append(slot)
                day_counts[slot["day"]] += 1

        return selected

    def save_state(self, path: str):
        """Save the bandit state for persistence."""
        state = {
            "arms": {str(k): v for k, v in self.arms.items()},
            "history_count": len(self.history),
        }
        Path(path).write_text(json.dumps(state, indent=2))

    def get_exploration_report(self):
        """
        Report on exploration vs exploitation balance.
        Identifies under-explored arms that might be valuable.
        """
        under_explored = []
        high_potential = []

        for key, arm in self.arms.items():
            uncertainty = self._compute_uncertainty(arm)
            mean = arm["alpha"] / (arm["alpha"] + arm["beta"])

            if arm["n_trials"] < 3:
                under_explored.append({
                    "arm": key,
                    "n_trials": arm["n_trials"],
                    "uncertainty": uncertainty,
                })
            elif mean > 0.6 and uncertainty > 0.02:
                high_potential.append({
                    "arm": key,
                    "mean": mean,
                    "uncertainty": uncertainty,
                    "n_trials": arm["n_trials"],
                })

        return {
            "total_arms": len(self.arms),
            "under_explored": sorted(under_explored, key=lambda x: x["n_trials"])[:20],
            "high_potential": sorted(high_potential, key=lambda x: x["mean"], reverse=True)[:10],
            "total_trials": sum(a["n_trials"] for a in self.arms.values()),
        }
```

### 11.2 Contextual Bandit for Content-Aware Scheduling

```python
class ContextualBanditScheduler:
    """
    Extension of Thompson Sampling that considers clip features
    (context) when selecting posting times.

    Different types of content may perform better at different times:
    - Educational content -> morning/afternoon
    - Entertainment/humor -> evening/night
    - Emotional stories -> evening
    - Controversial takes -> peak social hours
    """

    def __init__(self):
        self.content_type_bandits = {}

    def get_or_create_bandit(self, content_type: str):
        if content_type not in self.content_type_bandits:
            self.content_type_bandits[content_type] = ThompsonSamplingScheduler()
        return self.content_type_bandits[content_type]

    def classify_clip_content(self, clip_features: dict) -> str:
        """
        Classify clip into content type for contextual scheduling.
        """
        tags = clip_features.get("topic_tags", [])
        hook_type = clip_features.get("hook_type", "")

        if any(t in tags for t in ["advice", "tutorial", "how-to", "tips"]):
            return "educational"
        elif any(t in tags for t in ["funny", "humor", "joke", "laugh"]):
            return "entertainment"
        elif any(t in tags for t in ["story", "personal", "emotional", "struggle"]):
            return "emotional_story"
        elif any(t in tags for t in ["debate", "controversial", "hot-take", "opinion"]):
            return "controversial"
        else:
            return "general"

    def select_posting_time(self, platform: str, clip_features: dict):
        """Select posting time based on content type and platform."""
        content_type = self.classify_clip_content(clip_features)
        bandit = self.get_or_create_bandit(content_type)
        return bandit.select_time_slot(platform, n_slots=3)

    def update_from_performance(self, platform: str, clip_features: dict,
                                  day: str, hour: int, reward: float):
        """Update the contextual bandit with observed performance."""
        content_type = self.classify_clip_content(clip_features)
        bandit = self.get_or_create_bandit(content_type)
        bandit.update(platform, day, hour, reward)
```

### 11.3 Full Scheduling Integration

```python
class AdaptivePublishingPipeline:
    """
    End-to-end pipeline that:
    1. Mines clips from new episodes
    2. Ranks them using the scoring pipeline
    3. Schedules using Bayesian optimization
    4. Tracks performance
    5. Updates models
    """

    def __init__(self):
        self.scheduler = ContextualBanditScheduler()
        self.feedback_loop = PerformanceFeedbackLoop()
        self.ab_tester = ClipABTester()

    def process_new_episode(self, audio_path: str, transcript_segments: list,
                              episode_title: str, episode_description: str):
        """Full pipeline for a new episode."""

        # 1. Mine clips
        candidates = run_full_pipeline(
            audio_path, transcript_segments,
            episode_title, episode_description
        )

        # 2. Take top N clips
        top_clips = candidates[:10]

        # 3. Schedule across platforms
        schedule = []
        for clip in top_clips:
            clip_features = {
                "topic_tags": clip.get("topic_tags", []),
                "hook_type": clip.get("hook_type", "general"),
                "virality_score": clip.get("virality_score", 5),
            }

            for platform in ["youtube_shorts", "tiktok", "instagram_reels"]:
                slots = self.scheduler.select_posting_time(platform, clip_features)
                if slots:
                    best_slot = slots[0]
                    schedule.append({
                        "clip": clip,
                        "platform": platform,
                        "day": best_slot["day"],
                        "hour": best_slot["hour"],
                        "expected_reward": best_slot["sampled_value"],
                    })

        return schedule

    def update_from_analytics(self, published_clips: list):
        """
        Called periodically (e.g., daily) to update models
        from actual performance data.
        """
        for clip_record in published_clips:
            video_id = clip_record["video_id"]
            platform = clip_record["platform"]

            # Get performance from appropriate API
            if platform == "youtube_shorts":
                analytics, youtube = get_youtube_analytics_service()
                perf = get_video_performance(analytics, youtube, video_id)
            elif platform == "tiktok":
                perf = get_tiktok_video_analytics(
                    clip_record["access_token"], [video_id]
                )
            else:
                continue

            # Compute engagement score
            engagement = self.feedback_loop.compute_engagement_score(
                perf.get("metrics", {})
            )

            # Update scheduler bandit
            self.scheduler.update_from_performance(
                platform=platform,
                clip_features=clip_record.get("features", {}),
                day=clip_record["posted_day"],
                hour=clip_record["posted_hour"],
                reward=engagement,
            )

            # Update feedback loop
            self.feedback_loop.update_performance(video_id, perf.get("metrics", {}))

        # Periodically re-learn optimal weights
        weight_update = self.feedback_loop.learn_optimal_weights()
        if weight_update and weight_update.get("optimal_weights"):
            print(f"Updated scoring weights based on {weight_update['n_samples']} samples:")
            for feature, weight in weight_update["optimal_weights"].items():
                print(f"  {feature}: {weight:.3f}")
```

---

## Summary of Key Implementation Recommendations

### Quick Start (MVP)

For the fastest path to working clip mining:

1. **Transcribe** with WhisperX (word-level timestamps + speaker diarization)
2. **Send to Claude** with the structured prompt from Section 4.2
3. **Use structured outputs** (Section 4.3) for guaranteed JSON
4. **Snap boundaries** to silence points (Section 7)
5. **Post at platform-optimal times** (Section 10.1)

This gets you 80% of the value with 20% of the effort. The audio analysis, NLP scoring,
and Bayesian scheduling add incremental improvements.

### Scaling Up

Once the MVP is working, add in order of impact:

1. **Analytics feedback loop** (Section 9.3) -- learn from actual performance
2. **Thompson Sampling scheduler** (Section 11.1) -- optimize posting times
3. **Audio energy scoring** (Section 2) -- catch moments LLM might miss
4. **Engagement prediction features** (Section 5) -- pre-filter before LLM
5. **Speaker dynamics** (Section 6) -- find high-energy exchanges

### Model Costs

For a 1-hour podcast episode:
- WhisperX transcription: ~2-5 minutes on GPU, free (self-hosted)
- Claude Sonnet for clip identification: ~$0.05-0.15 per episode
- Audio analysis with librosa: ~30 seconds, free (self-hosted)
- Total per episode: **under $0.20 in API costs**

---

## References and Sources

- [OpusClip Virality Score](https://help.opus.pro/docs/article/virality-score)
- [WhisperX: Word-level Timestamps and Diarization](https://github.com/m-bain/whisperX)
- [Claude Structured Outputs Documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [pyAudioAnalysis Library](https://github.com/tyiannak/pyAudioAnalysis)
- [Laughter Detection](https://github.com/jrgillick/laughter-detection)
- [librosa.effects.split Documentation](https://librosa.org/doc/main/generated/librosa.effects.split.html)
- [librosa.feature.rms Documentation](https://librosa.org/doc/main/generated/librosa.feature.rms.html)
- [YouTube Analytics API Metrics](https://developers.google.com/youtube/analytics/metrics)
- [YouTube Analytics Sample Requests](https://developers.google.com/youtube/analytics/sample-requests)
- [Rhapsody: Podcast Highlight Detection Dataset](https://arxiv.org/html/2505.19429v2)
- [NLP for Podcast Promotion (Towards Data Science)](https://towardsdatascience.com/finding-the-best-part-of-your-podcast-to-promote-via-nlp-f844a88b287a)
- [Thompson Sampling Tutorial (Stanford)](https://web.stanford.edu/~bvr/pubs/TS_Tutorial.pdf)
- [Thompson Sampling Multi-Armed Bandits (Towards Data Science)](https://towardsdatascience.com/multi-armed-bandits-thompson-sampling-algorithm-fea205cf31df/)
- [Best Posting Times: YouTube Shorts 2026](https://www.shortimize.com/blog/best-time-to-post-youtube-shorts)
- [Best Posting Times: TikTok, Shorts, Reels 2025](https://www.clipgoat.com/blog/best-times-to-post-tiktok-youtube-shorts-and-instagram-reels-in-2025-(and-how-to-automate-it))
- [TikTok Analytics Tools](https://agencyanalytics.com/blog/tiktok-analytics)
- [Short-Form Video Hooks Trends 2025](https://driveeditor.com/blog/trends-short-form-video-hooks)
- [Audio Slicer (Silence Detection)](https://github.com/openvpi/audio-slicer)
- [pyannote Speaker Diarization](https://www.pyannote.ai/blog/what-is-speaker-diarization)
- [Speaker Diarization for Podcasts](https://www.clipto.com/blog/what-is-speaker-diarization-and-its-application-in-podcasts-and-interviews)
