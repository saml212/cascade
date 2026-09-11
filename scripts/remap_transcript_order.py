#!/usr/bin/env python3
"""Remap a camera-audio transcript after source chunks are reordered.

This avoids another ASR request when every source chunk is present unchanged.
The command plans and validates in memory unless --apply is explicitly passed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

TRANSCRIPT_FILES = (
    "transcript.json",
    "diarized_transcript.json",
    "subtitles/transcript.srt",
    "transcribe.json",
)
DOWNSTREAM_MARKERS = (
    "clips.json",
    "segments.json",
    "clip_miner.json",
    "longform_render.json",
    "shorts_render.json",
    "metadata_gen.json",
    "thumbnail_gen.json",
    "qa.json",
    "podcast_feed.json",
    "publish.json",
    "backup.json",
    "metadata/metadata.json",
    "qa/qa.json",
)
DOWNSTREAM_AGENTS = {
    "speaker_cut",
    "clip_miner",
    "longform_render",
    "shorts_render",
    "metadata_gen",
    "thumbnail_gen",
    "qa",
    "podcast_feed",
    "publish",
    "backup",
}


def _parse_order(value: str) -> list[str]:
    result = [part.strip() for part in value.split(",") if part.strip()]
    if len(result) != len(set(result)):
        raise ValueError("An order cannot contain duplicate filenames")
    return result


def _chunks(files: list[dict], order: list[str]) -> list[dict]:
    by_name = {item["filename"]: item for item in files}
    if set(order) != set(by_name) or len(order) != len(files):
        raise ValueError("Each order must name every ingest file exactly once")
    elapsed = 0.0
    chunks = []
    for name in order:
        duration = float(by_name[name]["duration_seconds"])
        chunks.append({"name": name, "start": elapsed, "end": elapsed + duration})
        elapsed += duration
    return chunks


def _chunk_for_time(chunks: list[dict], value: float) -> dict:
    for index, chunk in enumerate(chunks):
        if chunk["start"] <= value < chunk["end"] or (
            index == len(chunks) - 1 and value <= chunk["end"] + 0.05
        ):
            return chunk
    raise ValueError(f"Timestamp {value:.3f}s lies outside the source timeline")


def _offsets(old: list[dict], new: list[dict]) -> dict[str, float]:
    new_starts = {chunk["name"]: chunk["start"] for chunk in new}
    return {chunk["name"]: new_starts[chunk["name"]] - chunk["start"] for chunk in old}


def _remap_word(
    word: dict, old: list[dict], offsets: dict[str, float]
) -> tuple[str, dict]:
    midpoint = (float(word["start"]) + float(word["end"])) / 2
    chunk = _chunk_for_time(old, midpoint)
    mapped = copy.deepcopy(word)
    mapped["start"] = float(mapped["start"]) + offsets[chunk["name"]]
    mapped["end"] = float(mapped["end"]) + offsets[chunk["name"]]
    return chunk["name"], mapped


def _text(words: list[dict]) -> str:
    return " ".join(word.get("punctuated_word", word.get("word", "")) for word in words)


def _remap_utterances(
    utterances: list[dict], old: list[dict], offsets: dict[str, float]
) -> list[dict]:
    remapped = []
    for utterance in utterances:
        groups: dict[str, list[dict]] = defaultdict(list)
        for word in utterance.get("words", []):
            name, mapped = _remap_word(word, old, offsets)
            groups[name].append(mapped)
        if not groups:
            midpoint = (float(utterance["start"]) + float(utterance["end"])) / 2
            chunk = _chunk_for_time(old, midpoint)
            item = copy.deepcopy(utterance)
            item["start"] += offsets[chunk["name"]]
            item["end"] += offsets[chunk["name"]]
            remapped.append(item)
            continue
        for words in groups.values():
            item = {
                key: copy.deepcopy(value)
                for key, value in utterance.items()
                if key != "words"
            }
            item["words"] = words
            item["start"] = min(word["start"] for word in words)
            item["end"] = max(word["end"] for word in words)
            item["transcript"] = _text(words)
            remapped.append(item)
    return sorted(remapped, key=lambda item: (item["start"], item["end"]))


def _paragraphs(utterances: list[dict]) -> dict:
    paragraphs = []
    for utterance in utterances:
        words = utterance.get("words", [])
        paragraphs.append(
            {
                "sentences": [
                    {
                        "text": utterance["transcript"],
                        "start": utterance["start"],
                        "end": utterance["end"],
                    }
                ],
                "speaker": utterance.get("speaker", 0),
                "num_words": len(words),
                "start": utterance["start"],
                "end": utterance["end"],
            }
        )
    rendered = "\n\n".join(
        f"Speaker {item.get('speaker', 0)}: {item['transcript']}" for item in utterances
    )
    return {"transcript": rendered, "paragraphs": paragraphs}


def _remap_raw(raw: dict, old: list[dict], offsets: dict[str, float]) -> dict:
    result = copy.deepcopy(raw)
    utterances = _remap_utterances(result["results"]["utterances"], old, offsets)
    result["results"]["utterances"] = utterances
    result.setdefault("metadata", {})["source_order_remapped"] = {
        "old": [chunk["name"] for chunk in old],
        "offset_seconds_by_file": offsets,
    }
    for channel in result["results"]["channels"]:
        for alternative in channel["alternatives"]:
            words = [
                _remap_word(word, old, offsets)[1]
                for word in alternative.get("words", [])
            ]
            words.sort(key=lambda word: (word["start"], word["end"]))
            alternative["words"] = words
            alternative["transcript"] = " ".join(
                word.get("punctuated_word", word["word"]) for word in words
            )
            alternative["paragraphs"] = _paragraphs(utterances)
    return result


def _diarized(raw: dict, original: dict) -> dict:
    utterances = []
    for item in raw["results"]["utterances"]:
        words = item.get("words", [])
        utterances.append(
            {
                "speaker": item.get(
                    "speaker", words[0].get("speaker", 0) if words else 0
                ),
                "start": item["start"],
                "end": item["end"],
                "text": item.get("transcript", ""),
                "confidence": item.get("confidence", 0),
                "words": [
                    {
                        key: word[key]
                        for key in ("word", "start", "end", "confidence", "speaker")
                        if key in word
                    }
                    for word in words
                ],
            }
        )
    result = {"mode": original.get("mode", "diarized"), "utterances": utterances}
    if "speaker_map" in original:
        result["speaker_map"] = original["speaker_map"]
    return result


def _timecode(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _srt(utterances: list[dict]) -> str:
    words = [word for item in utterances for word in item.get("words", [])]
    words.sort(key=lambda word: (word["start"], word["end"]))
    blocks = []
    for number, index in enumerate(range(0, len(words), 5), 1):
        group = words[index : index + 5]
        blocks.append(
            f"{number}\n{_timecode(group[0]['start'])} --> {_timecode(group[-1]['end'])}\n{_text(group)}\n"
        )
    return "\n".join(blocks)


def _write_atomic(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".remapping")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(content)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True, type=Path)
    parser.add_argument(
        "--old-order", required=True, help="comma-separated original stitch order"
    )
    parser.add_argument("--new-order", help="defaults to the current ingest.json order")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    episode = args.episode.expanduser().resolve()
    ingest = json.loads((episode / "ingest.json").read_text())
    old_order = _parse_order(args.old_order)
    new_order = (
        _parse_order(args.new_order)
        if args.new_order
        else [item["filename"] for item in ingest["files"]]
    )
    old_chunks = _chunks(ingest["files"], old_order)
    new_chunks = _chunks(ingest["files"], new_order)
    offsets = _offsets(old_chunks, new_chunks)

    raw_original = json.loads((episode / "transcript.json").read_text())
    diarized_original = json.loads((episode / "diarized_transcript.json").read_text())
    raw = _remap_raw(raw_original, old_chunks, offsets)
    diarized = _diarized(raw, diarized_original)
    utterances = diarized["utterances"]
    words = [word for item in utterances for word in item["words"]]
    if any(a["start"] > b["start"] for a, b in pairwise(words)):
        raise ValueError("Remapped word timestamps are not chronological")
    speakers_before = {
        word.get("speaker")
        for item in diarized_original["utterances"]
        for word in item.get("words", [])
    }
    speakers_after = {word.get("speaker") for word in words}
    if speakers_before != speakers_after:
        raise ValueError(f"Speaker IDs changed: {speakers_before} -> {speakers_after}")

    stale = [name for name in DOWNSTREAM_MARKERS if (episode / name).exists()]
    split_count = len(raw["results"]["utterances"]) - len(
        raw_original["results"]["utterances"]
    )
    print("Chunk remap:")
    for chunk in old_chunks:
        print(f"  {chunk['name']}: {offsets[chunk['name']]:+.3f}s")
    print(f"Words: {len(words)}, utterances split at seams: {split_count}")
    print(f"Speaker IDs preserved: {sorted(speakers_after)}")
    print(f"Final transcript word: {words[-1]['end']:.3f}s {words[-1]['word']!r}")
    print(f"Stale downstream artifacts: {stale or 'none'}")
    if stale:
        raise RuntimeError(
            "Move or remove stale downstream artifacts before applying the transcript remap"
        )
    if not args.apply:
        print("Plan only. Re-run with --apply to install the validated remap.")
        return 0

    backup = episode / "work" / "transcript-before-source-reorder"
    if backup.exists():
        raise FileExistsError(f"Transcript backup already exists: {backup}")
    backup.mkdir(parents=True)
    for name in TRANSCRIPT_FILES:
        source = episode / name
        if source.exists():
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    stale_audio = episode / "work" / "audio.m4a"
    if stale_audio.exists():
        shutil.move(stale_audio, backup / "audio.m4a")
    stale_previews = episode / "work" / "audio_preview"
    if stale_previews.exists():
        shutil.move(stale_previews, backup / "audio_preview")

    _write_atomic(episode / "transcript.json", json.dumps(raw, indent=2) + "\n")
    _write_atomic(
        episode / "diarized_transcript.json", json.dumps(diarized, indent=2) + "\n"
    )
    _write_atomic(
        episode / "subtitles" / "transcript.srt", _srt(raw["results"]["utterances"])
    )
    transcribe_path = episode / "transcribe.json"
    transcribe = json.loads(transcribe_path.read_text())
    transcribe.update(
        {
            "utterance_count": len(utterances),
            "word_count": len(words),
            "source_order_remapped": {"old": old_order, "new": new_order},
            "_status": "completed",
        }
    )
    _write_atomic(transcribe_path, json.dumps(transcribe, indent=2) + "\n")
    episode_path = episode / "episode.json"
    episode_data = json.loads(episode_path.read_text())
    pipeline = episode_data.get("pipeline")
    if isinstance(pipeline, dict):
        completed = pipeline.get("agents_completed", [])
        pipeline["agents_completed"] = [
            name for name in completed if name not in DOWNSTREAM_AGENTS
        ]
        if "transcribe" not in pipeline["agents_completed"]:
            pipeline["agents_completed"].append("transcribe")
        pipeline["completed_at"] = None
    _write_atomic(episode_path, json.dumps(episode_data, indent=2) + "\n")
    print(f"Installed transcript remap; originals retained in {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
