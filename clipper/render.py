"""ffmpeg rendering: 1080x1920 H.264/AAC clips (moving crop or blurred-fit) with optional captions."""

from __future__ import annotations

import subprocess
from pathlib import Path

from clipper.captions import apply_corrections, build_ass, chunk_words, words_in_range
from clipper.models import CaptionStyle, CropPlan, Word
from clipper.reframe import crop_width

_NVENC = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"]
_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
_AUDIO = ["-c:a", "aac", "-b:a", "160k"]
_encoder: list[str] | None = None


class RenderError(Exception):
    """Message is the tail of ffmpeg's stderr."""


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def _tail(r: subprocess.CompletedProcess) -> str:
    return "\n".join(r.stderr.strip().splitlines()[-20:])


def pick_encoder() -> list[str]:
    global _encoder
    if _encoder is None:
        try:
            ok = _run(["-f", "lavfi", "-i", "color=black:s=256x256:d=1", "-c:v", "h264_nvenc", "-f", "null", "-"],
                      Path.cwd()).returncode == 0
        except OSError:
            ok = False
        _encoder = _NVENC if ok else _X264
    return _encoder


def _sendcmd(keys, cw: int, src_w: int) -> str:
    """sendcmd script: crop's x is runtime-tunable, so each key pair becomes one linear ramp over t."""
    xs = [(k.t, min(max(k.x, 0), src_w - cw)) for k in keys]
    lines = [f"0 crop x {xs[0][1]};"]
    for (t0, x0), (t1, x1) in zip(xs, xs[1:]):
        if t1 <= t0 or x0 == x1:  # a scene cut (or a flat stretch) is a plain jump
            lines.append(f"{t1:.3f} crop x {x1};")
        else:
            lines.append(f"{t0:.3f}-{t1:.3f} crop x '{x0}+({x1}-{x0})*(t-{t0:.3f})/{t1 - t0:.3f}';")
            lines.append(f"{t1:.3f} crop x {x1};")  # stop extrapolating the ramp after the key
    return "\n".join(lines) + "\n"


def build_filter(plan: CropPlan, ass_name: str | None, cmd_name: str = "sendcmd.txt") -> tuple[str, str | None]:
    cmd = None
    if plan.mode == "follow":
        cw = crop_width(plan.src_h)
        cmd = _sendcmd(plan.keys, cw, plan.src_w)
        x0 = min(max(plan.keys[0].x, 0), plan.src_w - cw)
        f = f"sendcmd=f={cmd_name},crop={cw}:{plan.src_h}:{x0}:0,scale=1080:1920"
    else:
        f = ("split=2[bg][fg];"
             "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5[b];"
             "[fg]scale=1080:-2[f];[b][f]overlay=(W-w)/2:(H-h)/2")
    f += ",setsar=1,format=yuv420p"
    if ass_name:
        f += f",subtitles={ass_name}"
    return f, cmd


def render_clip(source: Path, out_path: Path, start: float, end: float, plan: CropPlan,
                words: list[Word] | None, style: CaptionStyle | None, delay: float,
                corrections: dict[int, str] | None = None) -> None:
    global _encoder
    source, out_path = Path(source).resolve(), Path(out_path).resolve()
    folder, stem = out_path.parent, out_path.stem
    tmp = out_path.with_suffix(".tmp.mp4")
    cmd_file, ass_final = folder / f"{stem}.tmp.txt", folder / f"{stem}.ass"
    ass_tmp, jpg_tmp = folder / f"{stem}.ass.tmp", folder / f"{stem}.tmp.jpg"
    jpg_final = out_path.with_suffix(".jpg")
    try:
        ass_name = None
        if words is not None:
            clip_words = apply_corrections(words_in_range(words, start, end), corrections or {})
            ass_tmp.write_text(build_ass(chunk_words(clip_words, delay=delay), style or "bold_white"), encoding="utf-8")
            ass_tmp.replace(ass_final)
            ass_name = ass_final.name
        vf, cmd = build_filter(plan, ass_name, cmd_file.name)
        if cmd:
            cmd_file.write_text(cmd, encoding="utf-8")

        def encode(enc: list[str]) -> subprocess.CompletedProcess:
            return _run(["-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}", "-vf", vf,
                         *enc, *_AUDIO, "-movflags", "+faststart", str(tmp)], folder)

        enc = pick_encoder()
        r = encode(enc)
        if r.returncode != 0 and enc is _NVENC and any(k in r.stderr.lower() for k in ("nvenc", "cuda", "nvcuda")):
            _encoder = _X264  # NVENC itself failed; stop trying it
            r = encode(_X264)
        if r.returncode != 0:
            raise RenderError(_tail(r))

        # poster first, from the temp clip, so the final clip name only appears once everything succeeded
        r = _run(["-ss", "1" if end - start > 1 else "0", "-i", str(tmp), "-frames:v", "1", "-q:v", "2", str(jpg_tmp)], folder)
        if r.returncode != 0:
            raise RenderError(_tail(r))
        jpg_tmp.replace(jpg_final)
        tmp.replace(out_path)
    except BaseException:
        ass_final.unlink(missing_ok=True)
        jpg_final.unlink(missing_ok=True)
        raise
    finally:
        for p in (tmp, cmd_file, ass_tmp, jpg_tmp):
            p.unlink(missing_ok=True)
