"""Pipeline orchestrator — runs agents sequentially, updates episode.json."""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from agents import AGENT_REGISTRY, PIPELINE_ORDER
from agents.base import BaseAgent

logger = logging.getLogger("cascade")


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
    output_dir = Path(config["paths"]["output_dir"])
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

    for agent_name in agent_names:
        # Check for cancellation between agents
        if _is_cancelled(episode_id):
            logger.info(f"Pipeline cancelled for {episode_id}")
            episode["status"] = "cancelled"
            episode["pipeline"].pop("current_agent", None)
            _save_episode(episode_file, episode)
            return episode

        if agent_name not in AGENT_REGISTRY:
            logger.warning(f"Unknown agent: {agent_name}, skipping")
            continue

        agent_cls = AGENT_REGISTRY[agent_name]
        agent: BaseAgent = agent_cls(episode_dir, config)

        # Inject source_path for ingest agent
        if agent_name == "ingest":
            agent.source_path = source_path

        episode["pipeline"]["current_agent"] = agent_name
        _save_episode(episode_file, episode)

        try:
            result = agent.run()
        except Exception as e:
            logger.error(f"Agent {agent_name} failed for {episode_id}: {e}")
            episode["pipeline"]["current_agent"] = None
            episode["pipeline"].setdefault("errors", {})[agent_name] = str(e)
            # Non-critical agents: log error and continue
            if agent_name in ("podcast_feed", "publish"):
                logger.info(f"Skipping non-critical agent {agent_name}, continuing pipeline")
                continue
            # Critical agent failure: stop pipeline with error status
            episode["status"] = "error"
            _save_episode(episode_file, episode)
            return episode

        # Update episode with agent results
        episode["pipeline"]["agents_completed"].append(agent_name)
        if "duration_seconds" in result and result["duration_seconds"]:
            episode["duration_seconds"] = result["duration_seconds"]
        if "clips" in result:
            episode["clips"] = result["clips"]

        _save_episode(episode_file, episode)

        # After clip_miner, rename episode dir if guest_name was extracted
        if agent_name == "clip_miner":
            # Re-read episode.json (clip_miner may have updated it)
            with open(episode_file) as f:
                episode = json.load(f)
            guest_name = episode.get("guest_name", "")
            if guest_name and not _has_name_slug(episode_id):
                slug = _slugify(guest_name)
                new_id = f"{episode_id}_{slug}"
                new_dir = output_dir / new_id
                try:
                    episode_dir.rename(new_dir)
                    episode_dir = new_dir
                    episode_id = new_id
                    episode["episode_id"] = new_id
                    episode_file = new_dir / "episode.json"
                    _save_episode(episode_file, episode)
                    logger.info(f"Renamed episode dir to {new_id}")
                except OSError as e:
                    logger.warning(f"Failed to rename episode dir: {e}")

        # After stitch, pause for crop setup if crop_config not yet set
        if agent_name == "stitch" and "crop_config" not in episode:
            episode["status"] = "awaiting_crop_setup"
            episode["pipeline"].pop("current_agent", None)
            _save_episode(episode_file, episode)
            logger.info(f"Pipeline paused for {episode_id}: awaiting crop setup")
            return episode

    # Pipeline complete
    episode["pipeline"]["completed_at"] = datetime.now(timezone.utc).isoformat()
    episode["pipeline"].pop("current_agent", None)
    episode["status"] = "ready_for_review"
    # Clean up progress file
    progress_file = episode_dir / "progress.json"
    if progress_file.exists():
        progress_file.unlink()
    _save_episode(episode_file, episode)

    logger.info(f"Pipeline complete for {episode_id}")
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
