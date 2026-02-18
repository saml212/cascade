"""BaseAgent ABC — foundation for all Cascade pipeline agents."""

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger("cascade")


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

    def report_progress(self, current: int, total: int, detail: str = ""):
        """Write progress.json so the API can report real-time status."""
        progress = {
            "agent": self.name,
            "current": current,
            "total": total,
            "percent": round(current / total * 100, 1) if total > 0 else 0,
            "detail": detail,
        }
        path = self.episode_dir / "progress.json"
        with open(path, "w") as f:
            json.dump(progress, f)

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

    def save_json(self, filename: str, data: dict):
        """Save a JSON file to the episode directory."""
        path = self.episode_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
