"""BaseAgent ABC — foundation for all Cascade pipeline agents."""

import json
import logging
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from lib.atomic_write import atomic_write_json

logger = logging.getLogger("cascade")


def timed_ffmpeg(cmd: list, agent_logger=None, **kwargs) -> subprocess.CompletedProcess:
    """Run an ffmpeg/ffprobe subprocess with timing. Logs command summary and elapsed time."""
    start = time.time()
    result = subprocess.run(cmd, **kwargs)
    elapsed = time.time() - start
    # Log a short summary: binary name + key args
    binary = Path(cmd[0]).name
    summary = f"{binary} ({elapsed:.1f}s)"
    if agent_logger:
        agent_logger.info(f"  {summary}")
    return result


class BaseAgent(ABC):
    """Abstract base class for pipeline agents.

    Each agent receives an episode directory and produces a JSON output
    file alongside any media artifacts.
    """

    name: str = "base"

    def __init__(self, episode_dir: Path, config: dict):
        self.episode_dir = Path(episode_dir)
        self.config = config
        self.logger = logging.getLogger(f"cascade.{self.name}")
        self._progress_start_time: Optional[float] = None

    def report_progress(self, current: int, total: int, detail: str = ""):
        """Write progress.json so the API can report real-time status.

        Tracks the first call's timestamp to compute eta_seconds for
        later calls. The frontend's ProgressBar component reads
        `{percent, eta_seconds, detail}` — total and current are there
        too for older clients.
        """
        now = time.time()
        if self._progress_start_time is None:
            self._progress_start_time = now

        eta_seconds: Optional[float] = None
        if current > 0 and total > current:
            elapsed = now - self._progress_start_time
            if elapsed > 0:
                per_unit = elapsed / current
                remaining_units = total - current
                eta_seconds = round(per_unit * remaining_units, 1)

        progress = {
            "agent": self.name,
            "current": current,
            "total": total,
            "percent": round(current / total * 100, 1) if total > 0 else 0,
            "eta_seconds": eta_seconds,
            "detail": detail,
            "updated_at": now,
        }
        atomic_write_json(self.episode_dir / "progress.json", progress, indent=0)

    @abstractmethod
    def execute(self) -> dict:
        """Run the agent's core logic. Return a result dict."""
        ...

    def run(self) -> dict:
        """Execute with timing, logging, and JSON output."""
        self.logger.info(f"[{self.name}] Starting...")
        start = time.time()

        try:
            result = self.execute()
            elapsed = time.time() - start
            result["_agent"] = self.name
            result["_elapsed_seconds"] = round(elapsed, 2)
            result["_status"] = "completed"
            self.logger.info(f"[{self.name}] Completed in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - start
            result = {
                "_agent": self.name,
                "_elapsed_seconds": round(elapsed, 2),
                "_status": "failed",
                "_error": str(e),
            }
            self.logger.error(f"[{self.name}] Failed after {elapsed:.1f}s: {e}")
            raise

        # Write agent output JSON
        out_path = self.episode_dir / f"{self.name}.json"
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, default=str)

        return result

    def load_json(self, filename: str) -> dict:
        """Load a JSON file from the episode directory."""
        path = self.episode_dir / filename
        with open(path) as f:
            return json.load(f)

    def load_json_safe(self, filename: str, default: dict | None = None) -> dict:
        """Load a JSON file, returning default (empty dict) on missing/invalid file."""
        try:
            return self.load_json(filename)
        except (FileNotFoundError, json.JSONDecodeError):
            return default if default is not None else {}

    def get_config(self, *keys: str, default=None):
        """Traverse nested config keys. e.g. get_config('processing', 'video_crf', default=22)."""
        val = self.config
        for key in keys:
            if isinstance(val, dict):
                val = val.get(key)
            else:
                return default
            if val is None:
                return default
        return val

    def save_json(self, filename: str, data: dict):
        """Save a JSON file to the episode directory (atomic write)."""
        path = self.episode_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
