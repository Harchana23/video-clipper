from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

VideoType = Literal["talk", "gaming", "movie"]
LengthPreset = Literal["auto", "short", "medium", "long"]
CaptionStyle = Literal["bold_white", "yellow_pop"]


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    language: str
    words: list[Word]
    segments: list[Segment]

    def save(self, path: Path) -> None:
        # write to .tmp then rename so a crash never leaves a half-written file
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self)), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Transcript:
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            d["language"],
            [Word(**w) for w in d["words"]],
            [Segment(**s) for s in d["segments"]],
        )


@dataclass(frozen=True)
class Candidate:
    start: float
    end: float
    title: str
    ai_score: float
    reason: str


@dataclass(frozen=True)
class Moment:
    start: float
    end: float
    title: str
    score: int  # 0-100


@dataclass(frozen=True)
class CaptionEvent:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class CropKey:
    t: float  # relative to clip start
    x: int  # crop left edge in source px


@dataclass
class CropPlan:
    mode: Literal["follow", "fit"]
    src_w: int
    src_h: int
    keys: list[CropKey]


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class SourceInfo:
    path: Path
    title: str
    duration: float
    thumbnail: Path | None


LENGTH_RANGES: dict[LengthPreset, tuple[float, float]] = {
    "auto": (30, 90),
    "short": (15, 30),
    "medium": (30, 60),
    "long": (60, 90),
}


class Canceled(Exception): ...


Progress = Callable[[float], None]  # fraction 0..1 of the current step
ShouldStop = Callable[[], bool]
