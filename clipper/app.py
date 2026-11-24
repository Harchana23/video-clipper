"""Web app: login, home page, live queue API. Every route except /login and /static needs a session."""
from __future__ import annotations

import math
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import get_args
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from clipper import db
from clipper.auth import SESSION_MAX_AGE, Lockout, check_session, make_session, verify_password
from clipper.config import Settings
from clipper.models import LENGTH_RANGES, CaptionStyle, VideoType
from clipper.worker import Worker, job_dir

HERE = Path(__file__).parent
UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
CHUNK = 1024 * 1024
VIDEO_TYPES = {"talk": "Talk show", "gaming": "Gaming", "movie": "Movie"}
LENGTHS = {"auto": "Auto (30–90s)", "short": "Under 30s", "medium": "30–60s", "long": "60–90s"}
STYLES = {"bold_white": "Bold White", "yellow_pop": "Yellow Pop"}
assert set(VIDEO_TYPES) == set(get_args(VideoType)) and set(STYLES) == set(get_args(CaptionStyle))
assert set(LENGTHS) == set(LENGTH_RANGES)


class _BadForm(Exception):
    pass


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

    def require_session(request: Request) -> None:
        if check_session(request.cookies.get("session"), settings.session_secret, settings.password_hash):
            return
        if request.url.path.startswith(("/api/", "/media/")):
            raise HTTPException(401, "Sign in required")
        raise HTTPException(303, headers={"Location": "/login"})

    auth = [Depends(require_session)]

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

    @app.get("/", dependencies=auth)
    def home(request: Request):
        return home_page(request)

    @app.post("/jobs", dependencies=auth)
    def create_job(url: str = Form(""), video_type: str = Form(""), length_preset: str = Form(""),
                   captions_on: str = Form(""), caption_style: str = Form("")):
        url = url.strip()
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise _BadForm("Paste a link that starts with http:// or https://.")
        opts = options(video_type, length_preset, captions_on, caption_style)
        db.create_job(conn, source_url=url, **opts)
        return RedirectResponse("/", 303)

    @app.post("/jobs/upload", dependencies=auth)
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

    @app.get("/api/jobs", dependencies=auth)
    def api_jobs():
        now = time.time()
        active, finished = db.list_jobs(conn)

        def rows(jobs):
            return [_job_json(settings, j, sum(1 for c in db.list_clips(conn, j["id"]) if c["file"] is not None), now)
                    for j in jobs if j["kind"] != "rerender"]
        return {"active": rows(active), "finished": rows(finished)}

    @app.post("/jobs/{job_id}/cancel", dependencies=auth)
    def cancel(job_id: int):
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404)
        if job["status"] in ("queued", "running"):
            db.update_job(conn, job_id, cancel_requested=1)  # the worker notices at its next checkpoint
        return {"ok": True}

    @app.post("/jobs/{job_id}/retry", dependencies=auth)
    def retry(job_id: int):
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404)
        if job["status"] not in ("failed", "canceled"):
            raise HTTPException(409, "Only failed or canceled jobs can be retried")
        db.update_job(conn, job_id, status="queued", error=None, cancel_requested=0, finished_at=None,
                      step=0, step_progress=0.0)
        return {"ok": True}

    @app.get("/media/thumbs/{job_id}.jpg", dependencies=auth)
    def thumb(job_id: int):
        path = job_dir(settings, job_id) / "source.jpg"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    return app
