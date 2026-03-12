"""Episode endpoints."""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from lib.clips import normalize_clip as _normalize_clip
from lib.paths import get_episodes_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/episodes", tags=["episodes"])

EPISODES_DIR = get_episodes_dir()


class NewEpisodeRequest(BaseModel):
    source_path: Optional[str] = None
    audio_path: Optional[str] = None
    speaker_count: Optional[int] = None


def read_episode(episode_id: str) -> dict:
    """Read episode.json for a given episode."""
    ep_dir = EPISODES_DIR / episode_id
    ep_file = ep_dir / "episode.json"
    if not ep_file.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    with open(ep_file) as f:
        return json.load(f)


def write_episode(episode_id: str, data: dict):
    """Write episode.json for a given episode."""
    ep_dir = EPISODES_DIR / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    with open(ep_dir / "episode.json", "w") as f:
        json.dump(data, f, indent=2)


@router.get("/")
async def list_episodes() -> list[dict]:
    """List all episodes with summary info."""
    logger.info("GET /api/episodes/")
    if not EPISODES_DIR.exists():
        return []

    episodes = []
    for ep_dir in sorted(EPISODES_DIR.iterdir()):
        if not ep_dir.is_dir():
            continue
        ep_file = ep_dir / "episode.json"
        if not ep_file.exists():
            continue
        try:
            with open(ep_file) as f:
                ep = json.load(f)
            episodes.append({
                "episode_id": ep.get("episode_id", ep_dir.name),
                "title": ep.get("title", ep_dir.name),
                "status": ep.get("status", "processing"),
                "duration_seconds": ep.get("duration_seconds"),
                "created_at": ep.get("created_at"),
                "clips": ep.get("clips", []),
                "guest_name": ep.get("guest_name", ""),
                "guest_title": ep.get("guest_title", ""),
                "episode_name": ep.get("episode_name", ""),
                "episode_description": ep.get("episode_description", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue

    return episodes


@router.post("/")
async def create_episode(req: NewEpisodeRequest) -> dict:
    """Trigger a new episode ingest."""
    logger.info("POST /api/episodes/ source_path=%s", req.source_path)
    now = datetime.now(timezone.utc)
    episode_id = f"ep_{now.strftime('%Y-%m-%d')}_{now.strftime('%H%M%S')}"

    episode = {
        "episode_id": episode_id,
        "title": "",
        "status": "processing",
        "source_path": req.source_path,
        "audio_path": req.audio_path,
        "speaker_count": req.speaker_count,
        "duration_seconds": None,
        "created_at": now.isoformat(),
        "clips": [],
        "pipeline": {
            "started_at": now.isoformat(),
            "completed_at": None,
            "agents_completed": [],
        },
    }

    write_episode(episode_id, episode)

    # Create subdirectories
    ep_dir = EPISODES_DIR / episode_id
    for sub in ["shorts", "subtitles", "metadata", "qa"]:
        (ep_dir / sub).mkdir(parents=True, exist_ok=True)

    return {"episode_id": episode_id, "status": "processing"}


@router.get("/{episode_id}")
async def get_episode(episode_id: str) -> dict:
    """Get full episode detail."""
    logger.info("GET /api/episodes/%s", episode_id)
    ep = read_episode(episode_id)

    # Also load clips.json if it exists
    clips_file = EPISODES_DIR / episode_id / "clips.json"
    if clips_file.exists() and not ep.get("clips"):
        try:
            with open(clips_file) as f:
                clips_data = json.load(f)
            clips = clips_data.get("clips", clips_data) if isinstance(clips_data, dict) else clips_data
            ep["clips"] = [_normalize_clip(c) for c in clips]
        except (json.JSONDecodeError, OSError):
            pass

    # Normalize any clips already in episode data
    if ep.get("clips"):
        ep["clips"] = [_normalize_clip(c) for c in ep["clips"]]

    return ep


class EpisodeUpdateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list] = None
    guest_name: Optional[str] = None
    guest_title: Optional[str] = None
    episode_name: Optional[str] = None
    episode_description: Optional[str] = None
    youtube_longform_url: Optional[str] = None
    spotify_longform_url: Optional[str] = None
    link_tree_url: Optional[str] = None


@router.patch("/{episode_id}")
async def update_episode(episode_id: str, req: EpisodeUpdateRequest) -> dict:
    """Update episode metadata."""
    ep = read_episode(episode_id)
    if req.title is not None:
        ep["title"] = req.title
    if req.description is not None:
        ep["description"] = req.description
    if req.tags is not None:
        ep["tags"] = req.tags
    if req.guest_name is not None:
        ep["guest_name"] = req.guest_name
    if req.guest_title is not None:
        ep["guest_title"] = req.guest_title
    if req.episode_name is not None:
        ep["episode_name"] = req.episode_name
    if req.episode_description is not None:
        ep["episode_description"] = req.episode_description
    if req.youtube_longform_url is not None:
        ep["youtube_longform_url"] = req.youtube_longform_url
    if req.spotify_longform_url is not None:
        ep["spotify_longform_url"] = req.spotify_longform_url
    if req.link_tree_url is not None:
        ep["link_tree_url"] = req.link_tree_url
    write_episode(episode_id, ep)
    return {"status": "updated", "episode_id": episode_id}


@router.delete("/{episode_id}")
async def delete_episode(episode_id: str) -> dict:
    """Delete an episode and all its files."""
    logger.info("DELETE /api/episodes/%s", episode_id)
    ep_dir = EPISODES_DIR / episode_id
    if not ep_dir.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")

    # Check if pipeline is actively running (allow delete if cancelled)
    from server.routes.pipeline import _running, _cancel_requested
    if episode_id in _running and _running[episode_id].is_alive():
        # If already cancelled, force-allow deletion
        ep_file = ep_dir / "episode.json"
        if ep_file.exists():
            with open(ep_file) as f:
                ep_data = json.load(f)
            if ep_data.get("status") != "cancelled":
                raise HTTPException(
                    status_code=409,
                    detail="Cannot delete episode while pipeline is running. Cancel the pipeline first."
                )
        # Signal cancellation and clean up tracking
        _cancel_requested.add(episode_id)
        del _running[episode_id]

    shutil.rmtree(ep_dir)
    return {"status": "deleted", "episode_id": episode_id}


@router.post("/{episode_id}/approve")
async def approve_episode(episode_id: str) -> dict:
    """Approve the entire episode batch."""
    ep = read_episode(episode_id)
    ep["status"] = "approved"
    ep["approved_at"] = datetime.now(timezone.utc).isoformat()

    # Mark all pending clips as approved
    for clip in ep.get("clips", []):
        if clip.get("status", "pending") == "pending":
            clip["status"] = "approved"

    # Also update clips.json if it exists
    clips_file = EPISODES_DIR / episode_id / "clips.json"
    if clips_file.exists():
        try:
            with open(clips_file) as f:
                clips_data = json.load(f)
            clips_list = clips_data.get("clips", clips_data) if isinstance(clips_data, dict) else clips_data
            for clip in clips_list:
                if clip.get("status", "pending") == "pending":
                    clip["status"] = "approved"
            with open(clips_file, "w") as f:
                json.dump(clips_data, f, indent=2)
        except (json.JSONDecodeError, OSError):
            pass

    write_episode(episode_id, ep)
    return {"status": "approved", "episode_id": episode_id}


@router.get("/{episode_id}/crop-frame")
async def get_crop_frame(episode_id: str):
    """Serve the crop_frame.jpg extracted by the stitch agent."""
    frame_path = EPISODES_DIR / episode_id / "crop_frame.jpg"
    if not frame_path.exists():
        raise HTTPException(status_code=404, detail="Crop frame not found. Run stitch first.")
    return FileResponse(frame_path, media_type="image/jpeg")


@router.get("/{episode_id}/audio-preview/{track_name}")
async def get_audio_preview(
    episode_id: str,
    track_name: str,
    start: float = 30.0,
    duration: float = 60.0,
):
    """Serve an MP3 preview clip of an audio track, time-aligned to video time.

    start/duration are in video time; the sync offset from episode.json is
    applied automatically so the audio lines up with what's on screen.
    """
    import subprocess

    ep = read_episode(episode_id)
    ep_dir = EPISODES_DIR / episode_id

    # Find track by stem name
    audio_tracks = ep.get("audio_tracks", [])
    track = None
    for t in audio_tracks:
        stem = Path(t["filename"]).stem
        if stem == track_name or t.get("filename") == track_name:
            track = t
            break
    if not track:
        raise HTTPException(status_code=404, detail=f"Track '{track_name}' not found")

    wav_path = Path(track["dest_path"])
    if not wav_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found on disk")

    # audio_time = video_time + offset_seconds
    offset = ep.get("audio_sync", {}).get("offset_seconds", 0)
    audio_start = max(0, start + offset)

    cache_dir = ep_dir / "work" / "audio_preview"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{track_name}_{int(start)}_{int(duration)}.mp3"

    if not cache_file.exists():
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(audio_start),
            "-i", str(wav_path),
            "-t", str(duration),
            "-ac", "1", "-ar", "44100", "-b:a", "128k",
            str(cache_file),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"ffmpeg error: {result.stderr[:300]}")

    return FileResponse(cache_file, media_type="audio/mpeg")


class SpeakerCropConfig(BaseModel):
    label: str
    center_x: int
    center_y: int
    zoom: float = 1.0
    track: Optional[int] = None  # H6E track number (1-based) mapped to this speaker
    volume: float = 1.0  # Audio volume for this speaker's track (0.0-2.0)


class AmbientTrackConfig(BaseModel):
    track_number: int
    volume: float = 0.2


class CropConfigRequest(BaseModel):
    # New N-speaker format
    speakers: Optional[list[SpeakerCropConfig]] = None
    ambient_tracks: Optional[list[AmbientTrackConfig]] = None
    # Wide shot (all speakers) crop
    wide_center_x: Optional[int] = None
    wide_center_y: Optional[int] = None
    wide_zoom: Optional[float] = None
    # Legacy 2-speaker format (backward compat)
    speaker_l_center_x: Optional[int] = None
    speaker_l_center_y: Optional[int] = None
    speaker_r_center_x: Optional[int] = None
    speaker_r_center_y: Optional[int] = None
    speaker_l_zoom: float = 1.0
    speaker_r_zoom: float = 1.0
    zoom: float = 1.0


@router.post("/{episode_id}/crop-config")
async def save_crop_config(episode_id: str, req: CropConfigRequest) -> dict:
    """Save crop config to episode.json and update status."""
    ep = read_episode(episode_id)

    # Get source dimensions from stitch.json
    stitch_file = EPISODES_DIR / episode_id / "stitch.json"
    source_width = 1920
    source_height = 1080
    if stitch_file.exists():
        with open(stitch_file) as f:
            stitch_data = json.load(f)
        output_path = stitch_data.get("output_path", "")
        if output_path:
            import subprocess
            try:
                probe_cmd = [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_streams",
                    output_path,
                ]
                result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
                probe = json.loads(result.stdout)
                for s in probe.get("streams", []):
                    if s.get("codec_type") == "video":
                        source_width = int(s["width"])
                        source_height = int(s["height"])
                        break
            except (subprocess.CalledProcessError, KeyError, ValueError):
                pass

    if req.speakers:
        # New N-speaker format
        ep["crop_config"] = {
            "source_width": source_width,
            "source_height": source_height,
            "speakers": [s.model_dump() for s in req.speakers],
        }
        if req.ambient_tracks:
            ep["crop_config"]["ambient_tracks"] = [t.model_dump() for t in req.ambient_tracks]
        if req.wide_center_x is not None:
            ep["crop_config"]["wide_center_x"] = req.wide_center_x
            ep["crop_config"]["wide_center_y"] = req.wide_center_y
            ep["crop_config"]["wide_zoom"] = req.wide_zoom or 1.0
        # Also store legacy fields for backward compat with existing render agents
        if len(req.speakers) >= 2:
            ep["crop_config"]["speaker_l_center_x"] = req.speakers[0].center_x
            ep["crop_config"]["speaker_l_center_y"] = req.speakers[0].center_y
            ep["crop_config"]["speaker_r_center_x"] = req.speakers[1].center_x
            ep["crop_config"]["speaker_r_center_y"] = req.speakers[1].center_y
            ep["crop_config"]["speaker_l_zoom"] = req.speakers[0].zoom
            ep["crop_config"]["speaker_r_zoom"] = req.speakers[1].zoom
        elif len(req.speakers) == 1:
            ep["crop_config"]["speaker_l_center_x"] = req.speakers[0].center_x
            ep["crop_config"]["speaker_l_center_y"] = req.speakers[0].center_y
            ep["crop_config"]["speaker_r_center_x"] = req.speakers[0].center_x
            ep["crop_config"]["speaker_r_center_y"] = req.speakers[0].center_y
            ep["crop_config"]["speaker_l_zoom"] = req.speakers[0].zoom
            ep["crop_config"]["speaker_r_zoom"] = req.speakers[0].zoom
    else:
        # Legacy 2-speaker format
        ep["crop_config"] = {
            "source_width": source_width,
            "source_height": source_height,
            "speaker_l_center_x": req.speaker_l_center_x,
            "speaker_l_center_y": req.speaker_l_center_y,
            "speaker_r_center_x": req.speaker_r_center_x,
            "speaker_r_center_y": req.speaker_r_center_y,
            "speaker_l_zoom": req.speaker_l_zoom,
            "speaker_r_zoom": req.speaker_r_zoom,
            "zoom": req.zoom,
            "speakers": [
                {"label": "Speaker L", "center_x": req.speaker_l_center_x,
                 "center_y": req.speaker_l_center_y, "zoom": req.speaker_l_zoom},
                {"label": "Speaker R", "center_x": req.speaker_r_center_x,
                 "center_y": req.speaker_r_center_y, "zoom": req.speaker_r_zoom},
            ],
        }

    # Transition from awaiting_crop_setup to processing
    if ep.get("status") == "awaiting_crop_setup":
        ep["status"] = "processing"

    write_episode(episode_id, ep)
    return {"status": "saved", "crop_config": ep["crop_config"]}
