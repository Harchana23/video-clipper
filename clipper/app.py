"""Web app: login, home page, live queue API. Every route except /login and /static needs a session."""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
import zipfile

import anyio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import get_args
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from clipper import db
from clipper.auth import SESSION_MAX_AGE, Lockout, check_session, make_session, verify_password
from clipper.captions import words_in_range
from clipper.config import Settings
from clipper.ingest import safe_filename
from clipper.models import LENGTH_RANGES, CaptionStyle, Transcript, VideoType
from clipper.worker import Worker, _find_source, job_dir

HERE = Path(__file__).parent
UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
CHUNK = 1024 * 1024
STALE_ZIP_SECONDS = 3600
log = logging.getLogger(__name__)
VIDEO_TYPES = {"talk": "Talk show", "gaming": "Gaming", "movie": "Movie"}
LENGTHS = {"auto": "Auto (30–90s)", "short": "Under 30s", "medium": "30–60s", "long": "60–90s"}
STYLES = {"bold_white": "Bold White", "yellow_pop": "Yellow Pop"}
assert set(VIDEO_TYPES) == set(get_args(VideoType)) and set(STYLES) == set(get_args(CaptionStyle))
assert set(LENGTHS) == set(LENGTH_RANGES)


class _BadForm(Exception):
    pass


MIN_CLIP = 3.0  # seconds
MAX_WORD = 40  # characters in one caption word
PROCESSING = "This video is still processing."
CLEANED_UP = "The source video was cleaned up, so this clip can't be changed."
BUSY = "This clip is already being re-rendered."
DELETE_BUSY = "This clip is being re-rendered."
UPLOAD_GONE = "The uploaded file was cleaned up — upload it again."


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


async def stream_and_delete(path: Path):
    """Stream a file, then remove it, even when the client disconnects (the stream is closed or cancelled)."""
    try:
        with open(path, "rb") as f:
            while chunk := await anyio.to_thread.run_sync(f.read, CHUNK):
                yield chunk
    finally:
        path.unlink(missing_ok=True)


def sweep_stale_zips(d: Path, now: float) -> None:
    """Temp zips a crash or a never-started stream left behind (old enough not to be a download in progress)."""
    for f in d.glob("*.zip.tmp"):
        try:
            if now - f.stat().st_mtime > STALE_ZIP_SECONDS:
                f.unlink()
        except OSError:
            pass


