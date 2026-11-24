from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import httpx
import numpy as np

from clipper.config import Settings
from clipper.ingest import probe
from clipper.models import (
    LENGTH_RANGES, Candidate, Canceled, LengthPreset, Moment, Progress, ShouldStop,
    Transcript, VideoType, Word,
)
from clipper.moments import (
    audio_candidates, dedupe, final_score, hook_score, pace_score, select, sentence_ends, snap,
)

log = logging.getLogger(__name__)

CHUNK = 600.0
OVERLAP = 60.0
HOP = 0.5

_LOOK_FOR = {
    "talk": "strong statements, memorable stories, jokes and surprising claims",
    "gaming": "big reactions, hype moments, funny moments and clutch plays",
    "movie": "dramatic, emotional or quotable scenes",
}


class MomentsError(Exception):
    """A problem the user can fix (e.g. Ollama not running); the message is meant to be shown as is."""


def _client() -> httpx.Client:  # tests monkeypatch this to inject a MockTransport
    return httpx.Client(timeout=httpx.Timeout(300, connect=5))


def _generate(c: httpx.Client, settings: Settings, body: dict) -> httpx.Response:
    try:
        r = c.post(f"{settings.ollama_host}/api/generate", json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise MomentsError(f"Ollama is not reachable at {settings.ollama_host}. Start Ollama and try again.")
    except httpx.TimeoutException:  # after ConnectTimeout above, which is also one
        raise MomentsError("The local AI model took too long to answer. "
                           "Try again, or use a smaller ollama_model in config.toml.")
    if r.status_code == 404:
        raise MomentsError(f"Model {settings.ollama_model} isn't installed. Run: ollama pull {settings.ollama_model}")
    if r.is_error:
        raise MomentsError(f"The local AI model returned an error (HTTP {r.status_code}). Check that Ollama is running.")
    return r


# ---------- audio energy ----------

def energy_profile(source: Path, hop: float = HOP) -> np.ndarray:
    if not probe(source).has_audio:
        return np.zeros(0, dtype=np.float32)
    rate = 16000
    n = int(rate * hop)
    cmd = ["ffmpeg", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", str(rate),
           "-f", "s16le", "-"]
    out: list[float] = []
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as p:
        buf = b""
        while True:
            data = p.stdout.read(n * 2 * 240)  # ~2 minutes at a time
            if not data:
                break
            buf += data
            whole = len(buf) // (n * 2) * (n * 2)
            if whole:
                a = np.frombuffer(buf[:whole], dtype=np.int16).astype(np.float32).reshape(-1, n)
                out.extend(np.sqrt((a * a).mean(axis=1)))
                buf = buf[whole:]
        if p.wait() != 0:
            raise RuntimeError("ffmpeg could not decode the audio")
    prof = np.asarray(out, dtype=np.float32)
    peak = prof.max() if len(prof) else 0.0
    return prof / peak if peak > 0 else prof


def energy_score(profile: np.ndarray, hop: float, start: float, end: float) -> float:
    if len(profile) == 0:
        return 0.0
    win = profile[int(start / hop): max(int(start / hop) + 1, math.ceil(end / hop))]
    if len(win) == 0:
        return 0.0
    med, top = float(np.median(profile)), float(profile.max())
    if top - med <= 0:
        return 0.0
    return float(min(1.0, max(0.0, (np.percentile(win, 95) - med) / (top - med))))


# ---------- local model ----------

def format_chunk(words: list[Word], start: float, end: float) -> str:
    inside = [w for w in words if start <= w.start <= end]
    ends = set(sentence_ends(inside))
    lines, cur = [], []
    for i, w in enumerate(inside):
        cur.append(w)
        if i in ends or i == len(inside) - 1:
            lines.append(f"[{cur[0].start:.1f}] " + " ".join(x.text for x in cur))
            cur = []
    return "\n".join(lines)


def _prompt(text: str, cs: float, ce: float, video_type: VideoType, lo: float, hi: float) -> str:
    return (
        f"You pick the best short clips from a video transcript. Each line starts with its time in "
        f"seconds in brackets.\n"
        f"Find clips with {_LOOK_FOR[video_type]}.\n"
        f"Rules: every clip must be between {lo:g} and {hi:g} seconds long, must make sense on its "
        f"own without the rest of the video, and must start and end on a full sentence. Use only "
        f"times between {cs:g} and {ce:g}. Give each clip a short title and a score from 0 to 100 "
        f"for how good it is.\n"
        f'Reply with JSON only, in this shape: {{"clips": [{{"start": 12.0, "end": 55.0, '
        f'"title": "...", "score": 80, "reason": "..."}}]}}. Return at most 5 clips, or an empty '
        f"list if nothing is good.\n\nTranscript:\n{text}"
    )


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _parse(raw: str, cs: float, ce: float) -> list[Candidate]:
    data = json.loads(raw)
    items = data.get("clips") if isinstance(data, dict) else data  # some models return a bare list
    if not isinstance(items, list):
        raise ValueError("no clips list")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        s, e = _num(it.get("start")), _num(it.get("end"))
        if s is None or e is None or e <= s:
            continue
        s, e = min(max(s, cs), ce), min(max(e, cs), ce)
        if e <= s:
            continue
        score = _num(it.get("score")) or 0.0
        out.append(Candidate(s, e, str(it.get("title", "")).strip()[:80],
                             min(100.0, max(0.0, score)), str(it.get("reason", ""))))
    return out


def ollama_candidates(text: str, chunk_start: float, chunk_end: float, video_type: VideoType,
                      lo: float, hi: float, settings: Settings) -> list[Candidate]:
    body = {
        "model": settings.ollama_model,
        "prompt": _prompt(text, chunk_start, chunk_end, video_type, lo, hi),
        "format": "json", "think": False, "stream": False,
        "options": {"temperature": 0.3, "num_ctx": 8192}, "keep_alive": "5m",
    }
    with _client() as c:
        for attempt in range(2):
            r = _generate(c, settings, body)
            try:
                return _parse(r.json()["response"], chunk_start, chunk_end)
            except (ValueError, KeyError, TypeError) as e:  # JSONDecodeError is a ValueError
                log.warning("bad model output (attempt %d): %s", attempt + 1, e)
    return []


def unload_model(settings: Settings) -> None:
    with _client() as c:
        _generate(c, settings, {"model": settings.ollama_model, "keep_alive": 0})


# ---------- pipeline ----------

def _atomic_write(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def cache_key(video_type: VideoType, preset: LengthPreset, settings: Settings) -> dict:
    lo, hi = LENGTH_RANGES[preset]
    template = _prompt("{text}", 0, 0, video_type, lo, hi)
    return {"video_type": video_type, "preset": preset, "ollama_model": settings.ollama_model,
            "prompt_sha256": hashlib.sha256(template.encode()).hexdigest()}


def find_moments(source: Path, transcript: Transcript, video_type: VideoType, preset: LengthPreset,
                 settings: Settings, job_dir: Path, progress: Progress,
                 should_stop: ShouldStop) -> list[Moment]:
    final = job_dir / "moments.json"
    lo, hi = LENGTH_RANGES[preset]
    chunk_dir = job_dir / "moment_chunks"
    key = cache_key(video_type, preset, settings)
    key_file = job_dir / "moments_key.json"
    stored = json.loads(key_file.read_text(encoding="utf-8")) if key_file.exists() else None
    if stored != key:  # results from another type/length/model/prompt must not be reused
        shutil.rmtree(chunk_dir, ignore_errors=True)
        final.unlink(missing_ok=True)
        job_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(key_file, key)
    if final.exists():
        return [Moment(**m) for m in json.loads(final.read_text(encoding="utf-8"))]
    duration = probe(source).duration
    chunk_dir.mkdir(parents=True, exist_ok=True)
    words = transcript.words
    step = CHUNK - OVERLAP
    total = max(1, math.ceil((duration - OVERLAP) / step))
    try:
        profile = energy_profile(source, HOP)
        cands: list[Candidate] = []
        for i in range(total):
            if should_stop():
                raise Canceled()
            saved = chunk_dir / f"{i:04d}.json"
            if saved.exists():
                part = [Candidate(**d) for d in json.loads(saved.read_text(encoding="utf-8"))]
            else:
                cs = i * step
                ce = min(cs + CHUNK, duration)
                text = format_chunk(words, cs, ce)
                part = ollama_candidates(text, cs, ce, video_type, lo, hi, settings) if text else []
                _atomic_write(saved, [asdict(c) for c in part])
            cands += part
            progress((i + 1) / total)

        if len(words) < 20 * duration / 600 and len(profile):
            cands += audio_candidates(profile, HOP, lo, hi, max(3, math.ceil(duration / 600)))

        def scored(s: Candidate) -> Moment:
            inside = [w for w in words if s.start <= w.start < s.end]
            hook = hook_score(" ".join(w.text for w in inside))
            score = final_score(s.ai_score, energy_score(profile, HOP, s.start, s.end),
                                pace_score(inside), hook, video_type)
            return Moment(s.start, s.end, s.title, score)

        moments = [scored(s) for c in cands if (s := snap(c, words, lo, hi)) is not None]
        if not moments and duration > 120:  # spec: a video over 2 minutes always gets at least one clip
            width = min((lo + hi) / 2, duration)
            fallback = (audio_candidates(profile, HOP, lo, hi, 1) if len(profile) else
                        [Candidate((duration - width) / 2, (duration + width) / 2, "Highlight", 50, "middle")])
            moments = [scored(snap(c, words, lo, hi) or c) for c in fallback]
        result = select(dedupe(moments), duration, settings.score_threshold, settings.clips_per_hour)
        _atomic_write(final, [asdict(m) for m in result])
        return result
    finally:
        try:
            unload_model(settings)
        except Exception:
            log.warning("could not unload the model", exc_info=True)
