# Distil — Podcast Automation Pipeline Specification

**Version:** 2.0
**Status:** Draft
**Last Updated:** 2026-02-13

Distil is a self-hosted, AI-assisted podcast automation pipeline. It extracts the essence from raw podcast content into concentrated, shareable clips. This spec is written so an engineer can implement independently — no prior context required.

---

## Table of Contents

1. [Goals](#1-goals)
2. [Assumptions & Constraints](#2-assumptions--constraints)
3. [System Overview](#3-system-overview)
4. [Data Flow (End-to-End)](#4-data-flow-end-to-end)
5. [Agent Specifications](#5-agent-specifications)
6. [Platform Publishing Reference](#6-platform-publishing-reference)
7. [Video Processing Reference](#7-video-processing-reference)
8. [Transcription & Diarization Reference](#8-transcription--diarization-reference)
9. [Clip Mining & Scoring](#9-clip-mining--scoring)
10. [Scheduling & Analytics](#10-scheduling--analytics)
11. [File/Folder Layout](#11-filefolder-layout)
12. [Configuration Spec](#12-configuration-spec)
13. [JSON Schemas](#13-json-schemas)
14. [QA Rules](#14-qa-rules)
15. [Security & Credentials](#15-security--credentials)
16. [Testing Strategy](#16-testing-strategy)
17. [Mac Mini Autonomous Workflow](#17-mac-mini-autonomous-workflow)
18. [Interactive Clip Review UI](#18-interactive-clip-review-ui)
19. [Feedback Loop & Persistent Context](#19-feedback-loop--persistent-context)
20. [Roadmap / Extensions](#20-roadmap--extensions)

---

## 1. Goals

1. **Ingest** raw camera files (single camera, two speakers in left/right halves) — including autonomous SD card ingest on the Mac Mini.
2. **Detect audio layout** — if audio is true split L/R, cut between speakers based on who is talking.
3. **Render** one longform video (16:9) plus 10 short clips (9:16 vertical). All crops preserve aspect ratio — no distortion.
4. **Transcribe** via Deepgram Nova-3 for AI analysis; subtitle burn-in is a downstream use.
5. **Mine clips** using Claude Opus 4.6 to identify the 10 most compelling segments.
6. **Provide** an interactive web-based clip review UI with approve/reject/request alternative/manual clip entry.
7. **Publish** to: YouTube (longform + Shorts), TikTok, Instagram Reels, and podcast RSS. Each clip gets platform-specific metadata with links back to the longform.
8. **Schedule** 1 clip/day Mon–Thu, 2 clips/day Fri–Sun (10 clips over 7 days).
9. **Capture** analytics and adapt future clip selection and scheduling via a persistent feedback loop.
10. **Automate** the full lifecycle on a Mac Mini via cron — SD card ingest, local processing, SSD storage, external HDD backup, and post-publish cleanup.

---

## 2. Assumptions & Constraints

### Source Material
- Video is 16:9 (1920×1080), both speakers visible (left/right halves of frame).
- Source files are MP4/MOV from a single camera. Episodes may span multiple files.
- Audio may be:
  - **True split stereo** (left mic on L channel, right mic on R channel) — ideal.
  - **Mixed mono duplicated to L/R** — must be detected and flagged; speaker cuts disabled.

### Processing Rules
- No time-stretching. Only cutting and concatenation.
- **No distortion.** All crops preserve the original aspect ratio via crop-then-zoom. Never stretch/squeeze.
- All timestamps are frame-accurate. Cuts happen on keyframes or after re-encode.
- Output codec is always H.264 High Profile + AAC-LC in MP4 container with `faststart` flag.

### Infrastructure
- Runs on a **Mac Mini** (Apple Silicon M2/M4). Storage and compute are local.
- Acceptable output latency: **1-3 hours** for a 1-hour episode (full pipeline).
- Internet required for: Deepgram transcription, Claude clip analysis, publishing, and analytics collection.
- Cron-based autonomous operation — see [Section 17](#17-mac-mini-autonomous-workflow).

### Platform Constraints
- Instagram Reels requires video hosted at a **public URL** at upload time.
- YouTube requires OAuth compliance audit for public uploads (new projects upload as private until audit passes).
- TikTok unaudited apps can only post to the developer's own private account.

---

## 3. System Overview

A **pipeline orchestrator** runs a series of **agents**. Each agent:
- Receives structured JSON input from upstream agents.
- Produces artifacts (files) and writes structured JSON output for downstream steps.
- Logs its actions and errors to a per-episode log.
- Is idempotent — re-running an agent with the same input produces the same output.

All data is stored in a **per-episode directory** on the internal SSD, with backups to the external HDD.

### Agent Pipeline

```
                                    +--> Clip Miner Agent
                                    |        |
Ingest --> Stitch --> Audio    --> Speaker --> Longform    --> Metadata --> QA --> Clip Review --> Publisher
Agent     Agent     Analysis     Cut        Render          Agent        Agent   UI (Web)       Agent
                    Agent        Agent      Agent                                                 |
                                    |        |                                                    v
                                    +--> Shorts Render Agent                              Analytics Agent
                                                                                         (feedback loop)
```

**Key change from v1:** Transcription is now via Deepgram API (not local Whisper), runs in parallel with Speaker Segmentation. The Approval Agent is replaced by an interactive web UI.

### Agents

| # | Agent | Input | Output |
|---|-------|-------|--------|
| 1 | **Ingest Agent** | Watch folder, SD card, or manual trigger | `ingest.json`, file inventory |
| 2 | **Stitch Agent** | `ingest.json` | `source_merged.mp4`, `stitch.json` |
| 3 | **Audio Analysis Agent** | `source_merged.mp4` | `audio_analysis.json` (channel info, flags) |
| 4 | **Speaker Cut Agent** | `source_merged.mp4`, `audio_analysis.json` | `segments.json` (speaker timeline with L/R/BOTH) |
| 5 | **Longform Render Agent** | `source_merged.mp4`, `segments.json` | `longform.mp4` |
| 6 | **Transcription Agent** | `source_merged.mp4` | `transcript.json`, `diarized_transcript.json`, SRT files |
| 7 | **Clip Miner Agent** | `diarized_transcript.json`, `segments.json` | `clips.json` (10 clip candidates) |
| 8 | **Shorts Render Agent** | `source_merged.mp4`, `clips.json`, subtitles | `shorts/*.mp4` |
| 9 | **Metadata Agent** | `clips.json`, transcript | `metadata.json` (per-platform per-clip) |
| 10 | **QA Agent** | All artifacts | `qa.json` (validation report) |
| 11 | **Clip Review UI** | All artifacts | `approval_batch.json` (via web interaction) |
| 12 | **Publisher Agent** | Approved batch | `publish_log.json` |
| 13 | **Analytics Agent** | Platform analytics APIs | `analytics.json`, updated context files |

---

## 4. Data Flow (End-to-End)

### A. Ingest + Stitch

1. Watch a configured folder for new files, detect SD card mounts, or accept manual input.
2. For multi-file episodes, read `creation_time` from file metadata via `ffprobe`:
   ```bash
   ffprobe -v error -show_entries format_tags=creation_time -of default=noprint_wrappers=1:nokey=1 file.mp4
   ```
   Fallback to file `mtime` if metadata is absent.
3. Sort chronologically. Validate codec/resolution/framerate uniformity.
4. Merge via concat demuxer (stream copy if uniform) or concat filter (re-encode if mismatched).
5. Write `ingest.json` containing file list, timestamps, merge method, and any flags.

### B. Audio Preflight

1. Probe audio stream:
   ```bash
   ffprobe -v error -select_streams a \
     -show_entries stream=channels,channel_layout,sample_rate,codec_name \
     -of json source_merged.mp4
   ```
2. If `channels < 2` or layout is not stereo: flag `audio_not_stereo`.
3. If stereo, compute channel similarity:
   - Extract L and R channels to mono WAV.
   - Compute per-window (1-second) Pearson correlation and RMS ratio (dB difference).
   - If mean correlation > 0.95 **and** mean RMS delta < 3 dB: flag `audio_channels_identical`.
4. Write `audio_analysis.json`.

### C. Speaker Segmentation

**Only runs if audio is true split L/R** (no flags from Audio Preflight).

1. Compute per-frame RMS energy (frame size = 0.1s).
2. Estimate noise floor as the 10th percentile of RMS values.
3. Speech threshold = noise floor + 12 dB.
4. For each frame, classify:
   - `L` — left channel above threshold, right below.
   - `R` — right channel above threshold, left below.
   - `BOTH` — both channels above threshold.
   - `NONE` — both channels below threshold.
5. Apply debouncing:
   - `NONE` frames inherit the last non-ambiguous state (`L`, `R`, or `BOTH`).
   - **`BOTH` frames are preserved as-is** — they represent both speakers talking simultaneously.
   - Merge consecutive segments with the same label.
   - Segments shorter than 2.0s are absorbed into the adjacent longer segment.
6. Write `segments.json` with three segment types: `L`, `R`, `BOTH`.

### D. Longform Render (Distortion-Free)

Three render modes based on segment type — all preserve aspect ratio:

**`L` segment (left speaker zoom):**
1. Crop left half: 960×1080 (left half of 1920×1080)
2. Crop vertically to 16:9 within that half: 960×540 (centered vertically)
3. Scale to 1920×1080 with Lanczos
```bash
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -vf "crop=iw/2:ih:0:0,crop=iw:iw*9/16:(ih-iw*9/16)/2:0,scale=1920:1080:flags=lanczos" \
  -c:v libx264 -crf 18 -preset medium -c:a copy segment_L.mp4
```

**`R` segment (right speaker zoom):**
1. Crop right half: 960×1080
2. Crop vertically to 16:9: 960×540 (centered)
3. Scale to 1920×1080
```bash
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -vf "crop=iw/2:ih:iw/2:0,crop=iw:iw*9/16:(ih-iw*9/16)/2:0,scale=1920:1080:flags=lanczos" \
  -c:v libx264 -crf 18 -preset medium -c:a copy segment_R.mp4
```

**`BOTH` segment (wide frame, both speakers):**
Full frame pass-through. No crop, no scale.
```bash
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -c:v libx264 -crf 18 -preset medium -c:a copy segment_BOTH.mp4
```

**Assembly:** Render all segments independently (parallelizable), then concat via demuxer.

If no valid speaker separation (audio flags present): output full-frame longform without speaker cuts.

### E. Transcription (Deepgram Nova-3)

Transcription is generated for AI analysis; subtitle burn-in is a downstream use.

1. Send audio to Deepgram Nova-3 via the Python SDK:
   ```python
   from deepgram import DeepgramClient, PrerecordedOptions

   deepgram = DeepgramClient(api_key=DEEPGRAM_API_KEY)

   with open("source_merged.mp4", "rb") as audio:
       options = PrerecordedOptions(
           model="nova-3",
           language="en",
           smart_format=True,
           diarize=True,
           utterances=True,
           punctuate=True,
       )
       response = deepgram.listen.rest.v("1").transcribe_file(
           {"buffer": audio.read()}, options
       )
   ```
2. Single API call handles both transcription AND diarization. No separate tools needed.
3. Word-level timestamps returned by default.
4. Generate SRT via `deepgram-python-captions`:
   ```python
   from deepgram_captions import DeepgramConverter, srt

   converter = DeepgramConverter(response)
   captions = srt(converter)
   ```
5. Cost: ~$0.46/hour of audio. No local model management.
6. Write `transcript.json`, `diarized_transcript.json`, `transcript.srt`, and per-clip SRT files.

### F. Clip Mining (Claude Opus 4.6)

Generate 10 short clip candidates (30-90 seconds each) using LLM analysis.

1. Send the diarized transcript to Claude Opus 4.6 with structured output.
2. Claude identifies the 10 most compelling segments:
   - `start_seconds`, `end_seconds`, `title`, `hook_text`, `compelling_reason`, `virality_score` (1-10).
3. Snap clip boundaries to silence/pause points for clean cuts.
4. Cost: ~$0.10-0.30 per episode with `claude-opus-4-6`.
5. The clipping agent consults `clipping_agent_context.json` for learned preferences from past episodes.

See [Section 9](#9-clip-mining--scoring) for full detail.

### G. Shorts Render (Distortion-Free)

For each clip candidate:

**Single-speaker clip (9:16):**
1. Determine active speaker from `segments.json` at clip midpoint.
2. Crop speaker's half (960×1080), take center vertical slice (608×1080), scale to 1080×1920.
```bash
# Left speaker → 9:16
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -vf "crop=iw/2:ih:0:0,crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos" \
  -c:v libx264 -crf 20 -preset medium -c:a aac -b:a 128k \
  -movflags +faststart short.mp4
```

**Both-speaker clip (9:16):**
Center crop of full frame to 9:16 (608×1080 centered), scale to 1080×1920.
```bash
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -vf "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos" \
  -c:v libx264 -crf 20 -preset medium -c:a aac -b:a 128k \
  -movflags +faststart short.mp4
```

Burn in subtitles (TikTok/Reels style — large, bold, center-screen):
```bash
ffmpeg -i short_raw.mp4 \
  -vf "subtitles=clip.srt:force_style='FontName=Montserrat,FontSize=18,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,Alignment=10,MarginV=0'" \
  -c:v libx264 -crf 20 -c:a copy short_final.mp4
```

### H. Metadata + Scheduling (Channel-Specific)

1. **Longform**: same title and description across all platforms (YouTube + podcast RSS).
2. **Short clips**: platform-specific variations for each clip, generated in one Claude pass:
   - **YouTube Shorts**: title, description with `#Shorts` tag, link to longform in description.
   - **TikTok**: caption with hashtags, "Full episode" link in bio/comment.
   - **Instagram Reels**: caption with hashtags, "Link in bio" CTA pointing to longform.
3. Each clip description/caption includes a reference/link to the full longform video.
4. Claude Opus 4.6 generates all variations per clip. Cost: ~$0.02-0.05 per episode.
5. Schedule: 1 clip/day Mon–Thu, 2 clips/day Fri–Sun = 10 clips over 7 days.

### I. Interactive Clip Review

Replaces the old CLI approval gate. See [Section 18](#18-interactive-clip-review-ui) for full detail.

The web UI displays all 10 clip candidates with video preview, metadata, and per-clip actions (approve, reject, request alternative, manual clip entry, metadata editing).

### J. Publish

1. After approval, generate a release queue ordered by scheduled datetime.
2. Publisher runs on a cron/scheduler, checks queue, and posts via platform adapters.
3. Each adapter handles auth, upload, status polling, retry with backoff.
4. Write `publish_log.json` with post IDs, URLs, and status per platform.

### K. Analytics Loop

1. Weekly analytics collection from YouTube, TikTok, Instagram APIs.
2. Per-episode context saved to `clip_context.json`.
3. Per-agent persistent context updated — see [Section 19](#19-feedback-loop--persistent-context).
4. Feedback loop adjusts clip scoring weights, posting time preferences, content style preferences.

---

## 5. Agent Specifications

### 5.1 Ingest Agent

**Trigger:** File system watcher (FSEvents on macOS) on `ingest_dir`, SD card mount detection, or manual CLI/web invocation.

**Logic:**
1. Detect new files matching video extensions (`.mp4`, `.mov`, `.mxf`).
2. Wait for file stability (no size change for 10 seconds) to avoid partial copies.
3. Copy from source (SD card) to local working directory on SSD.
4. Extract metadata via `ffprobe`.
5. Group files by episode.
6. Write `ingest.json`.

### 5.2 Stitch Agent

**Input:** `ingest.json`

**Logic:** Validate uniformity, merge via concat demuxer or filter. Verify output duration matches sum of inputs.

**Output:** `source_merged.mp4`, `stitch.json`

### 5.3 Audio Analysis Agent

**Input:** `source_merged.mp4`

**Logic:** Probe audio, extract L/R channels, compute correlation + RMS ratio, classify.

**Output:** `audio_analysis.json`

### 5.4 Speaker Cut Agent

**Input:** `source_merged.mp4`, `audio_analysis.json`

**Precondition:** No audio flags.

**Logic:** RMS-based per-frame speaker classification with debouncing. Outputs three segment types: `L`, `R`, `BOTH`.

**Output:** `segments.json`
```json
{
  "segments": [
    {"start": 0.0, "end": 12.4, "speaker": "L"},
    {"start": 12.4, "end": 18.9, "speaker": "R"},
    {"start": 18.9, "end": 25.1, "speaker": "BOTH"},
    {"start": 25.1, "end": 38.7, "speaker": "L"}
  ]
}
```

### 5.5 Longform Render Agent

**Input:** `source_merged.mp4`, `segments.json`

**Logic:** Distortion-free segment-then-crop-then-concat approach (see Section 4D). Three render modes: L (zoom left), R (zoom right), BOTH (full wide frame). Parallelizable.

**Output:** `longform.mp4`

### 5.6 Transcription Agent (Deepgram)

**Input:** `source_merged.mp4`

**Logic:** Deepgram Nova-3 API call for transcription + diarization (see Section 4E).

**Output:** `transcript.json`, `diarized_transcript.json`, `transcript.srt`, per-clip SRT files.

### 5.7 Clip Miner Agent

**Input:** `diarized_transcript.json`, `segments.json`, `audio_analysis.json`

**Logic:** Multi-signal scoring with Claude Opus 4.6 (see Section 9). Outputs 10 ranked candidates. Consults `clipping_agent_context.json` for learned preferences.

**Output:** `clips.json`

### 5.8 Shorts Render Agent

**Input:** `source_merged.mp4`, `clips.json`, per-clip SRT files

**Logic:** Distortion-free vertical crop + subtitle burn-in (see Section 4G).

**Output:** `shorts/clip_01.mp4` ... `shorts/clip_10.mp4`

### 5.9 Metadata Agent

**Input:** `clips.json`, `diarized_transcript.json`

**Logic:** Claude Opus 4.6 generates per-platform metadata from transcript context. Longform metadata is uniform. Clip metadata is platform-specific with longform links.

**Output:** `metadata.json`

### 5.10 QA Agent

**Input:** All upstream artifacts.

**Logic:** Validates against QA rules (see Section 14). Produces pass/fail report.

**Output:** `qa.json`

### 5.11 Clip Review UI

**Input:** All artifacts + `qa.json`

**Logic:** Web-based interactive review. See [Section 18](#18-interactive-clip-review-ui).

**Output:** `approval_batch.json`

### 5.12 Publisher Agent

**Input:** Approved `approval_batch.json`

**Logic:** Scheduled publishing via platform adapters (see Section 6). Retry with backoff.

**Output:** `publish_log.json`

### 5.13 Analytics Agent

**Input:** `publish_log.json`, platform analytics APIs.

**Logic:** Collects metrics, updates persistent context files (see Section 19).

**Output:** `analytics.json`, updated `clipping_agent_context.json`, updated `scheduling_agent_context.json`.

---

## 6. Platform Publishing Reference

### 6.1 Universal Output Format

All videos are rendered as:
- **Container:** MP4
- **Video codec:** H.264 High Profile, 4:2:0 chroma, progressive scan
- **Audio codec:** AAC-LC
- **Sample rate:** 48 kHz
- **Moov atom:** Front of file (`-movflags +faststart`)
- **Keyframe interval:** Every 2 seconds (`-g <fps*2>`)

### 6.2 Platform Matrix

| Platform | Max Size | Max Duration | Aspect Ratio | Upload Method | Native Scheduling | Auth |
|----------|----------|-------------|--------------|--------------|-------------------|------|
| **YouTube** | 256 GB | 12 hr (long), 3 min (Shorts) | 16:9 / 9:16 | Resumable upload | Yes (`publishAt`) | OAuth 2.0 |
| **TikTok** | ~500 MB | 5 min (API) | 9:16 | Chunked PUT or Pull URL | No | OAuth 2.0 |
| **Instagram Reels** | 1 GB | 90 sec | 9:16 | URL-based container | Limited | OAuth 2.0 (via Facebook) |
| **Podcast RSS** | Host-dependent | No limit | N/A (audio) | Self-hosted file + XML | `<pubDate>` convention | N/A |

### 6.3 Platform-Specific Notes

**YouTube:**
- Include `#Shorts` in title/description for Shorts classification.
- Set `privacyStatus: "private"` with `publishAt` for scheduled publishing.
- Quota: 10,000 units/day; `videos.insert` costs 1,600 units (~6 uploads/day on default quota).
- Shorts description includes link to longform video.

**TikTok:**
- Caption limited to 2,200 UTF-16 characters.
- Upload URL expires after 1 hour; chunk size: 5-64 MB per chunk.
- "Full episode" link in bio or first comment.

**Instagram Reels:**
- Container-based flow: `POST /{user-id}/media` → poll status → `POST /{user-id}/media_publish`.
- Video **must** be at a public URL; no direct binary upload.
- "Link in bio" CTA pointing to longform.

**Podcast RSS:**
- RSS 2.0 with iTunes/Apple namespace extensions.
- Audio format: MP3 (128 kbps mono) or M4A (AAC).
- `<guid>` must never change once published.

---

## 7. Video Processing Reference

### 7.1 Encoding Presets

**Longform (16:9 1080p30) — speaker zoom (L or R):**
```bash
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -vf "crop=iw/2:ih:0:0,crop=iw:iw*9/16:(ih-iw*9/16)/2:0,scale=1920:1080:flags=lanczos" \
  -c:v libx264 -crf 18 -preset medium -profile:v high \
  -g 60 -keyint_min 60 -pix_fmt yuv420p \
  -c:a aac -b:a 192k -ar 48000 \
  -movflags +faststart \
  segment_L.mp4
```

**Longform (16:9 1080p30) — wide BOTH:**
```bash
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -c:v libx264 -crf 18 -preset medium -profile:v high \
  -g 60 -keyint_min 60 -pix_fmt yuv420p \
  -c:a aac -b:a 192k -ar 48000 \
  -movflags +faststart \
  segment_BOTH.mp4
```

**Shorts (9:16 1080×1920) — single speaker:**
```bash
# Left speaker: crop left half, take center 9:16 slice, scale up
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -vf "crop=iw/2:ih:0:0,crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos" \
  -c:v libx264 -crf 20 -preset medium -profile:v high \
  -g 60 -pix_fmt yuv420p \
  -c:a aac -b:a 128k -ar 48000 \
  -movflags +faststart \
  short_speaker.mp4
```

**Shorts (9:16 1080×1920) — BOTH speakers:**
```bash
# Center crop full frame to 9:16, scale up
ffmpeg -i source.mp4 -ss <start> -to <end> \
  -vf "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos" \
  -c:v libx264 -crf 20 -preset medium -profile:v high \
  -g 60 -pix_fmt yuv420p \
  -c:a aac -b:a 128k -ar 48000 \
  -movflags +faststart \
  short_both.mp4
```

### 7.2 Crop Math (No Distortion)

Starting frame: 1920×1080.

| Render | Step 1 | Step 2 | Step 3 | Final |
|--------|--------|--------|--------|-------|
| Longform L | Crop left half: 960×1080 | Crop to 16:9: 960×540 (centered) | Scale: 1920×1080 | 16:9, clean zoom |
| Longform R | Crop right half: 960×1080 | Crop to 16:9: 960×540 (centered) | Scale: 1920×1080 | 16:9, clean zoom |
| Longform BOTH | No crop | — | — | 1920×1080 pass-through |
| Short L | Crop left half: 960×1080 | Crop center 9:16: 608×1080 | Scale: 1080×1920 | 9:16, no distortion |
| Short R | Crop right half: 960×1080 | Crop center 9:16: 608×1080 | Scale: 1080×1920 | 9:16, no distortion |
| Short BOTH | Crop center 9:16: 608×1080 | Scale: 1080×1920 | — | 9:16, center of wide frame |

### 7.3 Hardware Acceleration (Apple Silicon)

**VideoToolbox encoding** (5-8x faster, slightly lower quality):
```bash
ffmpeg -i input.mp4 \
  -c:v h264_videotoolbox -profile:v high -q:v 65 \
  -g 60 -pix_fmt yuv420p \
  -c:a aac -b:a 192k -ar 48000 \
  -movflags +faststart output.mp4
```

Suitable for draft/preview renders; use `libx264` for final output.

### 7.4 Performance Estimates (1-Hour Source, Mac Mini)

| Task | M2 | M4 |
|------|-----|-----|
| Concat demuxer (stream copy) | ~15 sec | ~15 sec |
| `libx264 -crf 18 -preset medium` (1080p) | ~40 min | ~25 min |
| 10 shorts (30-90s each, libx264) | ~8 min | ~5 min |
| Deepgram transcription (API) | ~2-3 min | ~2-3 min |
| **Full pipeline estimate** | ~2-3 hr | ~1-1.5 hr |

---

## 8. Transcription & Diarization Reference

### 8.1 Deepgram Nova-3

**Provider:** Deepgram. **Model:** Nova-3. **Cost:** ~$0.0043/min (~$0.26/hr for standard, ~$0.46/hr with diarization).

**Single API call** handles transcription AND diarization. No separate tools, no local model management.

**Python SDK usage:**
```python
from deepgram import DeepgramClient, PrerecordedOptions, FileSource

deepgram = DeepgramClient(api_key=os.getenv("DEEPGRAM_API_KEY"))

with open("source_merged.mp4", "rb") as audio:
    payload: FileSource = {"buffer": audio.read()}
    options = PrerecordedOptions(
        model="nova-3",
        language="en",
        smart_format=True,
        diarize=True,
        utterances=True,
        punctuate=True,
    )
    response = deepgram.listen.rest.v("1").transcribe_file(payload, options)
```

**Features used:**
- `diarize=True`: Speaker labels (Speaker 0, Speaker 1, etc.)
- `utterances=True`: Groups words into natural utterances
- `smart_format=True`: Numbers, dates, etc. formatted naturally
- Word-level timestamps returned by default

### 8.2 SRT Generation

Using `deepgram-python-captions`:
```python
from deepgram_captions import DeepgramConverter, srt

converter = DeepgramConverter(response)
captions_srt = srt(converter)

with open("transcript.srt", "w") as f:
    f.write(captions_srt)
```

### 8.3 Diarized Transcript Output Format

```json
{
  "segments": [
    {
      "start": 0.0,
      "end": 4.2,
      "speaker": "SPEAKER_00",
      "text": "Welcome to the show. Today we're talking about...",
      "words": [
        {"word": "Welcome", "start": 0.0, "end": 0.4},
        {"word": "to", "start": 0.4, "end": 0.5},
        {"word": "the", "start": 0.5, "end": 0.6},
        {"word": "show.", "start": 0.6, "end": 0.9}
      ]
    }
  ],
  "speakers": {
    "SPEAKER_00": {"label": "L", "total_seconds": 1823.4},
    "SPEAKER_01": {"label": "R", "total_seconds": 1871.2}
  }
}
```

### 8.4 Voice Activity Detection: Silero VAD

Used for boundary snapping (clean clip cuts), not transcription.

```python
from silero_vad import load_silero_vad, read_audio, get_speech_timestamps

model = load_silero_vad()
audio = read_audio("audio_mono.wav")
speech_timestamps = get_speech_timestamps(audio, model,
    threshold=0.5, min_speech_duration_ms=250,
    min_silence_duration_ms=100, return_seconds=True
)
```

Performance: ~15 seconds for 1 hour of audio on CPU.

---

## 9. Clip Mining & Scoring

### 9.1 Scoring Signals

| Signal | Weight (default) | Description |
|--------|---------|-------------|
| LLM virality score | 30% | Claude Opus 4.6 rates clip potential from transcript context |
| Engagement prediction | 20% | Feature-based scoring (hook quality, emotional arc, resolution) |
| Quotability | 12% | Sentiment intensity, rhetorical patterns, specificity |
| Audio energy | 8% | RMS energy peaks, spectral brightness, pitch variance |
| Speaker dynamics | 8% | Turn rate, rapid exchanges, interruptions |
| Topic coherence | 7% | Self-contained topic within clip boundaries |
| Vocal emphasis | 5% | Local energy/pitch vs speaker baseline |
| Laughter detection | 4% | Rhythmic energy bursts with high zero-crossing rate |
| Q&A pairs | 3% | Question from one speaker, substantive answer from another |
| Boundary quality | 3% | Clean silence/pause at clip start and end |

Weights are adjusted by the analytics feedback loop over time (see Section 19).

### 9.2 LLM Clip Identification (Claude Opus 4.6)

**Model:** `claude-opus-4-6`
**Temperature:** 0.3

**Transcript format for LLM:**
```
Episode: "Building a Startup in 2026"
Speakers: Sam (L), Alex (R)
Duration: 58:23

[00:00] SAM: Welcome to the show. Today we're talking about...
[00:30] ALEX: Thanks for having me. So the first thing I want to say is...
```

**Prompt structure:**
```
System: You are a viral short-form content expert. Identify the most
compelling 30-90 second segments from this podcast transcript.

A great clip has:
- A strong hook in the first 5 seconds
- Self-contained meaning (makes sense without context)
- Emotional arc (setup → tension → resolution or insight)
- Shareability (viewer wants to tag someone or rewatch)

User: [transcript + context from clipping_agent_context.json]

Find the 10 best clips. For each, provide start_seconds, end_seconds,
title, hook_text, compelling_reason, and virality_score (1-10).
```

Use Claude's structured output (JSON schema) for reliable parsing.

### 9.3 Boundary Snapping

1. Use Silero VAD silence detection output.
2. Find the nearest silence/pause point within 3 seconds of the LLM-suggested start/end.
3. Snap to that silence point for clean cuts.
4. Ensure final duration is between 30-90 seconds.

### 9.4 Viral Hook Patterns

| Pattern | Example |
|---------|---------|
| Hidden knowledge | "What nobody tells you about..." |
| Data surprise | "97% of founders make this mistake..." |
| Vulnerability | "I lost everything when..." |
| Contrarian | "Stop doing X. It doesn't work." |
| Story hook | "Three years ago, something happened..." |
| Direct address | "If you're a [role], you need to hear this" |
| Superlative | "The single biggest mistake I ever made..." |

---

## 10. Scheduling & Analytics

### 10.1 Default Schedule: 1/day weekdays, 2/day weekends

```
Mon: 1 clip    Tue: 1 clip    Wed: 1 clip    Thu: 1 clip
Fri: 2 clips   Sat: 2 clips   Sun: 2 clips
Total: 10 clips over 7 days
```

Peak days are **Friday, Saturday, Sunday** — fixed, not configurable.

### 10.2 Platform-Specific Posting Windows

| Platform | Best Hours (local) | Best Days | Max Posts/Day |
|----------|--------------------|-----------|---------------|
| YouTube Shorts | 2-4 PM, 8-11 PM | Fri, Sat, Sun | 2 |
| TikTok | 1-10 PM | Wed, Thu, Fri | 3 |
| Instagram Reels | 7-11 AM, 7-9 PM | Tue, Wed, Thu | 1 |

### 10.3 Analytics Collection

**YouTube Analytics API:** views, estimatedMinutesWatched, averageViewDuration, likes, comments, shares. Audience retention curve.

**TikTok Analytics:** video_views, likes, comments, shares, average_watch_time.

**Instagram Insights:** reach, plays, likes, comments, shares.

**Engagement Score Formula:**
```
score = (0.3 * normalized_views) + (0.25 * watch_time_ratio) +
        (0.2 * like_rate) + (0.15 * comment_rate) + (0.1 * share_rate)
```

### 10.4 Thompson Sampling Scheduler (Future)

Each (platform, day_of_week, hour) tuple is a bandit arm with `Beta(successes + 1, failures + 1)`. Natural exploration/exploitation balance.

---

## 11. File/Folder Layout

```
/Users/samuellarson/local/podcast/          # Project umbrella
  GitHub/
    distil/                                 # Git repo — all code
      frontend/                             # Single-page web app
      server/                               # FastAPI backend
      config/                               # Default configuration
      start.sh                              # One-command startup
      requirements.txt
      SPEC.md                               # This file
      CLIP_MINING_RESEARCH.md
  # Future: media/, backup links, etc.

# Runtime directories (created by start.sh, gitignored):
distil/
  output/
    episodes/<episode_id>/
      source_merged.mp4
      longform.mp4
      shorts/
        clip_01.mp4 ... clip_10.mp4
      subtitles/
        transcript.srt
        transcript.json
        diarized_transcript.json
        clip_01.srt ...
      metadata/
        metadata.json
        schedule.json
      qa/
        qa.json
      ingest.json
      stitch.json
      audio_analysis.json
      segments.json
      clips.json
      approval_batch.json
      publish_log.json
      episode.json
      clip_context.json                     # Per-episode context
  work/
    temp_audio/
    temp_video/
  data/
    analytics.json
    clipping_agent_context.json             # Persistent clipping preferences
    scheduling_agent_context.json           # Persistent scheduling preferences
    scoring_weights.json
  logs/
    pipeline.log
    episodes/<episode_id>/agent_logs/
```

---

## 12. Configuration Spec

### config.toml

```toml
[project]
name = "distil"
version = "0.1.0"

[paths]
ingest_dir = "~/podcast_ingest"
output_dir = "./output"
work_dir = "./work"
backup_dir = "/Volumes/Backup/podcast"
public_media_dir = ""

[processing]
frame_seconds = 0.1
speech_db_margin = 12
min_segment_seconds = 2.0
max_channel_correlation = 0.95
max_channel_rms_ratio_delta = 3.0
clip_min_seconds = 30
clip_max_seconds = 90
clip_count = 10
output_resolution = "1920x1080"
shorts_resolution = "1080x1920"
video_crf = 18
shorts_crf = 20
audio_bitrate = "192k"
shorts_audio_bitrate = "128k"
use_hardware_accel = false

[transcription]
provider = "deepgram"
model = "nova-3"
language = "en"
diarize = true
smart_format = true
utterances = true

[clip_mining]
method = "llm"
llm_model = "claude-opus-4-6"
llm_temperature = 0.3
boundary_snap_tolerance_seconds = 3.0

[schedule]
shorts_per_day_weekday = 1
shorts_per_day_weekend = 2
peak_days = ["friday", "saturday", "sunday"]
longform_delay_days = 0
timezone = "America/Los_Angeles"

[automation]
cron_enabled = false
sd_card_mount = "/Volumes/Untitled"
auto_backup = true
cleanup_after_publish = true
cleanup_grace_days = 7

[platforms.youtube]
enabled = true
require_approval = true
include_shorts_hashtag = true
link_to_longform = true

[platforms.tiktok]
enabled = true
require_approval = true
max_caption_chars = 2200
link_to_longform = true

[platforms.instagram]
enabled = true
require_approval = true
requires_public_url = true
link_to_longform = true

[platforms.podcast_rss]
enabled = true
feed_url = ""
audio_format = "mp3"
audio_bitrate = "128k"
artwork_path = ""
```

---

## 13. JSON Schemas

### episode.json (Master Manifest)

```json
{
  "episode_id": "ep_2026-02-12_001",
  "title": "Building a Startup in 2026",
  "status": "ready_for_review",
  "source": "source_merged.mp4",
  "longform": "longform.mp4",
  "duration_seconds": 3498.7,
  "created_at": "2026-02-12T15:00:00Z",
  "segments": [
    {"start": 0.0, "end": 12.4, "speaker": "L"},
    {"start": 12.4, "end": 18.9, "speaker": "BOTH"},
    {"start": 18.9, "end": 25.1, "speaker": "R"}
  ],
  "clips": [
    {
      "id": "clip_01",
      "rank": 1,
      "start": 423.5,
      "end": 487.2,
      "duration": 63.7,
      "title": "Why most founders get pricing wrong",
      "hook_text": "Here's the thing nobody tells you about pricing...",
      "virality_score": 8.5,
      "speaker": "L",
      "status": "pending",
      "file": "shorts/clip_01.mp4",
      "metadata": {
        "youtube": {
          "title": "Why Most Founders Get Pricing Wrong #Shorts",
          "description": "Here's the thing nobody tells you... Full episode: https://youtube.com/watch?v=xxx"
        },
        "tiktok": {
          "caption": "Why most founders get pricing wrong 💰 #startup #pricing #founders"
        },
        "instagram": {
          "caption": "Why most founders get pricing wrong 💰 Link in bio for the full episode! #startup #pricing"
        }
      }
    }
  ],
  "metadata": {
    "longform": {
      "title": "Building a Startup in 2026 | Full Episode",
      "description": "Sam and Alex discuss..."
    }
  },
  "schedule": {
    "longform_publish_at": "2026-02-13T14:00:00-08:00",
    "clips": [
      {"id": "clip_01", "publish_at": "2026-02-14T14:00:00-08:00", "platforms": ["youtube", "tiktok", "instagram"]},
      {"id": "clip_02", "publish_at": "2026-02-15T10:00:00-08:00", "platforms": ["youtube", "tiktok", "instagram"]}
    ]
  },
  "pipeline": {
    "started_at": "2026-02-12T15:00:00Z",
    "completed_at": "2026-02-12T16:47:00Z",
    "agents_completed": ["ingest", "stitch", "audio_analysis", "speaker_cut", "transcription", "longform_render", "clip_miner", "shorts_render", "metadata", "qa"]
  }
}
```

---

## 14. QA Rules

### Hard Failures (block pipeline)

| Rule | Condition | Action |
|------|-----------|--------|
| Source corrupt | `ffprobe` returns error | Abort pipeline |
| Zero duration | Source duration < 60 seconds | Abort pipeline |
| No audio | Audio stream missing or silent | Abort pipeline |
| Render failure | Any FFmpeg exits non-zero | Abort pipeline |

### Soft Failures (flag in QA, continue)

| Rule | Condition | Action |
|------|-----------|--------|
| `audio_not_stereo` | Channels < 2 | Disable speaker cuts; full-frame longform |
| `audio_channels_identical` | Correlation > 0.95 and RMS < 3 dB | Disable speaker cuts; full-frame longform |
| `clip_boundary_imprecise` | No silence within snap tolerance | Use LLM boundary; warn |
| `clip_duration_out_of_range` | Clip < 30s or > 90s | Adjust or flag |
| `low_virality_score` | All clips < 4/10 | Warn |
| `metadata_truncated` | Exceeds platform limit | Auto-truncate; warn |

---

## 15. Security & Credentials

- Store API keys in `.env` file (in `.gitignore`, `0600` permissions).
- macOS Keychain as optional upgrade path.
- Never log secrets. Mask in output.
- OAuth flows for YouTube, TikTok, Instagram.
- Token refresh: check before each API call, refresh proactively.

---

## 16. Testing Strategy

### Unit Tests
- Audio analysis with known audio files.
- Segment merging debounce edge cases.
- Clip boundary snapping.
- Schedule generation (Mon-Thu × 1, Fri-Sun × 2).

### Integration Tests
- Concat merge, speaker crop render, shorts render.
- Full pipeline against a 5-minute test episode.
- Deepgram API call with test audio.

### Manual QA Checklist
- [ ] Longform: speaker cuts smooth, BOTH segments show full wide frame, no distortion.
- [ ] Each short: correct speaker framing, no distortion, subtitles readable.
- [ ] Metadata reads well on each platform.
- [ ] Schedule dates are Mon(1) Tue(1) Wed(1) Thu(1) Fri(2) Sat(2) Sun(2).

---

## 17. Mac Mini Autonomous Workflow

### Overview

The Mac Mini handles the full lifecycle autonomously via cron:
1. **Detect** — watch for SD card mount or new files in ingest folder.
2. **Ingest** — copy video from SD card to local SSD working directory.
3. **Process** — run the full pipeline (stitch → transcribe → mine → render → metadata → QA).
4. **Review** — present clips in the web UI, wait for user approval.
5. **Publish** — on schedule, push approved clips to platforms.
6. **Backup** — copy processed episode data to external HDD.
7. **Cleanup** — after all clips for an episode are published and grace period passes, remove source files and intermediates from SSD. Keep only the final rendered clips and metadata needed for the week's deployments.

### Cron Setup

```bash
# Check for new media every 15 minutes
*/15 * * * * /Users/samuellarson/local/podcast/GitHub/distil/scripts/check_ingest.sh

# Run publisher queue every hour
0 * * * * /Users/samuellarson/local/podcast/GitHub/distil/scripts/publish_queue.sh

# Weekly analytics collection (Sunday midnight)
0 0 * * 0 /Users/samuellarson/local/podcast/GitHub/distil/scripts/collect_analytics.sh

# Daily backup to external HDD
0 2 * * * /Users/samuellarson/local/podcast/GitHub/distil/scripts/backup.sh

# Daily cleanup of old episodes
0 3 * * * /Users/samuellarson/local/podcast/GitHub/distil/scripts/cleanup.sh
```

### SD Card Detection

```bash
# check_ingest.sh
SD_MOUNT="/Volumes/Untitled"
INGEST_DIR="$HOME/podcast_ingest"

if [ -d "$SD_MOUNT" ] && ls "$SD_MOUNT"/*.mp4 "$SD_MOUNT"/*.mov 2>/dev/null; then
    cp "$SD_MOUNT"/*.mp4 "$SD_MOUNT"/*.mov "$INGEST_DIR/" 2>/dev/null
    # Trigger pipeline for new files
fi
```

### Backup Strategy

- After pipeline completes, `rsync` the episode directory to external HDD.
- External HDD serves as the long-term archive.
- SSD keeps only what's needed for the current week's deployments.

### Cleanup Rules

- After all clips for an episode are published:
  - Wait `cleanup_grace_days` (default: 7 days).
  - Remove `source_merged.mp4`, `work/` intermediates.
  - Keep: rendered clips, metadata, publish logs (small footprint).
  - The full episode including source is on the external HDD backup.

---

## 18. Interactive Clip Review UI

### Architecture

FastAPI (Python) backend + single-page vanilla HTML/CSS/JS frontend. No build step, no Node.js. Tailwind CSS via CDN. Served from `start.sh` on port 8420.

### API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/episodes` | List all episodes |
| `POST` | `/api/episodes` | Trigger new episode ingest |
| `GET` | `/api/episodes/{id}` | Episode detail + all clip data |
| `GET` | `/api/episodes/{id}/clips` | All clip candidates |
| `GET` | `/api/episodes/{id}/clips/{clipId}` | Single clip detail |
| `POST` | `/api/episodes/{id}/clips/{clipId}/approve` | Approve clip |
| `POST` | `/api/episodes/{id}/clips/{clipId}/reject` | Reject clip |
| `POST` | `/api/episodes/{id}/clips/{clipId}/alternative` | Request Claude replacement |
| `POST` | `/api/episodes/{id}/clips/manual` | Add custom clip by timestamp |
| `PATCH` | `/api/episodes/{id}/clips/{clipId}/metadata` | Edit clip metadata |
| `POST` | `/api/episodes/{id}/approve` | Approve entire batch |
| `GET` | `/api/schedule` | Full publish schedule |
| `GET` | `/api/analytics` | Analytics dashboard data |

### Frontend Views

1. **Dashboard** — episode list with status badges, pipeline progress, upcoming schedule sidebar.
2. **Episode Detail** — longform video preview, grid of 10 clip cards with scores/status, metadata editor, "Approve All" button.
3. **Clip Review** — large video player, transcript excerpt, per-platform metadata tabs (YouTube/TikTok/Instagram), approve/reject/request alternative buttons, time range editor, custom clip entry.
4. **Schedule** — week calendar grid, color-coded by platform, showing 1/day Mon-Thu and 2/day Fri-Sun.
5. **Analytics** — per-clip performance table, scoring weight visualization, feedback loop status.

---

## 19. Feedback Loop & Persistent Context

### Per-Episode Context (`clip_context.json`)

Saved per episode after processing:
```json
{
  "episode_id": "ep_2026-02-12_001",
  "captions_used": ["clip_01.srt", "clip_02.srt"],
  "keywords": ["pricing", "startup", "product-market fit"],
  "titles": ["Why most founders get pricing wrong", "..."],
  "hashtags": ["#startup", "#pricing", "#founders"],
  "posting_times": {"clip_01": "2026-02-14T14:00:00", "clip_02": "..."},
  "performance": {}
}
```

### Clipping Agent Context (`data/clipping_agent_context.json`)

Persistent across episodes. Updated by analytics agent:
```json
{
  "last_updated": "2026-02-20T00:00:00Z",
  "episodes_analyzed": 5,
  "preferred_clip_length": {"min": 35, "max": 75, "sweet_spot": 55},
  "high_engagement_features": [
    "contrarian hooks",
    "specific tactical advice",
    "personal vulnerability stories"
  ],
  "successful_hook_patterns": ["Hidden knowledge", "Data surprise"],
  "topics_that_perform_well": ["pricing", "hiring", "fundraising"],
  "scoring_weight_adjustments": {
    "llm_virality": 0.32,
    "quotability": 0.14
  }
}
```

### Scheduling Agent Context (`data/scheduling_agent_context.json`)

```json
{
  "last_updated": "2026-02-20T00:00:00Z",
  "best_posting_times": {
    "youtube": {"best_hour": 15, "best_day": "friday"},
    "tiktok": {"best_hour": 18, "best_day": "wednesday"},
    "instagram": {"best_hour": 10, "best_day": "tuesday"}
  },
  "day_of_week_performance": {
    "monday": 0.7, "tuesday": 0.85, "wednesday": 0.9,
    "thursday": 0.88, "friday": 1.0, "saturday": 0.95, "sunday": 0.92
  },
  "frequency_effects": "no_diminishing_returns_at_2_per_day"
}
```

### Feedback Loop Process

1. Analytics agent runs weekly (Sunday cron).
2. Collects performance data from YouTube, TikTok, Instagram APIs.
3. Correlates clip features with engagement scores.
4. Updates `clipping_agent_context.json` with learned preferences.
5. Updates `scheduling_agent_context.json` with optimal posting times.
6. Adjusts `scoring_weights.json` — which signals matter most.
7. Next episode's clip mining and scheduling use the updated context.

---

## 20. Roadmap / Extensions

### Phase 1 — MVP (Current)
- Ingest + Stitch + Audio Analysis + Speaker Cut (L/R/BOTH) + Longform Render.
- Deepgram Nova-3 transcription + diarization.
- LLM clip mining with Claude Opus 4.6.
- Distortion-free shorts render.
- Interactive web UI for clip review.
- YouTube publishing (longform + Shorts).
- Cron-based Mac Mini automation.

### Phase 2 — Multi-Platform
- TikTok and Instagram Reels publishing.
- Platform-specific metadata with longform linking.
- Podcast RSS feed generation.
- Scheduled 1/day + 2/day Fri-Sun publishing.

### Phase 3 — Optimization
- Weekly analytics collection.
- Persistent context feedback loop.
- Updated scoring weights from real performance data.
- Thompson Sampling scheduler for posting times.

### Phase 4 — Advanced
- Face-tracking smart reframing (YOLOv8 + MediaPipe).
- Multi-camera support.
- Content-aware scheduling (different bandits for educational vs entertainment).
- Chapter markers for YouTube (from topic boundaries).
- Automated highlights reel.

---

## Appendix A: Quick Reference — FFmpeg Commands

| Operation | Command |
|-----------|---------|
| Probe audio | `ffprobe -v error -select_streams a -show_entries stream=channels,channel_layout -of json input.mp4` |
| Extract left channel | `ffmpeg -i input.mp4 -af "pan=mono\|c0=FL" -c:a pcm_s16le left.wav` |
| Extract right channel | `ffmpeg -i input.mp4 -af "pan=mono\|c0=FR" -c:a pcm_s16le right.wav` |
| Concat (stream copy) | `ffmpeg -f concat -safe 0 -i list.txt -c copy output.mp4` |
| Longform L zoom | `ffmpeg -i in.mp4 -vf "crop=iw/2:ih:0:0,crop=iw:iw*9/16:(ih-iw*9/16)/2:0,scale=1920:1080:flags=lanczos" out.mp4` |
| Longform R zoom | `ffmpeg -i in.mp4 -vf "crop=iw/2:ih:iw/2:0,crop=iw:iw*9/16:(ih-iw*9/16)/2:0,scale=1920:1080:flags=lanczos" out.mp4` |
| Short L (9:16) | `ffmpeg -i in.mp4 -vf "crop=iw/2:ih:0:0,crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos" out.mp4` |
| Short BOTH (9:16) | `ffmpeg -i in.mp4 -vf "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos" out.mp4` |
| Burn subtitles | `ffmpeg -i in.mp4 -vf "subtitles=subs.srt" -c:v libx264 -crf 20 -c:a copy out.mp4` |
| Add faststart | `ffmpeg -i in.mp4 -c copy -movflags +faststart out.mp4` |

## Appendix B: Platform API Quick Reference

| Platform | Upload Endpoint | Auth Scope | Scheduling Field |
|----------|----------------|------------|-----------------|
| YouTube | `POST googleapis.com/upload/youtube/v3/videos` | `youtube.upload` | `status.publishAt` |
| TikTok | `POST /v2/post/publish/video/init/` | `video.publish` | N/A (self-manage) |
| Instagram | `POST /{user-id}/media` + `POST /{user-id}/media_publish` | `instagram_content_publish` | N/A (self-manage) |
