from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from clipper.models import Canceled, MediaInfo, Progress, ShouldStop, SourceInfo

FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]"
_PCT = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


class IngestError(Exception):
    """Message is plain English, shown to the owner."""


def probe(path: Path) -> MediaInfo:
    bad = IngestError("This file isn't a video we can read.")
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise bad
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise bad
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise bad
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return MediaInfo(duration, int(video["width"]), int(video["height"]),
                     any(s.get("codec_type") == "audio" for s in streams))


def safe_filename(title: str, max_len: int = 80) -> str:
    s = re.sub(r'[\/:*?"<>|\x00-\x1f]', "", title)
    s = re.sub(r"[^\x20-\x7e]", "", s)
    s = re.sub(r"\s+", " ", s).strip()[:max_len].strip()
    return s or "clip"


def map_ytdlp_error(stderr: str) -> str:
    low = stderr.lower()
    if "private video" in low:
        return "This video is private."
    if "video unavailable" in low or "removed" in low:
        return "This video was removed or is unavailable."
    if "country" in low or "geo" in low:
        return "This video is blocked in your country."
    if "unsupported url" in low:
        return "This site isn't supported."
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return "Download failed: " + (lines[-1].strip()[:200] if lines else "unknown error")


def check_disk(directory: Path, needed_bytes: int) -> None:
    if shutil.disk_usage(directory)[2] < 3 * needed_bytes:
        raise IngestError("Not enough disk space to download this video.")


def _cleanup(job_dir: Path) -> None:
    for f in job_dir.glob("dl.*"):
        f.unlink(missing_ok=True)


def download(url: str, job_dir: Path, progress: Progress, should_stop: ShouldStop) -> SourceInfo:
    source, title_file, thumb = job_dir / "source.mp4", job_dir / "title.txt", job_dir / "source.jpg"
    if source.exists():  # resume: only a completed download is ever named source.mp4
        title = title_file.read_text(encoding="utf-8") if title_file.exists() else "clip"
        return SourceInfo(source, title, probe(source).duration, thumb if thumb.exists() else None)

    base = [sys.executable, "-m", "yt_dlp", "--no-playlist"]
    r = subprocess.run(base + ["--skip-download", "--print", "%(title)s", "--print", "%(filesize_approx)s", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise IngestError(map_ytdlp_error(r.stderr))
    lines = r.stdout.strip().splitlines()
    title = lines[0] if lines else "clip"
    if len(lines) > 1 and lines[-1].isdigit():
        check_disk(job_dir, int(lines[-1]))

    _cleanup(job_dir)  # stale partials from a crashed attempt
    p = subprocess.Popen(
        base + ["-f", FORMAT, "--merge-output-format", "mp4", "--write-thumbnail", "--convert-thumbnails", "jpg",
                "--newline", "-o", str(job_dir / "dl.%(ext)s"), url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    output: list[str] = []
    for line in p.stdout:
        if should_stop():
            p.terminate()
            p.wait()
            _cleanup(job_dir)
            raise Canceled()
        output.append(line)
        if m := _PCT.search(line):
            progress(float(m.group(1)) / 100)
    if p.wait() != 0 or not (job_dir / "dl.mp4").exists():
        _cleanup(job_dir)
        raise IngestError(map_ytdlp_error("".join(output)))

    thumbnail = None
    if (job_dir / "dl.jpg").exists():
        (job_dir / "dl.jpg").replace(thumb)
        thumbnail = thumb
    title_file.write_text(title, encoding="utf-8")
    (job_dir / "dl.mp4").replace(source)
    _cleanup(job_dir)
    return SourceInfo(source, title, probe(source).duration, thumbnail)


def accept_upload(tmp_file: Path, job_dir: Path) -> SourceInfo:
    info = probe(tmp_file)
    dest = job_dir / f"source{tmp_file.suffix.lower() or '.mp4'}"
    part = dest.with_name(dest.name + ".tmp")
    shutil.move(str(tmp_file), part)
    part.replace(dest)
    return SourceInfo(dest, tmp_file.stem, info.duration, None)