def attachment(name: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{name}"'}  # safe_filename output: ASCII, no quotes


def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError
    return float(v)


def _eta(job, now: float) -> int | None:
    """Seconds left in the current step, from its progress so far; None when unknown."""
    f, t0 = job["step_progress"] or 0.0, job["step_started_at"]
    if job["status"] != "running" or not t0 or f < 0.02 or f >= 1:
        return None
    return max(0, round((now - t0) * (1 - f) / f))


def _job_json(settings: Settings, job, clip_count: int, now: float) -> dict:
    return {
        "id": job["id"],
        "title": job["title"] or job["source_url"] or "Untitled",
        "duration": job["duration"], "step": job["step"] or 0, "step_progress": job["step_progress"] or 0.0,
        "status": job["status"], "error": job["error"], "clip_count": clip_count,
        "eta_seconds": _eta(job, now), "cancel_requested": bool(job["cancel_requested"]),
        "has_thumb": (job_dir(settings, job["id"]) / "source.jpg").is_file(),
        "created_at": job["created_at"], "finished_at": job["finished_at"],
    }


def create_app(settings: Settings, db_path: Path, start_worker: bool = True) -> FastAPI:
    conn = db.connect(Path(db_path))  # the web app's own connection; the Worker opens another
    uploads = settings.data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    lockout, lock = Lockout(), threading.Lock()  # sync routes run in a threadpool
    worker = Worker(settings, Path(db_path)) if start_worker else None

    @asynccontextmanager
    async def lifespan(_app):
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()
            worker.join(timeout=5)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        # runs before routing and body parsing, so anonymous uploads are never read or spooled to disk
        path = request.url.path
        if path in ("/login", "/logout") or path.startswith("/static/"):
            return await call_next(request)
        if check_session(request.cookies.get("session"), settings.session_secret, settings.password_hash):
            return await call_next(request)
        if request.method == "GET" and not path.startswith(("/api/", "/media/")):
            return RedirectResponse("/login", 303)
        return PlainTextResponse("Sign in required", 401)

    def home_page(request: Request, error: str | None = None, status: int = 200):
        ctx = {"error": error, "video_types": VIDEO_TYPES, "lengths": LENGTHS, "styles": STYLES,
               "captions_on": settings.default_captions_on, "caption_style": settings.default_caption_style}
        return templates.TemplateResponse(request, "home.html", ctx, status_code=status)

    def options(video_type: str, length_preset: str, captions_on: str, caption_style: str) -> dict:
        if video_type not in VIDEO_TYPES:
            raise _BadForm("Pick a video type from the list.")
        if length_preset not in LENGTHS:
            raise _BadForm("Pick a clip length from the list.")
        style = caption_style or settings.default_caption_style
        if style not in STYLES:
            raise _BadForm("Pick a caption style from the list.")
        return {"kind": "video", "status": "queued", "video_type": video_type, "length_preset": length_preset,
                "captions_on": int(captions_on.lower() in ("on", "1", "true", "yes")), "caption_style": style}

    @app.exception_handler(_BadForm)
    async def bad_form(request: Request, exc: _BadForm):
        return home_page(request, str(exc), 400)

    # --- login ---------------------------------------------------------------------

    def login_page(request: Request, error: str | None = None, status: int = 200):
        return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=status)

    @app.get("/login")
    def login_form(request: Request):
        if check_session(request.cookies.get("session"), settings.session_secret, settings.password_hash):
            return RedirectResponse("/", 303)
        return login_page(request)

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        now = time.time()
        with lock:
            if lockout.is_locked(now):
                minutes = math.ceil(lockout.remaining(now) / 60)
                return login_page(request, f"Too many tries. Try again in {minutes} minutes.", 429)
            ok = verify_password(settings.password_hash, password)
            lockout.reset() if ok else lockout.record_failure(now)
        if not ok:
            return login_page(request, "Wrong password.", 401)
        resp = RedirectResponse("/", 303)
        resp.set_cookie("session", make_session(settings.session_secret, settings.password_hash),
                        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=settings.cookie_secure)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie("session", httponly=True, samesite="lax", secure=settings.cookie_secure)
        return resp

    # --- pages and jobs ------------------------------------------------------------

    @app.get("/")
    def home(request: Request):
        return home_page(request)

    @app.post("/jobs")
    def create_job(url: str = Form(""), video_type: str = Form(""), length_preset: str = Form(""),
                   captions_on: str = Form(""), caption_style: str = Form("")):
        url = url.strip()
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise _BadForm("Paste a link that starts with http:// or https://.")
        opts = options(video_type, length_preset, captions_on, caption_style)
        db.create_job(conn, source_url=url, **opts)
        return RedirectResponse("/", 303)

    @app.post("/jobs/upload")
    def upload_job(file: UploadFile | None = File(None), video_type: str = Form(""), length_preset: str = Form(""),
                   captions_on: str = Form(""), caption_style: str = Form("")):
        opts = options(video_type, length_preset, captions_on, caption_style)
        name = (file.filename or "") if file else ""
        ext = Path(name.replace("\\", "/")).suffix.lower()  # client name only supplies the extension and title
        if not name or ext not in UPLOAD_EXTS:
            raise _BadForm("Choose a video file (MP4, MOV, MKV, WebM or M4V).")
        stem = uuid.uuid4().hex
        part, final = uploads / f"{stem}.part", uploads / f"{stem}{ext}"
        try:
            with open(part, "wb") as out:
                while chunk := file.file.read(CHUNK):
                    out.write(chunk)
            part.replace(final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        title = Path(name.replace("\\", "/")).stem
        db.create_job(conn, upload_path=str(final), title=title, **opts)
        return RedirectResponse("/", 303)

    @app.get("/api/jobs")
    def api_jobs():
        now = time.time()
        active, finished = db.list_jobs(conn)

        def rows(jobs):
            return [_job_json(settings, j, sum(1 for c in db.list_clips(conn, j["id"]) if c["file"] is not None), now)
                    for j in jobs if j["kind"] != "rerender"]
        rerendering = sorted({j["clip_id"] for j in active if j["kind"] == "rerender" and j["clip_id"] is not None})
        return {"active": rows(active), "finished": rows(finished), "rerendering": rerendering}

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: int):
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404)
        if job["status"] in ("queued", "running") and not db.cancel_if_queued(conn, job_id, time.time()):
            db.update_job(conn, job_id, cancel_requested=1)  # running: the worker notices at its next checkpoint
        return {"ok": True}

    @app.post("/jobs/{job_id}/retry")
    def retry(job_id: int):
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404)
        if job["status"] not in ("failed", "canceled"):
            raise HTTPException(409, "Only failed or canceled jobs can be retried")
        if (job["upload_path"] and not Path(job["upload_path"]).is_file()
                and _find_source(job_dir(settings, job_id)) is None):  # neither the upload nor its moved copy
            raise HTTPException(409, UPLOAD_GONE)
        db.update_job(conn, job_id, status="queued", error=None, cancel_requested=0, finished_at=None,
                      step=0, step_progress=0.0, source_deleted=0)  # a link downloads again if it was cleaned up
        return {"ok": True}

    @app.get("/media/thumbs/{job_id}.jpg")
    def thumb(job_id: int):
        path = job_dir(settings, job_id) / "source.jpg"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    # --- results page: clips, media, edits -----------------------------------------

    def rendered_clips(job_id: int):
        return [c for c in db.list_clips(conn, job_id) if c["file"] is not None]

    def get_clip_or_404(clip_id: int):
        clip = db.get_clip(conn, clip_id)
        if clip is None or clip["file"] is None:  # still rendering or failed: never shown or served
            raise HTTPException(404)
        return clip, db.get_job(conn, clip["job_id"])

    def clip_path(clip, job, column: str) -> Path:
        """File named by the DB row, only if it lies inside the job's folder."""
        d = job_dir(settings, job["id"]).resolve()
        raw = clip[column]
        path = Path(raw).resolve() if raw else None
        if path is None or not path.is_relative_to(d) or not path.is_file():
            raise HTTPException(404)
        return path

    def rerender_active(clip_id: int) -> bool:
        return any(j["kind"] == "rerender" and j["clip_id"] == clip_id for j in db.list_jobs(conn)[0])

    def editable_clip(clip_id: int, for_edit: bool = True):
        clip, job = get_clip_or_404(clip_id)
        if job["status"] != "done":
            raise HTTPException(409, PROCESSING)
        if for_edit and job["source_deleted"]:
            raise HTTPException(409, CLEANED_UP)
        return clip, job

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "Send a JSON object.")
        return body

    def queue_rerender(clip, params: dict) -> dict:
        if rerender_active(clip["id"]):  # no await since the check, so check + create can't interleave
            raise HTTPException(409, BUSY)
        job_id = db.create_job(conn, kind="rerender", status="queued", priority=10, clip_id=clip["id"],
                               params=json.dumps(params))
        return {"ok": True, "job_id": job_id}

    def clip_words(clip, job) -> list:
        tfile = job_dir(settings, job["id"]) / "transcript.json"
        if not tfile.is_file():
            raise HTTPException(404, "No transcript for this video.")
        return words_in_range(Transcript.load(tfile).words, clip["start"], clip["end"])

    @app.get("/jobs/{job_id}")
    def results(request: Request, job_id: int):
        job = db.get_job(conn, job_id)
        if job is None or job["kind"] == "rerender":
            raise HTTPException(404)
        active, finished = db.list_jobs(conn)
        busy = {j["clip_id"] for j in active if j["kind"] == "rerender"}
        last = {}  # clip id -> its most recent finished re-render (finished is newest first)
        for j in finished:
            if j["kind"] == "rerender":
                last.setdefault(j["clip_id"], j)

        def rerender_error(cid: int) -> str | None:  # shown on the card until the next re-render succeeds
            j = last.get(cid)
            return (j["error"] or "Re-render failed.") if j and j["status"] == "failed" and cid not in busy else None

        clips = [{"id": c["id"], "title": c["title"], "score": c["score"], "version": c["version"],
                  "length": _mmss(c["end"] - c["start"]), "at": _mmss(c["start"]), "start": c["start"],
                  "end": c["end"], "busy": c["id"] in busy, "fixes": json.loads(c["corrections"] or "{}") != {},
                  "failed": rerender_error(c["id"])} for c in rendered_clips(job_id)]
        ctx = {"job": job, "title": job["title"] or job["source_url"] or "Untitled", "clips": clips,
               "styles": STYLES, "duration": job["duration"] or 0, "done": job["status"] == "done",
               "cleaned": bool(job["source_deleted"]), "when": time.strftime(
                   "%b %d", time.localtime(job["finished_at"] or job["created_at"]))}
        return templates.TemplateResponse(request, "results.html", ctx)

    @app.get("/media/clips/{clip_id}.mp4")
    def clip_video(clip_id: int):
        clip, job = get_clip_or_404(clip_id)
        return FileResponse(clip_path(clip, job, "file"), media_type="video/mp4")  # answers Range with 206

    @app.get("/media/posters/{clip_id}.jpg")
    def clip_poster(clip_id: int):
        clip, job = get_clip_or_404(clip_id)
        return FileResponse(clip_path(clip, job, "poster"), media_type="image/jpeg")

    @app.get("/clips/{clip_id}/download")
    def clip_download(clip_id: int):
        clip, job = get_clip_or_404(clip_id)
        return FileResponse(clip_path(clip, job, "file"), media_type="video/mp4",
                            headers=attachment(f"{safe_filename(clip['title'] or 'clip')}.mp4"))

    @app.get("/jobs/{job_id}/zip")
    def job_zip(job_id: int):
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404)
        entries = [(f"{n:02d}_{safe_filename(c['title'] or 'clip')}.mp4", clip_path(c, job, "file"))
                   for n, c in enumerate(rendered_clips(job_id), 1)]
        if not entries:
            raise HTTPException(404)
        sweep_stale_zips(job_dir(settings, job_id), time.time())
        with tempfile.NamedTemporaryFile(dir=job_dir(settings, job_id), suffix=".zip.tmp", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as z:  # mp4 is already compressed
                for name, path in entries:
                    z.write(path, name)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        headers = {**attachment(f"{safe_filename(job['title'] or 'clips')}.zip"),
                   "Content-Length": str(os.path.getsize(tmp_path))}
        return StreamingResponse(stream_and_delete(tmp_path), media_type="application/zip", headers=headers)

    @app.post("/clips/{clip_id}/delete")
    def clip_delete(clip_id: int):
        _, job = editable_clip(clip_id, for_edit=False)
        if rerender_active(clip_id):
            raise HTTPException(409, DELETE_BUSY)
        db.delete_clip(conn, clip_id)
        for f in (job_dir(settings, job["id"]) / "clips").glob(f"{clip_id}_*"):  # "5_" never matches clip 55's files
            try:
                f.unlink(missing_ok=True)
            except OSError:  # e.g. still open for playback on Windows; the row is already gone, so don't 500
                log.warning("could not delete %s", f, exc_info=True)
        return {"ok": True}

    @app.get("/api/clips/{clip_id}/words")
    def clip_words_api(clip_id: int):
        clip, job = get_clip_or_404(clip_id)
        fixes = json.loads(clip["corrections"] or "{}")
        words = [{"i": i, "start": round(w.start, 3), "end": round(w.end, 3), "orig": w.text,
                  "text": fixes.get(str(i), w.text)} for i, w in enumerate(clip_words(clip, job))]
        return {"words": words, "captions_on": bool(clip["captions_on"]), "caption_style": clip["caption_style"]}

    @app.post("/clips/{clip_id}/trim")
    async def clip_trim(request: Request, clip_id: int):
        body = await json_body(request)
        clip, job = editable_clip(clip_id)
        try:
            start, end = _num(body.get("start")), _num(body.get("end"))
        except ValueError:
            raise HTTPException(400, "Start and end must be numbers.")
        if not (0 <= start < end <= (job["duration"] or 0)):
            raise HTTPException(400, "Pick a start and end inside the video.")
        if end - start < MIN_CLIP:
            raise HTTPException(400, f"A clip must be at least {MIN_CLIP:.0f} seconds long.")
        params = {"start": start, "end": end}
        if (start, end) != (clip["start"], clip["end"]):
            params["corrections"] = {}  # stored fixes are positional within the old word range
        return queue_rerender(clip, params)

    @app.post("/clips/{clip_id}/captions")
    async def clip_captions(request: Request, clip_id: int):
        body = await json_body(request)
        clip, job = editable_clip(clip_id)
        on, style, fixes = body.get("captions_on"), body.get("caption_style"), body.get("corrections")
        if not isinstance(on, bool) or style not in STYLES or not isinstance(fixes, dict):
            raise HTTPException(400, "Captions on/off, a caption style and the word fixes are required.")
        count, clean = len(clip_words(clip, job)), {}
        for k, v in fixes.items():
            if not (isinstance(k, str) and k.isascii() and k.isdigit() and int(k) < count) or not isinstance(v, str):
                raise HTTPException(400, "A word fix points at a word that isn't in this clip.")
            text = " ".join(v.split())  # drops newlines and collapses spaces; "" removes the word
            if len(text) > MAX_WORD:
                raise HTTPException(400, f"A caption word can be at most {MAX_WORD} characters.")
            clean[str(int(k))] = text
        return queue_rerender(clip, {"captions_on": on, "caption_style": style, "corrections": clean})

    return app
