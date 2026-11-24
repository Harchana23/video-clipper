from __future__ import annotations

import gc
import math
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from clipper.ingest import probe
from clipper.models import Canceled, Progress, Segment, ShouldStop, Transcript, Word

CHUNK_SECONDS = 600
_dlls_registered = False


def _register_cuda_dlls() -> None:
    # Windows: cuBLAS/cuDNN come from the nvidia-* pip wheels; make their DLLs findable.
    # Once per process: repeating it would grow PATH past Windows' 32767-char limit.
    global _dlls_registered
    if sys.platform != "win32" or _dlls_registered:
        return
    _dlls_registered = True
    for p in sys.path:
        for lib in ("cublas", "cudnn"):
            d = Path(p) / "nvidia" / lib / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


def _load_model(name: str, compute_type: str):
    _register_cuda_dlls()
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="cuda", compute_type=compute_type)


def merge_chunks(chunks: list[tuple[float, Transcript]]) -> Transcript:
    words = [Word(w.start + off, w.end + off, w.text) for off, t in chunks for w in t.words]
    segs = [Segment(s.start + off, s.end + off, s.text) for off, t in chunks for s in t.segments]
    langs = Counter(t.language for _, t in chunks)
    return Transcript(
        langs.most_common(1)[0][0] if langs else "en",
        sorted(words, key=lambda w: w.start),
        sorted(segs, key=lambda s: s.start),
    )


def _extract_wav(source: Path, offset: float, wav: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(offset), "-t", str(CHUNK_SECONDS),
         "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )


def transcribe(source: Path, job_dir: Path, model_name: str, compute_type: str,
               progress: Progress, should_stop: ShouldStop) -> Transcript:
    final = job_dir / "transcript.json"
    if final.exists():
        return Transcript.load(final)
    chunk_dir = job_dir / "transcript_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    info = probe(source)
    if not info.has_audio:
        result = Transcript("en", [], [])
        result.save(final)
        return result
    total = max(1, math.ceil(info.duration / CHUNK_SECONDS))
    model = None
    parts: list[tuple[float, Transcript]] = []
    try:
        for i in range(total):
            if should_stop():
                raise Canceled()
            offset = float(i * CHUNK_SECONDS)
            saved = chunk_dir / f"{i:04d}.json"
            if saved.exists():
                parts.append((offset, Transcript.load(saved)))
            else:
                if model is None:
                    model = _load_model(model_name, compute_type)
                wav = chunk_dir / f"{i:04d}.wav"
                try:
                    # shortcut: words straddling a 600 s boundary may be cut; upgrade to
                    # silence-aligned chunk edges if it shows up in captions.
                    _extract_wav(source, offset, wav)
                    segments, ginfo = model.transcribe(str(wav), word_timestamps=True, vad_filter=True)
                    words, segs = [], []
                    for s in segments:
                        segs.append(Segment(s.start, s.end, s.text.strip()))
                        words += [Word(w.start, w.end, w.word.strip()) for w in s.words or []]
                finally:
                    wav.unlink(missing_ok=True)
                t = Transcript(ginfo.language, words, segs)
                t.save(saved)
                parts.append((offset, t))
            progress((i + 1) / total)
    finally:
        del model
        gc.collect()
    result = merge_chunks(parts)
    result.save(final)
    return result
