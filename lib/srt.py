"""SRT subtitle utilities."""

from pathlib import Path


def fmt_timecode(seconds: float) -> str:
    """Format seconds as SRT timecode: HH:MM:SS,mmm"""
    seconds = max(0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def escape_srt_path(path: Path) -> str:
    """Escape a file path for use in ffmpeg subtitle filters.

    Handles backslashes, colons, and single quotes that would break
    ffmpeg's subtitle filter path parsing.
    """
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def generate_srt_from_diarized(diarized: dict, start: float, end: float, srt_path: Path, words_per_chunk: int = 4):
    """Slice word-level diarized transcript to a time range and write SRT.

    Groups words into chunks of `words_per_chunk` with times offset to be
    relative to `start`. Used by both longform and shorts render agents.
    """
    words = []
    for utt in diarized.get("utterances", []):
        for w in utt.get("words", []):
            w_start = w.get("start", 0)
            w_end = w.get("end", 0)
            if w_start >= start and w_end <= end:
                words.append(w)

    srt_lines = []
    idx = 1
    i = 0
    while i < len(words):
        chunk = words[i : i + words_per_chunk]
        t_start = chunk[0]["start"] - start
        t_end = chunk[-1]["end"] - start
        text = " ".join(w.get("word", "") for w in chunk)

        srt_lines.append(
            f"{idx}\n"
            f"{fmt_timecode(t_start)} --> {fmt_timecode(t_end)}\n"
            f"{text}\n"
        )
        idx += 1
        i += words_per_chunk

    with open(srt_path, "w") as f:
        f.write("\n".join(srt_lines))


def parse_srt(srt_path: Path) -> list[dict]:
    """Parse an SRT file into a list of {start, end, text} dicts."""
    entries = []
    try:
        with open(srt_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        return entries

    blocks = content.strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        timecode = lines[1]
        parts = timecode.split(" --> ")
        if len(parts) != 2:
            continue
        start = parse_srt_time(parts[0].strip())
        end = parse_srt_time(parts[1].strip())
        text = " ".join(lines[2:])
        entries.append({"start": start, "end": end, "text": text})
    return entries


def parse_srt_time(ts: str) -> float:
    """Parse SRT timestamp (HH:MM:SS,mmm) to seconds."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    if len(parts) != 3:
        return 0.0
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
