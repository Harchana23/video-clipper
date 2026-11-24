from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from clipper.models import Canceled, MediaInfo, Progress, ShouldStop, SourceInfo

FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]"
IS_WINDOWS = sys.platform == "win32"
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
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
    s = re.sub(r'[\\/:*?"<>|{}\x00-\x1f]', "", title)
    s = re.sub(r"[^\x20-\x7e]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")[:max_len].strip(" .")
    return "clip" if not s or s.split(".")[0].strip().upper() in _RESERVED else s


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
        try:
            f.unlink(missing_ok=True)
        except OSError:  # still locked by a dying child; the next run clears it
            pass


def _kill_tree(p: subprocess.Popen) -> None:
    # yt-dlp spawns ffmpeg; on Windows terminate() would leave that child holding files open
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    p.kill()


def download(url: str, job_dir: Path, progress: Progress, should_stop: ShouldStop) -> SourceInfo:
    source, title_file, thumb = job_dir / "source.mp4", job_dir / "title.txt", job_dir / "source.jpg"
    if source.exists():  # resume: only a completed download is ever named source.mp4
        title = title_file.read_text(encoding="utf-8") if title_file.exists() else "clip"
        return SourceInfo(source, title, probe(source).duration, thumb if thumb.exists() else None)

    base = [sys.executable, "-m", "yt_dlp", "--no-playlist"]
    try:
        r = subprocess.run(base + ["--skip-download", "--print", "%(title)s", "--print", "%(filesize_approx)s", url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        raise IngestError("Timed out reaching the video site.")
    if r.returncode != 0:
        raise IngestError(map_ytdlp_error(r.stderr))
    lines = r.stdout.strip().splitlines()
    title = lines[0] if lines else "clip"
    if len(lines) > 1 and lines[-1].isdigit():
        check_disk(job_dir, int(lines[-1]))

    _cleanup(job_dir)  # stale partials from a crashed attempt
    p = subprocess.Popen(
        base + ["-f", FORMAT, "--merge-output-format", "mp4", "--remux-video", "mp4",
                "--write-thumbnail", "--convert-thumbnails", "jpg",
                "--newline", "-o", str(job_dir / "dl.%(ext)s"), url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    lines_q: queue.Queue = queue.Queue()

    def pump() -> None:
        for ln in p.stdout:
            lines_q.put(ln)
        lines_q.put(None)

    threading.Thread(target=pump, daemon=True).start()
    output: list[str] = []
    try:
        try:
            while True:
                if should_stop():  # polled every 0.5 s even when yt-dlp is silent
                    raise Canceled()
                try:
                    line = lines_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    break
                output.append(line)
                if m := _PCT.search(line):
                    progress(float(m.group(1)) / 100)
            rc = p.wait(timeout=30)
        finally:
            if p.poll() is None:
                _kill_tree(p)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            try:
                p.stdout.close()
            except Exception:
                pass
    except BaseException:
        _cleanup(job_dir)
        raise
    if rc != 0:
        _cleanup(job_dir)
        raise IngestError(map_ytdlp_error("".join(output)))
    if not (job_dir / "dl.mp4").exists():
        _cleanup(job_dir)
        raise IngestError("The download finished but no video file was produced.")

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
