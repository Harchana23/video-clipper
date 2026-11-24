"""Word-timed captions. Rule: no word is on screen before it is spoken (+ delay)."""

from __future__ import annotations

import math

from clipper.models import CaptionEvent, CaptionStyle, Word

# ASS colours are &HAABBGGRR
_PRIMARY: dict[str, str] = {"bold_white": "&H00FFFFFF", "yellow_pop": "&H0042C5F5"}


def words_in_range(words: list[Word], start: float, end: float) -> list[Word]:
    """Words fully inside [start, end], shifted so `start` becomes 0."""
    return [Word(w.start - start, w.end - start, w.text) for w in words if w.start >= start and w.end <= end]


def apply_corrections(words: list[Word], corrections: dict[int, str]) -> list[Word]:
    """Replace text by index, keeping timing; an empty correction removes the word."""
    out = []
    for i, w in enumerate(words):
        text = corrections.get(i, w.text)
        if text.strip():
            out.append(Word(w.start, w.end, text))
    return out


def chunk_words(
    words: list[Word], delay: float = 0.1, pair_gap: float = 0.45, hold: float = 0.3
) -> list[CaptionEvent]:
    """2-word UPPERCASE chunks with progressive reveal when the words are not tightly spoken."""
    chunks: list[list[tuple[float, str]]] = []  # per chunk: [(start, text), ...] in reveal order
    last_end: list[float] = []
    for i in range(0, len(words), 2):
        a = words[i]
        b = words[i + 1] if i + 1 < len(words) else None
        text_a = a.text.upper()
        if b is None:
            chunks.append([(a.start + delay, text_a)])
            last_end.append(a.end)
        elif b.start - a.start <= pair_gap:
            chunks.append([(a.start + delay, f"{text_a} {b.text.upper()}")])
            last_end.append(b.end)
        else:
            chunks.append([(a.start + delay, text_a), (b.start + delay, f"{text_a} {b.text.upper()}")])
            last_end.append(b.end)

    events: list[CaptionEvent] = []
    for ci, parts in enumerate(chunks):
        for pi, (start, text) in enumerate(parts):
            if pi + 1 < len(parts):
                end = parts[pi + 1][0]
            else:
                end = last_end[ci] + delay + hold
                if ci + 1 < len(chunks):
                    end = min(end, chunks[ci + 1][0][0])
            # ms precision so 1.0 + 0.1 == 1.1; starts round UP so a word is never shown early
            events.append(CaptionEvent(math.ceil(round(start * 1000, 6)) / 1000, round(end, 3), text))
    return events


def _ass_time(t: float) -> str:
    cs = round(max(t, 0) * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    # ASS has no brace escape, so swap the characters for harmless look-alikes
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def build_ass(events: list[CaptionEvent], style: CaptionStyle, width: int = 1080, height: int = 1920) -> str:
    margin_v = round(560 * height / 1920)
    size = round(84 * height / 1920)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {style},Arial,{size},{_PRIMARY[style]},&H000000FF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,6,0,2,60,60,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines += [
        f"Dialogue: 0,{_ass_time(e.start)},{_ass_time(e.end)},{style},,0,0,0,,{_escape(e.text)}" for e in events
    ]
    return "\n".join(lines) + "\n"
