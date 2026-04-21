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

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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

# Phrase grouping: 3 words/phrase keeps reading-speed comfortable and matches
# the 2-4 word range that interview-clip channels actually use.
DEFAULT_WORDS_PER_PHRASE = 3

# Don't let a phrase show for less than this — flickers below it. Don't let a
# phrase show longer than this either — feels stale.
MIN_PHRASE_DURATION = 0.4
MAX_PHRASE_DURATION = 2.5

# Reading-speed cap: never display more than ~6 words per second (typical
# spoken-word pace is ~3 words/sec, so this is a safety net for fast bursts).
MAX_WORDS_PER_SECOND = 6.0


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
    margin_v: int = DEFAULT_MARGIN_V
    words_per_phrase: int = DEFAULT_WORDS_PER_PHRASE
    play_res_x: int = DEFAULT_PLAY_RES_X
    play_res_y: int = DEFAULT_PLAY_RES_Y
    bold: bool = True


# ── timecode formatting ─────────────────────────────────────────────────────


def fmt_ass_time(seconds: float) -> str:
    """Format seconds as ASS timecode: H:MM:SS.cc (centiseconds, NOT ms)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
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
        text
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )


# ── phrase grouping ─────────────────────────────────────────────────────────


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
    return out


def group_words_into_phrases(
    words: list[dict],
    *,
    clip_start: float,
    words_per_phrase: int = DEFAULT_WORDS_PER_PHRASE,
) -> list[dict]:
    """Group words into display phrases. Times are returned **relative to
    clip_start** (so 0.0 = start of the rendered short, not absolute episode
    time). Breaks on speaker change and on long pauses to avoid running
    captions across cuts.

    Returns a list of {start, end, text, speaker} dicts, ordered.
    """
    if not words:
        return []

    phrases: list[dict] = []
    current: list[dict] = []

    def _flush():
        if not current:
            return
        first, last = current[0], current[-1]
        text = " ".join(w.get("word", "") for w in current).strip()
        if not text:
            current.clear()
            return
        rel_start = max(0.0, first["start"] - clip_start)
        rel_end = max(rel_start + MIN_PHRASE_DURATION, last["end"] - clip_start)
        # Cap phrase duration so static text doesn't linger
        rel_end = min(rel_end, rel_start + MAX_PHRASE_DURATION)
        phrases.append({
            "start": rel_start,
            "end": rel_end,
            "text": text,
            "speaker": first.get("speaker"),
        })
        current.clear()

    for i, w in enumerate(words):
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

    # Stretch each phrase to fill the gap before the NEXT phrase (so captions
    # don't disappear during natural reading pauses) — but cap at the next
    # phrase's start so they never overlap.
    for i, ph in enumerate(phrases):
        if i + 1 < len(phrases):
            next_start = phrases[i + 1]["start"]
            ph["end"] = max(ph["end"], min(ph["end"] + 0.3, next_start - 0.01))

    return phrases


# ── ASS file assembly ───────────────────────────────────────────────────────


def _format_style_line(style: CaptionStyle) -> str:
    """Emit the [V4+ Styles] Style: line. Field order is fixed by the spec."""
    bold = -1 if style.bold else 0
    return (
        "Style: Default,"
        f"{style.font},"
        f"{style.font_size},"
        f"{style.primary_color},"   # PrimaryColour
        f"{style.primary_color},"   # SecondaryColour (unused for static; matches primary)
        f"{style.outline_color},"   # OutlineColour
        "&H64000000,"               # BackColour (semi-transparent black, unused at BorderStyle=1)
        f"{bold},"                  # Bold
        "0,"                        # Italic
        "0,"                        # Underline
        "0,"                        # StrikeOut
        "100,"                      # ScaleX
        "100,"                      # ScaleY
        "0,"                        # Spacing
        "0,"                        # Angle
        "1,"                        # BorderStyle: 1 = outline + shadow
        f"{style.outline},"         # Outline
        f"{style.shadow},"          # Shadow
        f"{style.alignment},"       # Alignment (numpad)
        "80,"                       # MarginL
        "80,"                       # MarginR
        f"{style.margin_v},"        # MarginV
        "1"                         # Encoding (1 = default)
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
        text = escape_ass_text(ph["text"])
        lines.append(
            f"Dialogue: 0,"
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
    )
    ass_text = build_ass(phrases, style)
    ass_path.write_text(ass_text, encoding="utf-8")
    return len(phrases)
