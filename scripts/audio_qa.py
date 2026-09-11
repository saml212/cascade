#!/usr/bin/env python3
"""Run source-clock audio continuity checks for one Cascade episode."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.audio_qa import analyze_episode_audio, render_finding_preview


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect source-channel dropouts without relying on cached channel extracts."
        )
    )
    parser.add_argument("episode_dir", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        help="JSON destination (default: EPISODE/qa/audio-quality.json)",
    )
    parser.add_argument("--ffmpeg", help="FFmpeg executable or absolute path")
    parser.add_argument(
        "--preview-dir",
        type=Path,
        help="Render source and grounded fallback WAVs for blocking findings",
    )
    parser.add_argument(
        "--all-previews",
        action="store_true",
        help="With --preview-dir, also render non-blocking finding previews",
    )
    parser.add_argument(
        "--preview-limit",
        type=int,
        default=10,
        help="Maximum previews to render in one run (default: 10)",
    )
    args = parser.parse_args()

    if args.preview_limit < 1 or args.preview_limit > 100:
        parser.error("--preview-limit must be between 1 and 100")

    episode_dir = args.episode_dir.resolve()
    report_path = args.report or episode_dir / "qa" / "audio-quality.json"
    report = analyze_episode_audio(
        episode_dir,
        report_path=report_path,
        ffmpeg_bin=args.ffmpeg,
    )
    previews = []
    if args.preview_dir:
        blocking = set(report["release_gate"]["blocking_finding_ids"])
        for finding in report["findings"]:
            if args.all_previews or finding["id"] in blocking:
                previews.append(
                    render_finding_preview(
                        report["source"]["path"],
                        finding,
                        args.preview_dir,
                        ffmpeg_bin=args.ffmpeg,
                    )
                )
                if len(previews) >= args.preview_limit:
                    break

    result = {
        "report": str(report_path.resolve()),
        "fingerprint": report["fingerprint"],
        "release_gate": report["release_gate"],
        "finding_count": len(report["findings"]),
        "previews": previews,
    }
    print(json.dumps(result, indent=2))
    return 0 if report["release_gate"]["safe"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
