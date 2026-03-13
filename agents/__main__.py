"""CLI entry point: python -m agents --source-path /path/to/media

Thin wrapper around the Cascade API server (localhost:8420).
Requires the server to be running — start it with ./start.sh
"""

import argparse
import signal
import sys
import time
from pathlib import Path

BASE_URL = "http://localhost:8420"


def _check_server(client) -> bool:
    """Return True if the API server is reachable."""
    try:
        resp = client.get(f"{BASE_URL}/api/episodes/", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _create_episode(client, source_path, audio_path, speaker_count) -> str:
    """Create a new episode via POST /api/episodes/ and return the episode_id."""
    body = {"source_path": source_path}
    if audio_path:
        body["audio_path"] = audio_path
    if speaker_count is not None:
        body["speaker_count"] = speaker_count

    resp = client.post(f"{BASE_URL}/api/episodes/", json=body)
    resp.raise_for_status()
    data = resp.json()
    return data["episode_id"]


def _start_pipeline(client, episode_id, source_path, audio_path, agents):
    """Trigger pipeline via POST /api/episodes/{id}/run-pipeline."""
    body = {}
    if source_path:
        body["source_path"] = source_path
    if audio_path:
        body["audio_path"] = audio_path
    if agents:
        body["agents"] = agents

    resp = client.post(f"{BASE_URL}/api/episodes/{episode_id}/run-pipeline", json=body)
    if resp.status_code == 409:
        print(f"Pipeline already running for {episode_id}")
        return True
    resp.raise_for_status()
    data = resp.json()
    print(f"Pipeline started: {data.get('status')}")
    return True


def _cancel_pipeline(client, episode_id):
    """Request pipeline cancellation."""
    try:
        resp = client.post(
            f"{BASE_URL}/api/episodes/{episode_id}/cancel-pipeline",
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"\nPipeline cancellation: {data.get('status')}")
        else:
            print(f"\nCancel request failed (HTTP {resp.status_code})")
    except Exception:
        print("\nCould not reach server to cancel pipeline")


def _poll_status(client, episode_id):
    """Poll pipeline status every 5 seconds until complete. Returns final status dict."""
    last_agents = []
    last_current = None

    while True:
        try:
            resp = client.get(
                f"{BASE_URL}/api/episodes/{episode_id}/pipeline-status",
                timeout=10,
            )
        except Exception as e:
            print(f"  [warning] Could not reach server: {e}")
            time.sleep(5)
            continue

        if resp.status_code == 404:
            print(f"  Episode {episode_id} not found (may have been renamed)")
            # Try to find the renamed episode
            try:
                list_resp = client.get(f"{BASE_URL}/api/episodes/", timeout=5)
                if list_resp.status_code == 200:
                    episodes = list_resp.json()
                    # Look for an episode that starts with the same date prefix
                    prefix = episode_id.split("_")[0:2]  # ["ep", "YYYY-MM-DD"]
                    for ep in episodes:
                        ep_id = ep.get("episode_id", "")
                        ep_prefix = ep_id.split("_")[0:2]
                        if ep_prefix == prefix and ep_id != episode_id:
                            print(f"  Episode renamed to: {ep_id}")
                            episode_id = ep_id
                            break
            except Exception:
                pass
            time.sleep(5)
            continue

        if resp.status_code != 200:
            print(f"  [warning] Unexpected status code: {resp.status_code}")
            time.sleep(5)
            continue

        data = resp.json()
        status = data.get("status", "unknown")
        is_running = data.get("is_running", False)
        current_agent = data.get("current_agent")
        agents_completed = data.get("agents_completed", [])
        errors = data.get("errors", {})
        progress = data.get("progress")

        # Print newly completed agents
        new_agents = [a for a in agents_completed if a not in last_agents]
        for agent in new_agents:
            if agent in errors:
                print(f"  [FAIL] {agent}: {errors[agent]}")
            else:
                print(f"  [done] {agent}")
        last_agents = list(agents_completed)

        # Print current agent if changed
        if current_agent and current_agent != last_current:
            progress_str = ""
            if progress and progress.get("agent") == current_agent:
                pct = progress.get("percent", 0)
                msg = progress.get("message", "")
                progress_str = f" ({pct}% — {msg})" if msg else f" ({pct}%)"
            print(f"  [running] {current_agent}{progress_str}")
            last_current = current_agent
        elif current_agent and current_agent == last_current and progress:
            # Same agent, but progress may have updated
            if progress.get("agent") == current_agent:
                pct = progress.get("percent", 0)
                msg = progress.get("message", "")
                if pct > 0:
                    sys.stdout.write(f"\r  [running] {current_agent} ({pct}%{' — ' + msg if msg else ''})")
                    sys.stdout.flush()

        # Check terminal states
        if status in ("completed", "failed", "error", "cancelled"):
            print()
            return data, episode_id

        if status == "awaiting_crop_setup":
            print()
            print("Pipeline paused: awaiting crop setup.")
            print(f"Configure speaker crop points in the web UI, then resume:")
            print(f"  curl -X POST {BASE_URL}/api/episodes/{episode_id}/resume-pipeline")
            return data, episode_id

        if status == "awaiting_backup_approval":
            print()
            print("Pipeline paused: awaiting backup approval.")
            print(f"  curl -X POST {BASE_URL}/api/episodes/{episode_id}/approve-backup")
            return data, episode_id

        if not is_running and status not in ("processing",):
            # Pipeline finished but status isn't a standard terminal state
            print()
            return data, episode_id

        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(
        description="Cascade — podcast automation pipeline (API client)"
    )
    parser.add_argument(
        "--source-path",
        nargs="+",
        required=True,
        help="Path(s) to source media (directory, single file, or multiple files)",
    )
    parser.add_argument(
        "--audio-path",
        default=None,
        help="Path to external audio recorder directory (e.g., Zoom H6E SD card folder)",
    )
    parser.add_argument(
        "--speaker-count",
        type=int,
        default=None,
        help="Number of speakers (for multi-track audio mapping)",
    )
    parser.add_argument(
        "--episode-id",
        default=None,
        help="Episode ID (auto-generated if omitted)",
    )
    parser.add_argument(
        "--agents",
        nargs="+",
        default=None,
        help="Run only specific agents (e.g. --agents ingest stitch)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    args = parser.parse_args()

    # Validate all source paths exist
    source_paths = args.source_path
    for sp in source_paths:
        if not Path(sp).exists():
            print(f"Error: source path does not exist: {sp}")
            sys.exit(1)

    if args.audio_path and not Path(args.audio_path).exists():
        print(f"Error: audio path does not exist: {args.audio_path}")
        sys.exit(1)

    # Single path: pass as string; multiple: pass first (API expects string)
    source = source_paths[0] if len(source_paths) == 1 else source_paths[0]
    if len(source_paths) > 1:
        print(f"Warning: API only supports a single source_path. Using: {source}")

    import httpx

    client = httpx.Client(timeout=30)

    # Check server is running
    if not _check_server(client):
        print("Error: Cascade API server is not running at localhost:8420")
        print()
        print("Start the server first:")
        print("  ./start.sh")
        print()
        print("Then re-run this command.")
        sys.exit(1)

    episode_id = args.episode_id

    # Create episode if no ID provided
    if not episode_id:
        episode_id = _create_episode(
            client, source, args.audio_path, args.speaker_count
        )
        print(f"Created episode: {episode_id}")
    else:
        print(f"Using existing episode: {episode_id}")

    # Start the pipeline
    print(f"Starting pipeline for {episode_id}...")
    _start_pipeline(client, episode_id, source, args.audio_path, args.agents)

    # Set up Ctrl+C handler
    cancelled = False

    def _handle_sigint(sig, frame):
        nonlocal cancelled
        if cancelled:
            print("\nForce quit.")
            sys.exit(1)
        cancelled = True
        print("\nCancelling pipeline...")
        _cancel_pipeline(client, episode_id)
        sys.exit(130)

    signal.signal(signal.SIGINT, _handle_sigint)

    # Poll for status
    print("Polling pipeline status...")
    final_status, episode_id = _poll_status(client, episode_id)

    status = final_status.get("status", "unknown")
    completed = final_status.get("agents_completed", [])
    errors = final_status.get("errors", {})

    print(f"Pipeline {status}: {episode_id}")
    print(f"Agents completed: {', '.join(completed) if completed else 'none'}")
    if errors:
        print(f"Errors:")
        for agent, error in errors.items():
            print(f"  {agent}: {error}")

    # Exit with non-zero if failed/error
    if status in ("failed", "error"):
        sys.exit(1)
    elif status == "cancelled":
        sys.exit(130)


if __name__ == "__main__":
    main()
