"""Chat endpoint — AI-powered episode editing assistant."""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.crop import compute_crop, resolve_speaker
from lib.encoding import get_video_encoder_args
from lib.paths import get_episodes_dir
from lib.srt import escape_srt_path, fmt_timecode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes/{episode_id}", tags=["chat"])

EPISODES_DIR = get_episodes_dir()

# Episode context cache: {episode_id: {"ctx": dict, "mtime": float, "loaded_at": float}}
_context_cache = {}
_CACHE_TTL = 300  # 5 minutes


def _load_episode_context_cached(ep_dir: Path, episode_id: str) -> dict:
    """Load episode context with file-mtime-based caching."""
    episode_file = ep_dir / "episode.json"
    try:
        current_mtime = episode_file.stat().st_mtime
    except OSError:
        current_mtime = 0

    cached = _context_cache.get(episode_id)
    now = time.time()

    if (cached
            and cached["mtime"] == current_mtime
            and now - cached["loaded_at"] < _CACHE_TTL):
        return cached["ctx"]

    ctx = _load_episode_context(ep_dir)
    _context_cache[episode_id] = {
        "ctx": ctx,
        "mtime": current_mtime,
        "loaded_at": now,
    }
    return ctx


def _load_chat_history(ep_dir: Path) -> list:
    """Load conversation history from chat_history.json."""
    history_file = ep_dir / "chat_history.json"
    if not history_file.exists():
        return []
    try:
        with open(history_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_chat_history(ep_dir: Path, history: list):
    """Save conversation history to chat_history.json. Keep last 20 turns."""
    history_file = ep_dir / "chat_history.json"
    # Keep only last 20 message pairs (40 messages) to avoid context explosion
    if len(history) > 40:
        history = history[-40:]
    with open(history_file, "w") as f:
        json.dump(history, f, indent=2)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str
    actions_taken: List[dict]


# ---------------------------------------------------------------------------
# Helpers — episode data loading
# ---------------------------------------------------------------------------

def _episode_dir(episode_id: str) -> Path:
    ep_dir = EPISODES_DIR / episode_id
    if not ep_dir.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return ep_dir


def _load_json_safe(path: Path) -> Optional[dict]:
    """Load a JSON file if it exists, otherwise return None."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _load_episode_context(ep_dir: Path) -> dict:
    """Load all relevant episode files into a context dict."""
    ctx = {}
    ctx["episode"] = _load_json_safe(ep_dir / "episode.json")
    ctx["clips"] = _load_json_safe(ep_dir / "clips.json")
    ctx["diarized_transcript"] = _load_json_safe(ep_dir / "diarized_transcript.json")
    ctx["metadata"] = _load_json_safe(ep_dir / "metadata" / "metadata.json")
    ctx["segments"] = _load_json_safe(ep_dir / "segments.json")
    return ctx


def _load_clips(ep_dir: Path) -> tuple:
    """Load clips list and the file path. Returns (clips_list, clips_file_path)."""
    clips_file = ep_dir / "clips.json"
    if clips_file.exists():
        with open(clips_file) as f:
            data = json.load(f)
        clips = data.get("clips", data) if isinstance(data, dict) else data
        return clips, clips_file
    return [], clips_file


def _save_clips(clips: list, clips_file: Path):
    """Save clips list to clips.json."""
    clips_file.parent.mkdir(parents=True, exist_ok=True)
    with open(clips_file, "w") as f:
        json.dump({"clips": clips}, f, indent=2)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """You are Cascade, a podcast production assistant. You help editors refine their podcast episodes and short-form clips.

You have access to the following episode data:

## Episode info
{episode_json}

## Clips
{clips_json}

## Full transcript (timestamped)
{transcript_text}

## Metadata
{metadata_json}

## Speaker segments (summary)
{segments_summary}

## Available actions

You can take actions by including a JSON block in your response wrapped in ```action tags. Each action block should contain a single JSON object with an "action" field and parameters.

Available actions:

1. **update_clip_metadata** — Update a clip's title, hook_text, compelling_reason, or hashtags.
   ```action
   {{"action": "update_clip_metadata", "clip_id": "clip_01", "title": "New Title", "hook_text": "...", "compelling_reason": "...", "hashtags": ["#tag1", "#tag2"]}}
   ```

2. **update_clip_times** — Adjust a clip's start and/or end time.
   ```action
   {{"action": "update_clip_times", "clip_id": "clip_01", "start_seconds": 120.5, "end_seconds": 180.0}}
   ```

3. **add_clip** — Suggest and add a new clip from the transcript.
   ```action
   {{"action": "add_clip", "start_seconds": 300.0, "end_seconds": 360.0, "title": "...", "hook_text": "...", "compelling_reason": "...", "virality_score": 8, "speaker": "L"}}
   ```

4. **reject_clip** — Reject/remove a clip.
   ```action
   {{"action": "reject_clip", "clip_id": "clip_03"}}
   ```

5. **rerender_short** — Re-render a specific short clip video.
   ```action
   {{"action": "rerender_short", "clip_id": "clip_01"}}
   ```

6. **approve_clips** — Approve multiple clips by IDs or criteria (e.g. minimum score).
   ```action
   {{"action": "approve_clips", "clip_ids": ["clip_01", "clip_02"]}}
   ```
   Or by score threshold:
   ```action
   {{"action": "approve_clips", "min_score": 8}}
   ```

7. **reject_clips** — Reject multiple clips by IDs or criteria.
   ```action
   {{"action": "reject_clips", "clip_ids": ["clip_04", "clip_05"]}}
   ```
   Or by score threshold:
   ```action
   {{"action": "reject_clips", "max_score": 4}}
   ```

8. **update_platform_metadata** — Update a specific platform's metadata for a clip. Supported platforms: youtube, tiktok, instagram, linkedin, x, facebook, threads, pinterest, bluesky.
   ```action
   {{"action": "update_platform_metadata", "clip_id": "clip_01", "platform": "tiktok", "caption": "New caption", "hashtags": ["#tag1"]}}
   ```

9. **delete_clip** — Permanently remove a clip.
   ```action
   {{"action": "delete_clip", "clip_id": "clip_03"}}
   ```

10. **update_longform_metadata** — Update the longform episode title, description, and/or tags.
   ```action
   {{"action": "update_longform_metadata", "title": "...", "description": "...", "tags": ["tag1", "tag2"]}}
   ```

11. **update_episode_info** — Update episode info fields (guest_name, guest_title, episode_name, episode_description).
   ```action
   {{"action": "update_episode_info", "guest_name": "...", "guest_title": "...", "episode_name": "...", "episode_description": "..."}}
   ```

12. **edit_longform** — Cut or trim sections from the longform video. Use the transcript to find precise timestamps.
   To cut out a section (e.g., a break, dead time, off-topic tangent):
   ```action
   {{"action": "edit_longform", "type": "cut", "start_seconds": 1234.5, "end_seconds": 1289.3, "reason": "Parking payment break"}}
   ```
   To set where the longform should start (e.g., "start when the guest introduces themselves"):
   ```action
   {{"action": "edit_longform", "type": "trim_start", "seconds": 45.2, "reason": "Start at guest introduction"}}
   ```
   To set where the longform should end:
   ```action
   {{"action": "edit_longform", "type": "trim_end", "seconds": 3600.0, "reason": "End after final question"}}
   ```

13. **rerender_longform** — Re-render the longform video (e.g., after making edits).
   ```action
   {{"action": "rerender_longform"}}
   ```

14. **auto_trim** — Automatically detect and trim fluff from the start and end of the episode.
   Analyzes the transcript to find where the real conversation begins (after setup, mic checks, greetings)
   and where it ends (before goodbyes, wrap-up). Creates trim_start and trim_end edits automatically.
   ```action
   {{"action": "auto_trim"}}
   ```

## Platform metadata schema
Each clip can have per-platform metadata. The supported platforms and their fields:
- **youtube**: title, description
- **tiktok**: caption, hashtags
- **instagram**: caption, hashtags
- **linkedin**: title, description
- **x**: text (max 280 chars)
- **facebook**: title, description
- **threads**: text
- **pinterest**: title, description
- **bluesky**: text

## Guidelines

- You have the FULL transcript above with timestamps. Use it to find clips with precise start/end times.
- When suggesting new clips, cite the exact timestamps from the transcript.
- Good clips have: a strong hook in the first 5 seconds, 30-90 second duration, a complete micro-story or insight, and emotional engagement.
- Respond conversationally and confirm what you changed.
- When the user asks you to edit clips, include the appropriate action blocks.
- You can include multiple action blocks in a single response.
- Always explain your reasoning when making changes.
- If the user asks a question, answer it based on the episode data without taking actions.
- When approving or rejecting multiple clips, use the bulk actions (approve_clips/reject_clips) instead of individual actions.
- For longform editing: search the transcript carefully for the exact moments the user describes. Find where they mention stopping (e.g. "gotta pay for parking") and where they resume (next question or topic). Use precise timestamps from the transcript for cuts.
- Multiple edits can be stacked — each cut/trim is stored and applied in order during re-render.
- After making longform edits, suggest running rerender_longform to apply them.
- When asked to auto-trim, clean up, or edit the episode, analyze the transcript to find: (1) where the actual substantive conversation begins (skip mic checks, "are we rolling?", casual pre-show chatter, "let me get settled"), and (2) where the conversation actually ends (skip "thanks for coming", "okay we're done", casual post-show chatter). Use edit_longform trim_start/trim_end with precise timestamps. You can also use the auto_trim action to have AI automatically detect these points.
- If this is the first message in the conversation and there are no existing longform_edits, proactively offer to auto-trim the episode and identify any sections that should be cut (breaks, technical issues, off-topic tangents).
- When the user describes a section to cut (e.g. "we took a break to deal with parking"), search the transcript thoroughly for that moment, find the exact start and end timestamps, and use edit_longform with type "cut".
"""


_MAX_TRANSCRIPT_CHARS = 320_000  # ~80K tokens at ~4 chars/token


def _speaker_label(speaker_id, speaker_map: list | None = None) -> str:
    """Map a speaker integer to a display label.

    Uses speaker_map from diarized_transcript.json when available (multichannel
    mode gives "Speaker 0", "Speaker 1", etc.).  Falls back to "L"/"R" for
    legacy 2-speaker episodes without a speaker_map.
    """
    if not isinstance(speaker_id, int):
        return str(speaker_id)
    if speaker_map:
        for entry in speaker_map:
            if entry.get("index") == speaker_id:
                return entry.get("label", f"Speaker {speaker_id}")
    # Legacy fallback
    return "L" if speaker_id == 0 else "R"


def _format_transcript_text(diarized: Optional[dict]) -> str:
    """Format the full diarized transcript as compact timestamped text lines.

    Format: [0.0s - 3.5s] Speaker 0: Welcome...
    If the transcript exceeds ~80K tokens, truncate from the middle.
    """
    if not diarized:
        return "No transcript available."

    utterances = diarized.get("utterances", [])
    if not utterances:
        return "No transcript available."

    speaker_map = diarized.get("speaker_map")

    lines = []
    for utt in utterances:
        start = utt.get("start", 0)
        end = utt.get("end", 0)
        speaker = utt.get("speaker", utt.get("channel", "?"))
        label = _speaker_label(speaker, speaker_map)
        text = utt.get("text", "").strip()
        if text:
            lines.append(f"[{start:.1f}s - {end:.1f}s] {label}: {text}")

    full_text = "\n".join(lines)

    if len(full_text) <= _MAX_TRANSCRIPT_CHARS:
        return full_text

    # Truncate from the middle, keeping first and last halves
    half = _MAX_TRANSCRIPT_CHARS // 2
    first_half = full_text[:half]
    second_half = full_text[-half:]
    # Trim to line boundaries
    first_half = first_half[:first_half.rfind("\n")]
    second_half = second_half[second_half.find("\n") + 1:]
    omitted = len(lines) - first_half.count("\n") - second_half.count("\n") - 2
    return f"{first_half}\n\n... [{omitted} utterances omitted for length] ...\n\n{second_half}"


def _build_system_prompt(ctx: dict) -> str:
    """Build the system prompt with episode context."""
    # Episode JSON (full)
    episode_json = json.dumps(ctx.get("episode") or {}, indent=2)

    # Clips JSON (full)
    clips_data = ctx.get("clips")
    if clips_data:
        clips_list = clips_data.get("clips", clips_data) if isinstance(clips_data, dict) else clips_data
        clips_json = json.dumps(clips_list, indent=2)
    else:
        clips_json = "No clips data available."

    # Full transcript as compact text lines
    transcript_text = _format_transcript_text(ctx.get("diarized_transcript"))

    # Metadata
    metadata_json = json.dumps(ctx.get("metadata") or {}, indent=2)

    # Segments summary
    segments_data = ctx.get("segments")
    if segments_data:
        segs = segments_data.get("segments", [])
        segments_summary = f"{len(segs)} speaker segments. First few: {json.dumps(segs[:5], indent=2)}"
    else:
        segments_summary = "No segments data available."

    return SYSTEM_PROMPT_TEMPLATE.format(
        episode_json=episode_json,
        clips_json=clips_json,
        transcript_text=transcript_text,
        metadata_json=metadata_json,
        segments_summary=segments_summary,
    )


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def _execute_action(action: dict, ep_dir: Path) -> dict:
    """Execute a single action and return a result dict."""
    action_type = action.get("action")

    if action_type == "update_clip_metadata":
        return _action_update_clip_metadata(action, ep_dir)
    elif action_type == "update_clip_times":
        return _action_update_clip_times(action, ep_dir)
    elif action_type == "add_clip":
        return _action_add_clip(action, ep_dir)
    elif action_type == "reject_clip":
        return _action_reject_clip(action, ep_dir)
    elif action_type == "rerender_short":
        return _action_rerender_short(action, ep_dir)
    elif action_type == "approve_clips":
        return _action_approve_clips(action, ep_dir)
    elif action_type == "reject_clips":
        return _action_reject_clips(action, ep_dir)
    elif action_type == "update_platform_metadata":
        return _action_update_platform_metadata(action, ep_dir)
    elif action_type == "delete_clip":
        return _action_delete_clip(action, ep_dir)
    elif action_type == "update_longform_metadata":
        return _action_update_longform_metadata(action, ep_dir)
    elif action_type == "update_episode_info":
        return _action_update_episode_info(action, ep_dir)
    elif action_type == "edit_longform":
        return _action_edit_longform(action, ep_dir)
    elif action_type == "rerender_longform":
        return _action_rerender_longform(action, ep_dir)
    elif action_type == "auto_trim":
        return _action_auto_trim(action, ep_dir)
    else:
        return {"action": action_type, "status": "error", "detail": f"Unknown action: {action_type}"}


def _action_update_clip_metadata(action: dict, ep_dir: Path) -> dict:
    clip_id = action.get("clip_id")
    if not clip_id:
        return {"action": "update_clip_metadata", "status": "error", "detail": "Missing clip_id"}

    clips, clips_file = _load_clips(ep_dir)
    for i, clip in enumerate(clips):
        if clip.get("id") == clip_id:
            for field in ("title", "hook_text", "compelling_reason", "hashtags"):
                if field in action:
                    clip[field] = action[field]
            clips[i] = clip
            _save_clips(clips, clips_file)
            return {"action": "update_clip_metadata", "status": "ok", "clip_id": clip_id}

    return {"action": "update_clip_metadata", "status": "error", "detail": f"Clip {clip_id} not found"}


def _action_update_clip_times(action: dict, ep_dir: Path) -> dict:
    clip_id = action.get("clip_id")
    if not clip_id:
        return {"action": "update_clip_times", "status": "error", "detail": "Missing clip_id"}

    clips, clips_file = _load_clips(ep_dir)
    for i, clip in enumerate(clips):
        if clip.get("id") == clip_id:
            if "start_seconds" in action:
                clip["start_seconds"] = action["start_seconds"]
                clip["start"] = action["start_seconds"]
            if "end_seconds" in action:
                clip["end_seconds"] = action["end_seconds"]
                clip["end"] = action["end_seconds"]
            clip["duration"] = clip.get("end_seconds", clip.get("end", 0)) - clip.get("start_seconds", clip.get("start", 0))
            clips[i] = clip
            _save_clips(clips, clips_file)
            return {"action": "update_clip_times", "status": "ok", "clip_id": clip_id}

    return {"action": "update_clip_times", "status": "error", "detail": f"Clip {clip_id} not found"}


def _action_add_clip(action: dict, ep_dir: Path) -> dict:
    start = action.get("start_seconds")
    end = action.get("end_seconds")
    if start is None or end is None:
        return {"action": "add_clip", "status": "error", "detail": "Missing start_seconds or end_seconds"}
    if end <= start:
        return {"action": "add_clip", "status": "error", "detail": "end_seconds must be > start_seconds"}

    clips, clips_file = _load_clips(ep_dir)

    # Generate clip ID
    existing_ids = {c.get("id", "") for c in clips}
    clip_num = len(clips) + 1
    while f"clip_{clip_num:02d}" in existing_ids:
        clip_num += 1
    clip_id = f"clip_{clip_num:02d}"

    new_clip = {
        "id": clip_id,
        "rank": len(clips) + 1,
        "start_seconds": start,
        "end_seconds": end,
        "start": start,
        "end": end,
        "duration": end - start,
        "title": action.get("title", "Untitled clip"),
        "hook_text": action.get("hook_text", ""),
        "compelling_reason": action.get("compelling_reason", "Suggested by AI assistant"),
        "virality_score": action.get("virality_score", 0),
        "speaker": action.get("speaker", "BOTH"),
        "status": "pending",
    }

    clips.append(new_clip)
    _save_clips(clips, clips_file)

    # Auto-generate subtitles and render the short
    render_result = _auto_render_new_clip(clip_id, start, end, ep_dir)

    return {"action": "add_clip", "status": "ok", "clip_id": clip_id, "render": render_result}


def _generate_clip_srt(ep_dir: Path, clip_id: str, start: float, end: float):
    """Generate SRT subtitles for a clip from diarized transcript word timings."""
    diarized = _load_json_safe(ep_dir / "diarized_transcript.json")
    if not diarized:
        return False

    words = []
    for utt in diarized.get("utterances", []):
        for w in utt.get("words", []):
            w_start = w.get("start", 0)
            w_end = w.get("end", 0)
            if w_start >= start and w_end <= end:
                words.append(w)

    if not words:
        return False

    # Group into ~4-word subtitle blocks, offset times to clip-relative
    srt_lines = []
    idx = 1
    i = 0
    while i < len(words):
        chunk = words[i : i + 4]
        t_start = chunk[0]["start"] - start
        t_end = chunk[-1]["end"] - start
        text = " ".join(w.get("word", "") for w in chunk)

        srt_lines.append(
            f"{idx}\n"
            f"{fmt_timecode(t_start)} --> {fmt_timecode(t_end)}\n"
            f"{text}\n"
        )
        idx += 1
        i += 4

    srt_dir = ep_dir / "subtitles"
    srt_dir.mkdir(exist_ok=True)
    srt_path = srt_dir / f"{clip_id}.srt"
    with open(srt_path, "w") as f:
        f.write("\n".join(srt_lines))

    return True


def _auto_render_new_clip(clip_id: str, start: float, end: float, ep_dir: Path) -> dict:
    """Generate subtitles and render a newly added clip."""
    try:
        _generate_clip_srt(ep_dir, clip_id, start, end)
    except Exception as e:
        logger.warning("SRT generation failed for %s: %s", clip_id, e)

    try:
        return _action_rerender_short({"clip_id": clip_id}, ep_dir)
    except Exception as e:
        return {"status": "error", "detail": f"Render failed: {e}"}


def _action_reject_clip(action: dict, ep_dir: Path) -> dict:
    clip_id = action.get("clip_id")
    if not clip_id:
        return {"action": "reject_clip", "status": "error", "detail": "Missing clip_id"}

    clips, clips_file = _load_clips(ep_dir)
    for i, clip in enumerate(clips):
        if clip.get("id") == clip_id:
            clip["status"] = "rejected"
            clips[i] = clip
            _save_clips(clips, clips_file)
            return {"action": "reject_clip", "status": "ok", "clip_id": clip_id}

    return {"action": "reject_clip", "status": "error", "detail": f"Clip {clip_id} not found"}


def _action_rerender_short(action: dict, ep_dir: Path) -> dict:
    """Re-render a single short clip using ffmpeg, matching shorts_render agent logic."""
    clip_id = action.get("clip_id")
    if not clip_id:
        return {"action": "rerender_short", "status": "error", "detail": "Missing clip_id"}

    clips, _ = _load_clips(ep_dir)
    clip = None
    for c in clips:
        if c.get("id") == clip_id:
            clip = c
            break
    if not clip:
        return {"action": "rerender_short", "status": "error", "detail": f"Clip {clip_id} not found"}

    merged_path = ep_dir / "source_merged.mp4"
    if not merged_path.exists():
        return {"action": "rerender_short", "status": "error", "detail": "source_merged.mp4 not found"}

    start = clip.get("start_seconds", clip.get("start", 0))
    end = clip.get("end_seconds", clip.get("end", 0))
    duration = end - start

    shorts_dir = ep_dir / "shorts"
    shorts_dir.mkdir(exist_ok=True)
    output_path = shorts_dir / f"{clip_id}.mp4"

    # Probe source dimensions
    try:
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(merged_path),
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(probe_result.stdout)
        video_stream = next(s for s in probe_data["streams"] if s["codec_type"] == "video")
        src_w = int(video_stream["width"])
        src_h = int(video_stream["height"])
    except (subprocess.CalledProcessError, StopIteration, KeyError, json.JSONDecodeError) as e:
        return {"action": "rerender_short", "status": "error", "detail": f"ffprobe failed: {e}"}

    # Load crop config for speaker positioning (lib/crop.py is source of truth)
    episode_data = _load_json_safe(ep_dir / "episode.json") or {}
    crop_config = episode_data.get("crop_config") or {}

    speaker = clip.get("speaker", "BOTH")
    cx, cy, zoom, _ = resolve_speaker(speaker, src_w, src_h, crop_config)
    x_offset, y_offset, crop_w, crop_h = compute_crop(src_w, src_h, cx, cy, zoom, "short")

    # Load config for encoder args
    from agents.pipeline import load_config
    config = load_config()
    encoder_args = get_video_encoder_args(config, crf_key="shorts_crf")
    audio_bitrate = config.get("processing", {}).get("shorts_audio_bitrate", "128k")

    # Build subtitle filter if SRT exists
    srt_path = ep_dir / "subtitles" / f"{clip_id}.srt"
    if srt_path.exists():
        srt_escaped = escape_srt_path(srt_path)
        vf = (
            f"crop={crop_w}:{crop_h}:{x_offset}:{y_offset},"
            f"scale=1080:1920,"
            f"subtitles='{srt_escaped}':force_style="
            f"'FontSize=12,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=3,Outline=1,Shadow=0,MarginV=80'"
        )
    else:
        vf = f"crop={crop_w}:{crop_h}:{x_offset}:{y_offset},scale=1080:1920"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(merged_path),
        "-t", str(duration),
        "-vf", vf,
        "-af", "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1",
        *encoder_args,
        "-r", "30", "-g", "30", "-bf", "0",
        "-vsync", "cfr",
        "-pix_fmt", "yuv420p",
        "-video_track_timescale", "30000",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-use_editlist", "0",
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        return {"action": "rerender_short", "status": "error", "detail": f"ffmpeg failed: {e.stderr[:500]}"}

    return {"action": "rerender_short", "status": "ok", "clip_id": clip_id, "output": str(output_path)}


def _action_approve_clips(action: dict, ep_dir: Path) -> dict:
    """Approve multiple clips by IDs or by minimum score threshold."""
    clips, clips_file = _load_clips(ep_dir)
    clip_ids = action.get("clip_ids")
    min_score = action.get("min_score")
    approved = []

    for clip in clips:
        should_approve = False
        if clip_ids and clip.get("id") in clip_ids:
            should_approve = True
        elif min_score is not None and (clip.get("virality_score", 0) >= min_score):
            should_approve = True

        if should_approve and clip.get("status") != "rejected":
            clip["status"] = "approved"
            approved.append(clip.get("id"))

    if approved:
        _save_clips(clips, clips_file)

    return {"action": "approve_clips", "status": "ok", "approved": approved, "count": len(approved)}


def _action_reject_clips(action: dict, ep_dir: Path) -> dict:
    """Reject multiple clips by IDs or by maximum score threshold."""
    clips, clips_file = _load_clips(ep_dir)
    clip_ids = action.get("clip_ids")
    max_score = action.get("max_score")
    rejected = []

    for clip in clips:
        should_reject = False
        if clip_ids and clip.get("id") in clip_ids:
            should_reject = True
        elif max_score is not None and (clip.get("virality_score", 0) <= max_score):
            should_reject = True

        if should_reject and clip.get("status") != "approved":
            clip["status"] = "rejected"
            rejected.append(clip.get("id"))

    if rejected:
        _save_clips(clips, clips_file)

    return {"action": "reject_clips", "status": "ok", "rejected": rejected, "count": len(rejected)}


def _action_update_platform_metadata(action: dict, ep_dir: Path) -> dict:
    """Update platform-specific metadata for a clip."""
    clip_id = action.get("clip_id")
    platform = action.get("platform")
    if not clip_id or not platform:
        return {"action": "update_platform_metadata", "status": "error", "detail": "Missing clip_id or platform"}

    clips, clips_file = _load_clips(ep_dir)
    for i, clip in enumerate(clips):
        if clip.get("id") == clip_id:
            meta = clip.get("metadata", {})
            plat_meta = meta.get(platform, {})
            # Copy all fields except action, clip_id, platform
            for key, val in action.items():
                if key not in ("action", "clip_id", "platform"):
                    plat_meta[key] = val
            meta[platform] = plat_meta
            clip["metadata"] = meta
            clips[i] = clip
            _save_clips(clips, clips_file)
            return {"action": "update_platform_metadata", "status": "ok", "clip_id": clip_id, "platform": platform}

    return {"action": "update_platform_metadata", "status": "error", "detail": f"Clip {clip_id} not found"}


def _action_delete_clip(action: dict, ep_dir: Path) -> dict:
    """Permanently remove a clip."""
    clip_id = action.get("clip_id")
    if not clip_id:
        return {"action": "delete_clip", "status": "error", "detail": "Missing clip_id"}

    clips, clips_file = _load_clips(ep_dir)
    original_len = len(clips)
    clips = [c for c in clips if c.get("id") != clip_id]

    if len(clips) == original_len:
        return {"action": "delete_clip", "status": "error", "detail": f"Clip {clip_id} not found"}

    _save_clips(clips, clips_file)
    return {"action": "delete_clip", "status": "ok", "clip_id": clip_id}


def _action_update_longform_metadata(action: dict, ep_dir: Path) -> dict:
    """Update longform title/description/tags in episode.json."""
    episode_file = ep_dir / "episode.json"
    episode_data = _load_json_safe(episode_file) or {}

    updated = []
    for field in ("title", "description", "tags"):
        if field in action:
            episode_data[field] = action[field]
            updated.append(field)

    if not updated:
        return {"action": "update_longform_metadata", "status": "error", "detail": "No fields to update"}

    with open(episode_file, "w") as f:
        json.dump(episode_data, f, indent=2)

    return {"action": "update_longform_metadata", "status": "ok", "updated_fields": updated}


def _action_update_episode_info(action: dict, ep_dir: Path) -> dict:
    """Update episode info fields (guest_name, guest_title, etc.) in episode.json."""
    episode_file = ep_dir / "episode.json"
    episode_data = _load_json_safe(episode_file) or {}

    updated = []
    for field in ("guest_name", "guest_title", "episode_name", "episode_description"):
        if field in action:
            episode_data[field] = action[field]
            updated.append(field)

    if not updated:
        return {"action": "update_episode_info", "status": "error", "detail": "No fields to update"}

    with open(episode_file, "w") as f:
        json.dump(episode_data, f, indent=2)

    return {"action": "update_episode_info", "status": "ok", "updated_fields": updated}


def _action_edit_longform(action: dict, ep_dir: Path) -> dict:
    """Add a cut or trim edit to the longform video."""
    edit_type = action.get("type")
    if edit_type not in ("cut", "trim_start", "trim_end"):
        return {"action": "edit_longform", "status": "error", "detail": f"Unknown edit type: {edit_type}"}

    episode_file = ep_dir / "episode.json"
    episode_data = _load_json_safe(episode_file) or {}
    edits = episode_data.get("longform_edits", [])

    if edit_type == "cut":
        start = action.get("start_seconds")
        end = action.get("end_seconds")
        if start is None or end is None:
            return {"action": "edit_longform", "status": "error", "detail": "Missing start_seconds or end_seconds"}
        if end <= start:
            return {"action": "edit_longform", "status": "error", "detail": "end_seconds must be > start_seconds"}
        edit = {"type": "cut", "start_seconds": start, "end_seconds": end,
                "reason": action.get("reason", ""), "duration_removed": round(end - start, 2)}
    elif edit_type == "trim_start":
        seconds = action.get("seconds")
        if seconds is None:
            return {"action": "edit_longform", "status": "error", "detail": "Missing seconds"}
        edit = {"type": "trim_start", "seconds": seconds, "reason": action.get("reason", "")}
    elif edit_type == "trim_end":
        seconds = action.get("seconds")
        if seconds is None:
            return {"action": "edit_longform", "status": "error", "detail": "Missing seconds"}
        edit = {"type": "trim_end", "seconds": seconds, "reason": action.get("reason", "")}

    edits.append(edit)
    episode_data["longform_edits"] = edits

    with open(episode_file, "w") as f:
        json.dump(episode_data, f, indent=2)

    return {"action": "edit_longform", "status": "ok", "edit": edit, "total_edits": len(edits)}


def _action_auto_trim(action: dict, ep_dir: Path) -> dict:
    """Auto-detect fluff at start/end and create trim edits using Claude."""
    import httpx

    # Load transcript
    transcribe_file = ep_dir / "transcribe.json"
    if not transcribe_file.exists():
        return {"action": "auto_trim", "status": "error", "detail": "No transcript available. Run transcribe first."}

    transcribe_data = _load_json_safe(transcribe_file) or {}
    diarized = transcribe_data.get("diarized", {})
    utterances = diarized.get("utterances", [])
    if not utterances:
        return {"action": "auto_trim", "status": "error", "detail": "No utterances in transcript."}

    episode_data = _load_json_safe(ep_dir / "episode.json") or {}
    duration = episode_data.get("duration_seconds", 0)

    # Format first 3 min and last 3 min of transcript for analysis
    speaker_map = diarized.get("speaker_map")
    first_lines, last_lines = [], []
    for utt in utterances:
        start = utt.get("start", 0)
        end = utt.get("end", 0)
        speaker = utt.get("speaker", utt.get("channel", "?"))
        label = _speaker_label(speaker, speaker_map)
        text = utt.get("text", "").strip()
        if not text:
            continue
        line = f"[{start:.1f}s - {end:.1f}s] {label}: {text}"
        if start < 300:
            first_lines.append(line)
        if end > duration - 300:
            last_lines.append(line)

    prompt = f"""Analyze this podcast transcript to find where the real conversation starts and ends.

## First 5 minutes of transcript:
{chr(10).join(first_lines[:100])}

## Last 5 minutes of transcript:
{chr(10).join(last_lines[-100:])}

## Total episode duration: {duration:.1f} seconds ({duration/60:.1f} minutes)

Find:
1. **trim_start**: The timestamp (in seconds) where the actual substantive conversation/interview begins. Skip past any: mic checks, "are we rolling?", "let me adjust this", greetings before the interview actually starts, test claps, setup talk. Find where the host's first real question or introduction begins.

2. **trim_end**: The timestamp (in seconds) where the conversation actually ends. Skip any: "thanks for coming", "okay we're done", "let me stop the recording", post-show chatter, goodbyes.

Respond with ONLY a JSON object, no other text:
{{"trim_start": {{"seconds": <number>, "reason": "<brief reason>"}}, "trim_end": {{"seconds": <number>, "reason": "<brief reason>"}}}}

If the episode starts or ends cleanly (no fluff to trim), use 0 for trim_start or the full duration for trim_end."""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"action": "auto_trim", "status": "error", "detail": "ANTHROPIC_API_KEY not set"}

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]
        # Extract JSON from response
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            return {"action": "auto_trim", "status": "error", "detail": f"Could not parse AI response: {content[:200]}"}
        trim_data = json.loads(json_match.group())
    except Exception as e:
        return {"action": "auto_trim", "status": "error", "detail": f"AI analysis failed: {e}"}

    edits = episode_data.get("longform_edits", [])
    results = []

    ts = trim_data.get("trim_start", {})
    if ts and ts.get("seconds", 0) > 5:  # Only trim if more than 5s of fluff
        edit = {"type": "trim_start", "seconds": ts["seconds"], "reason": ts.get("reason", "Auto-detected start")}
        edits.append(edit)
        results.append(edit)

    te = trim_data.get("trim_end", {})
    if te and te.get("seconds", duration) < duration - 5:  # Only trim if more than 5s of fluff
        edit = {"type": "trim_end", "seconds": te["seconds"], "reason": te.get("reason", "Auto-detected end")}
        edits.append(edit)
        results.append(edit)

    episode_data["longform_edits"] = edits
    with open(ep_dir / "episode.json", "w") as f:
        json.dump(episode_data, f, indent=2)

    return {"action": "auto_trim", "status": "ok", "edits": results, "total_edits": len(edits)}


def _action_rerender_longform(action: dict, ep_dir: Path) -> dict:
    """Re-render the longform video by running the longform_render agent."""
    # Clear previously rendered segment files to force re-render
    work_dir = ep_dir / "work"
    if work_dir.exists():
        for f in work_dir.glob("longform_seg_*.mp4"):
            f.unlink()

    try:
        from agents.pipeline import load_config
        from agents import AGENT_REGISTRY
        config = load_config()
        agent_cls = AGENT_REGISTRY["longform_render"]
        agent = agent_cls(ep_dir, config)
        result = agent.run()
        return {"action": "rerender_longform", "status": "ok", "result": result}
    except Exception as e:
        return {"action": "rerender_longform", "status": "error", "detail": str(e)}


# ---------------------------------------------------------------------------
# Metadata completeness checker
# ---------------------------------------------------------------------------

def _check_metadata_completeness(ep_dir: Path) -> dict:
    """Check what metadata fields are missing for the episode and its clips.

    Returns dict with 'missing_longform', 'missing_clips', and 'complete' flag.
    """
    episode_data = _load_json_safe(ep_dir / "episode.json") or {}
    clips, _ = _load_clips(ep_dir)
    metadata = _load_json_safe(ep_dir / "metadata" / "metadata.json") or {}
    meta_clips = {c["id"]: c for c in metadata.get("clips", [])}

    # Check longform fields
    missing_longform = []
    for field in ("title", "description", "tags", "guest_name", "episode_name"):
        val = episode_data.get(field)
        if not val or (isinstance(val, list) and len(val) == 0):
            missing_longform.append(field)

    # Check per-clip metadata (only non-rejected clips)
    platforms = ["youtube", "tiktok", "instagram", "linkedin", "x", "facebook", "threads", "pinterest", "bluesky"]
    missing_clips = {}
    for clip in clips:
        if clip.get("status") == "rejected":
            continue
        clip_id = clip.get("id", "")
        clip_missing = []

        # Check clip title
        if not clip.get("title"):
            clip_missing.append("title")

        # Check per-platform metadata (from metadata.json or clips.json inline)
        clip_meta = clip.get("metadata", {})
        meta_clip = meta_clips.get(clip_id, {})

        for platform in platforms:
            pm = clip_meta.get(platform) or meta_clip.get(platform) or {}
            if not pm:
                clip_missing.append(f"{platform} (all fields)")

        if clip_missing:
            missing_clips[clip_id] = clip_missing

    return {
        "missing_longform": missing_longform,
        "missing_clips": missing_clips,
        "complete": len(missing_longform) == 0 and len(missing_clips) == 0,
    }


# ---------------------------------------------------------------------------
# Parse action blocks from AI response
# ---------------------------------------------------------------------------

def _parse_actions(text: str) -> list:
    """Extract action JSON blocks from ```action ... ``` fences in AI response."""
    pattern = r"```action\s*\n(.*?)\n```"
    matches = re.findall(pattern, text, re.DOTALL)
    actions = []
    for match in matches:
        try:
            action = json.loads(match.strip())
            actions.append(action)
        except json.JSONDecodeError:
            continue
    return actions


def _strip_action_blocks(text: str) -> str:
    """Remove action blocks from the AI response text for cleaner output."""
    pattern = r"```action\s*\n.*?\n```"
    return re.sub(pattern, "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------

@router.get("/chat/history")
async def get_chat_history(episode_id: str) -> dict:
    """Return persisted chat history for an episode."""
    ep_dir = _episode_dir(episode_id)
    history = _load_chat_history(ep_dir)
    return {"messages": history}


@router.post("/chat")
async def chat_with_episode(episode_id: str, req: ChatRequest) -> dict:
    """Chat with an AI assistant about the episode. The assistant can view and
    modify clips, suggest new ones, re-render shorts, and answer questions."""
    logger.info("POST /api/episodes/%s/chat message_length=%d", episode_id, len(req.message))
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    ep_dir = _episode_dir(episode_id)
    ctx = _load_episode_context_cached(ep_dir, episode_id)
    system_prompt = _build_system_prompt(ctx)

    # Call Anthropic API
    try:
        import anthropic
    except ImportError:
        raise HTTPException(status_code=500, detail="anthropic package is not installed")

    client = anthropic.Anthropic(api_key=api_key)

    # Load config for model selection
    from agents.pipeline import load_config
    config = load_config()
    chat_model = config.get("chat", {}).get("model", "claude-sonnet-4-20250514")

    # Load conversation history for multi-turn context
    history = _load_chat_history(ep_dir)
    messages = history + [{"role": "user", "content": req.message}]

    try:
        message = client.messages.create(
            model=chat_model,
            max_tokens=4096,
            temperature=0.4,
            system=system_prompt,
            messages=messages,
        )
    except Exception as e:
        logger.error("Anthropic API error for %s: %s", episode_id, e)
        raise HTTPException(status_code=500, detail=f"Anthropic API error: {str(e)}")

    # Extract text from response
    ai_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            ai_text += block.text

    # Save conversation history
    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": ai_text})
    _save_chat_history(ep_dir, history)

    # Invalidate context cache since actions may have modified data
    _context_cache.pop(episode_id, None)

    # Parse and execute actions
    actions = _parse_actions(ai_text)
    actions_taken = []
    for action in actions:
        result = _execute_action(action, ep_dir)
        actions_taken.append(result)

    # Return clean response (action blocks stripped)
    clean_response = _strip_action_blocks(ai_text)

    return {"response": clean_response, "actions_taken": actions_taken}


# ---------------------------------------------------------------------------
# Auto-complete metadata endpoint
# ---------------------------------------------------------------------------

class CompleteMetadataResponse(BaseModel):
    complete: bool
    iterations: int
    actions_taken: List[dict]
    summary: str


@router.post("/complete-metadata")
async def complete_metadata(episode_id: str) -> dict:
    """Iteratively fill in all missing metadata using Claude.

    Loops up to 5 times: check what's missing, ask Claude to fill it, execute actions.
    """
    logger.info("POST /api/episodes/%s/complete-metadata", episode_id)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    ep_dir = _episode_dir(episode_id)

    try:
        import anthropic
    except ImportError:
        raise HTTPException(status_code=500, detail="anthropic package is not installed")

    client = anthropic.Anthropic(api_key=api_key)
    from agents.pipeline import load_config
    config = load_config()
    chat_model = config.get("chat", {}).get("model", "claude-sonnet-4-20250514")

    all_actions = []
    max_iterations = 5

    for iteration in range(max_iterations):
        # Check completeness
        status = _check_metadata_completeness(ep_dir)
        if status["complete"]:
            return {
                "complete": True,
                "iterations": iteration,
                "actions_taken": all_actions,
                "summary": f"All metadata complete after {iteration} iteration(s).",
            }

        # Build targeted prompt
        ctx = _load_episode_context(ep_dir)
        system_prompt = _build_system_prompt(ctx)

        missing_parts = []
        if status["missing_longform"]:
            missing_parts.append(f"Missing longform fields: {', '.join(status['missing_longform'])}")
        if status["missing_clips"]:
            # Only list first 5 clips to keep prompt manageable
            clip_items = list(status["missing_clips"].items())[:5]
            for clip_id, fields in clip_items:
                missing_parts.append(f"  {clip_id}: missing {', '.join(fields)}")
            remaining = len(status["missing_clips"]) - len(clip_items)
            if remaining > 0:
                missing_parts.append(f"  ... and {remaining} more clips with missing metadata")

        user_prompt = (
            "Please fill in all the missing metadata listed below. "
            "Use action blocks for each update.\n\n"
            "MISSING METADATA:\n" + "\n".join(missing_parts) + "\n\n"
            "For clips missing platform metadata, use update_platform_metadata actions. "
            "For missing longform fields, use update_longform_metadata. "
            "For missing episode info, use update_episode_info. "
            "Generate compelling, platform-appropriate content for each field."
        )

        try:
            message = client.messages.create(
                model=chat_model,
                max_tokens=8192,
                temperature=0.4,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e:
            logger.error("Anthropic API error in complete-metadata: %s", e)
            return {
                "complete": False,
                "iterations": iteration + 1,
                "actions_taken": all_actions,
                "summary": f"API error on iteration {iteration + 1}: {str(e)}",
            }

        ai_text = ""
        for block in message.content:
            if hasattr(block, "text"):
                ai_text += block.text

        # Parse and execute actions
        actions = _parse_actions(ai_text)
        _context_cache.pop(episode_id, None)

        for action in actions:
            result = _execute_action(action, ep_dir)
            all_actions.append(result)

        if not actions:
            # Claude didn't produce any actions — stop looping
            break

    # Final check
    final_status = _check_metadata_completeness(ep_dir)
    return {
        "complete": final_status["complete"],
        "iterations": max_iterations,
        "actions_taken": all_actions,
        "summary": f"Completed {max_iterations} iterations. {'All metadata filled.' if final_status['complete'] else 'Some fields still missing.'}",
    }
