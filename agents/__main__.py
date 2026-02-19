"""CLI entry point: python -m agents --source-path /path/to/media"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from agents.pipeline import run_pipeline


def main():
    parser = argparse.ArgumentParser(
        description="Cascade — podcast automation pipeline"
    )
    parser.add_argument(
        "--source-path",
        required=True,
        help="Path to source media (SD card directory or single file)",
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
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    source = Path(args.source_path)
    if not source.exists():
        print(f"Error: source path does not exist: {source}")
        sys.exit(1)

    result = run_pipeline(
        source_path=str(source),
        episode_id=args.episode_id,
        agents=args.agents,
    )

    print(f"\nPipeline complete: {result['episode_id']}")
    print(f"Status: {result['status']}")
    completed = result["pipeline"]["agents_completed"]
    print(f"Agents completed: {', '.join(completed)}")


if __name__ == "__main__":
    main()
