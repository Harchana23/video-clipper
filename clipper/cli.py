from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from clipper.config import load_settings
from clipper.ingest import IngestError, accept_upload, download, probe, safe_filename
from clipper.models import LENGTH_RANGES, SourceInfo
from clipper.moments_llm import MomentsError, find_moments
from clipper.transcribe import transcribe


def _mmss(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def _progress(label: str):
    def cb(f: float) -> None:
        print(f"\r{label}: {f * 100:3.0f}%", end="", file=sys.stderr, flush=True)
        if f >= 1:
            print(file=sys.stderr)
    return cb


def find_moments_cmd(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config))
    never = lambda: False  # noqa: E731
    root = settings.data_dir / "tryout"
    if args.source.startswith(("http://", "https://")):
        # downloaded into a staging dir first: the final folder is named after the video title
        stage = root / "_incoming"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        info = download(args.source, stage, _progress("download"), never)
        job_dir = root / safe_filename(info.title)
        job_dir.mkdir(parents=True, exist_ok=True)
        dest = job_dir / info.path.name
        dest.unlink(missing_ok=True)
        shutil.move(str(info.path), dest)
        info = replace(info, path=dest)
        shutil.rmtree(stage, ignore_errors=True)
    else:
        src = Path(args.source)
        job_dir = root / safe_filename(src.stem)
        job_dir.mkdir(parents=True, exist_ok=True)
        existing = next((f for f in job_dir.glob("source.*") if f.suffix != ".tmp"), None)
        if existing is not None:  # already ingested: resume
            info = SourceInfo(existing, src.stem, probe(existing).duration, None)
        else:
            tmp = job_dir / f"incoming{src.suffix}"  # accept_upload moves its input; keep the original
            shutil.copy2(src, tmp)
            info = accept_upload(tmp, job_dir)
    transcript = transcribe(info.path, job_dir, settings.whisper_model, settings.whisper_compute_type,
                            _progress("transcribe"), never)
    moments = find_moments(info.path, transcript, args.type, args.length, settings, job_dir,
                           _progress("moments"), never)
    print(f"{'SCORE':>5}  {'START-END':<13}  TITLE")
    for m in sorted(moments, key=lambda m: -m.score):
        print(f"{m.score:>5}  {_mmss(m.start)}-{_mmss(m.end):<7}  {m.title}")
    if not moments:
        print("(no moments scored above the threshold)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clipper")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find-moments", help="print the best clip moments for a video")
    f.add_argument("source", help="URL or local video file")
    f.add_argument("--type", choices=["talk", "gaming", "movie"], default="talk")
    f.add_argument("--length", choices=list(LENGTH_RANGES), default="auto")
    f.add_argument("--config", default="config.toml")
    f.set_defaults(func=find_moments_cmd)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (MomentsError, IngestError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
