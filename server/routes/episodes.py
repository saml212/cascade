"""Episode endpoints."""

import asyncio
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from lib.atomic_write import atomic_write_json
from lib.clips import normalize_clip as _normalize_clip


async def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run an ffmpeg (or similar blocking subprocess) on a thread so the
    asyncio event loop stays responsive. ffmpeg on large 32-bit float WAVs
    can take minutes; calling subprocess.run inline wedges uvicorn and
    starves every other request until it finishes.
    """
    return await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True,
        text=True,
    )


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
    """Write episode.json for a given episode (atomic write)."""
    ep_dir = EPISODES_DIR / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ep_dir / "episode.json", data)


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
            episodes.append(
                {
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
                }
            )
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

    # clips.json is the source of truth — it's what the clip action handlers
    # (approve, reject, update_metadata) write to. episode.json often carries
    # a stale snapshot from initial clip-mining. Always prefer clips.json if
    # it exists.
    clips_file = EPISODES_DIR / episode_id / "clips.json"
    if clips_file.exists():
        try:
            with open(clips_file) as f:
                clips_data = json.load(f)
            clips = (
                clips_data.get("clips", clips_data)
                if isinstance(clips_data, dict)
                else clips_data
            )
            ep["clips"] = [_normalize_clip(c) for c in clips]
        except (json.JSONDecodeError, OSError):
            # Fall back to whatever episode.json has if clips.json is malformed
            if ep.get("clips"):
                ep["clips"] = [_normalize_clip(c) for c in ep["clips"]]
    elif ep.get("clips"):
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
                    detail="Cannot delete episode while pipeline is running. Cancel the pipeline first.",
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
            clips_list = (
                clips_data.get("clips", clips_data)
                if isinstance(clips_data, dict)
                else clips_data
            )
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
        raise HTTPException(
            status_code=404, detail="Crop frame not found. Run stitch first."
        )
    return FileResponse(frame_path, media_type="image/jpeg")


@router.get("/{episode_id}/thumbnail")
async def get_thumbnail(
    episode_id: str,
    type: str = "longform",
    clip_id: Optional[str] = None,
):
    """Serve an episode thumbnail image with a crop_frame.jpg fallback.

    Query params:
      type=longform (default) — returns thumbnails/longform.jpg if present,
        else falls back to thumbnail.png at the episode root (legacy),
        else falls back to crop_frame.jpg.
      type=clip&clip_id=clip_01 — returns thumbnails/<clip_id>.jpg if present,
        else falls back to crop_frame.jpg.

    The fallback ordering lets the UI render SOMETHING useful even when
    thumbnail_gen hasn't run (it's gated behind an opt-in API env var).
    """
    ep_dir = EPISODES_DIR / episode_id
    if not ep_dir.exists():
        raise HTTPException(status_code=404, detail=f"Episode not found: {episode_id}")

    candidates: list[Path] = []
    if type == "clip" and clip_id:
        # Normalize clip_id to strip path traversal attempts
        safe_id = clip_id.replace("/", "").replace("..", "")
        candidates.append(ep_dir / "thumbnails" / f"{safe_id}.jpg")
        candidates.append(ep_dir / "thumbnails" / f"{safe_id}.png")
    else:
        candidates.append(ep_dir / "thumbnails" / "longform.jpg")
        candidates.append(ep_dir / "thumbnails" / "longform.png")
        candidates.append(ep_dir / "thumbnail.png")  # legacy thumbnail_gen output

    # Universal fallback — crop_frame.jpg is produced by stitch for ANY episode.
    candidates.append(ep_dir / "crop_frame.jpg")

    for path in candidates:
        if path.exists():
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            return FileResponse(path, media_type=mime)

    raise HTTPException(
        status_code=404, detail="No thumbnail available for this episode"
    )


@router.get("/{episode_id}/video-preview")
async def get_video_preview(episode_id: str):
    """Serve source_merged.mp4 for video preview in crop/sync UI."""
    ep_dir = EPISODES_DIR / episode_id
    merged = ep_dir / "source_merged.mp4"
    if not merged.exists():
        raise HTTPException(
            status_code=404, detail="source_merged.mp4 not found. Run stitch first."
        )
    return FileResponse(merged, media_type="video/mp4")


@router.get("/{episode_id}/sync-preview")
async def get_sync_preview(episode_id: str, duration: float = 120.0):
    """Return waveform data for camera and H6E audio for visual sync verification."""
    import subprocess
    import numpy as np

    ep = read_episode(episode_id)
    ep_dir = EPISODES_DIR / episode_id
    sync = ep.get("audio_sync", {})
    offset = sync.get("offset_seconds", 0)

    merged = ep_dir / "source_merged.mp4"
    if not merged.exists():
        raise HTTPException(status_code=404, detail="source_merged.mp4 not found")

    # Find best H6E track for display (prefer stereo_mix or builtin_mic)
    h6e_path = None
    for pref in ["stereo_mix", "builtin_mic", "input"]:
        for t in ep.get("audio_tracks", []):
            if t.get("track_type") == pref and Path(t["dest_path"]).exists():
                h6e_path = t["dest_path"]
                break
        if h6e_path:
            break
    if not h6e_path:
        raise HTTPException(status_code=404, detail="No H6E audio tracks found")

    sr = 1000  # 1kHz — enough for waveform display
    pps = 100  # peaks per second for the waveform

    def extract_rms(path, seek=0, dur=120):
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(seek),
            "-i",
            str(path),
            "-t",
            str(dur),
            "-ar",
            str(sr),
            "-ac",
            "1",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            return []
        data = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
        # Compute RMS in windows
        win = max(1, sr // pps)
        n = len(data) // win
        if n == 0:
            return []
        frames = data[: n * win].reshape(n, win)
        rms = np.sqrt(np.mean(frames**2, axis=1))
        # Normalize to 0-1
        mx = np.max(rms)
        if mx > 0:
            rms = rms / mx
        return [round(float(v), 3) for v in rms]

    # For negative offset (camera started first), shift the camera waveform forward
    # instead of seeking H6E to a negative position (which ffmpeg silently ignores)
    if offset >= 0:
        cam_waveform = extract_rms(str(merged), seek=0, dur=duration)
        h6e_waveform = extract_rms(h6e_path, seek=offset, dur=duration)
    else:
        cam_waveform = extract_rms(str(merged), seek=abs(offset), dur=duration)
        h6e_waveform = extract_rms(h6e_path, seek=0, dur=duration)

    return {
        "camera_waveform": cam_waveform,
        "h6e_waveform": h6e_waveform,
        "offset_seconds": offset,
        "duration": duration,
        "peaks_per_second": pps,
        "tempo_factor": sync.get("tempo_factor", 1.0),
        "confidence": sync.get("confidence", 0),
        "drift_rate_ppm": sync.get("drift_rate_ppm", 0),
    }


class SyncOffsetRequest(BaseModel):
    offset_seconds: float


class SyncOffsetResponse(BaseModel):
    status: str
    offset_seconds: float


@router.post("/{episode_id}/sync-offset")
async def save_sync_offset(
    episode_id: str, req: SyncOffsetRequest
) -> SyncOffsetResponse:
    """Save a manually adjusted sync offset."""
    ep = read_episode(episode_id)
    if "audio_sync" not in ep:
        ep["audio_sync"] = {}
    ep["audio_sync"]["offset_seconds"] = req.offset_seconds
    ep["audio_sync"]["manually_adjusted"] = True
    write_episode(episode_id, ep)

    # Delete ALL stale cached files that depend on sync offset
    work = EPISODES_DIR / episode_id / "work"
    for pattern in [
        "audio_mix.wav",
        "speaker_*_channel.npy",
        "speaker_*_rms_db.npy",
        "transcript_audio.*",
        "longform_seg_*.mp4",
        "audio_preview/*.mp3",
    ]:
        for f in work.glob(pattern):
            f.unlink()

    return {"status": "saved", "offset_seconds": req.offset_seconds}


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

    ffmpeg runs on a thread (via _run_ffmpeg) so the asyncio event loop
    stays responsive. WAVs here are often 1-2GB 32-bit float and the
    transcode can take 30-60s on first call; running sync would block
    every other request for that duration.
    """
    ep = read_episode(episode_id)
    ep_dir = EPISODES_DIR / episode_id

    # Find track by stem or logical name. H6E filenames are session-timestamp-
    # prefixed (e.g. "260311_162356_TrLR.WAV"), which forces clients to resolve
    # the prefix on every call. Accept logical suffixes (TrLR, TrMic, Tr1-Tr4)
    # and track_type aliases (stereo_mix, builtin_mic) as first-class names.
    audio_tracks = ep.get("audio_tracks", [])
    track = None
    type_aliases = {
        "stereo_mix": "TrLR",
        "builtin_mic": "TrMic",
        "TrLR": "TrLR",
        "TrMic": "TrMic",
    }
    wanted_suffix = type_aliases.get(track_name, track_name)
    for t in audio_tracks:
        stem = Path(t["filename"]).stem
        # Exact match (legacy)
        if stem == track_name or t.get("filename") == track_name:
            track = t
            break
        # Logical-suffix match: stem ends with "_TrLR" / "_TrMic" / "_Tr1" etc.
        if stem.endswith(f"_{wanted_suffix}"):
            track = t
            break
        # track_type alias (e.g. client asks for "stereo_mix"):
        if t.get("track_type") == track_name:
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
            "ffmpeg",
            "-y",
            "-ss",
            str(audio_start),
            "-i",
            str(wav_path),
            "-t",
            str(duration),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            str(cache_file),
        ]
        result = await _run_ffmpeg(cmd)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500, detail=f"ffmpeg error: {result.stderr[:300]}"
            )

    return FileResponse(cache_file, media_type="audio/mpeg")


