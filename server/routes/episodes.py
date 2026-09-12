"""Episode endpoints."""

import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agents.pipeline import load_config
from agents.podcast_feed import current_podcast_audio
from agents.qa import quality_snapshot
from agents.speaker_cut import current_speaker_segments, rebind_visual_crop_segments
from lib.atomic_write import atomic_write_json
from lib.audio_mix import (
    CAMERA_AUDIO_TIMELINE_FILTER,
    audio_selection_settings,
    selected_audio_source,
)
from lib.clips import (
    clip_selection_status,
    is_selected_clip,
    load_clips,
)
from lib.clips import normalize_clip as _normalize_clip
from lib.crop import speaker_crop_state, visual_crop_state
from lib.delivery_video import migrate_unchanged_short_crop_fingerprints
from lib.ffprobe import get_dimensions
from server.routes.delivery import (
    current_delivery_video_fields,
    migrate_unchanged_delivery_audio_fingerprint,
)


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

_MAX_AUDIO_PREVIEW_SECONDS = 120.0


def _delivery_snapshot(ep_dir: Path, config: dict | None = None) -> dict | None:
    """Read cached delivery state without probing media or changing job state."""
    path = ep_dir / "delivery.json"
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    keys = (
        "status",
        "completed_at",
        "download_url",
        "duration_seconds",
        "video_status",
        "video_completed_at",
        "video_download_url",
        "video",
    )
    snapshot = {key: raw[key] for key in keys if key in raw}
    try:
        episode = json.loads((ep_dir / "episode.json").read_text())
        processing_config = config if config is not None else load_config()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        episode, processing_config = {}, {}

    if snapshot.get("status") == "ready":
        try:
            current_audio = current_podcast_audio(
                ep_dir,
                episode,
                processing_config,
                verify_content=False,
            )
        except (OSError, TypeError, ValueError):
            current_audio = None
        if current_audio is None or raw.get("output_stat") != {
            "size": current_audio.stat().st_size,
            "mtime_ns": current_audio.stat().st_mtime_ns,
        }:
            snapshot["status"] = "not_prepared"
    if snapshot.get("video_status") in {None, "ready"}:
        snapshot = {
            key: value
            for key, value in snapshot.items()
            if key != "video" and not key.startswith("video_")
        }
        snapshot.update(
            current_delivery_video_fields(ep_dir, episode, processing_config)
        )
    return snapshot


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


def _canonical_episode_clips(ep_dir: Path, inline_clips: object) -> list[dict]:
    """Load clips.json when available, with episode.json as a safe fallback."""
    if (ep_dir / "clips.json").exists():
        try:
            return load_clips(ep_dir)
        except (json.JSONDecodeError, OSError, TypeError):
            pass
    if not isinstance(inline_clips, list):
        return []
    return [
        _normalize_clip(dict(clip)) for clip in inline_clips if isinstance(clip, dict)
    ]


