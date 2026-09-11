#!/usr/bin/env python3
"""Safely rebuild an episode's merged source in camera segment order.

The command is read-only unless --apply is supplied.  It stages and validates
the complete replacement before removing the generated source_merged.mp4.
Original camera files are never changed or removed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ffprobe = importlib.import_module("lib.ffprobe").probe


SEGMENT_RE = re.compile(r"_(\d+)_D\.MP4$", re.IGNORECASE)


def _segment_number(file_info: dict) -> int:
    match = SEGMENT_RE.search(file_info["filename"])
    if not match:
        raise ValueError(
            f"Cannot infer camera segment number from {file_info['filename']!r}; "
            "pass --order with every filename explicitly"
        )
    return int(match.group(1))


def _ordered(files: list[dict], names: list[str] | None) -> list[dict]:
    by_name = {item["filename"]: item for item in files}
    if len(by_name) != len(files):
        raise ValueError("ingest.json contains duplicate filenames")
    if names:
        if len(names) != len(files) or set(names) != set(by_name):
            missing = sorted(set(by_name) - set(names))
            unknown = sorted(set(names) - set(by_name))
            raise ValueError(
                "--order must name every ingest file exactly once; "
                f"missing={missing}, unknown={unknown}"
            )
        return [by_name[name] for name in names]
    numbered = [(_segment_number(item), item) for item in files]
    numbers = [number for number, _ in numbered]
    if len(set(numbers)) != len(numbers):
        raise ValueError("Camera segment numbers are not unique; pass --order")
    return [item for _, item in sorted(numbered)]


def _source_path(item: dict) -> Path:
    for key in ("dest_path", "source_path"):
        candidate = Path(item.get(key, ""))
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Neither copied nor archived source exists for {item['filename']}"
    )


def _validate_sources(files: list[dict]) -> None:
    for item in files:
        path = _source_path(item)
        recorded_size = item.get("size_bytes")
        if recorded_size and path.stat().st_size != recorded_size:
            raise ValueError(f"Source size changed: {path}")


def _concat_list(path: Path, files: list[dict]) -> None:
    def escaped(value: str) -> str:
        return value.replace("'", "'\\''")

    path.write_text(
        "".join(f"file '{escaped(str(_source_path(item)))}'\n" for item in files)
    )


def _validate_output(path: Path, expected_duration: float, first_source: Path) -> float:
    output = ffprobe(path)
    source = ffprobe(first_source)
    duration = float(output["format"]["duration"])
    if abs(duration - expected_duration) > 2.0:
        raise ValueError(
            f"Staged duration {duration:.3f}s differs from expected "
            f"{expected_duration:.3f}s"
        )
    # Camera telemetry and attached JPEG thumbnails are not episode media.
    output_streams = [
        (s.get("codec_type"), s.get("codec_name"))
        for s in output["streams"]
        if s.get("codec_type") in {"audio", "video"}
        and not s.get("disposition", {}).get("attached_pic")
    ]
    source_streams = [
        (s.get("codec_type"), s.get("codec_name"))
        for s in source["streams"]
        if s.get("codec_type") in {"audio", "video"}
        and not s.get("disposition", {}).get("attached_pic")
    ]
    if output_streams != source_streams:
        raise ValueError(
            f"Staged streams {output_streams} differ from source {source_streams}"
        )
    return duration


def _write_json_atomic(path: Path, payload: dict) -> None:
    temp = path.with_suffix(path.suffix + ".repairing")
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True, type=Path)
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument(
        "--order",
        help="comma-separated filenames; defaults to the numeric _NNNN_D segment suffix",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--reuse-staged",
        action="store_true",
        help="Validate and install the previously staged repair",
    )
    args = parser.parse_args()

    episode = args.episode.expanduser().resolve()
    staging_dir = args.staging_dir.expanduser().resolve()
    ingest_path = episode / "ingest.json"
    ingest = json.loads(ingest_path.read_text())
    current = ingest["files"]
    explicit = [part.strip() for part in args.order.split(",")] if args.order else None
    ordered = _ordered(current, explicit)
    _validate_sources(ordered)

    print(f"Episode: {episode}")
    print("Current order:")
    for item in current:
        print(f"  {item['filename']}  {item['duration_seconds']:.3f}s")
    print("Proposed order:")
    elapsed = 0.0
    for item in ordered:
        print(f"  {elapsed:10.3f}  {item['filename']}  {item['duration_seconds']:.3f}s")
        elapsed += float(item["duration_seconds"])
    print(f"Expected duration: {elapsed:.3f}s")

    if [item["filename"] for item in ordered] == [item["filename"] for item in current]:
        print("No order change is needed.")
        return 0
    if not args.apply:
        print(
            "Plan only. Re-run with --apply to stage, validate, and install the repair."
        )
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / f"{episode.name}-source_merged.repaired.mp4"
    concat = staging_dir / f"{episode.name}-concat.txt"
    partial = episode / "source_merged.mp4.repairing"
    merged = episode / "source_merged.mp4"
    if partial.exists() or (staged.exists() and not args.reuse_staged):
        raise FileExistsError(
            f"Remove prior repair artifact first: {staged if staged.exists() else partial}"
        )

    if args.reuse_staged:
        check_list = staging_dir / f"{episode.name}-concat.check.txt"
        _concat_list(check_list, ordered)
        if (
            not staged.is_file()
            or not concat.is_file()
            or check_list.read_text() != concat.read_text()
        ):
            raise ValueError(
                "Staged repair or matching source-order manifest is missing"
            )
        check_list.unlink()
    else:
        _concat_list(concat, ordered)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-c",
                "copy",
                str(staged),
            ],
            check=True,
        )
    first_source = _source_path(ordered[0])
    duration = _validate_output(staged, elapsed, first_source)
    _validate_sources(ordered)

    # The old file is derived and reproducible from the validated originals.
    # Removing it first permits cross-volume recovery when the episode volume
    # cannot hold both merged files.  Copy to a temporary name, then atomically
    # install so source_merged.mp4 is never a partial file.
    if merged.is_symlink() or merged.is_file():
        merged.unlink()
    shutil.copy2(staged, partial)
    copied_duration = _validate_output(partial, elapsed, first_source)
    os.replace(partial, merged)

    ingest["files"] = ordered
    ingest["duration_seconds"] = round(copied_duration, 3)
    ingest["total_duration_seconds"] = round(copied_duration, 3)
    _write_json_atomic(ingest_path, ingest)

    stitch_path = episode / "stitch.json"
    stitch = json.loads(stitch_path.read_text()) if stitch_path.exists() else {}
    stitch.update(
        {
            "output_path": str(merged),
            "input_count": len(ordered),
            "duration_seconds": round(copied_duration, 3),
            "expected_duration_seconds": round(elapsed, 3),
            "_status": "completed",
        }
    )
    _write_json_atomic(stitch_path, stitch)

    episode_path = episode / "episode.json"
    if episode_path.exists():
        episode_data = json.loads(episode_path.read_text())
        episode_data["source_path"] = str(merged)
        episode_data["duration_seconds"] = round(duration, 3)
        _write_json_atomic(episode_path, episode_data)

    print(f"Installed validated repair: {merged}")
    print(f"Staged safety copy retained: {staged}")
    print("Re-run transcribe and every downstream stage; their timelines are stale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