@router.get("/{episode_id}/channel-preview/{channel}")
async def get_channel_preview(
    episode_id: str,
    channel: str,
    start: float = 30.0,
    duration: float = 60.0,
):
    """Serve an MP3 preview of one channel of source_merged.mp4 audio.

    For camera-audio episodes (no separate H6E recording), the camera's
    embedded stereo audio carries one speaker per channel. This endpoint
    extracts and previews just the left or right channel so the user can
    identify which speaker is on which side when setting up crop config.

    channel: "left" or "right"

    ffmpeg runs on a thread (via _run_ffmpeg) so the event loop stays
    responsive even if source_merged.mp4 seek + transcode takes seconds.
    """
    if channel not in ("left", "right"):
        raise HTTPException(status_code=400, detail="channel must be 'left' or 'right'")

    ep_dir = EPISODES_DIR / episode_id
    source = ep_dir / "source_merged.mp4"
    if not source.exists():
        raise HTTPException(status_code=404, detail="source_merged.mp4 not found")

    cache_dir = ep_dir / "work" / "audio_preview"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"channel_{channel}_{int(start)}_{int(duration)}.mp3"

    if not cache_file.exists():
        # Use channelsplit to extract just the requested channel
        ch_idx = "FL" if channel == "left" else "FR"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(duration),
            "-vn",
            "-af",
            f"pan=mono|c0={ch_idx}",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            str(cache_file),
        ]
        result = await _run_ffmpeg(cmd)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500, detail=f"ffmpeg error: {result.stderr[:300]}"
            )

    return FileResponse(cache_file, media_type="audio/mpeg")


