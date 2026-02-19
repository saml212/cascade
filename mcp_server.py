#!/usr/bin/env python3
"""Cascade MCP Server — exposes the full podcast pipeline as tools for AI agents.

This MCP server lets Claude Code, Codex, or any MCP-compatible AI agent
autonomously set up, run, and manage the Cascade podcast pipeline.

Usage:
    # Run directly
    python mcp_server.py

    # Or via Claude Code config (see cascade.mcp.json)
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("cascade-mcp")

mcp = FastMCP(
    "cascade",
    description="Cascade — Podcast automation pipeline. "
    "Ingest, stitch, transcribe, mine clips, render, and publish podcast episodes.",
)

# Track the running server process
_server_proc = None
_server_lock = threading.Lock()


def _load_config():
    """Load config.toml."""
    config_path = ROOT_DIR / "config" / "config.toml"
    try:
        import tomli
    except ImportError:
        import tomllib as tomli
    with open(config_path, "rb") as f:
        return tomli.load(f)


def _episodes_dir() -> Path:
    """Get the episodes output directory from config."""
    config = _load_config()
    return Path(config["paths"]["output_dir"])


def _load_env():
    """Load .env file into environment."""
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())


# Load .env on import so all tools have access to API keys
_load_env()


# ===========================================================================
# SETUP & ENVIRONMENT TOOLS
# ===========================================================================


@mcp.tool()
def check_prerequisites() -> str:
    """Check all prerequisites for running Cascade.

    Verifies: Python version, ffmpeg, API keys, SSD/output directory,
    virtual environment, and installed packages.
    Returns a status report with pass/fail for each check.
    """
    checks = []

    # Python version
    v = sys.version_info
    py_ok = v >= (3, 10)
    checks.append(f"{'PASS' if py_ok else 'FAIL'}: Python {v.major}.{v.minor}.{v.micro} (need 3.10+)")

    # ffmpeg
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        version_line = result.stdout.split("\n")[0] if result.stdout else "unknown"
        checks.append(f"PASS: ffmpeg installed ({version_line})")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        checks.append("FAIL: ffmpeg not found (install via: brew install ffmpeg)")

    # API keys
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    deepgram_key = os.environ.get("DEEPGRAM_API_KEY", "")
    checks.append(f"{'PASS' if anthropic_key else 'FAIL'}: ANTHROPIC_API_KEY {'set' if anthropic_key else 'missing'}")
    checks.append(f"{'PASS' if deepgram_key else 'FAIL'}: DEEPGRAM_API_KEY {'set' if deepgram_key else 'missing'}")

    # Output directory
    try:
        config = _load_config()
        output_dir = Path(config["paths"]["output_dir"])
        if output_dir.exists():
            free_gb = shutil.disk_usage(output_dir).free / (1024 ** 3)
            checks.append(f"PASS: Output dir exists ({output_dir}) — {free_gb:.1f} GB free")
        else:
            checks.append(f"WARN: Output dir does not exist ({output_dir}) — will be created on first run")
    except Exception as e:
        checks.append(f"WARN: Could not check output dir: {e}")

    # Virtual environment
    venv_path = ROOT_DIR / ".venv"
    if venv_path.exists():
        checks.append(f"PASS: Virtual environment exists at {venv_path}")
    else:
        checks.append("FAIL: No .venv found (run setup_environment to create)")

    # .env file
    env_path = ROOT_DIR / ".env"
    checks.append(f"{'PASS' if env_path.exists() else 'FAIL'}: .env file {'exists' if env_path.exists() else 'missing (copy .env.example to .env)'}")

    all_pass = all(c.startswith("PASS") for c in checks)
    header = "ALL CHECKS PASSED" if all_pass else "SOME CHECKS FAILED"
    return f"{header}\n\n" + "\n".join(checks)


@mcp.tool()
def setup_environment() -> str:
    """Set up the Cascade development environment.

    Creates a Python 3.12 virtual environment, installs all dependencies,
    and verifies the installation. Run this before anything else.
    """
    steps = []

    # Create venv
    venv = ROOT_DIR / ".venv"
    if not venv.exists():
        steps.append("Creating virtual environment...")
        result = subprocess.run(
            ["uv", "venv", "--python", "3.12"],
            capture_output=True, text=True, cwd=str(ROOT_DIR),
        )
        if result.returncode != 0:
            # Fall back to standard venv
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return f"ERROR: Failed to create venv: {result.stderr}"
        steps.append("Virtual environment created.")
    else:
        steps.append("Virtual environment already exists.")

    # Install dependencies
    steps.append("Installing dependencies...")
    pip_cmd = [str(venv / "bin" / "pip"), "install", "-r", str(ROOT_DIR / "requirements.txt")]

    # Try uv first (much faster)
    uv_result = subprocess.run(
        ["uv", "pip", "install", "-r", str(ROOT_DIR / "requirements.txt")],
        capture_output=True, text=True, cwd=str(ROOT_DIR),
    )
    if uv_result.returncode == 0:
        steps.append("Dependencies installed via uv.")
    else:
        pip_result = subprocess.run(pip_cmd, capture_output=True, text=True)
        if pip_result.returncode != 0:
            return f"ERROR: pip install failed: {pip_result.stderr[:500]}"
        steps.append("Dependencies installed via pip.")

    # Copy .env.example if .env doesn't exist
    env_path = ROOT_DIR / ".env"
    example_path = ROOT_DIR / ".env.example"
    if not env_path.exists() and example_path.exists():
        shutil.copy2(example_path, env_path)
        steps.append("Created .env from .env.example — fill in your API keys.")

    # Verify key imports
    python = str(venv / "bin" / "python")
    verify = subprocess.run(
        [python, "-c", "import fastapi, anthropic, numpy; print('OK')"],
        capture_output=True, text=True,
    )
    if verify.returncode == 0:
        steps.append("Import verification: OK")
    else:
        steps.append(f"Import verification: FAILED ({verify.stderr[:200]})")

    return "\n".join(steps)


@mcp.tool()
def start_server() -> str:
    """Start the Cascade web server (FastAPI on port 8420).

    Starts the server in the background. The web UI will be available
    at http://localhost:8420. Returns immediately.
    """
    global _server_proc

    with _server_lock:
        if _server_proc and _server_proc.poll() is None:
            return "Server is already running on http://localhost:8420"

        python = str(ROOT_DIR / ".venv" / "bin" / "uvicorn")
        if not Path(python).exists():
            python = str(ROOT_DIR / ".venv" / "bin" / "python")
            cmd = [python, "-m", "uvicorn", "server.app:app",
                   "--host", "0.0.0.0", "--port", "8420"]
        else:
            cmd = [python, "server.app:app",
                   "--host", "0.0.0.0", "--port", "8420"]

        env = os.environ.copy()
        # Load .env into subprocess environment
        env_path = ROOT_DIR / ".env"
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        env[key.strip()] = val.strip()

        _server_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait a moment to check it started
        time.sleep(2)
        if _server_proc.poll() is not None:
            stderr = _server_proc.stderr.read().decode() if _server_proc.stderr else ""
            return f"ERROR: Server failed to start: {stderr[:500]}"

        return (
            "Server started successfully.\n"
            f"  Web UI: http://localhost:8420\n"
            f"  API:    http://localhost:8420/api/episodes\n"
            f"  PID:    {_server_proc.pid}"
        )


@mcp.tool()
def stop_server() -> str:
    """Stop the running Cascade web server."""
    global _server_proc

    with _server_lock:
        if _server_proc is None or _server_proc.poll() is not None:
            return "Server is not running."

        pid = _server_proc.pid
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
            _server_proc.wait()
        _server_proc = None
        return f"Server stopped (PID {pid})."


@mcp.tool()
def get_server_status() -> str:
    """Check if the Cascade web server is currently running."""
    with _server_lock:
        if _server_proc and _server_proc.poll() is None:
            return f"Server is running (PID {_server_proc.pid}) at http://localhost:8420"
        return "Server is not running. Use start_server() to start it."


# ===========================================================================
# SOURCE MEDIA TOOLS
# ===========================================================================


@mcp.tool()
def list_source_media(path: str = "") -> str:
    """List available media files at a given path (e.g. SD card).

    Args:
        path: Directory to scan for media files. Defaults to the configured
              SD card mount point (/Volumes/7/DCIM/100CANON/).

    Returns a list of MP4 files with sizes.
    """
    if not path:
        try:
            config = _load_config()
            sd_mount = config.get("automation", {}).get("sd_card_mount", "/Volumes/7")
            path = f"{sd_mount}/DCIM/100CANON"
        except Exception:
            path = "/Volumes/7/DCIM/100CANON"

    p = Path(path)
    if not p.exists():
        return f"Path does not exist: {path}\nIs the SD card or drive mounted?"

    files = []
    for f in sorted(p.glob("*.MP4")):
        if f.name.startswith("._"):
            continue  # Skip macOS resource forks
        size_mb = f.stat().st_size / (1024 * 1024)
        files.append(f"  {f.name}  ({size_mb:.0f} MB)")

    if not files:
        # Also check lowercase
        for f in sorted(p.glob("*.mp4")):
            if f.name.startswith("._"):
                continue
            size_mb = f.stat().st_size / (1024 * 1024)
            files.append(f"  {f.name}  ({size_mb:.0f} MB)")

    if not files:
        return f"No MP4 files found in {path}"

    total_mb = sum(
        f.stat().st_size / (1024 * 1024)
        for f in p.glob("*.[Mm][Pp]4")
        if not f.name.startswith("._")
    )
    return f"Found {len(files)} MP4 files in {path} ({total_mb:.0f} MB total):\n" + "\n".join(files)


# ===========================================================================
# PIPELINE TOOLS
# ===========================================================================


@mcp.tool()
def run_pipeline(
    source_path: str,
    episode_id: str = "",
    agents: str = "",
) -> str:
    """Run the Cascade pipeline to process podcast media.

    This is the main entry point. It ingests media, stitches, transcribes,
    mines clips, renders video, and generates metadata.

    Args:
        source_path: Path to source media (SD card directory or file).
        episode_id: Optional episode ID. Auto-generated if empty.
        agents: Optional comma-separated list of specific agents to run
                (e.g. "ingest,stitch,audio_analysis"). Runs all if empty.

    Returns the episode ID and status. The pipeline will pause after
    'stitch' for crop configuration if not already set.
    """
    python = str(ROOT_DIR / ".venv" / "bin" / "python")
    cmd = [python, "-m", "agents", "--source-path", source_path]

    if episode_id:
        cmd += ["--episode-id", episode_id]
    if agents:
        cmd += ["--agents"] + [a.strip() for a in agents.split(",")]

    env = os.environ.copy()
    _load_env()
    env.update(os.environ)

    logger.info(f"Running pipeline: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
        env=env,
        timeout=3600,  # 1 hour max
    )

    output = result.stdout + result.stderr
    if result.returncode != 0:
        return f"Pipeline failed (exit {result.returncode}):\n{output[-2000:]}"

    return f"Pipeline completed successfully.\n\n{output[-2000:]}"


@mcp.tool()
def get_pipeline_status(episode_id: str) -> str:
    """Check the current pipeline status for an episode.

    Args:
        episode_id: The episode ID to check.

    Returns pipeline progress, current agent, and any errors.
    """
    ep_dir = _episodes_dir() / episode_id
    episode_file = ep_dir / "episode.json"

    if not episode_file.exists():
        return f"Episode {episode_id} not found."

    with open(episode_file) as f:
        episode = json.load(f)

    pipeline = episode.get("pipeline", {})
    status = episode.get("status", "unknown")
    completed = pipeline.get("agents_completed", [])
    current = pipeline.get("current_agent")
    errors = pipeline.get("errors", {})

    lines = [
        f"Episode: {episode_id}",
        f"Status: {status}",
        f"Completed agents ({len(completed)}): {', '.join(completed) if completed else 'none'}",
    ]
    if current:
        lines.append(f"Currently running: {current}")
    if errors:
        lines.append(f"Errors: {json.dumps(errors, indent=2)}")

    # Check for progress file
    progress_file = ep_dir / "progress.json"
    if progress_file.exists():
        with open(progress_file) as f:
            progress = json.load(f)
        lines.append(f"Progress: {progress.get('completed', 0)}/{progress.get('total', '?')} — {progress.get('message', '')}")

    return "\n".join(lines)


# ===========================================================================
# EPISODE TOOLS
# ===========================================================================


@mcp.tool()
def list_episodes() -> str:
    """List all episodes with their status and key metadata.

    Returns a formatted list of all episodes found in the output directory.
    """
    episodes_dir = _episodes_dir()
    if not episodes_dir.exists():
        return "No episodes directory found. Run a pipeline first."

    episodes = []
    for ep_dir in sorted(episodes_dir.iterdir()):
        if not ep_dir.is_dir():
            continue
        ep_file = ep_dir / "episode.json"
        if not ep_file.exists():
            continue
        try:
            with open(ep_file) as f:
                ep = json.load(f)
            status = ep.get("status", "unknown")
            title = ep.get("episode_name") or ep.get("title") or ""
            guest = ep.get("guest_name", "")
            duration = ep.get("duration_seconds")
            dur_str = f" ({duration/60:.0f} min)" if duration else ""
            label = f"{guest} — {title}" if guest and title else (guest or title or "untitled")
            episodes.append(f"  {ep_dir.name}  [{status}]  {label}{dur_str}")
        except (json.JSONDecodeError, OSError):
            episodes.append(f"  {ep_dir.name}  [error reading episode.json]")

    if not episodes:
        return "No episodes found."

    return f"Found {len(episodes)} episodes:\n" + "\n".join(episodes)


@mcp.tool()
def get_episode(episode_id: str) -> str:
    """Get full details for a specific episode.

    Args:
        episode_id: The episode ID to retrieve.

    Returns episode metadata, pipeline status, clip info, and file listing.
    """
    ep_dir = _episodes_dir() / episode_id
    ep_file = ep_dir / "episode.json"

    if not ep_file.exists():
        return f"Episode {episode_id} not found."

    with open(ep_file) as f:
        episode = json.load(f)

    # Key info
    lines = [
        f"Episode: {episode.get('episode_id', episode_id)}",
        f"Status: {episode.get('status', 'unknown')}",
        f"Guest: {episode.get('guest_name', 'unknown')}",
        f"Title: {episode.get('episode_name', '')}",
        f"Duration: {episode.get('duration_seconds', 0)/60:.1f} min" if episode.get('duration_seconds') else "Duration: unknown",
        f"Source: {episode.get('source_path', '')}",
    ]

    # Crop config
    if "crop_config" in episode:
        lines.append(f"Crop config: set")
    else:
        lines.append(f"Crop config: NOT SET (required for rendering)")

    # Pipeline
    pipeline = episode.get("pipeline", {})
    completed = pipeline.get("agents_completed", [])
    lines.append(f"\nPipeline: {len(completed)}/13 agents completed")
    lines.append(f"Completed: {', '.join(completed)}")

    # Clips
    clips_file = ep_dir / "clips.json"
    if clips_file.exists():
        with open(clips_file) as f:
            clips_data = json.load(f)
        clips = clips_data.get("clips", [])
        approved = sum(1 for c in clips if c.get("status") == "approved")
        pending = sum(1 for c in clips if c.get("status") == "pending")
        rejected = sum(1 for c in clips if c.get("status") == "rejected")
        lines.append(f"\nClips: {len(clips)} total ({approved} approved, {pending} pending, {rejected} rejected)")
        for c in clips:
            lines.append(f"  {c.get('id')}: [{c.get('status')}] {c.get('title', '')} ({c.get('duration', 0):.0f}s, score={c.get('virality_score', 0)})")

    # Key files
    lines.append("\nFiles:")
    for name in ["source_merged.mp4", "longform.mp4", "segments.json",
                  "diarized_transcript.json", "clips.json"]:
        p = ep_dir / name
        if p.exists():
            size_mb = p.stat().st_size / (1024 * 1024)
            lines.append(f"  {name} ({size_mb:.1f} MB)")

    # Shorts
    shorts_dir = ep_dir / "shorts"
    if shorts_dir.exists():
        shorts = list(shorts_dir.glob("*.mp4"))
        if shorts:
            lines.append(f"  shorts/ ({len(shorts)} clips)")

    return "\n".join(lines)


# ===========================================================================
# CROP CONFIGURATION TOOLS
# ===========================================================================


@mcp.tool()
def set_crop_config(
    episode_id: str,
    speaker_l_center_x: int,
    speaker_l_center_y: int,
    speaker_r_center_x: int,
    speaker_r_center_y: int,
) -> str:
    """Set speaker crop configuration for an episode.

    This must be set before rendering. The coordinates define the center
    point of each speaker in the source video frame (1920x1080).

    For a typical two-person podcast shot on a single camera:
    - Left speaker (L): usually around x=480, y=540
    - Right speaker (R): usually around x=1440, y=540

    Args:
        episode_id: The episode to configure.
        speaker_l_center_x: X center of left speaker (0-1920).
        speaker_l_center_y: Y center of left speaker (0-1080).
        speaker_r_center_x: X center of right speaker (0-1920).
        speaker_r_center_y: Y center of right speaker (0-1080).
    """
    ep_dir = _episodes_dir() / episode_id
    ep_file = ep_dir / "episode.json"

    if not ep_file.exists():
        return f"Episode {episode_id} not found."

    with open(ep_file) as f:
        episode = json.load(f)

    episode["crop_config"] = {
        "speaker_l_center_x": speaker_l_center_x,
        "speaker_l_center_y": speaker_l_center_y,
        "speaker_r_center_x": speaker_r_center_x,
        "speaker_r_center_y": speaker_r_center_y,
    }

    with open(ep_file, "w") as f:
        json.dump(episode, f, indent=2, default=str)

    return (
        f"Crop config set for {episode_id}:\n"
        f"  Speaker L: ({speaker_l_center_x}, {speaker_l_center_y})\n"
        f"  Speaker R: ({speaker_r_center_x}, {speaker_r_center_y})\n\n"
        f"You can now resume the pipeline with render agents."
    )


@mcp.tool()
def extract_frame(episode_id: str, timestamp: float = 5.0) -> str:
    """Extract a single frame from the source video for crop reference.

    Saves a JPEG frame to the episode directory and returns the path.
    Use this to visually determine speaker positions for crop config.

    Args:
        episode_id: The episode to extract from.
        timestamp: Time in seconds to extract the frame (default: 5.0).
    """
    ep_dir = _episodes_dir() / episode_id
    source = ep_dir / "source_merged.mp4"

    if not source.exists():
        return f"source_merged.mp4 not found for {episode_id}. Run ingest + stitch first."

    frame_path = ep_dir / "crop_reference_frame.jpg"
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", str(source),
        "-frames:v", "1",
        "-q:v", "2",
        str(frame_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return f"Failed to extract frame: {result.stderr[:500]}"

    return (
        f"Frame extracted to: {frame_path}\n\n"
        f"Open this image to identify speaker positions, then use set_crop_config.\n"
        f"Typical values for a two-person podcast (1920x1080):\n"
        f"  Speaker L (left of frame):  x=480, y=540\n"
        f"  Speaker R (right of frame): x=1440, y=540"
    )


# ===========================================================================
# CLIP MANAGEMENT TOOLS
# ===========================================================================


@mcp.tool()
def list_clips(episode_id: str) -> str:
    """List all clips for an episode with their status and metadata.

    Args:
        episode_id: The episode to list clips for.
    """
    ep_dir = _episodes_dir() / episode_id
    clips_file = ep_dir / "clips.json"

    if not clips_file.exists():
        return f"No clips found for {episode_id}. Run clip_miner first."

    with open(clips_file) as f:
        data = json.load(f)

    clips = data.get("clips", [])
    if not clips:
        return "No clips."

    lines = [f"Clips for {episode_id} ({len(clips)} total):"]
    for c in clips:
        lines.append(
            f"  {c.get('id')}: [{c.get('status', 'pending')}] "
            f"\"{c.get('title', '')}\" "
            f"({c.get('start_seconds', 0):.1f}s–{c.get('end_seconds', 0):.1f}s, "
            f"{c.get('duration', 0):.0f}s, score={c.get('virality_score', 0)})"
        )
    return "\n".join(lines)


@mcp.tool()
def approve_clips(episode_id: str, clip_ids: str = "", min_score: int = 0) -> str:
    """Approve clips for an episode.

    Args:
        episode_id: The episode.
        clip_ids: Comma-separated clip IDs to approve (e.g. "clip_01,clip_02").
                  If empty and min_score > 0, approves by score threshold.
        min_score: Approve all clips with virality_score >= this value.
                   Set to 0 to approve only specific clip_ids.
    """
    ep_dir = _episodes_dir() / episode_id
    clips_file = ep_dir / "clips.json"

    if not clips_file.exists():
        return f"No clips found for {episode_id}."

    with open(clips_file) as f:
        data = json.load(f)

    clips = data.get("clips", [])
    ids_set = {c.strip() for c in clip_ids.split(",") if c.strip()} if clip_ids else set()
    approved = []

    for clip in clips:
        should_approve = False
        if ids_set and clip.get("id") in ids_set:
            should_approve = True
        elif min_score > 0 and clip.get("virality_score", 0) >= min_score:
            should_approve = True

        if should_approve and clip.get("status") != "rejected":
            clip["status"] = "approved"
            approved.append(clip["id"])

    with open(clips_file, "w") as f:
        json.dump(data, f, indent=2)

    if approved:
        return f"Approved {len(approved)} clips: {', '.join(approved)}"
    return "No clips were approved. Check clip IDs or score threshold."


@mcp.tool()
def auto_approve_clips(episode_id: str) -> str:
    """Auto-approve all pending clips for an episode.

    Args:
        episode_id: The episode to auto-approve.
    """
    ep_dir = _episodes_dir() / episode_id
    clips_file = ep_dir / "clips.json"

    if not clips_file.exists():
        return f"No clips found for {episode_id}."

    with open(clips_file) as f:
        data = json.load(f)

    clips = data.get("clips", [])
    approved = []
    for clip in clips:
        if clip.get("status") == "pending":
            clip["status"] = "approved"
            approved.append(clip["id"])

    with open(clips_file, "w") as f:
        json.dump(data, f, indent=2)

    return f"Auto-approved {len(approved)} clips: {', '.join(approved)}" if approved else "No pending clips to approve."


# ===========================================================================
# CHAT TOOL
# ===========================================================================


@mcp.tool()
def chat_with_episode(episode_id: str, message: str) -> str:
    """Chat with the AI assistant about an episode.

    The assistant can view episode data, suggest edits, modify clips,
    re-render shorts, and answer questions about the episode.

    Args:
        episode_id: The episode to discuss.
        message: Your message to the assistant.
    """
    import httpx

    try:
        response = httpx.post(
            f"http://localhost:8420/api/episodes/{episode_id}/chat",
            json={"message": message},
            timeout=120,
        )
        if response.status_code != 200:
            return f"Chat API error ({response.status_code}): {response.text[:500]}"

        data = response.json()
        result = data.get("response", "")
        actions = data.get("actions_taken", [])

        if actions:
            result += "\n\nActions taken:\n"
            for a in actions:
                result += f"  - {a.get('action')}: {a.get('status')} ({a.get('clip_id', '')})\n"

        return result
    except httpx.ConnectError:
        return "ERROR: Server is not running. Use start_server() first."
    except Exception as e:
        return f"ERROR: {e}"


# ===========================================================================
# UTILITY TOOLS
# ===========================================================================


@mcp.tool()
def run_single_agent(episode_id: str, agent_name: str, source_path: str = "") -> str:
    """Run a single pipeline agent for an episode.

    Useful for re-running specific steps without the full pipeline.

    Args:
        episode_id: The episode to process.
        agent_name: Agent to run. One of: ingest, stitch, audio_analysis,
                    speaker_cut, transcribe, clip_miner, longform_render,
                    shorts_render, metadata_gen, qa, podcast_feed, publish, backup.
        source_path: Required only for 'ingest' agent.
    """
    valid_agents = [
        "ingest", "stitch", "audio_analysis", "speaker_cut", "transcribe",
        "clip_miner", "longform_render", "shorts_render", "metadata_gen",
        "qa", "podcast_feed", "publish", "backup",
    ]
    if agent_name not in valid_agents:
        return f"Unknown agent: {agent_name}. Valid agents: {', '.join(valid_agents)}"

    python = str(ROOT_DIR / ".venv" / "bin" / "python")
    cmd = [python, "-m", "agents", "--agents", agent_name, "--episode-id", episode_id]
    if source_path:
        cmd += ["--source-path", source_path]
    elif agent_name == "ingest":
        return "ERROR: source_path is required for the ingest agent."

    env = os.environ.copy()
    _load_env()
    env.update(os.environ)

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=str(ROOT_DIR), env=env, timeout=3600,
    )

    output = result.stdout + result.stderr
    if result.returncode != 0:
        return f"Agent {agent_name} failed:\n{output[-1500:]}"
    return f"Agent {agent_name} completed.\n{output[-1500:]}"


@mcp.tool()
def get_config() -> str:
    """Get the current Cascade configuration (config.toml contents)."""
    config = _load_config()
    return json.dumps(config, indent=2)


@mcp.tool()
def get_transcript(episode_id: str, max_utterances: int = 50) -> str:
    """Get the diarized transcript for an episode.

    Args:
        episode_id: The episode.
        max_utterances: Maximum number of utterances to return (default 50).
    """
    ep_dir = _episodes_dir() / episode_id
    transcript_file = ep_dir / "diarized_transcript.json"

    if not transcript_file.exists():
        return f"No transcript found for {episode_id}. Run transcribe agent first."

    with open(transcript_file) as f:
        data = json.load(f)

    utterances = data.get("utterances", [])
    lines = [f"Transcript for {episode_id} ({len(utterances)} utterances total):"]

    for utt in utterances[:max_utterances]:
        speaker = utt.get("speaker", "?")
        start = utt.get("start", 0)
        end = utt.get("end", 0)
        text = utt.get("text", "")
        lines.append(f"  [{start:.1f}s–{end:.1f}s] Speaker {speaker}: {text}")

    if len(utterances) > max_utterances:
        lines.append(f"\n  ... and {len(utterances) - max_utterances} more utterances")

    return "\n".join(lines)


@mcp.tool()
def backup_episode(episode_id: str) -> str:
    """Backup an episode to the configured external HDD via rsync.

    Args:
        episode_id: The episode to backup.
    """
    config = _load_config()
    backup_dir = config.get("paths", {}).get("backup_dir", "")
    if not backup_dir:
        return "ERROR: No backup_dir configured in config.toml"

    ep_dir = _episodes_dir() / episode_id
    if not ep_dir.exists():
        return f"Episode {episode_id} not found."

    dest = Path(backup_dir) / episode_id
    if not Path(backup_dir).exists():
        return f"Backup drive not mounted: {backup_dir}"

    cmd = [
        "rsync", "-av", "--progress",
        f"{ep_dir}/", f"{dest}/",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        return f"Backup failed: {result.stderr[:500]}"

    return f"Episode {episode_id} backed up to {dest}"


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    mcp.run()
