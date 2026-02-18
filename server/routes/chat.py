"""Chat and trim endpoints — AI-powered episode editing assistant."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/episodes/{episode_id}", tags=["chat"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_output_env = os.getenv("CASCADE_OUTPUT_DIR", "")
if _output_env:
    EPISODES_DIR = Path(_output_env)
else:
    EPISODES_DIR = PROJECT_ROOT / "output" / "episodes"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str
    actions_taken: List[dict]


class TrimRequest(BaseModel):
    trim_start_seconds: float = 0.0
    trim_end_seconds: float = 0.0


class TrimResponse(BaseModel):
    new_duration: float
    backup_path: str


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

## Diarized transcript (summary)
{transcript_summary}

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

- Respond conversationally and confirm what you changed.
- When the user asks you to edit clips, include the appropriate action blocks.
- You can include multiple action blocks in a single response.
- When suggesting new clips, reference the transcript to find compelling moments.
- Always explain your reasoning when making changes.
- If the user asks a question, answer it based on the episode data without taking actions.
- When approving or rejecting multiple clips, use the bulk actions (approve_clips/reject_clips) instead of individual actions.
"""


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

    # Transcript summary — include first/last utterances and total count
    transcript = ctx.get("diarized_transcript")
    if transcript:
        utterances = transcript.get("utterances", [])
        total = len(utterances)
        if total > 20:
            preview = utterances[:10] + [{"text": f"... ({total - 20} more utterances) ..."}] + utterances[-10:]
        else:
            preview = utterances
        transcript_summary = json.dumps(preview, indent=2)
    else:
        transcript_summary = "No transcript available."

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
        transcript_summary=transcript_summary,
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
    return {"action": "add_clip", "status": "ok", "clip_id": clip_id}


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

    # Load crop config for speaker positioning
    episode_data = _load_json_safe(ep_dir / "episode.json") or {}
    crop_config = episode_data.get("crop_config")

    # Determine speaker crop
    speaker = clip.get("speaker", "BOTH")
    crop_w = int(src_h * 9 / 16)

    if crop_config and speaker in ("L", "R"):
        # Use configured center points
        if speaker == "L":
            cx = crop_config["speaker_l_center_x"]
        else:
            cx = crop_config["speaker_r_center_x"]
        x_offset = max(0, min(cx - crop_w // 2, src_w - crop_w))
    else:
        x_offset = (src_w - crop_w) // 2

    # Build subtitle filter if SRT exists
    srt_path = ep_dir / "subtitles" / f"{clip_id}.srt"
    if srt_path.exists():
        srt_escaped = str(srt_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        vf = (
            f"crop={crop_w}:{src_h}:{x_offset}:0,"
            f"scale=1080:1920,"
            f"subtitles='{srt_escaped}':force_style="
            f"'FontSize=12,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=3,Outline=1,Shadow=0,MarginV=80'"
        )
    else:
        vf = f"crop={crop_w}:{src_h}:{x_offset}:0,scale=1080:1920"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(merged_path),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        "-r", "30", "-g", "30", "-bf", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
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

@router.post("/chat")
async def chat_with_episode(episode_id: str, req: ChatRequest) -> dict:
    """Chat with an AI assistant about the episode. The assistant can view and
    modify clips, suggest new ones, re-render shorts, and answer questions."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    ep_dir = _episode_dir(episode_id)
    ctx = _load_episode_context(ep_dir)
    system_prompt = _build_system_prompt(ctx)

    # Call Anthropic API
    try:
        import anthropic
    except ImportError:
        raise HTTPException(status_code=500, detail="anthropic package is not installed")

    client = anthropic.Anthropic(api_key=api_key)

    try:
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            temperature=0.4,
            system=system_prompt,
            messages=[
                {"role": "user", "content": req.message},
            ],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {str(e)}")

    # Extract text from response
    ai_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            ai_text += block.text

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
# Helpers
# ---------------------------------------------------------------------------


def _fix_track_durations(mp4_path: Path) -> Path:
    """Ensure audio and video tracks have matching durations.

    Stream-copy trims cut video at keyframes but audio precisely, leaving a
    duration mismatch that platforms like Spotify reject.  If the tracks
    differ by more than 50 ms, re-mux with -t set to the shorter duration.
    Returns the (possibly replaced) output path.
    """
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", str(mp4_path),
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        streams = json.loads(result.stdout).get("streams", [])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return mp4_path  # Can't probe — return as-is

    durations = {}
    for s in streams:
        if "duration" in s:
            durations[s["codec_type"]] = float(s["duration"])

    v_dur = durations.get("video")
    a_dur = durations.get("audio")
    if v_dur is None or a_dur is None:
        return mp4_path

    diff = abs(v_dur - a_dur)
    if diff <= 0.05:
        return mp4_path  # Close enough

    shorter = min(v_dur, a_dur)
    fixed_path = mp4_path.with_suffix(".fixed.mp4")
    fix_cmd = [
        "ffmpeg", "-y",
        "-i", str(mp4_path),
        "-t", str(shorter),
        "-c", "copy",
        "-movflags", "+faststart",
        str(fixed_path),
    ]
    try:
        subprocess.run(fix_cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        return mp4_path  # Fix failed — return original

    os.remove(str(mp4_path))
    shutil.move(str(fixed_path), str(mp4_path))
    return mp4_path


# ---------------------------------------------------------------------------
# Trim endpoint
# ---------------------------------------------------------------------------

@router.post("/trim")
async def trim_episode(episode_id: str, req: TrimRequest) -> dict:
    """Trim the source_merged.mp4 by cutting off the beginning and/or end.

    Creates a backup of the original before replacing it.
    """
    ep_dir = _episode_dir(episode_id)
    source_path = ep_dir / "source_merged.mp4"

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="source_merged.mp4 not found")

    # Probe current duration
    try:
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(source_path),
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(probe_result.stdout)
        current_duration = float(probe_data["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=500, detail=f"Could not probe source file: {e}")

    trim_start = req.trim_start_seconds
    trim_end = req.trim_end_seconds if req.trim_end_seconds > 0 else current_duration

    if trim_start < 0 or trim_end < 0:
        raise HTTPException(status_code=400, detail="Trim values must be non-negative")
    if trim_end > current_duration:
        trim_end = current_duration
    if trim_start >= trim_end:
        raise HTTPException(status_code=400, detail="Trim start must be before trim end")

    new_duration = trim_end - trim_start

    # Render trimmed version
    trimmed_path = ep_dir / "source_merged_trimmed.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(trim_start),
        "-to", str(trim_end),
        "-i", str(source_path),
        "-c", "copy",
        "-shortest",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(trimmed_path),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg trim failed: {e.stderr[:500]}")

    # Backup original and replace
    backup_path = ep_dir / "source_merged_original.mp4"
    if not backup_path.exists():
        # Only backup if we haven't already (first trim)
        shutil.move(str(source_path), str(backup_path))
    else:
        # Subsequent trims — just remove the current version
        os.remove(str(source_path))

    shutil.move(str(trimmed_path), str(source_path))

    # Trim longform.mp4 the same way if it exists
    longform_path = ep_dir / "longform.mp4"
    if longform_path.exists():
        longform_backup = ep_dir / "longform_original.mp4"
        longform_trimmed = ep_dir / "longform_trimmed.mp4"
        lf_cmd = [
            "ffmpeg", "-y",
            "-ss", str(trim_start),
            "-to", str(trim_end),
            "-i", str(longform_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(longform_trimmed),
        ]
        try:
            subprocess.run(lf_cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"ffmpeg longform trim failed: {e.stderr[:500]}")

        # Verify audio/video track durations match; fix if they diverge
        longform_trimmed = _fix_track_durations(longform_trimmed)

        if not longform_backup.exists():
            shutil.move(str(longform_path), str(longform_backup))
        else:
            os.remove(str(longform_path))
        shutil.move(str(longform_trimmed), str(longform_path))

    # Update episode.json with new duration
    episode_file = ep_dir / "episode.json"
    if episode_file.exists():
        with open(episode_file) as f:
            episode = json.load(f)
        episode["duration_seconds"] = new_duration
        with open(episode_file, "w") as f:
            json.dump(episode, f, indent=2)

    return {
        "new_duration": new_duration,
        "backup_path": str(backup_path),
    }