class SpeakerCropConfig(BaseModel):
    label: str
    # Shorts (9:16) center point — required. This is the primary anchor.
    center_x: int
    center_y: int
    zoom: float = 1.0  # Shorts zoom (9:16 portrait crop)
    # Longform (16:9) center point — optional. If unset, falls back to the
    # shorts center. Sam can place the longform crop independently of the
    # shorts crop (useful when a tight portrait frame isn't the same region
    # that looks good in landscape).
    longform_center_x: Optional[int] = None
    longform_center_y: Optional[int] = None
    longform_zoom: float = (
        0.75  # Longform zoom (16:9) — lower = wider. Default shows ~2/3 frame.
    )
    track: Optional[int] = None  # H6E track number (1-based) mapped to this speaker
    volume: float = 1.0  # Audio volume for this speaker's track (0.0-2.0)


class AmbientTrackConfig(BaseModel):
    track_number: Optional[int] = None
    stem: Optional[str] = None  # filename stem for tracks without a number (Mix, Mic)
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
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_streams",
                    output_path,
                ]
                result = subprocess.run(
                    probe_cmd, capture_output=True, text=True, check=True
                )
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
            ep["crop_config"]["ambient_tracks"] = [
                t.model_dump() for t in req.ambient_tracks
            ]
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
                {
                    "label": "Speaker L",
                    "center_x": req.speaker_l_center_x,
                    "center_y": req.speaker_l_center_y,
                    "zoom": req.speaker_l_zoom,
                },
                {
                    "label": "Speaker R",
                    "center_x": req.speaker_r_center_x,
                    "center_y": req.speaker_r_center_y,
                    "zoom": req.speaker_r_zoom,
                },
            ],
        }

    # When crop_config changes on an already-processed episode, we need to
    # invalidate the downstream agents that depend on crop but PRESERVE the
    # expensive work (transcription, clip mining, metadata) which only
    # depends on the audio/transcript, not the crop.
    #
    # Crop-dependent agents that must re-run:
    #   speaker_cut      (speaker labels come from crop)
    #   longform_render  (uses crop rectangle for video)
    #   shorts_render    (uses crop rectangle for video)
    #   qa               (validates renders)
    #   podcast_feed     (depends on final artifacts)
    #   publish          (depends on final artifacts)
    #   backup           (depends on final artifacts)
    #
    # Crop-INDEPENDENT agents we keep (save money + time):
    #   ingest, stitch, audio_analysis  (always kept)
    #   transcribe                       (Deepgram, ~$0.50 — big savings)
    #   clip_miner                       (Claude LLM, ~$0.20)
    #   metadata_gen                     (Claude LLM, ~$0.10-0.20)
    #   thumbnail_gen                    (OpenAI caricature, ~$0.10)
    CROP_DEPENDENT_AGENTS = {
        "speaker_cut",
        "longform_render",
        "shorts_render",
        "qa",
        "podcast_feed",
        "publish",
        "backup",
    }

    pipeline_state = ep.setdefault("pipeline", {})
    completed = pipeline_state.get("agents_completed", [])
    had_crop_dependent_work = any(a in completed for a in CROP_DEPENDENT_AGENTS)

    # Remove crop-dependent agents from completed list so resume_pipeline will re-run them
    pipeline_state["agents_completed"] = [
        a for a in completed if a not in CROP_DEPENDENT_AGENTS
    ]

    # Clear any errors from those agents so the pipeline doesn't refuse to continue
    errors = pipeline_state.get("errors", {})
    pipeline_state["errors"] = {
        name: msg for name, msg in errors.items() if name not in CROP_DEPENDENT_AGENTS
    }

    # If we actually invalidated downstream work, nuke the artifacts so they
    # get regenerated. Only do this when there was prior work — initial crop
    # setup shouldn't delete anything (nothing exists yet).
    if had_crop_dependent_work:
        ep_dir = EPISODES_DIR / episode_id
        work_dir = ep_dir / "work"
        # Files that must be regenerated because crop changed
        artifacts_to_remove = [
            ep_dir / "longform.mp4",
            ep_dir / "segments.json",
            ep_dir / "speaker_cut.json",
            ep_dir / "longform_render.json",
            ep_dir / "shorts_render.json",
            ep_dir / "qa.json",
            ep_dir / "publish.json",
        ]
        for f in artifacts_to_remove:
            if f.exists():
                f.unlink()
        # Shorts: delete all rendered clips
        shorts_dir = ep_dir / "shorts"
        if shorts_dir.exists():
            for f in shorts_dir.glob("*.mp4"):
                f.unlink(missing_ok=True)
        # Work dir: segment caches, speaker channel caches, concat lists
        if work_dir.exists():
            for pattern in [
                "longform_seg_*.mp4",
                "longform_raw.mp4",
                "longform_concat.txt",
                "short_temp_*.mp4",
                "speaker_*_channel.npy",
                "speaker_*_rms_db.npy",
                "rms_meta.json",
                "audio_mix.wav",  # forces re-mix with new audio chain too
                "audio_mix_enhanced.wav",
                "audio_mix_denoised.wav",
            ]:
                for f in work_dir.glob(pattern):
                    f.unlink(missing_ok=True)

    # Transition from awaiting_crop_setup or error back to processing so the
    # pipeline can run again.
    if ep.get("status") in (
        "awaiting_crop_setup",
        "error",
        "ready_for_review",
        "awaiting_backup_approval",
        "awaiting_longform_approval",
    ):
        ep["status"] = "processing"

    write_episode(episode_id, ep)

    # Auto-generate audio_mix.wav if H6E audio tracks exist
    # This uses speaker-to-track assignments and volumes from crop_config
    audio_tracks = ep.get("audio_tracks", [])
    if audio_tracks:
        from lib.audio_mix import generate_audio_mix

        ep_dir = EPISODES_DIR / episode_id
        # Re-read to get full data with audio_tracks merged
        ep_fresh = read_episode(episode_id)
        try:
            mix_path = generate_audio_mix(ep_dir, ep_fresh)
            if mix_path:
                logger.info(
                    "Generated audio_mix.wav for %s (%.1f MB)",
                    episode_id,
                    mix_path.stat().st_size / 1e6,
                )
                # Invalidate ALL cached files that depend on crop/audio config
                work_dir = ep_dir / "work"
                for pattern in [
                    "longform_seg_*.mp4",  # rendered video segments
                    "speaker_*_channel.npy",  # cached speaker audio extractions
                    "speaker_*_rms_db.npy",  # cached RMS data
                    "transcript_audio.*",  # multichannel transcript audio
                    "audio_preview/*.mp3",  # cached audio previews
                ]:
                    for f in work_dir.glob(pattern):
                        f.unlink(missing_ok=True)
                # Also invalidate rendered shorts
                shorts_dir = ep_dir / "shorts"
                if shorts_dir.exists():
                    for f in shorts_dir.glob("*.mp4"):
                        f.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to generate audio_mix.wav for %s", episode_id)

    return {"status": "saved", "crop_config": ep["crop_config"]}
