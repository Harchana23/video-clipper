from __future__ import annotations

import math
import re
from dataclasses import replace

import numpy as np

from clipper.models import Candidate, Moment, VideoType, Word

_OPENERS = ("nobody", "never", "secret", "truth", "mistake", "why", "how", "stop")
_WEIGHTS = {
    "talk": (0.6, 0.2, 0.1, 0.1),
    "gaming": (0.4, 0.45, 0.05, 0.1),
    "movie": (0.4, 0.45, 0.05, 0.1),
}


def sentence_ends(words: list[Word]) -> list[int]:
    return [i for i, w in enumerate(words) if w.text.rstrip().endswith((".", "?", "!"))]


def snap(c: Candidate, words: list[Word], lo: float, hi: float) -> Candidate | None:
    i0 = next((i for i, w in enumerate(words) if w.end > c.start), None)
    if i0 is None or words[i0].start >= c.end:
        # no transcript here: only the length can be fixed (extend or cut the end)
        return replace(c, end=c.start + min(max(c.end - c.start, lo), hi))

    ends = set(sentence_ends(words))
    last = len(words) - 1
    i1 = max(i for i, w in enumerate(words) if w.start < c.end)

    while i0 > 0 and (i0 - 1) not in ends:
        i0 -= 1
    while i1 < last and i1 not in ends:
        i1 += 1

    start = words[i0].start
    while words[i1].end - start < lo and i1 < last:  # extend by whole sentences
        i1 += 1
        while i1 < last and i1 not in ends:
            i1 += 1
    end = words[i1].end

    if end - start > hi:
        limit = start + hi
        cuts = [j for j in range(i0, i1 + 1) if words[j].end <= limit]
        sent = [j for j in cuts if j in ends]
        # sentence end if it still leaves a long-enough clip, else the last word that fits
        pick = sent[-1] if sent and words[sent[-1]].end - start >= lo else (cuts[-1] if cuts else None)
        if pick is None:
            return None
        end = words[pick].end

    if end - start < lo:
        return None  # source ran out
    return replace(c, start=start, end=end)


def hook_score(text: str) -> float:
    first = re.match(r"[^.?!]*[.?!]?", text.strip()).group(0)
    score = 0.4 if "?" in first else 0.0
    if any(ch.isdigit() for ch in first):
        score += 0.3
    # each distinct opener counts, so "Why does nobody ...?" reaches 1.0
    tokens = set(re.findall(r"[a-z']+", first.lower()))
    score += 0.3 * sum(o in tokens for o in _OPENERS)
    return min(1.0, score)


def pace_score(words: list[Word]) -> float:
    if not words:
        return 0.0
    span = words[-1].end - words[0].start
    if span <= 0:
        return 0.0
    wps = len(words) / span
    if wps < 2.0:
        return max(0.0, (wps - 0.5) / 1.5)
    if wps <= 3.5:
        return 1.0
    return max(0.0, (5.5 - wps) / 2.0)


def final_score(ai: float, energy: float, pace: float, hook: float, video_type: VideoType) -> int:
    wa, we, wp, wh = _WEIGHTS[video_type]
    return round(100 * (wa * ai / 100 + we * energy + wp * pace + wh * hook))


def dedupe(moments: list[Moment]) -> list[Moment]:
    kept: list[Moment] = []
    for m in sorted(moments, key=lambda m: (-m.score, m.start)):
        for k in kept:
            overlap = min(m.end, k.end) - max(m.start, k.start)
            if overlap > 0.5 * min(m.end - m.start, k.end - k.start):
                break
        else:
            kept.append(m)
    return sorted(kept, key=lambda m: m.start)


def select(moments: list[Moment], duration: float, threshold: int, per_hour: int) -> list[Moment]:
    ranked = sorted(moments, key=lambda m: (-m.score, m.start))
    keep = [m for m in ranked if m.score >= threshold]
    if not keep and duration > 120:
        keep = ranked[:1]
    return keep[: max(1, math.ceil(duration / 3600 * per_hour))]


def audio_candidates(energy: np.ndarray, hop: float, lo: float, hi: float, n: int) -> list[Candidate]:
    total = len(energy) * hop
    width = min((lo + hi) / 2, total)
    chosen: list[Candidate] = []
    for idx in np.argsort(-energy, kind="stable"):
        if len(chosen) >= n:
            break
        t = float(idx) * hop
        start = min(max(0.0, t - width / 2), total - width)  # shift, don't shrink, at the edges
        end = start + width
        if any(start < c.end and end > c.start for c in chosen):
            continue
        mm, ss = divmod(int(t), 60)
        chosen.append(Candidate(start, end, f"Moment at {mm:02d}:{ss:02d}", 50, "audio energy peak"))
    return chosen
