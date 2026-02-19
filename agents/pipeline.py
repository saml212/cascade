"""Pipeline orchestrator — DAG-based parallel agent execution, updates episode.json."""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime, timezone
from pathlib import Path

from agents import AGENT_REGISTRY, PIPELINE_ORDER
from agents.base import BaseAgent
from lib.paths import resolve_path

logger = logging.getLogger("cascade")

# Dependency graph: agent -> set of agents that must complete first
AGENT_DEPS = {
    "ingest": set(),
    "stitch": {"ingest"},
    "audio_analysis": {"stitch"},
    "speaker_cut": {"audio_analysis"},
    "transcribe": {"stitch"},
    "clip_miner": {"transcribe"},
    "longform_render": {"speaker_cut", "transcribe"},
    "shorts_render": {"clip_miner", "speaker_cut"},
    "metadata_gen": {"clip_miner"},
    "qa": {"longform_render", "shorts_render", "metadata_gen"},
    "podcast_feed": {"qa"},
    "publish": {"qa"},
    "backup": {"qa"},
}

NON_CRITICAL_AGENTS = {"podcast_feed", "publish", "backup"}


def load_config() -> dict:
    """Load config.toml from project root."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "config.toml"
    try:
        import tomli
    except ImportError:
        import tomllib as tomli
    with open(config_path, "rb") as f:
        return tomli.load(f)


def run_pipeline(
    source_path: str,
    episode_id=None,
    agents=None,
) -> dict:
    """Run the full pipeline (or a subset of agents) for an episode.

    Args:
        source_path: Path to source media (SD card directory or single file).
        episode_id: Optional episode ID. If None, one is generated.
        agents: Optional list of agent names to run. If None, runs all.

    Returns:
        The final episode.json dict.
    """
    config = load_config()
    output_dir = resolve_path(config["paths"]["output_dir"], "episodes")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create or load episode
    if episode_id is None:
        now = datetime.now(timezone.utc)
        episode_id = f"ep_{now.strftime('%Y-%m-%d')}_{now.strftime('%H%M%S')}"

    episode_dir = output_dir / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)

    # Ensure subdirectories
    for sub in ["source", "shorts", "subtitles", "metadata", "qa", "work"]:
        (episode_dir / sub).mkdir(exist_ok=True)

    episode_file = episode_dir / "episode.json"
    if episode_file.exists():
        with open(episode_file) as f:
            episode = json.load(f)
    else:
        episode = {
            "episode_id": episode_id,
            "title": "",
            "status": "processing",
            "source_path": source_path,
            "duration_seconds": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "clips": [],
            "pipeline": {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "agents_completed": [],
            },
        }

    # Determine which agents to run
    agent_names = agents if agents else PIPELINE_ORDER

    # Store which agents were requested and reset their completion status
    episode["pipeline"]["agents_requested"] = agent_names
    if agents:
        # Partial re-run: remove requested agents from completed list so they re-run cleanly
        prev_completed = episode["pipeline"].get("agents_completed", [])
        episode["pipeline"]["agents_completed"] = [
            a for a in prev_completed if a not in agent_names
        ]

    # Save initial episode state
    _save_episode(episode_file, episode)

    logger.info(f"Pipeline: running {len(agent_names)} agents for {episode_id}")

    # Build dependency graph filtered to requested agents
    requested_set = set(agent_names)
    deps = {}
    for name in agent_names:
        if name not in AGENT_REGISTRY:
            logger.warning(f"Unknown agent: {name}, skipping")
            continue
        # Only include dependencies that are also in the requested set
        deps[name] = AGENT_DEPS.get(name, set()) & requested_set

    completed = set()
    failed = set()
    episode_lock = threading.Lock()

    # Mutable refs that may change after clip_miner rename
    mutable = {"episode_dir": episode_dir, "episode_id": episode_id,
               "episode_file": episode_file}

    def _get_ready():
        """Return agents whose dependencies are all satisfied."""
        return [
            name for name in deps
            if name not in completed and name not in failed
            and deps[name] <= completed
        ]

    def _run_agent(agent_name):
        """Execute a single agent and return (name, result_or_exception)."""
        with episode_lock:
            ed = mutable["episode_dir"]
            ef = mutable["episode_file"]

        agent_cls = AGENT_REGISTRY[agent_name]
        agent = agent_cls(ed, config)

        if agent_name == "ingest":
            agent.source_path = source_path

        with episode_lock:
            episode["pipeline"]["current_agent"] = agent_name
            _save_episode(ef, episode)

        result = agent.run()
        return result

    def _on_agent_complete(agent_name, result):
        """Handle successful agent completion — update episode state."""
        with episode_lock:
            episode["pipeline"]["agents_completed"].append(agent_name)
            if "duration_seconds" in result and result["duration_seconds"]:
                episode["duration_seconds"] = result["duration_seconds"]
            if "clips" in result:
                episode["clips"] = result["clips"]
            _save_episode(mutable["episode_file"], episode)

        # After clip_miner, rename episode dir if guest_name was extracted
        if agent_name == "clip_miner":
            with episode_lock:
                ef = mutable["episode_file"]
                with open(ef) as f:
                    ep_data = json.load(f)
                # Merge any updates clip_miner wrote directly
                episode.update({k: ep_data[k] for k in
                    ("guest_name", "guest_title", "episode_name", "episode_description")
                    if k in ep_data})
                guest_name = episode.get("guest_name", "")
                if guest_name and not _has_name_slug(mutable["episode_id"]):
                    slug = _slugify(guest_name)
                    new_id = f"{mutable['episode_id']}_{slug}"
                    new_dir = output_dir / new_id
                    try:
                        mutable["episode_dir"].rename(new_dir)
                        mutable["episode_dir"] = new_dir
                        mutable["episode_id"] = new_id
                        episode["episode_id"] = new_id
                        mutable["episode_file"] = new_dir / "episode.json"
                        _save_episode(mutable["episode_file"], episode)
                        logger.info(f"Renamed episode dir to {new_id}")
                    except OSError as e:
                        logger.warning(f"Failed to rename episode dir: {e}")

    # --- DAG execution loop ---
    # Special handling: stitch must pause for crop setup before continuing
    stitch_pause_needed = ("stitch" in requested_set and "crop_config" not in episode)

    with ThreadPoolExecutor(max_workers=3) as executor:
        pending_futures = {}  # future -> agent_name

        while len(completed) + len(failed) < len(deps):
            # Check cancellation
            if _is_cancelled(mutable["episode_id"]):
                logger.info(f"Pipeline cancelled for {mutable['episode_id']}")
                # Cancel pending futures
                for f in pending_futures:
                    f.cancel()
                episode["status"] = "cancelled"
                episode["pipeline"].pop("current_agent", None)
                _save_episode(mutable["episode_file"], episode)
                return episode

            # Submit ready agents that aren't already running
            running_names = set(pending_futures.values())
            for name in _get_ready():
                if name in running_names:
                    continue
                # If stitch just completed and we need crop setup, pause
                if stitch_pause_needed and name != "stitch" and "stitch" in completed:
                    # Check if crop_config has been set since we started
                    with episode_lock:
                        with open(mutable["episode_file"]) as f:
                            ep_check = json.load(f)
                        if "crop_config" not in ep_check:
                            episode["status"] = "awaiting_crop_setup"
                            episode["pipeline"].pop("current_agent", None)
                            _save_episode(mutable["episode_file"], episode)
                            logger.info(f"Pipeline paused for {mutable['episode_id']}: awaiting crop setup")
                            # Cancel any pending futures
                            for fut in pending_futures:
                                fut.cancel()
                            return episode
                        else:
                            episode.update(ep_check)
                            stitch_pause_needed = False

                future = executor.submit(_run_agent, name)
                pending_futures[future] = name

            if not pending_futures:
                # No agents running and none ready — check for deadlock
                break

            # Wait for at least one to complete
            done, _ = wait(pending_futures.keys(), return_when=FIRST_COMPLETED)

            for future in done:
                agent_name = pending_futures.pop(future)
                try:
                    result = future.result()
                    _on_agent_complete(agent_name, result)
                    completed.add(agent_name)
                    logger.info(f"Agent {agent_name} completed for {mutable['episode_id']}")
                except Exception as e:
                    logger.error(f"Agent {agent_name} failed for {mutable['episode_id']}: {e}")
                    with episode_lock:
                        episode["pipeline"]["current_agent"] = None
                        episode["pipeline"].setdefault("errors", {})[agent_name] = str(e)
                        _save_episode(mutable["episode_file"], episode)

                    if agent_name in NON_CRITICAL_AGENTS:
                        logger.info(f"Skipping non-critical agent {agent_name}, continuing pipeline")
                        # Mark as completed so dependents can still check
                        completed.add(agent_name)
                    else:
                        failed.add(agent_name)
                        # Cancel remaining futures
                        for f in pending_futures:
                            f.cancel()
                        episode["status"] = "error"
                        _save_episode(mutable["episode_file"], episode)
                        return episode

    # Pipeline complete
    episode["pipeline"]["completed_at"] = datetime.now(timezone.utc).isoformat()
    episode["pipeline"].pop("current_agent", None)
    episode["status"] = "ready_for_review"
    progress_file = mutable["episode_dir"] / "progress.json"
    if progress_file.exists():
        progress_file.unlink()
    _save_episode(mutable["episode_file"], episode)

    logger.info(f"Pipeline complete for {mutable['episode_id']}")
    return episode


def _is_cancelled(episode_id: str) -> bool:
    """Check if a pipeline cancellation has been requested."""
    try:
        from server.routes.pipeline import _cancel_requested
        if episode_id in _cancel_requested:
            _cancel_requested.discard(episode_id)
            return True
    except ImportError:
        pass
    return False


def _slugify(name: str) -> str:
    """Convert a name to a URL-safe slug (e.g. 'John Smith' -> 'john-smith')."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _has_name_slug(episode_id: str) -> bool:
    """Check if episode_id already has a name slug appended."""
    # Format is ep_YYYY-MM-DD_HHMMSS with optional _slug
    # Count underscores: base has 2 (ep_, date_, time), slug adds more
    parts = episode_id.split("_")
    return len(parts) > 3


def _save_episode(path: Path, episode: dict):
    with open(path, "w") as f:
        json.dump(episode, f, indent=2, default=str)
