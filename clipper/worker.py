"""Background worker: runs queued jobs from SQLite, resumes after a crash, honours cancel."""
from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from clipper import db
from clipper.config import Settings
from clipper.ingest import IngestError, accept_upload, download, probe
from clipper.models import Canceled, CropKey, CropPlan, Transcript
from clipper.moments_llm import MomentsError, find_moments
from clipper.reframe import plan_crop
from clipper.render import RenderError, render_clip
from clipper.transcribe import transcribe

GONE = "The source video was cleaned up, so this clip can't be changed."
LOG_HINT = r"Details are in logs\worker.log."
OOM = "The graphics card ran out of memory. Try a smaller whisper_model or ollama_model in config.toml."
_PLAIN_ERRORS = (IngestError, MomentsError, RenderError)


@dataclass
class Steps:
    """The pipeline callables; tests swap in fakes."""
    download: Callable = download
    accept_upload: Callable = accept_upload
    probe: Callable = probe
    transcribe: Callable = transcribe
    find_moments: Callable = find_moments
    plan_crop: Callable = plan_crop
    render_clip: Callable = render_clip


class JobError(Exception):
    """Plain-English failure shown to the owner as-is."""


class _Paused(Exception):
    """Worker.stop() was called; the job goes back to the queue and resumes next start."""


def job_dir(settings: Settings, job_id: int) -> Path:
    return settings.data_dir / "jobs" / str(job_id)


def _clip_path(d: Path, clip_id: int, version: int) -> Path:
    return d / "clips" / f"{clip_id}_v{version}.mp4"


def _find_source(d: Path) -> Path | None:
    # source.jpg is the download thumbnail; *.tmp is an unfinished upload move
    return next((p for p in sorted(d.glob("source.*")) if p.suffix not in (".jpg", ".tmp")), None)


def _corrections(raw) -> dict[int, str]:
    return {int(k): v for k, v in (json.loads(raw) if isinstance(raw, str) else raw or {}).items()}


def cleanup_sources(conn, settings: Settings, now: float) -> int:
    cutoff = now - settings.source_retention_days * 86400
    count = 0
    for job in db.list_jobs(conn)[1]:  # done, failed and canceled: all hold sources
        if (job["kind"] == "rerender" or job["source_deleted"]
                or not job["finished_at"] or job["finished_at"] >= cutoff):
            continue
        d = job_dir(settings, job["id"])
        try:
            for p in d.glob("source.*"):
                if p.suffix != ".jpg":  # keep the small thumbnail for the job card
                    p.unlink()
            if job["upload_path"]:  # an upload that failed before it was moved into the job folder
                Path(job["upload_path"]).unlink(missing_ok=True)
        except OSError:  # file in use (e.g. being streamed); try again next hour
            continue
        db.update_job(conn, job["id"], source_deleted=1)
        count += 1
    return count


