"""Advanced SubStation Alpha (.ass) subtitle generator for shorts.

Produces clean, mobile-readable burned-in captions for 9:16 short-form video.
Style is intentionally **static-per-phrase** (2-4 words per line, all words shown
together, no word-by-word highlighting). Per 2026 research, the bouncy
word-by-word "Submagic" style underperforms on tech/society interview content
because it reads as low-trust hustle-bro aesthetic. Clean static captions match
the niche of channels actually performing in this category (Lex / Dwarkesh /
Huberman / Acquired clips).

ASS gives us, vs the existing SRT path:
    - Real font selection (Helvetica / Inter / SF Pro) instead of libass default
    - Predictable sizing because PlayResX/Y is declared
    - Precise vertical margin (bottom-third positioning above the TikTok UI)
    - Proper outline + shadow control for readability on any background
    - Per-line styling overrides if we ever want a karaoke variant

The module is hermetic: pass it a diarized transcript dict and a (start, end)
range, get back ASS text. No I/O. Caller writes the file.

Why not use a third-party library: libass is already linked into the bundled
ffmpeg, the ASS format is plain text, and the only operation we need is
"transcript words → grouped phrases → styled dialogue lines." Adding
python-ass or pysubs2 would be more dependency than code.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from PIL import ImageFont

# ── styling defaults ────────────────────────────────────────────────────────

# Output canvas for shorts. ffmpeg `subtitles` filter scales ASS coordinates
# from PlayResX×PlayResY to the actual video size, so as long as these match
# the aspect ratio, fonts will render predictably.
DEFAULT_PLAY_RES_X = 1080
DEFAULT_PLAY_RES_Y = 1920

# Font: Helvetica is universally available on macOS where Cascade runs.
# libass falls back via fontconfig if the named font isn't found, so this is
# safe but predictable.
DEFAULT_FONT = "Helvetica"

# 72pt at 1080 wide is roughly 6.7% of frame width — readable on a phone,
# not so big that 3 words wrap.
DEFAULT_FONT_SIZE = 72

# ASS colors are &HAABBGGRR (alpha-blue-green-red, alpha INVERTED so 00=opaque).
# White opaque primary, black opaque outline.
COLOR_WHITE_OPAQUE = "&H00FFFFFF"
COLOR_BLACK_OPAQUE = "&H00000000"

# Heavy outline (4px scaled) so captions read on any background. No shadow —
# shadows look amateurish on talking-head clips.
DEFAULT_OUTLINE = 4
DEFAULT_SHADOW = 0

# Bottom-center alignment (ASS numpad: 2). Vertical margin keeps the caption
# above TikTok's bottom-third UI overlay (~280px from the bottom of a 1920px
# frame).
ALIGNMENT_BOTTOM_CENTER = 2
DEFAULT_MARGIN_V = 280
DEFAULT_MARGIN_H = 80

# Phrase grouping: 3 words/phrase keeps reading-speed comfortable and matches
# the 2-4 word range that interview-clip channels actually use.
DEFAULT_WORDS_PER_PHRASE = 3

# Don't let a phrase show for less than this — flickers below it. Don't let a
# phrase show longer than this either — feels stale.
MIN_PHRASE_DURATION = 0.4
MAX_PHRASE_DURATION = 2.5

CAPTION_SINGLE_LANE_VERSION = "single-lane/v1"
CAPTION_EVENT_GAP_SECONDS = 0.02
GAMEPLAY_CAPTION_WRAP_VERSION = "gameplay-center-word-wrap/v1"
GAMEPLAY_CAPTION_MAX_RASTER_WIDTH_PX = 534
GAMEPLAY_CAPTION_METRIC = "coretext-helvetica-bold-libass-scale/v1"
_LIBASS_HELVETICA_METRIC_SCALE = 0.0836
_CAPTION_METRIC_OVERSAMPLE = 10
_HELVETICA_FONT = Path("/System/Library/Fonts/Helvetica.ttc")


@dataclass
class CaptionStyle:
    """Tunable knobs. Defaults are the research-validated baseline."""

    font: str = DEFAULT_FONT
    font_size: int = DEFAULT_FONT_SIZE
    primary_color: str = COLOR_WHITE_OPAQUE
    outline_color: str = COLOR_BLACK_OPAQUE
    outline: int = DEFAULT_OUTLINE
    shadow: int = DEFAULT_SHADOW
    alignment: int = ALIGNMENT_BOTTOM_CENTER
    margin_l: int = DEFAULT_MARGIN_H
    margin_r: int = DEFAULT_MARGIN_H
    margin_v: int = DEFAULT_MARGIN_V
    words_per_phrase: int = DEFAULT_WORDS_PER_PHRASE
    play_res_x: int = DEFAULT_PLAY_RES_X
    play_res_y: int = DEFAULT_PLAY_RES_Y
    bold: bool = True
    max_raster_width_px: int | None = None


@dataclass(frozen=True)
class CaptionPlacement:
    """Absolute ASS position for one caption event."""

    x: int
    y: int
    alignment: int = ALIGNMENT_BOTTOM_CENTER
    font_size: int | None = None
    background_box: tuple[int, int, int, int] | None = None


def _speaker_identities(mapping: Mapping[str, object]) -> set[tuple[str, object]]:
    identities: set[tuple[str, object]] = set()
    logical_track = mapping.get("logical_track", mapping.get("track"))
    if isinstance(logical_track, int) and not isinstance(logical_track, bool):
        identities.add(("logical_track", logical_track))
    camera_channel = mapping.get("camera_channel")
    if isinstance(camera_channel, str) and camera_channel.strip():
        identities.add(("camera_channel", camera_channel.strip().casefold()))
    for field in ("person", "label"):
        value = mapping.get(field)
        if isinstance(value, str) and value.strip():
            identities.add(("person", value.strip().casefold()))
    return identities


def resolve_caption_speaker_targets(
    diarized: Mapping[str, object],
    segment_document: Mapping[str, object],
    crop_config: Mapping[str, object],
) -> dict[int, str]:
    """Resolve source-clock ASR ids to reviewed crop speakers.

    Explicit transcript bindings win. Older transcripts without a target use a
    unique identity match against the acoustic track map and configured crops.
    Missing, conflicting, and explicit wide-shot bindings resolve to ``BOTH`` so
    callers can use a neutral placement rather than guessing a panel.
    """
    if diarized.get("clock") != "source" or segment_document.get("clock") != "source":
        return {}

    speakers = crop_config.get("speakers", [])
    if not isinstance(speakers, list):
        speakers = []
    valid_targets = {f"speaker_{index}" for index in range(len(speakers))}
    targets_by_identity: dict[tuple[str, object], set[str]] = {}

    def register(target: object, mapping: Mapping[str, object]) -> None:
        if target not in valid_targets:
            return
        for identity in _speaker_identities(mapping):
            targets_by_identity.setdefault(identity, set()).add(str(target))

    for mapping in segment_document.get("track_mapping") or []:
        if isinstance(mapping, Mapping):
            register(mapping.get("speaker"), mapping)
    for index, speaker in enumerate(speakers):
        if isinstance(speaker, Mapping):
            register(f"speaker_{index}", speaker)

    resolved: dict[int, str] = {}
    for mapping in diarized.get("speaker_map") or []:
        if not isinstance(mapping, Mapping):
            continue
        source = mapping.get("index")
        if not isinstance(source, int) or isinstance(source, bool):
            continue

        explicit_target = mapping.get("target_speaker")
        if explicit_target == "BOTH" or explicit_target in valid_targets:
            resolved[source] = str(explicit_target)
            continue
        if explicit_target is not None:
            resolved[source] = "BOTH"
            continue
        if float(mapping.get("mapping_confidence", 1.0) or 0.0) < 0.6:
            resolved[source] = "BOTH"
            continue

        candidates = {
            target
            for identity in _speaker_identities(mapping)
            for target in targets_by_identity.get(identity, set())
        }
        resolved[source] = candidates.pop() if len(candidates) == 1 else "BOTH"
    return resolved


# ── timecode formatting ─────────────────────────────────────────────────────


def fmt_ass_time(seconds: float) -> str:
    """Format seconds as ASS timecode: H:MM:SS.cc (centiseconds, NOT ms)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = round((seconds - int(seconds)) * 100)
    # Carry centisecond rollover (cs=100 → s+=1)
    if cs >= 100:
        cs -= 100
        s += 1
        if s >= 60:
            s -= 60
            m += 1
            if m >= 60:
                m -= 60
                h += 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass_text(text: str) -> str:
    """Escape characters that ASS treats as override-block delimiters or
    line breaks. Curly braces wrap inline overrides, backslash starts an
    override tag, newlines must be \\N."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )


@lru_cache(maxsize=16)
def _caption_metric_font(
    font: str, font_size: int, bold: bool
) -> ImageFont.FreeTypeFont:
    """Load the exact CoreText face used by production libass renders.

    libass rasterizes Helvetica at 83.6% of the nominal ASS point size on the
    declared 1080x1920 canvas. Measuring an oversampled face and applying that
    scale matches the production raster while keeping this decision available
    before an expensive video render.
    """
    if font != DEFAULT_FONT or not _HELVETICA_FONT.is_file():
        raise ValueError("Width-bounded captions require macOS Helvetica")
    return ImageFont.truetype(
        str(_HELVETICA_FONT),
        font_size * _CAPTION_METRIC_OVERSAMPLE,
        index=1 if bold else 0,
    )


def caption_line_raster_width(text: str, style: CaptionStyle) -> int:
    """Estimate the full libass glyph-and-outline raster width in pixels."""
    if "\n" in text:
        return max(caption_line_raster_width(line, style) for line in text.split("\n"))
    font = _caption_metric_font(style.font, style.font_size, style.bold)
    glyph_width = round(font.getlength(text) * _LIBASS_HELVETICA_METRIC_SCALE)
    return glyph_width + style.outline * 2


def wrap_caption_text(text: str, style: CaptionStyle) -> str:
    """Wrap an overflowing caption at one word boundary without shrinking it."""
    limit = style.max_raster_width_px
    if limit is None:
        return text
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("Caption raster width must be a positive integer")

    wrapped_lines = []
    for line in text.split("\n"):
        if caption_line_raster_width(line, style) <= limit:
            wrapped_lines.append(line)
            continue
        words = line.split()
        candidates = []
        for index in range(1, len(words)):
            left = " ".join(words[:index])
            right = " ".join(words[index:])
            left_width = caption_line_raster_width(left, style)
            right_width = caption_line_raster_width(right, style)
            if left_width <= limit and right_width <= limit:
                candidates.append(
                    (
                        max(left_width, right_width),
                        abs(left_width - right_width),
                        index,
                        left,
                        right,
                    )
                )
        if not candidates:
            raise ValueError(
                f"Caption cannot fit {limit}px at word boundaries: {line!r}"
            )
        _, _, _, left, right = min(candidates)
        wrapped_lines.extend((left, right))
    return "\n".join(wrapped_lines)


# ── phrase grouping ─────────────────────────────────────────────────────────

_CAPTION_PHRASE_KEY = "_caption_display_phrase_id"


def _extract_words_in_range(diarized: dict, start: float, end: float) -> list[dict]:
    """Pull every Deepgram word inside [start, end). Includes per-word
    speaker labels so we can break phrases on speaker change."""
    out = []
    for utt in diarized.get("utterances", []):
        utt_speaker = utt.get("speaker")
        for w in utt.get("words", []):
            w_start = w.get("start", 0.0)
            w_end = w.get("end", 0.0)
            if w_start >= start and w_end <= end:
                # Carry the speaker forward in case word-level speaker is missing
                w_with_speaker = dict(w)
                w_with_speaker.setdefault("speaker", utt_speaker)
                out.append(w_with_speaker)
    out.sort(
        key=lambda word: (
            float(word.get("start", 0.0)),
            float(word.get("end", 0.0)),
        )
    )
    return out


def caption_ranges_use_target(
    diarized: dict,
    source_ranges: Iterable[tuple[float, float]],
    speaker_targets: Mapping[object, str],
    target: str,
) -> bool:
    """Return whether a fully contained caption word resolves to ``target``."""
    return any(
        speaker_targets.get(word.get("speaker"), "BOTH") == target
        for start, end in source_ranges
        for word in _extract_words_in_range(diarized, float(start), float(end))
    )


def _partition_reviewed_caption_words(
    words: list[dict],
) -> tuple[list[dict], dict[str, list[dict]]]:
    ordinary = []
    reviewed: dict[str, list[dict]] = {}
    for word in words:
        phrase_id = word.get(_CAPTION_PHRASE_KEY)
        if isinstance(phrase_id, str):
            reviewed.setdefault(phrase_id, []).append(word)
        else:
            ordinary.append(word)
    return ordinary, reviewed


def _append_reviewed_caption_phrases(
    phrases: list[dict],
    reviewed: Mapping[str, list[dict]],
    clip_start: float,
) -> None:
    if not reviewed:
        return
    for phrase_id, words in reviewed.items():
        words.sort(key=lambda word: (word["start"], word["end"]))
        first, last = words[0], words[-1]
        rel_start = max(0.0, first["start"] - clip_start)
        rel_end = max(rel_start + MIN_PHRASE_DURATION, last["end"] - clip_start)
        phrases.append(
            {
                "start": rel_start,
                "end": min(rel_end, rel_start + MAX_PHRASE_DURATION),
                "text": " ".join(
                    word.get("punctuated_word") or word.get("word", "")
                    for word in words
                ).strip(),
                "speaker": first.get("speaker"),
                _CAPTION_PHRASE_KEY: phrase_id,
            }
        )
    phrases.sort(
        key=lambda phrase: (
            phrase["start"],
            phrase["end"],
            str(phrase.get(_CAPTION_PHRASE_KEY, "")),
        )
    )


def _is_reviewed_parallel_phrase_pair(current: dict, following: dict) -> bool:
    current_identity = current.get(_CAPTION_PHRASE_KEY)
    following_identity = following.get(_CAPTION_PHRASE_KEY)
    return (
        isinstance(current_identity, str)
        and isinstance(following_identity, str)
        and current_identity != following_identity
        and current.get("speaker") != following.get("speaker")
    )


def group_words_into_phrases(
    words: list[dict],
    *,
    clip_start: float,
    words_per_phrase: int = DEFAULT_WORDS_PER_PHRASE,
    resolve_overlaps: bool = True,
    preserve_reviewed_panel_overlaps: bool = False,
) -> list[dict]:
    """Group words into display phrases. Times are returned **relative to
    clip_start** (so 0.0 = start of the rendered short, not absolute episode
    time). Breaks on speaker change and on long pauses to avoid running
    captions across cuts.

    Returns a list of {start, end, text, speaker} dicts, ordered.
    """
    if not words:
        return []

    ordinary_words, reviewed_groups = (
        _partition_reviewed_caption_words(words)
        if preserve_reviewed_panel_overlaps
        else (words, {})
    )

    phrases: list[dict] = []
    current: list[dict] = []

    def _flush():
        if not current:
            return
        first, last = current[0], current[-1]
        text = " ".join(
            w.get("punctuated_word") or w.get("word", "") for w in current
        ).strip()
        if not text:
            current.clear()
            return
        rel_start = max(0.0, first["start"] - clip_start)
        rel_end = max(rel_start + MIN_PHRASE_DURATION, last["end"] - clip_start)
        # Cap phrase duration so static text doesn't linger
        rel_end = min(rel_end, rel_start + MAX_PHRASE_DURATION)
        phrases.append(
            {
                "start": rel_start,
                "end": rel_end,
                "text": text,
                "speaker": first.get("speaker"),
            }
        )
        current.clear()

    for w in ordinary_words:
        if not current:
            current.append(w)
            continue

        prev = current[-1]

        # Break on speaker change
        if w.get("speaker") != prev.get("speaker"):
            _flush()
            current.append(w)
            continue

        # Break on long inter-word pause (>0.5s of silence)
        if w["start"] - prev["end"] > 0.5:
            _flush()
            current.append(w)
            continue

        # Break on words-per-phrase target
        if len(current) >= words_per_phrase:
            _flush()
            current.append(w)
            continue

        current.append(w)

    _flush()

    _append_reviewed_caption_phrases(phrases, reviewed_groups, clip_start)

    # Stretch through short pauses, then fit every event into one display lane.
    for i, ph in enumerate(phrases):
        if i + 1 < len(phrases):
            next_start = phrases[i + 1]["start"]
            ph["end"] = max(ph["end"], min(ph["end"] + 0.3, next_start - 0.01))

    if resolve_overlaps:
        for index in range(len(phrases) - 1):
            current = phrases[index]
            following = phrases[index + 1]
            if current["end"] <= following["start"] or (
                preserve_reviewed_panel_overlaps
                and _is_reviewed_parallel_phrase_pair(current, following)
            ):
                continue
            if current.get("speaker") == following.get("speaker"):
                midpoint = (current["end"] + following["start"]) / 2
                prior_end = midpoint - CAPTION_EVENT_GAP_SECONDS / 2
                next_start = midpoint + CAPTION_EVENT_GAP_SECONDS / 2
                if prior_end > current["start"] and next_start < following["end"]:
                    current["end"] = prior_end
                    following["start"] = next_start
                    continue
            available_end = following["start"] - CAPTION_EVENT_GAP_SECONDS
            if available_end > current["start"]:
                current["end"] = available_end
                continue
            current["end"] = current["start"] + 0.01
            following["start"] = current["end"] + CAPTION_EVENT_GAP_SECONDS
            following["end"] = max(following["end"], following["start"] + 0.01)

    return phrases


def caption_width_wrap_policy(
    diarized: dict,
    start: float,
    end: float,
    style: CaptionStyle,
    *,
    speaker_targets: Mapping[object, str] | None = None,
    fallback_target: str = "BOTH",
    fallback_font_size: int | None = None,
) -> dict | None:
    """Return a fingerprintable policy only when this clip needs wrapping."""
    words = _extract_words_in_range(diarized, start, end)
    phrases = group_words_into_phrases(
        words,
        clip_start=start,
        words_per_phrase=style.words_per_phrase,
        preserve_reviewed_panel_overlaps=(
            diarized.get("_caption_text_replacements_applied")
            == "cascade.short-caption-speaker-overrides/v2"
            and speaker_targets is not None
        ),
    )
    changed = []
    for ordinal, phrase in enumerate(phrases, 1):
        phrase_style = style
        if (
            speaker_targets is not None
            and fallback_font_size is not None
            and speaker_targets.get(phrase.get("speaker"), fallback_target)
            == fallback_target
        ):
            phrase_style = replace(style, font_size=fallback_font_size)
        wrapped = wrap_caption_text(phrase["text"], phrase_style)
        if wrapped == phrase["text"]:
            continue
        changed.append(
            {
                "ordinal": ordinal,
                "start": round(float(phrase["start"]), 6),
                "end": round(float(phrase["end"]), 6),
                "text": phrase["text"],
                "wrapped_text": wrapped,
                "font_size": phrase_style.font_size,
            }
        )
    if not changed:
        return None
    changed_revision = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return {
        "version": GAMEPLAY_CAPTION_WRAP_VERSION,
        "max_raster_width_px": style.max_raster_width_px,
        "metric": GAMEPLAY_CAPTION_METRIC,
        "changed_cue_count": len(changed),
        "changed_cues_revision": changed_revision,
    }


def requires_single_lane_caption_timing(
    diarized: dict, start: float, end: float
) -> bool:
    """Return whether the legacy phrase timings would draw simultaneous text."""
    words = _extract_words_in_range(diarized, start, end)
    phrases = group_words_into_phrases(
        words,
        clip_start=start,
        resolve_overlaps=False,
    )
    return any(
        current["end"] > following["start"] for current, following in pairwise(phrases)
    )


# ── ASS file assembly ───────────────────────────────────────────────────────


def _format_style_line(style: CaptionStyle) -> str:
    """Emit the [V4+ Styles] Style: line. Field order is fixed by the spec."""
    bold = -1 if style.bold else 0
    return (
        "Style: Default,"
        f"{style.font},"
        f"{style.font_size},"
        f"{style.primary_color},"  # PrimaryColour
        f"{style.primary_color},"  # SecondaryColour (unused for static; matches primary)
        f"{style.outline_color},"  # OutlineColour
        "&H64000000,"  # BackColour (semi-transparent black, unused at BorderStyle=1)
        f"{bold},"  # Bold
        "0,"  # Italic
        "0,"  # Underline
        "0,"  # StrikeOut
        "100,"  # ScaleX
        "100,"  # ScaleY
        "0,"  # Spacing
        "0,"  # Angle
        "1,"  # BorderStyle: 1 = outline + shadow
        f"{style.outline},"  # Outline
        f"{style.shadow},"  # Shadow
        f"{style.alignment},"  # Alignment (numpad)
        f"{style.margin_l},"  # MarginL
        f"{style.margin_r},"  # MarginR
        f"{style.margin_v},"  # MarginV
        "1"  # Encoding (1 = default)
    )


def build_ass(phrases: Iterable[dict], style: CaptionStyle | None = None) -> str:
    """Assemble a full .ass file from grouped phrases."""
    style = style or CaptionStyle()

    header = (
        "[Script Info]\n"
        "; Generated by cascade/lib/ass.py\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {style.play_res_x}\n"
        f"PlayResY: {style.play_res_y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{_format_style_line(style)}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [header]
    for ph in phrases:
        placement = ph.get("placement")
        phrase_style = style
        if isinstance(placement, CaptionPlacement) and placement.font_size is not None:
            phrase_style = replace(style, font_size=placement.font_size)
        text = escape_ass_text(wrap_caption_text(ph["text"], phrase_style))
        layer = 0
        if placement is not None:
            if not isinstance(placement, CaptionPlacement):
                raise TypeError("caption placement must be a CaptionPlacement")
            if placement.background_box is not None:
                box_x, box_y, box_w, box_h = placement.background_box
                drawing = (
                    f"{{\\an7\\pos({box_x},{box_y})\\p1\\bord0\\shad0"
                    "\\1c&H000000&}"
                    f"m 0 0 l {box_w} 0 l {box_w} {box_h} l 0 {box_h}"
                    "{\\p0}"
                )
                lines.append(
                    f"Dialogue: 0,{fmt_ass_time(ph['start'])},"
                    f"{fmt_ass_time(ph['end'])},Default,,0,0,0,,{drawing}\n"
                )
                layer = 1
            overrides = [
                f"\\an{placement.alignment}",
                f"\\pos({placement.x},{placement.y})",
            ]
            if placement.font_size is not None:
                overrides.append(f"\\fs{placement.font_size}")
            text = "{" + "".join(overrides) + "}" + text
        lines.append(
            f"Dialogue: {layer},"
            f"{fmt_ass_time(ph['start'])},"
            f"{fmt_ass_time(ph['end'])},"
            "Default,,0,0,0,,"
            f"{text}\n"
        )

    return "".join(lines)


def generate_ass_from_diarized(
    diarized: dict,
    start: float,
    end: float,
    ass_path: Path,
    style: CaptionStyle | None = None,
    *,
    speaker_targets: Mapping[int, str] | None = None,
    speaker_placements: Mapping[str, CaptionPlacement] | None = None,
    fallback_placement: CaptionPlacement | None = None,
) -> int:
    """One-call helper that mirrors the SRT generator's signature.

    Slices the diarized transcript to [start, end), groups words into
    phrases relative to `start`, writes a .ass file at `ass_path`. Returns
    the number of phrases written.
    """
    style = style or CaptionStyle()
    words = _extract_words_in_range(diarized, start, end)
    phrases = group_words_into_phrases(
        words,
        clip_start=start,
        words_per_phrase=style.words_per_phrase,
        preserve_reviewed_panel_overlaps=(
            diarized.get("_caption_text_replacements_applied")
            == "cascade.short-caption-speaker-overrides/v2"
            and speaker_targets is not None
            and speaker_placements is not None
        ),
    )
    if speaker_targets is not None and speaker_placements is not None:
        for phrase in phrases:
            target = speaker_targets.get(phrase.get("speaker"), "BOTH")
            phrase["placement"] = speaker_placements.get(target, fallback_placement)
    ass_text = build_ass(phrases, style)
    ass_path.write_text(ass_text, encoding="utf-8")
    return len(phrases)
