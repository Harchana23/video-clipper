import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper import db
from clipper.app import create_app
from clipper.auth import hash_password
from clipper.cli import uvicorn_kwargs
from clipper.config import load_settings
from clipper.worker import job_dir

PW = "correct horse"


def _make(tmp_path, secure=False):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        f'password_hash = "{hash_password(PW)}"\n'
        'session_secret = "test-secret"\n'
        f"cookie_secure = {str(secure).lower()}\n", encoding="utf-8")
    settings = load_settings(cfg)
    db_path = settings.data_dir / "clipper.db"
    return settings, db_path, TestClient(create_app(settings, db_path, start_worker=False), follow_redirects=False)


@pytest.fixture
def ctx(tmp_path):
    return _make(tmp_path)


@pytest.fixture
def client(ctx):
    return ctx[2]


@pytest.fixture
def authed(client):
    assert client.post("/login", data={"password": PW}).status_code == 303
    return client


@pytest.fixture
def conn(ctx):
    return db.connect(ctx[1])


OK = {"url": "https://youtu.be/x", "video_type": "talk", "length_preset": "auto"}


def test_pages_redirect_to_login_without_session(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_api_and_media_return_401_without_session(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/media/thumbs/1.jpg").status_code == 401
    assert client.get("/static/app.css").status_code == 200  # static is public


def test_login_sets_session_and_shows_home(client):
    r = client.post("/login", data={"password": PW})
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = client.get("/")
    assert home.status_code == 200
    assert "Talk show" in home.text and "Gaming" in home.text and "Movie" in home.text
    toggle = home.text.split('name="captions_on"')[1].split(">")[0]
    assert "checked" not in toggle


def test_wrong_password_rejected(client):
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401 and "Wrong password" in r.text
    assert client.get("/").status_code == 303


def test_cookie_flags(tmp_path):
    *_, c = _make(tmp_path, secure=True)
    h = c.post("/login", data={"password": PW}).headers["set-cookie"].lower()
    assert "httponly" in h and "samesite=lax" in h and "secure" in h and f"max-age={30 * 86400}" in h


def test_login_lockout_message_after_five_wrong(client):
    for _ in range(5):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    r = client.post("/login", data={"password": PW})  # even the right password is refused while locked
    assert r.status_code == 429 and "Too many tries. Try again in 15 minutes." in r.text
    assert client.get("/").status_code == 303


def test_successful_login_resets_failures(client):
    for _ in range(4):
        client.post("/login", data={"password": "nope"})
    assert client.post("/login", data={"password": PW}).status_code == 303
    client.post("/logout")
    for _ in range(4):
        assert client.post("/login", data={"password": "nope"}).status_code == 401


def test_logout_clears_session(authed):
    assert authed.post("/logout").status_code == 303
    assert authed.get("/").status_code == 303


def test_create_job_validates_url_and_options(authed):
    assert authed.post("/jobs", data={**OK, "url": "file:///etc/passwd"}).status_code == 400
    assert authed.post("/jobs", data={**OK, "url": "https://"}).status_code == 400
    assert authed.post("/jobs", data={**OK, "video_type": "bogus"}).status_code == 400
    assert authed.post("/jobs", data={**OK, "length_preset": "huge"}).status_code == 400
    assert authed.post("/jobs", data={**OK, "caption_style": "neon"}).status_code == 400


def test_create_job_defaults_captions_off(authed, conn):
    r = authed.post("/jobs", data=OK)
    assert r.status_code == 303
    job = db.list_jobs(conn)[0][0]
    assert (job["status"], job["kind"], job["source_url"]) == ("queued", "video", "https://youtu.be/x")
    assert job["captions_on"] == 0 and job["video_type"] == "talk" and job["length_preset"] == "auto"


def test_create_job_with_captions(authed, conn):
    authed.post("/jobs", data={**OK, "captions_on": "on", "caption_style": "yellow_pop"})
    job = db.list_jobs(conn)[0][0]
    assert job["captions_on"] == 1 and job["caption_style"] == "yellow_pop"


def test_upload_streams_to_part_file_and_queues(authed, ctx, conn, tiny_video):
    up = ctx[0].data_dir / "uploads"
    r = authed.post("/jobs/upload", data={"video_type": "movie", "length_preset": "short"},
                    files={"file": ("../../evil name.MP4", tiny_video.read_bytes(), "video/mp4")})
    assert r.status_code == 303
    job = db.list_jobs(conn)[0][0]
    path = Path(job["upload_path"])
    assert path.parent == up and path.suffix == ".mp4" and path.stat().st_size == tiny_video.stat().st_size
    assert [p.name for p in up.iterdir()] == [path.name]  # no .part left behind
    assert job["title"] == "evil name" and job["status"] == "queued" and job["video_type"] == "movie"


def test_upload_rejects_bad_extension_and_leaves_nothing(authed, ctx, conn):
    r = authed.post("/jobs/upload", data={"video_type": "talk", "length_preset": "auto"},
                    files={"file": ("x.exe", b"abc", "application/octet-stream")})
    assert r.status_code == 400
    assert db.list_jobs(conn) == ([], [])
    assert not list((ctx[0].data_dir / "uploads").iterdir())


def test_upload_requires_file_and_valid_options(authed):
    assert authed.post("/jobs/upload", data={"video_type": "talk", "length_preset": "auto"}).status_code == 400
    r = authed.post("/jobs/upload", data={"video_type": "x", "length_preset": "auto"},
                    files={"file": ("a.mp4", b"abc", "video/mp4")})
    assert r.status_code == 400


def test_api_jobs_shape_and_clip_count(authed, ctx, conn):
    s = ctx[0]
    a = db.create_job(conn, kind="video", status="running", title="Pod", step=3, step_progress=0.25,
                      step_started_at=time.time() - 60, duration=3600.0)
    q = db.create_job(conn, kind="video", status="queued", source_url="https://youtu.be/q")
    d = db.create_job(conn, kind="video", status="done", title="Old", finished_at=time.time())
    db.create_job(conn, kind="rerender", status="queued")  # internal, never listed
    db.add_clip(conn, job_id=d, file="a.mp4")
    db.add_clip(conn, job_id=d, file=None)
    job_dir(s, a).mkdir(parents=True)
    (job_dir(s, a) / "source.jpg").write_bytes(b"jpg")
    data = authed.get("/api/jobs").json()
    assert [j["id"] for j in data["active"]] == [a, q] and [j["id"] for j in data["finished"]] == [d]
    run = data["active"][0]
    assert (run["title"], run["step"], run["step_progress"], run["status"], run["duration"]) == ("Pod", 3, 0.25, "running", 3600.0)
    assert 170 <= run["eta_seconds"] <= 190  # 60 s elapsed at 25% -> 180 s left in this step
    assert run["has_thumb"] is True
    assert data["active"][1]["eta_seconds"] is None and data["active"][1]["title"] == "https://youtu.be/q"
    assert data["finished"][0]["clip_count"] == 1 and data["finished"][0]["has_thumb"] is False


def test_thumbs_served_and_missing_is_404(authed, ctx, conn):
    jid = db.create_job(conn, kind="video", status="done")
    assert authed.get(f"/media/thumbs/{jid}.jpg").status_code == 404
    job_dir(ctx[0], jid).mkdir(parents=True)
    (job_dir(ctx[0], jid) / "source.jpg").write_bytes(b"jpgdata")
    r = authed.get(f"/media/thumbs/{jid}.jpg")
    assert r.status_code == 200 and r.content == b"jpgdata"
    assert authed.get("/media/thumbs/abc.jpg").status_code == 422  # non-int id never reaches the filesystem


def test_cancel_and_retry(authed, conn):
    jid = db.create_job(conn, kind="video", status="running")
    assert authed.post(f"/jobs/{jid}/cancel").status_code == 200
    assert db.get_job(conn, jid)["cancel_requested"] == 1
    assert authed.post(f"/jobs/{jid}/retry").status_code == 409  # still running
    db.update_job(conn, jid, status="failed", error="boom", step=3, step_progress=0.5, finished_at=1.0)
    assert authed.post(f"/jobs/{jid}/retry").status_code == 200
    j = db.get_job(conn, jid)
    assert (j["status"], j["error"], j["cancel_requested"], j["finished_at"]) == ("queued", None, 0, None)
    assert authed.post("/jobs/999/cancel").status_code == 404
    assert authed.post("/jobs/999/retry").status_code == 404


def test_server_binds_localhost_only(ctx):
    kw = uvicorn_kwargs(ctx[0], port=None)
    assert kw["host"] == "127.0.0.1" and kw["port"] == ctx[0].port
    assert uvicorn_kwargs(ctx[0], port=9000)["port"] == 9000


def test_unauthenticated_upload_is_rejected_before_body_is_read(client, ctx, monkeypatch):
    from starlette.requests import Request

    async def never(self, *a, **k):
        raise AssertionError("request body was parsed for an anonymous client")
    monkeypatch.setattr(Request, "form", never)
    r = client.post("/jobs/upload", data={"video_type": "talk", "length_preset": "auto"},
                    files={"file": ("a.mp4", b"x" * 1000, "video/mp4")})
    assert r.status_code == 401
    assert client.post("/jobs", data=OK).status_code == 401
    assert list((ctx[0].data_dir / "uploads").iterdir()) == []


def test_cancel_of_queued_job_is_immediate(authed, conn):
    jid = db.create_job(conn, kind="video", status="queued")
    assert authed.post(f"/jobs/{jid}/cancel").status_code == 200
    j = db.get_job(conn, jid)
    assert (j["status"], j["cancel_requested"]) == ("canceled", 0) and j["finished_at"]
    assert db.next_job(conn) is None  # the worker can no longer claim it


def test_cancel_if_queued_loses_to_a_claim(conn):
    jid = db.create_job(conn, kind="video", status="queued")
    assert db.next_job(conn)["id"] == jid  # the worker claimed it first
    assert db.cancel_if_queued(conn, jid, time.time()) is False
    assert db.get_job(conn, jid)["status"] == "running"


def test_retry_after_cleanup(authed, ctx, conn):
    link = db.create_job(conn, kind="video", status="failed", source_url="https://x/y", source_deleted=1)
    assert authed.post(f"/jobs/{link}/retry").status_code == 200
    j = db.get_job(conn, link)
    assert (j["status"], j["source_deleted"]) == ("queued", 0)  # downloads again
    gone = db.create_job(conn, kind="video", status="failed", source_deleted=1,
                         upload_path=str(ctx[0].data_dir / "uploads" / "missing.mp4"))
    r = authed.post(f"/jobs/{gone}/retry")
    assert r.status_code == 409 and r.json()["detail"] == "The uploaded file was cleaned up — upload it again."
    assert db.get_job(conn, gone)["status"] == "failed"
    kept = ctx[0].data_dir / "uploads" / "kept.mp4"
    kept.write_bytes(b"v")
    still = db.create_job(conn, kind="video", status="failed", upload_path=str(kept))
    assert authed.post(f"/jobs/{still}/retry").status_code == 200
