from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from clipper.models import CaptionStyle


@dataclass
class Settings:
    data_dir: Path
    password_hash: str
    session_secret: str
    port: int = 8765
    whisper_model: str = "large-v3-turbo"
    whisper_compute_type: str = "int8_float16"
    ollama_model: str = "qwen3:4b"
    ollama_host: str = "http://127.0.0.1:11434"
    source_retention_days: int = 14
    clips_per_hour: int = 15
    score_threshold: int = 55
    caption_delay: float = 0.1
    default_captions_on: bool = False
    default_caption_style: CaptionStyle = "bold_white"
    cookie_secure: bool = True


def load_settings(path: Path) -> Settings:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    unknown = set(raw) - {f.name for f in fields(Settings)}
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    for key in ("data_dir", "password_hash", "session_secret"):
        if key not in raw:
            raise ValueError(f"missing required config key: {key}")
    for key in ("password_hash", "session_secret"):
        if not str(raw[key]).strip():
            raise ValueError(f"config key must not be empty: {key}")
    raw["data_dir"] = Path(raw["data_dir"])
    s = Settings(**raw)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
