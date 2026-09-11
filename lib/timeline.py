"""Map source-media timestamps through non-destructive episode edits.

Transcript words, speaker segments, edit decisions, and clip candidates use the
source clock.  Renderers create a :class:`Timeline` at the output boundary and
use its retained source intervals for both video and audio.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """One retained source interval and its position in the edited output."""

    source_start: float
    source_end: float
    output_start: float

    @property
    def duration(self) -> float:
        return self.source_end - self.source_start

    @property
    def output_end(self) -> float:
        return self.output_start + self.duration


class Timeline:
    """Immutable source-to-output mapping for a set of retained intervals."""

    def __init__(
        self, source_duration: float, intervals: Iterable[tuple[float, float]]
    ):
        self.source_duration = _finite_number(source_duration, "source duration")
        if self.source_duration < 0:
            raise ValueError("Source duration cannot be negative")

        normalized: list[tuple[float, float]] = []
        for raw_start, raw_end in intervals:
            start = _finite_number(raw_start, "interval start")
            end = _finite_number(raw_end, "interval end")
            if start < 0 or end > self.source_duration + 0.01 or end <= start:
                raise ValueError(f"Invalid source interval {start:.3f}-{end:.3f}s")
            start = max(0.0, start)
            end = min(self.source_duration, end)
            if normalized and start < normalized[-1][1] - 1e-9:
                raise ValueError("Source intervals must be ordered and non-overlapping")
            if normalized and math.isclose(start, normalized[-1][1], abs_tol=1e-9):
                normalized[-1] = (normalized[-1][0], end)
            else:
                normalized.append((start, end))

        output_start = 0.0
        spans = []
        for start, end in normalized:
            spans.append(Span(start, end, output_start))
            output_start += end - start
        self.spans = tuple(spans)

    @classmethod
    def from_edits(
        cls,
        duration: float,
        edits: Iterable[Mapping[str, object]] = (),
        *,
        minimum_interval: float = 0.1,
    ) -> Timeline:
        """Build a timeline after validating trims and interior cuts."""
        duration = _finite_number(duration, "source duration")
        if duration <= 0:
            raise ValueError("Source duration must be positive")
        minimum_interval = _finite_number(minimum_interval, "minimum interval")
        if minimum_interval < 0:
            raise ValueError("Minimum interval cannot be negative")

        trim_start = 0.0
        trim_end = duration
        cuts: list[tuple[float, float]] = []
        for edit in edits:
            kind = edit.get("type")
            if kind == "trim_start":
                trim_start = max(
                    trim_start,
                    _finite_number(edit.get("seconds"), "trim_start seconds"),
                )
            elif kind == "trim_end":
                trim_end = min(
                    trim_end, _finite_number(edit.get("seconds"), "trim_end seconds")
                )
            elif kind == "cut":
                cut_start = _finite_number(
                    edit.get("start_seconds"), "cut start_seconds"
                )
                cut_end = _finite_number(edit.get("end_seconds"), "cut end_seconds")
                if cut_end <= cut_start:
                    raise ValueError(
                        f"Invalid cut range {cut_start:.3f}-{cut_end:.3f}s"
                    )
                cuts.append((cut_start, cut_end))
            else:
                raise ValueError(f"Unsupported longform edit type: {kind!r}")

        if trim_start < 0 or trim_end > duration + 0.01 or trim_start >= trim_end:
            raise ValueError(f"Invalid trim range {trim_start:.3f}-{trim_end:.3f}s")
        trim_start = max(0.0, trim_start)
        trim_end = min(duration, trim_end)

        for cut_start, cut_end in cuts:
            if cut_start < 0 or cut_end > duration + 0.01:
                raise ValueError(f"Invalid cut range {cut_start:.3f}-{cut_end:.3f}s")

        intervals = [(trim_start, trim_end)]
        for cut_start, cut_end in _merge_ranges(cuts):
            next_intervals = []
            for start, end in intervals:
                if cut_end <= start or cut_start >= end:
                    next_intervals.append((start, end))
                    continue
                if start < cut_start:
                    next_intervals.append((start, cut_start))
                if cut_end < end:
                    next_intervals.append((cut_end, end))
            intervals = next_intervals

        intervals = [
            (start, end) for start, end in intervals if end - start >= minimum_interval
        ]
        if not intervals:
            raise ValueError("Longform edits remove the entire episode")
        return cls(duration, intervals)

    @property
    def keep_intervals(self) -> tuple[tuple[float, float], ...]:
        return tuple((span.source_start, span.source_end) for span in self.spans)

    @property
    def duration(self) -> float:
        return self.spans[-1].output_end if self.spans else 0.0

    def source_to_output(self, timestamp: float) -> float | None:
        """Return the edited timestamp, or ``None`` for removed source material."""
        timestamp = _finite_number(timestamp, "source timestamp")
        for span in self.spans:
            if span.source_start <= timestamp < span.source_end:
                return span.output_start + timestamp - span.source_start
        if self.spans and math.isclose(
            timestamp, self.spans[-1].source_end, abs_tol=1e-9
        ):
            return self.duration
        return None

    def output_to_source(self, timestamp: float) -> float:
        """Return the source timestamp represented by an edited timestamp."""
        timestamp = _finite_number(timestamp, "output timestamp")
        if timestamp < 0 or timestamp > self.duration + 1e-9 or not self.spans:
            raise ValueError(
                f"Output timestamp {timestamp:.3f}s is outside the timeline"
            )
        if math.isclose(timestamp, self.duration, abs_tol=1e-9):
            return self.spans[-1].source_end
        for span in self.spans:
            if span.output_start <= timestamp < span.output_end:
                return span.source_start + timestamp - span.output_start
        raise ValueError(f"Output timestamp {timestamp:.3f}s is outside the timeline")

    def source_ranges(self, start: float, end: float) -> list[tuple[float, float]]:
        """Intersect a source-clock range with retained material."""
        start = _finite_number(start, "range start")
        end = _finite_number(end, "range end")
        if start < 0 or end <= start or end > self.source_duration + 0.01:
            raise ValueError(f"Invalid source range {start:.3f}-{end:.3f}s")
        end = min(end, self.source_duration)
        return [
            (max(start, span.source_start), min(end, span.source_end))
            for span in self.spans
            if min(end, span.source_end) > max(start, span.source_start)
        ]

    def slice(self, start: float, end: float) -> Timeline:
        """Return a zero-based output mapping for retained material in a source range."""
        return Timeline(self.source_duration, self.source_ranges(start, end))

    def project(
        self,
        items: Iterable[Mapping[str, object]],
        *,
        start_key: str = "start",
        end_key: str = "end",
        output_clock: bool = False,
        minimum_duration: float = 0.0,
    ) -> list[dict]:
        """Split timed records at edits and optionally express them on the output clock."""
        projected = []
        for item in items:
            item_start = _finite_number(item.get(start_key), start_key)
            item_end = _finite_number(item.get(end_key), end_key)
            if item_end <= item_start:
                continue
            for span in self.spans:
                source_start = max(item_start, span.source_start)
                source_end = min(item_end, span.source_end)
                if source_end - source_start < minimum_duration:
                    continue
                record = dict(item)
                if output_clock:
                    record["source_start"] = source_start
                    record["source_end"] = source_end
                    record[start_key] = (
                        span.output_start + source_start - span.source_start
                    )
                    record[end_key] = span.output_start + source_end - span.source_start
                else:
                    record[start_key] = source_start
                    record[end_key] = source_end
                if "duration" in record:
                    record["duration"] = record[end_key] - record[start_key]
                projected.append(record)
        return projected


def build_keep_intervals(
    duration: float, edits: Iterable[Mapping[str, object]]
) -> list[tuple[float, float]]:
    """Compatibility wrapper returning retained source intervals."""
    return list(Timeline.from_edits(duration, edits).keep_intervals)


def rebase_diarized(diarized: Mapping[str, object], timeline: Timeline) -> dict:
    """Copy a diarized transcript onto a timeline's zero-based output clock.

    Words whose midpoint was removed are excluded. A word straddling an edit is
    clipped to its largest retained piece so the caption is never duplicated.
    """
    result = dict(diarized)
    utterances = []
    for utterance in diarized.get("utterances", []):
        rebased_words = []
        for word in utterance.get("words", []):
            start = _finite_number(word.get("start"), "word start")
            end = _finite_number(word.get("end"), "word end")
            if end <= start:
                continue
            overlaps = []
            for span in timeline.spans:
                overlap_start = max(start, span.source_start)
                overlap_end = min(end, span.source_end)
                if overlap_end > overlap_start:
                    overlaps.append(
                        (overlap_end - overlap_start, span, overlap_start, overlap_end)
                    )
            if not overlaps:
                continue
            _, span, source_start, source_end = max(
                overlaps, key=lambda value: value[0]
            )
            rebased = dict(word)
            rebased["source_start"] = source_start
            rebased["source_end"] = source_end
            rebased["start"] = span.output_start + source_start - span.source_start
            rebased["end"] = span.output_start + source_end - span.source_start
            rebased_words.append(rebased)

        if not rebased_words:
            continue
        rebased_utterance = dict(utterance)
        rebased_utterance["source_start"] = min(
            word["source_start"] for word in rebased_words
        )
        rebased_utterance["source_end"] = max(
            word["source_end"] for word in rebased_words
        )
        rebased_utterance["start"] = rebased_words[0]["start"]
        rebased_utterance["end"] = rebased_words[-1]["end"]
        rebased_utterance["words"] = rebased_words
        utterances.append(rebased_utterance)
    result["utterances"] = utterances
    return result


def _finite_number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _merge_ranges(ranges: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
