"""Pipeline orchestrator — DAG-based parallel agent execution, updates episode.json."""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime, timezone
from pathlib import Path

from agents import AGENT_REGISTRY, PIPELINE_ORDER
from lib.atomic_write import atomic_write_json
from lib.paths import resolve_path

logger = logging.getLogger("cascade")

# Dependency graph: agent -> set of agents that must complete first
AGENT_DEPS = {
    "ingest": set(),
    "stitch": {"ingest"},
    "audio_analysis": {"stitch"},
    "speaker_cut": {"audio_analysis"},
    "transcribe": {"stitch"},
    "clip_miner": {"transcribe", "speaker_cut"},
    "longform_render": {"speaker_cut", "transcribe"},
    "shorts_render": {"clip_miner", "speaker_cut"},
    "metadata_gen": {"clip_miner"},
    "thumbnail_gen": {"transcribe"},
    "qa": {"longform_render", "shorts_render", "metadata_gen", "thumbnail_gen"},
    "podcast_feed": {"qa"},
    "publish": {"qa"},
    "backup": {"publish", "podcast_feed", "thumbnail_gen"},
}

NON_CRITICAL_AGENTS = {"podcast_feed", "publish", "backup", "thumbnail_gen"}


def load_config() -> dict:
    """Load config.toml from project root."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "config.toml"
    import tomllib

    with open(config_path, "rb") as f:
        return tomllib.load(f)


def run_pipeline(
    source_path: str,
    audio_path: str = None,
    speaker_count: int = None,
    episode_id=None,
    agents=None,
) -> dict:
    """Run the full pipeline (or a subset of agents) for an episode.

    Args:
        source_path: Path(s) to source media (directory, file, or list of files).
        audio_path: Optional path to external audio recorder directory (e.g., Zoom H6E).
        speaker_count: Optional number of speakers — stored as metadata in episode.json
            for the frontend crop setup UI. Not used by pipeline agents directly.
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
            "audio_path": audio_path,
            "speaker_count": speaker_count,
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
    mutable = {
        "episode_dir": episode_dir,
        "episode_id": episode_id,
        "episode_file": episode_file,
    }

    def _get_ready():
        """Return agents whose dependencies are all satisfied."""
        return [
            name
            for name in deps
            if name not in completed and name not in failed and deps[name] <= completed
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
            agent.audio_path = audio_path or episode.get("audio_path")

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
            # Merge audio sync/track data from ingest
            # Preserve manually adjusted offset — don't overwrite with auto-detected
            if "audio_sync" in result:
                existing_sync = episode.get("audio_sync", {})
                if existing_sync.get("manually_adjusted"):
                    result["audio_sync"]["offset_seconds"] = existing_sync[
                        "offset_seconds"
                    ]
                    result["audio_sync"]["manually_adjusted"] = True
                episode["audio_sync"] = result["audio_sync"]
            if "audio" in result:
                episode["audio_tracks"] = result["audio"].get("tracks", [])
            if "source_properties" in result and result["source_properties"]:
                episode["source_properties"] = result["source_properties"]
            _save_episode(mutable["episode_file"], episode)

        # After stitch, remove source/ directory to reclaim ~20GB
        # (source_merged.mp4 contains everything needed downstream)
        if agent_name == "stitch":
            source_dir = mutable["episode_dir"] / "source"
            if source_dir.exists():
                import shutil

                try:
                    shutil.rmtree(source_dir)
                    logger.info(f"Cleaned up source/ directory after stitch")
                except OSError as e:
                    logger.warning(f"Failed to clean source/ directory: {e}")

        # After clip_miner, rename episode dir if guest_name was extracted
        if agent_name == "clip_miner":
            with episode_lock:
                ef = mutable["episode_file"]
                with open(ef) as f:
                    ep_data = json.load(f)
                # Merge any updates clip_miner wrote directly
                episode.update(
                    {
                        k: ep_data[k]
                        for k in (
                            "guest_name",
                            "guest_title",
                            "episode_name",
                            "episode_description",
                        )
                        if k in ep_data
                    }
                )
                guest_name = episode.get("guest_name", "")
                if guest_name and not _has_name_slug(mutable["episode_id"]):
                    slug = _slugify(guest_name)
                    new_id = f"{mutable['episode_id']}_{slug}"
                    new_dir = output_dir / new_id
                    old_dir_str = str(mutable["episode_dir"])
                    try:
                        mutable["episode_dir"].rename(new_dir)
                        mutable["episode_dir"] = new_dir
                        mutable["episode_id"] = new_id
                        episode["episode_id"] = new_id
                        mutable["episode_file"] = new_dir / "episode.json"

                        # Update stale paths in all agent JSON outputs
                        new_dir_str = str(new_dir)
                        for jf in new_dir.glob("*.json"):
                            try:
                                raw = jf.read_text()
                                if old_dir_str in raw:
                                    jf.write_text(raw.replace(old_dir_str, new_dir_str))
                            except OSError:
                                pass

                        _save_episode(mutable["episode_file"], episode)
                        logger.info(f"Renamed episode dir to {new_id}")
                    except OSError as e:
                        logger.warning(f"Failed to rename episode dir: {e}")

    # --- DAG execution loop ---
    # Special handling: pause for crop setup if crop_config isn't set and we're about
    # to run agents that depend on it (anything after stitch)
    # Agents that require crop_config to produce correct results
    crop_dependent_agents = {"speaker_cut", "longform_render", "shorts_render"}
    stitch_pause_needed = (
        bool(requested_set & crop_dependent_agents) and "crop_config" not in episode
    )
    # Pause after longform_render for user approval before spending API tokens
    # on clip mining, metadata, shorts, etc.
    # Only pause if longform_render is in the requested set AND has completed.
    longform_approval_agents = {"clip_miner", "shorts_render", "metadata_gen"}
    longform_pause_needed = (
        bool(requested_set & longform_approval_agents)
        and "longform_render" in requested_set
        and not episode.get("longform_approved")
    )
    # Special handling: backup must pause for user approval (destructive SD cleanup)
    backup_pause_needed = "backup" in requested_set and not episode.get(
        "backup_approved"
    )

    with ThreadPoolExecutor(max_workers=4) as executor:
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
                # If crop setup needed, pause before running crop-dependent agents
                if stitch_pause_needed and name in crop_dependent_agents:
                    # Check if crop_config has been set since we started
                    with episode_lock:
                        with open(mutable["episode_file"]) as f:
                            ep_check = json.load(f)
                        if "crop_config" not in ep_check:
                            episode["status"] = "awaiting_crop_setup"
                            episode["pipeline"].pop("current_agent", None)
                            _save_episode(mutable["episode_file"], episode)
                            logger.info(
                                f"Pipeline paused for {mutable['episode_id']}: awaiting crop setup"
                            )
                            # Cancel any pending futures
                            for fut in pending_futures:
                                fut.cancel()
                            return episode
                        else:
                            episode.update(ep_check)
                            stitch_pause_needed = False

                # Pause before clip_miner/shorts/metadata until longform is approved
                # Only pause if longform_render has actually completed
                if (
                    longform_pause_needed
                    and name in longform_approval_agents
                    and "longform_render" in completed
                ):
                    with episode_lock:
                        with open(mutable["episode_file"]) as f:
                            ep_check = json.load(f)
                        if not ep_check.get("longform_approved"):
                            episode["status"] = "awaiting_longform_approval"
                            episode["pipeline"].pop("current_agent", None)
                            _save_episode(mutable["episode_file"], episode)
                            logger.info(
                                f"Pipeline paused for {mutable['episode_id']}: awaiting longform approval"
                            )
                            for fut in pending_futures:
                                fut.cancel()
                            return episode
                        else:
                            episode.update(ep_check)
                            longform_pause_needed = False

                # If backup is ready but not approved, pause for user confirmation
                if backup_pause_needed and name == "backup":
                    with episode_lock:
                        with open(mutable["episode_file"]) as f:
                            ep_check = json.load(f)
                        if not ep_check.get("backup_approved"):
                            episode["status"] = "awaiting_backup_approval"
                            episode["pipeline"].pop("current_agent", None)
                            _save_episode(mutable["episode_file"], episode)
                            logger.info(
                                f"Pipeline paused for {mutable['episode_id']}: awaiting backup approval"
                            )
                            for fut in pending_futures:
                                fut.cancel()
                            return episode
                        else:
                            episode.update(ep_check)
                            backup_pause_needed = False

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
                    logger.info(
                        f"Agent {agent_name} completed for {mutable['episode_id']}"
                    )
                except Exception as e:
                    logger.error(
                        f"Agent {agent_name} failed for {mutable['episode_id']}: {e}"
                    )
                    with episode_lock:
                        episode["pipeline"]["current_agent"] = None
                        episode["pipeline"].setdefault("errors", {})[agent_name] = str(
                            e
                        )
                        _save_episode(mutable["episode_file"], episode)

                    if agent_name in NON_CRITICAL_AGENTS:
                        logger.info(
                            f"Skipping non-critical agent {agent_name}, continuing pipeline"
                        )
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

    # Pipeline complete. The "done" status depends on where we actually
    # landed. If crop_config is missing, the user still has crop setup to
    # do — don't pretend clips are ready for review.
    episode["pipeline"]["completed_at"] = datetime.now(timezone.utc).isoformat()
    episode["pipeline"].pop("current_agent", None)
    if not episode.get("crop_config"):
        episode["status"] = "awaiting_crop_setup"
    else:
        episode["status"] = "ready_for_review"
    progress_file = mutable["episode_dir"] / "progress.json"
    if progress_file.exists():
        progress_file.unlink()
    _save_episode(mutable["episode_file"], episode)

    # Log per-agent timing summary
    ed = mutable["episode_dir"]
    summary_parts = []
    for name in PIPELINE_ORDER:
        agent_json = ed / f"{name}.json"
        if agent_json.exists():
            try:
                with open(agent_json) as f:
                    data = json.load(f)
                elapsed = data.get("_elapsed_seconds", "?")
                summary_parts.append(f"{name}: {elapsed}s")
            except (json.JSONDecodeError, OSError):
                pass
    if summary_parts:
        logger.info(f"Pipeline timing: {', '.join(summary_parts)}")

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
    atomic_write_json(path, episode)