class Worker(threading.Thread):
    def __init__(self, settings: Settings, db_path: Path, steps: Steps | None = None):
        super().__init__(name="clipper-worker", daemon=True)
        self.settings = settings
        self.steps = steps or Steps()
        self.conn = db.connect(Path(db_path))  # own connection, never the web app's
        self._stop_event = threading.Event()
        self._last_cleanup = float("-inf")
        self._last_dl_drain = float("-inf")

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        db.requeue_running(self.conn)
        while not self._stop_event.is_set():
            try:
                now = time.time()
                if now - self._last_cleanup >= 3600:
                    self._last_cleanup = now
                    cleanup_sources(self.conn, self.settings, now)
                busy = self.run_once()
            except Exception:
                self._log(None)
                busy = False
            if not busy:
                self._stop_event.wait(1)

    def run_once(self) -> bool:
        job = db.next_job(self.conn)
        if job is None:
            return False
        self._process(job)
        return True

    # --- job handling ---------------------------------------------------------------

    def _process(self, job) -> None:
        jid = job["id"]
        try:
            if job["kind"] == "rerender":
                self._rerender(job)  # its final cancel check sits just before the clip row changes
            else:
                self._video(job)
                if self._cancel_requested(jid):  # final, uncached check: a late cancel still wins
                    raise Canceled()
            db.update_job(self.conn, jid, status="done", finished_at=time.time())
        except Canceled:
            db.update_job(self.conn, jid, status="canceled", finished_at=time.time())
        except _Paused:
            db.update_job(self.conn, jid, status="queued")
        except (JobError, *_PLAIN_ERRORS) as e:
            if isinstance(e, RenderError):
                self._log(jid, e.details)  # the owner sees a plain message; ffmpeg's own words go to the log
            db.update_job(self.conn, jid, status="failed", error=str(e), finished_at=time.time())
        except Exception as e:
            self._log(jid)
            msg = OOM if "out of memory" in str(e).lower() else f"Something went wrong: {type(e).__name__}. {LOG_HINT}"
            db.update_job(self.conn, jid, status="failed", error=msg, finished_at=time.time())

    def _log(self, job_id, details: str = "") -> None:
        try:
            logs = self.settings.data_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with open(logs / "worker.log", "a", encoding="utf-8") as f:
                extra = f"ffmpeg said:\n{details}\n" if details else ""
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} job {job_id}\n{traceback.format_exc()}{extra}\n")
        except OSError:
            pass

    def _cancel_requested(self, job_id: int) -> bool:
        return bool(db.get_job(self.conn, job_id)["cancel_requested"])

    def _should_stop(self, job_id: int) -> Callable[[], bool]:
        last, value = float("-inf"), False

        def should_stop() -> bool:  # cached: at most one SQLite read per 0.5 s
            nonlocal last, value
            now = time.monotonic()
            if now - last >= 0.5:
                last = now
                value = self._cancel_requested(job_id)
            return value
        return should_stop

    def _checkpoint(self, should_stop: Callable[[], bool]) -> None:
        if should_stop():
            raise Canceled()
        if self._stop_event.is_set():
            raise _Paused()

    def _chunk_progress(self, job_id: int) -> Callable[[float], None]:
        # transcribe/find_moments call progress only between chunks, never while a model runs,
        # so queued re-renders slot in here. The 4 GB VRAM budget relies on render using
        # NVENC (small memory) or falling back to libx264 on the CPU.
        def progress(f: float) -> None:
            db.update_job(self.conn, job_id, step_progress=f)
            self._drain_rerenders()
        return progress

    def _download_progress(self, job_id: int) -> Callable[[float], None]:
        # a long download must not hold up re-renders; each drain costs SQLite reads, so at most every 5 s
        def progress(f: float) -> None:
            db.update_job(self.conn, job_id, step_progress=f)
            if time.monotonic() - self._last_dl_drain >= 5:
                self._last_dl_drain = time.monotonic()
                self._drain_rerenders()
        return progress

    def _drain_rerenders(self) -> None:
        while (job := db.next_rerender(self.conn)) is not None:
            self._process(job)

    def _video(self, job) -> None:
        jid, s, conn = job["id"], self.steps, self.conn
        d = job_dir(self.settings, jid)
        d.mkdir(parents=True, exist_ok=True)
        should_stop = self._should_stop(jid)

        def begin(step: int) -> None:
            self._checkpoint(should_stop)
            db.update_job(conn, jid, step=step, step_progress=0.0, step_started_at=time.time())

        begin(1)
        source = _find_source(d)
        if source is None:
            if job["source_url"]:
                info = s.download(job["source_url"], d, self._download_progress(jid), should_stop)
            else:
                info = s.accept_upload(Path(job["upload_path"]), d)
            source = info.path
            db.update_job(conn, jid, title=job["title"] or info.title)
        media = s.probe(source)
        db.update_job(conn, jid, duration=media.duration)

        begin(2)
        tfile = d / "transcript.json"
        transcript = Transcript.load(tfile) if tfile.exists() else s.transcribe(
            source, d, self.settings.whisper_model, self.settings.whisper_compute_type,
            self._chunk_progress(jid), should_stop)

        begin(3)
        moments = s.find_moments(source, transcript, job["video_type"], job["length_preset"], self.settings, d,
                                 self._chunk_progress(jid), should_stop)

        begin(4)
        (d / "clips").mkdir(exist_ok=True)
        for i, m in enumerate(moments):
            self._checkpoint(should_stop)
            # a moment's clip is the row with its exact start/end, so a restart neither duplicates nor misses
            clip = next((c for c in db.list_clips(conn, jid) if c["start"] == m.start and c["end"] == m.end), None)
            if clip is None:
                cid = db.add_clip(conn, job_id=jid, start=m.start, end=m.end, title=m.title, score=m.score,
                                  captions_on=int(bool(job["captions_on"])),
                                  caption_style=job["caption_style"] or self.settings.default_caption_style)
                clip = db.get_clip(conn, cid)
            out = _clip_path(d, clip["id"], clip["version"])
            if not out.exists():
                self._render(source, media, d, clip["id"], out, m.start, m.end, job["video_type"],
                             transcript.words if clip["captions_on"] else None, clip["caption_style"],
                             _corrections(clip["corrections"]))
            # also heals a row left at file=NULL by a crash between render's final rename and this update
            if (clip["file"], clip["poster"]) != (str(out), str(out.with_suffix(".jpg"))):
                db.update_clip(conn, clip["id"], file=str(out), poster=str(out.with_suffix(".jpg")))
            db.update_job(conn, jid, step_progress=(i + 1) / len(moments))
            self._drain_rerenders()

    def _render(self, source, media, d, clip_id, out, start, end, video_type, words, style, corrections) -> None:
        # the crop plan is cached per clip so a caption-only re-render skips face tracking
        plan_file = d / "clips" / f"{clip_id}_plan.json"
        saved = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else {}
        if saved.get("start") == start and saved.get("end") == end:
            p = saved["plan"]
            plan = CropPlan(p["mode"], p["src_w"], p["src_h"], [CropKey(**k) for k in p["keys"]])
        else:
            plan = self.steps.plan_crop(source, start, end, video_type, media)
            tmp = plan_file.with_name(plan_file.name + ".tmp")
            tmp.write_text(json.dumps({"start": start, "end": end, "plan": asdict(plan)}), encoding="utf-8")
            tmp.replace(plan_file)
        self.steps.render_clip(source, out, start, end, plan, words, style, self.settings.caption_delay, corrections)

    def _rerender(self, job) -> None:
        if job["cancel_requested"]:
            raise Canceled()
        clip = db.get_clip(self.conn, job["clip_id"])
        if clip is None:
            raise JobError("This clip was deleted.")
        vjob = db.get_job(self.conn, clip["job_id"])
        d = job_dir(self.settings, vjob["id"])
        source = None if vjob["source_deleted"] else _find_source(d)
        if source is None:
            raise JobError(GONE)

        p = json.loads(job["params"] or "{}")
        start, end = float(p.get("start", clip["start"])), float(p.get("end", clip["end"]))
        captions_on = bool(p.get("captions_on", clip["captions_on"]))
        style = p.get("caption_style", clip["caption_style"])
        corrections = _corrections(p.get("corrections", clip["corrections"]))
        words = Transcript.load(d / "transcript.json").words if captions_on else None

        version = clip["version"] + 1
        while _clip_path(d, clip["id"], version).exists():  # leftover from a crash: never render over a file
            version += 1
        out = _clip_path(d, clip["id"], version)
        self._render(source, self.steps.probe(source), d, clip["id"], out, start, end, vjob["video_type"],
                     words, style, corrections)
        if self._cancel_requested(job["id"]):  # canceled while rendering: drop the new files, keep the clip as it was
            for f in (out, out.with_suffix(".jpg"), out.with_suffix(".ass")):
                f.unlink(missing_ok=True)
            raise Canceled()

        db.update_clip(self.conn, clip["id"], start=start, end=end, captions_on=int(captions_on),
                       caption_style=style, corrections=json.dumps({str(k): v for k, v in corrections.items()}),
                       file=str(out), poster=str(out.with_suffix(".jpg")), version=version)
        if clip["file"]:
            old = Path(clip["file"])
            for f in (old, old.with_suffix(".jpg"), old.with_suffix(".ass")):
                try:
                    f.unlink(missing_ok=True)
                except OSError:  # being streamed right now; harmless leftover
                    pass
