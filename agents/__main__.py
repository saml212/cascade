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

    # Validate all source paths
    source_paths = args.source_path
    for sp in source_paths:
        if not Path(sp).exists():
            print(f"Error: source path does not exist: {sp}")
            sys.exit(1)

    if args.audio_path and not Path(args.audio_path).exists():
        print(f"Error: audio path does not exist: {args.audio_path}")
        sys.exit(1)

    # Single path: pass as string (backward compat); multiple: pass as list
    source = source_paths[0] if len(source_paths) == 1 else source_paths

    result = run_pipeline(
        source_path=source,
        audio_path=args.audio_path,
        speaker_count=args.speaker_count,
        episode_id=args.episode_id,
        agents=args.agents,
    )

    print(f"\nPipeline complete: {result['episode_id']}")
    print(f"Status: {result['status']}")
    completed = result["pipeline"]["agents_completed"]
    print(f"Agents completed: {', '.join(completed)}")


if __name__ == "__main__":
    main()