@router.get("/")
async def list_episodes() -> list[dict]:
    """List all episodes with summary info."""
    logger.info("GET /api/episodes/")
    if not EPISODES_DIR.exists():
        return []

    episodes = []
    config = load_config()
    for ep_dir in sorted(EPISODES_DIR.iterdir()):
        if not ep_dir.is_dir():
            continue
        ep_file = ep_dir / "episode.json"
        if not ep_file.exists():
            continue
        try:
            with open(ep_file) as f:
                ep = json.load(f)
            clips = _canonical_episode_clips(ep_dir, ep.get("clips"))
            clip_states = [clip_selection_status(clip) for clip in clips]
            episodes.append(
                {
                    "episode_id": ep.get("episode_id", ep_dir.name),
                    "title": ep.get("title", ep_dir.name),
                    "status": ep.get("status", "processing"),
                    "duration_seconds": ep.get("duration_seconds"),
                    "created_at": ep.get("created_at"),
                    "clips": clips,
                    "clip_count": len(clips),
                    "selected_clip_count": sum(
                        is_selected_clip(clip) for clip in clips
                    ),
                    "nonrejected_clip_count": sum(
                        state != "rejected" for state in clip_states
                    ),
                    "rejected_clip_count": clip_states.count("rejected"),
                    "guest_name": ep.get("guest_name", ""),
                    "guest_title": ep.get("guest_title", ""),
                    "episode_name": ep.get("episode_name", ""),
                    "episode_description": ep.get("episode_description", ""),
                    # Boolean so clients can disambiguate the overloaded
                    # `ready_for_review` status (same string used for
                    # "truncated pipeline done, awaiting crop" and "full
                    # pipeline done, awaiting clip review").
                    "has_crop_config": bool(ep.get("crop_config")),
                    "delivery": _delivery_snapshot(ep_dir, config),
                    "quality": quality_snapshot(ep_dir, include_findings=False),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue

    return episodes


_DJI_TIMESTAMP_RE = re.compile(r"^DJI_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})_")


def _derive_filming_timestamp(source_path: Optional[str]) -> Optional[datetime]:
    """Pull the filming date+time off the first DJI MP4 in source_path.
    DJI filenames carry the recording start as DJI_YYYYMMDDhhmmss_NNNN_D.MP4,
    so the episode id (and the user-facing date label downstream) matches the
    actual shoot rather than whenever the ingest happened to fire. Returns
    None if nothing parseable is found — callers fall back to now()."""
    if not source_path:
        return None
    p = Path(source_path)
    if not p.exists():
        return None
    candidates = sorted(p.glob("DJI_*.MP4")) if p.is_dir() else [p]
    for f in candidates:
        if f.name.startswith("._"):
            continue
        m = _DJI_TIMESTAMP_RE.match(f.name)
        if m:
            y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
            try:
                return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


@router.post("/")
async def create_episode(req: NewEpisodeRequest) -> dict:
    """Trigger a new episode ingest."""
    for label, value in (
        ("Camera source", req.source_path),
        ("Audio source", req.audio_path),
    ):
        if value and not Path(value).expanduser().exists():
            raise HTTPException(
                status_code=422, detail=f"{label} does not exist: {value}"
            )
    if req.source_path:
        req.source_path = str(Path(req.source_path).expanduser().absolute())
    if req.audio_path:
        req.audio_path = str(Path(req.audio_path).expanduser().absolute())
    logger.info("POST /api/episodes/ source_path=%s", req.source_path)
    filming = _derive_filming_timestamp(req.source_path)
    now = filming or datetime.now(timezone.utc)
    episode_id = f"ep_{now.strftime('%Y-%m-%d')}_{now.strftime('%H%M%S')}"

    # Camera timestamps are stable, so importing the same recording twice
    # resolves to the same ID. Never overwrite the existing episode metadata.
    if (EPISODES_DIR / episode_id / "episode.json").exists():
        raise HTTPException(
            status_code=409, detail=f"Episode {episode_id} already exists"
        )

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
    ep_dir = EPISODES_DIR / episode_id
    ep["delivery"] = _delivery_snapshot(ep_dir)
    ep["quality"] = quality_snapshot(ep_dir, include_findings=False)

    # Stamp the actual longform.mp4 mtime so the UI shows when the file was
    # last written (after a re-mux the mtime is fresh even if pipeline
    # completed_at is weeks old).
    longform_path = ep_dir / "longform.mp4"
    if longform_path.exists():
        ep["longform_rendered_at"] = datetime.fromtimestamp(
            longform_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()

    # Clip actions write clips.json; episode.json may retain an ingest-time snapshot.
    ep["clips"] = _canonical_episode_clips(ep_dir, ep.get("clips"))

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
        ep["youtube_longform_url_source"] = "supplied"
        ep.pop("youtube_longform_url_captured_at", None)
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

    _, duration = _validate_preview_window(0, duration)
    ep = read_episode(episode_id)
    ep_dir = EPISODES_DIR / episode_id
    sync = ep.get("audio_sync") or {}
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
        r = subprocess.run(cmd, capture_output=True, check=False)
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


def _validate_preview_window(start: float, duration: float) -> tuple[float, float]:
    if not math.isfinite(start) or not math.isfinite(duration):
        raise HTTPException(
            status_code=400, detail="start and duration must be finite numbers"
        )
    if start < 0:
        raise HTTPException(status_code=400, detail="start must be non-negative")
    if duration <= 0 or duration > _MAX_AUDIO_PREVIEW_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"duration must be between 0 and {_MAX_AUDIO_PREVIEW_SECONDS:g} seconds",
        )
    return start, duration


def _preview_cache_path(
    ep_dir: Path,
    cache_name: str,
    tracks: list[dict],
    start: float,
    duration: float,
    offset: float,
    tempo: float = 1.0,
    channel: str | None = None,
    preserve_timestamps: bool = False,
) -> Path:
    """Key previews by exact sources and timeline parameters."""
    sources = []
    for track in tracks:
        path = Path(track["dest_path"])
        stat = path.stat()
        sources.append(
            {
                "path": str(path.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "duration": track.get("duration_seconds"),
            }
        )
    payload = json.dumps(
        {
            "sources": sources,
            "start": start,
            "duration": duration,
            "offset": offset,
            "tempo": tempo,
            "channel": channel,
            "preserve_timestamps": preserve_timestamps,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    cache_dir = ep_dir / "work" / "audio_preview"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", cache_name)
    return cache_dir / f"{safe_name}_{digest}.mp3"


def _preview_slices(
    tracks: list[dict], audio_start: float, duration: float
) -> list[tuple[Path, float, float]]:
    """Resolve a logical time window into local slices of consecutive files."""
    if duration <= 0:
        return []
    remaining = duration
    cursor = audio_start
    result = []
    for track in tracks:
        track_duration = float(track.get("duration_seconds") or 0)
        if track_duration <= 0:
            # Unknown duration is safe for a single legacy file.
            if len(tracks) == 1:
                return [(Path(track["dest_path"]), cursor, remaining)]
            continue
        if cursor >= track_duration:
            cursor -= track_duration
            continue
        slice_duration = min(remaining, track_duration - cursor)
        result.append((Path(track["dest_path"]), cursor, slice_duration))
        remaining -= slice_duration
        cursor = 0
        if remaining <= 1e-6:
            break
    return result


async def _render_audio_preview(
    ep_dir: Path,
    cache_name: str,
    tracks: list[dict],
    start: float,
    duration: float,
    offset: float,
    *,
    tempo: float = 1.0,
    channel: str | None = None,
    preserve_timestamps: bool = False,
) -> Path:
    start, duration = _validate_preview_window(start, duration)
    if not math.isfinite(offset) or not math.isfinite(tempo) or tempo <= 0:
        raise HTTPException(status_code=400, detail="Audio sync values are invalid")
    for track in tracks:
        if not Path(track["dest_path"]).exists():
            raise HTTPException(status_code=404, detail="Audio file not found on disk")

    cache_file = _preview_cache_path(
        ep_dir,
        cache_name,
        tracks,
        start,
        duration,
        offset,
        tempo,
        channel,
        preserve_timestamps,
    )
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return cache_file

    timeline_audio_start = start * tempo + offset
    leading_silence = min(duration, max(0.0, -timeline_audio_start / tempo))
    slices = _preview_slices(
        tracks,
        max(0.0, timeline_audio_start),
        (duration - leading_silence) * tempo,
    )
    if not slices and leading_silence <= 0:
        raise HTTPException(status_code=416, detail="Preview starts after audio ends")

    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    if leading_silence > 0:
        inputs.extend(
            [
                "-f",
                "lavfi",
                "-t",
                str(leading_silence * tempo),
                "-i",
                "anullsrc=r=44100:cl=mono",
            ]
        )
        labels.append("[p0]")
        filters.append("[0:a]anull[p0]")
    for path, local_start, slice_duration in slices:
        index = len(labels)
        inputs.extend(
            ["-ss", str(local_start), "-t", str(slice_duration), "-i", str(path)]
        )
        input_filter = f"[{index}:a]"
        if preserve_timestamps:
            input_filter += CAMERA_AUDIO_TIMELINE_FILTER + ","
        if channel:
            channel_name = "FL" if channel == "left" else "FR"
            input_filter += f"pan=mono|c0={channel_name}"
        else:
            input_filter += "aformat=channel_layouts=mono"
        label = f"p{index}"
        filters.append(f"{input_filter}[{label}]")
        labels.append(f"[{label}]")

    with tempfile.NamedTemporaryFile(
        prefix=f".{cache_file.stem}.",
        suffix=".mp3",
        dir=cache_file.parent,
        delete=False,
    ) as temp_handle:
        temp_file = Path(temp_handle.name)
    output_label = labels[0]
    if len(labels) > 1:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[joined]")
        output_label = "[joined]"
    if abs(tempo - 1.0) > 1e-7:
        filters.append(f"{output_label}atempo={tempo:.8f}[timed]")
        output_label = "[timed]"
    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        output_label,
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "128k",
        str(temp_file),
    ]
    result = await _run_ffmpeg(cmd)
    if result.returncode != 0:
        temp_file.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"ffmpeg error: {result.stderr[:300]}"
        )
    temp_file.replace(cache_file)
    return cache_file


@router.get("/{episode_id}/audio-preview/track/{track_number}")
async def get_logical_track_preview(
    episode_id: str,
    track_number: int,
    start: float = 30.0,
    duration: float = 60.0,
):
    """Preview every consecutive recorder segment for one logical mic track."""
    ep = read_episode(episode_id)
    tracks = [
        track
        for track in ep.get("audio_tracks", [])
        if track.get("track_number") == track_number
    ]
    if not tracks:
        raise HTTPException(
            status_code=404, detail=f"Logical track {track_number} not found"
        )
    sync = ep.get("audio_sync") or {}
    offset = float(sync.get("offset_seconds", 0))
    tempo = (
        float(sync.get("tempo_factor", 1)) if sync.get("r_squared", 0) > 0.5 else 1.0
    )
    cache_file = await _render_audio_preview(
        EPISODES_DIR / episode_id,
        f"track_{track_number}",
        tracks,
        start,
        duration,
        offset,
        tempo=tempo,
    )
    return FileResponse(cache_file, media_type="audio/mpeg")


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

    # audio_time = video_time + offset_seconds
    sync = ep.get("audio_sync") or {}
    offset = float(sync.get("offset_seconds", 0))
    tempo = (
        float(sync.get("tempo_factor", 1)) if sync.get("r_squared", 0) > 0.5 else 1.0
    )
    cache_file = await _render_audio_preview(
        ep_dir, track_name, [track], start, duration, offset, tempo=tempo
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

    start, duration = _validate_preview_window(start, duration)

    ep_dir = EPISODES_DIR / episode_id
    source = ep_dir / "source_merged.mp4"
    if not source.exists():
        raise HTTPException(status_code=404, detail="source_merged.mp4 not found")

    cache_file = await _render_audio_preview(
        ep_dir,
        f"channel_{channel}",
        [{"dest_path": str(source)}],
        start,
        duration,
        0.0,
        channel=channel,
        preserve_timestamps=True,
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


def _reviewed_crop_labels(ep_dir: Path) -> dict[int, str]:
    """Return crop-index identities protected by explicit transcript review."""
    provenance_path = ep_dir / "transcript_provenance.json"
    if not provenance_path.exists():
        return {}
    try:
        provenance = json.loads(provenance_path.read_text())
        bindings = provenance.get("speaker_map_override")
        if bindings is None:
            return {}
        if not isinstance(bindings, list):
            raise TypeError
        labels: dict[int, str] = {}
        for binding in bindings:
            crop_index = binding.get("crop_speaker_index")
            if crop_index is None:
                continue
            raw_label = binding.get("person") or binding.get("label")
            label = raw_label.strip() if isinstance(raw_label, str) else ""
            if (
                not isinstance(crop_index, int)
                or isinstance(crop_index, bool)
                or crop_index < 0
                or not label
                or (crop_index in labels and labels[crop_index] != label)
            ):
                raise ValueError
            labels[crop_index] = label
        return labels
    except (
        AttributeError,
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=409,
            detail="Reviewed speaker bindings are invalid; repair them before changing crop speakers.",
        ) from exc


def _guard_reviewed_crop_roster(ep_dir: Path, speakers: list[dict]) -> None:
    """Prevent crop list edits from transferring reviewed speaker identities."""
    for crop_index, reviewed_label in _reviewed_crop_labels(ep_dir).items():
        if crop_index >= len(speakers):
            raise HTTPException(
                status_code=409,
                detail=(
                    f'Cannot remove crop speaker {crop_index + 1} ("{reviewed_label}"): '
                    "it has an explicitly reviewed transcript binding. Only unbound "
                    "speakers at the end may be removed."
                ),
            )
        raw_label = speakers[crop_index].get("label")
        requested_label = raw_label.strip() if isinstance(raw_label, str) else ""
        if requested_label != reviewed_label:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot rename or reorder crop speaker {crop_index + 1}: its "
                    f'explicitly reviewed identity is "{reviewed_label}". Keep that '
                    "label at its existing speaker index."
                ),
            )


@router.post("/{episode_id}/crop-config")
async def save_crop_config(episode_id: str, req: CropConfigRequest) -> dict:
    """Save crop settings without rendering or deleting finished deliverables."""
    ep = read_episode(episode_id)
    old_episode = dict(ep)
    old_episode["crop_config"] = ep.get("crop_config", {})

    ep_dir = EPISODES_DIR / episode_id
    source_width = 1920
    source_height = 1080
    merged = ep_dir / "source_merged.mp4"
    if merged.exists():
        try:
            source_width, source_height = await asyncio.to_thread(
                get_dimensions, merged
            )
        except (OSError, StopIteration, subprocess.SubprocessError, ValueError):
            logger.warning("Could not probe source dimensions for %s", episode_id)

    if req.speakers:
        # New N-speaker format
        crop_config = {
            "source_width": source_width,
            "source_height": source_height,
            "speakers": [s.model_dump() for s in req.speakers],
        }
        if req.ambient_tracks:
            crop_config["ambient_tracks"] = [t.model_dump() for t in req.ambient_tracks]
        if req.wide_center_x is not None:
            crop_config["wide_center_x"] = req.wide_center_x
            crop_config["wide_center_y"] = req.wide_center_y
            crop_config["wide_zoom"] = req.wide_zoom or 1.0
        # Also store legacy fields for backward compat with existing render agents
        if len(req.speakers) >= 2:
            crop_config["speaker_l_center_x"] = req.speakers[0].center_x
            crop_config["speaker_l_center_y"] = req.speakers[0].center_y
            crop_config["speaker_r_center_x"] = req.speakers[1].center_x
            crop_config["speaker_r_center_y"] = req.speakers[1].center_y
            crop_config["speaker_l_zoom"] = req.speakers[0].zoom
            crop_config["speaker_r_zoom"] = req.speakers[1].zoom
        elif len(req.speakers) == 1:
            crop_config["speaker_l_center_x"] = req.speakers[0].center_x
            crop_config["speaker_l_center_y"] = req.speakers[0].center_y
            crop_config["speaker_r_center_x"] = req.speakers[0].center_x
            crop_config["speaker_r_center_y"] = req.speakers[0].center_y
            crop_config["speaker_l_zoom"] = req.speakers[0].zoom
            crop_config["speaker_r_zoom"] = req.speakers[0].zoom
    else:
        # Legacy 2-speaker format
        crop_config = {
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

    if crop_config == ep.get("crop_config"):
        return {"status": "saved", "changed": False, "crop_config": crop_config}

    _guard_reviewed_crop_roster(ep_dir, crop_config["speakers"])

    config = load_config()
    old_crop = old_episode["crop_config"]
    longform_changed = visual_crop_state(old_crop, "longform") != visual_crop_state(
        crop_config, "longform"
    )
    short_changed = visual_crop_state(old_crop, "short") != visual_crop_state(
        crop_config, "short"
    )
    speaker_mapping_changed = speaker_crop_state(old_crop) != speaker_crop_state(
        crop_config
    )
    new_episode = dict(ep)
    new_episode["crop_config"] = crop_config
    audio_changed = audio_selection_settings(old_episode) != audio_selection_settings(
        new_episode
    )

    ep["crop_config"] = crop_config
    crop_dependent_agents = set()
    if speaker_mapping_changed:
        crop_dependent_agents.update(
            {"speaker_cut", "longform_render", "shorts_render", "qa"}
        )
    if audio_changed:
        crop_dependent_agents.update(
            {"longform_render", "shorts_render", "qa", "podcast_feed"}
        )
    if longform_changed:
        crop_dependent_agents.update({"longform_render", "qa"})
    if short_changed:
        crop_dependent_agents.update({"shorts_render", "qa"})

    pipeline_state = ep.setdefault("pipeline", {})
    completed = pipeline_state.get("agents_completed", [])
    pipeline_state["agents_completed"] = [
        agent for agent in completed if agent not in crop_dependent_agents
    ]
    errors = pipeline_state.get("errors", {})
    pipeline_state["errors"] = {
        name: message
        for name, message in errors.items()
        if name not in crop_dependent_agents
    }

    work_dir = ep_dir / "work"
    disposable_patterns = []
    if "longform_render" in crop_dependent_agents:
        disposable_patterns.extend(
            ["longform_seg_*.mp4", "longform_raw.mp4", "longform_concat.txt"]
        )
    if "shorts_render" in crop_dependent_agents:
        disposable_patterns.append("short_temp_*.mp4")
    if speaker_mapping_changed:
        disposable_patterns.extend(
            ["speaker_*_channel.npy", "speaker_*_rms_db.npy", "rms_meta.json"]
        )
    for pattern in disposable_patterns:
        for path in work_dir.glob(pattern):
            path.unlink(missing_ok=True)

    if crop_dependent_agents:
        ep["status"] = "ready_to_render"

    write_episode(episode_id, ep)
    delivery_audio_preserved = migrate_unchanged_delivery_audio_fingerprint(
        ep_dir, old_episode, ep, config
    )

    rebound = None
    if not speaker_mapping_changed:
        rebound = rebind_visual_crop_segments(ep_dir, old_episode, ep, config)
    segments_preserved = bool(
        rebound and current_speaker_segments(ep_dir, ep, config) is not None
    )
    migrated_short_ids = []
    if segments_preserved and not short_changed and not audio_changed:
        try:
            audio_path = selected_audio_source(ep_dir, ep, config) or (
                ep_dir / "work" / "audio_mix.wav"
            )
        except ValueError:
            audio_path = None
        clips_path = ep_dir / "clips.json"
        try:
            stored_clips = json.loads(clips_path.read_text())
        except (OSError, json.JSONDecodeError):
            stored_clips = {"clips": []}
        clips = (
            stored_clips.get("clips", [])
            if isinstance(stored_clips, dict)
            else stored_clips
        )
        if audio_path is not None and audio_path.is_file() and isinstance(clips, list):
            migrated_short_ids = migrate_unchanged_short_crop_fingerprints(
                ep_dir,
                old_episode,
                ep,
                config,
                audio_path,
                rebound.get("segments", []),
                clips,
            )
    return {
        "status": "saved",
        "changed": True,
        "crop_config": crop_config,
        "invalidated_agents": sorted(crop_dependent_agents),
        "speaker_segments_preserved": segments_preserved,
        "migrated_short_render_ids": migrated_short_ids,
        "delivery_audio_preserved": delivery_audio_preserved,
    }
