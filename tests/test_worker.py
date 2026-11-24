import json
import shutil
import time
from pathlib import Path

import pytest

from clipper import db
from clipper.config import Settings
from clipper.ingest import IngestError
from clipper.models import Canceled, CropPlan, MediaInfo, Moment, SourceInfo, Transcript, Word
from clipper.render import RenderError
from clipper.worker import Worker, cleanup_sources, job_dir

DAY = 86400
MOMENTS = [Moment(10.0, 40.0, "First", 90), Moment(50.0, 80.0, "Second", 70)]
GONE = "The source video was cleaned up, so this clip can't be changed."


class FakeSteps:
    """Records calls and writes dummy files; no GPU, Ollama or ffmpeg."""

    def __init__(self, moments=MOMENTS):
        self.moments = list(moments)
        self.calls = []
        self.on_transcribe = None  # hook(progress, should_stop) run inside transcribe
        self.fail = {}  # step name -> exception to raise

    def _enter(self, name, *info):
        self.calls.append((name, *info))
        if name in self.fail:
            raise self.fail[name]

    def download(self, url, d, progress, should_stop):
        self._enter("download", url)
        (d / "source.mp4").write_bytes(b"v")
        progress(1.0)
        return SourceInfo(d / "source.mp4", "Downloaded title", 120.0, None)

    def accept_upload(self, tmp_file, d):
        self._enter("accept_upload", Path(tmp_file))
        dest = d / "source.mp4"
        shutil.move(str(tmp_file), dest)
        return SourceInfo(dest, "upload", 120.0, None)

    def probe(self, path):
        return MediaInfo(120.0, 1280, 720, True)

    def transcribe(self, source, d, model_name, compute_type, progress, should_stop):
        self._enter("transcribe")
        if self.on_transcribe:
            self.on_transcribe(progress, should_stop)
        progress(0.5)
        progress(1.0)
        t = Transcript("en", [Word(11.0, 11.5, "hello"), Word(12.0, 12.5, "world")], [])
        t.save(d / "transcript.json")
        return t

    def find_moments(self, source, transcript, video_type, preset, settings, d, progress, should_stop):
        self._enter("find_moments", video_type, preset)
        progress(1.0)
        return list(self.moments)

    def plan_crop(self, source, start, end, video_type, info):
        self._enter("plan_crop", start, end)
        return CropPlan("fit", info.width, info.height, [])

    def render_clip(self, source, out_path, start, end, plan, words, style, delay, corrections=None):
        self._enter("render", Path(out_path).name, start, end, words, style, corrections)
        assert not Path(out_path).exists(), "must never render over an existing file"
        Path(out_path).write_bytes(b"clip")
        Path(out_path).with_suffix(".jpg").write_bytes(b"jpg")
        if words is not None:
            Path(out_path).with_suffix(".ass").write_text("ass")


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", password_hash="x", session_secret="y")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def make_worker(settings, tmp_path, fake):
    return Worker(settings, tmp_path / "t.db", steps=fake)


def video_job(conn, **kw):
    fields = dict(kind="video", status="queued", source_url="https://example.com/v", video_type="talk",
                  length_preset="auto", captions_on=0, caption_style="bold_white")
    return db.create_job(conn, **{**fields, **kw})


def names(fake):
    return [c[0] for c in fake.calls]


def wait_status(conn, job_id, statuses, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.get_job(conn, job_id)
        if row["status"] in statuses:
            return row
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} stuck at {db.get_job(conn, job_id)['status']}")


def test_job_dir(settings):
    assert job_dir(settings, 7) == settings.data_dir / "jobs" / "7"


def test_worker_runs_full_video_job(settings, tmp_path, conn):
    fake = FakeSteps()
    jid = video_job(conn)
    w = make_worker(settings, tmp_path, fake)
    w.start()
    try:
        job = wait_status(conn, jid, {"done", "failed"})
    finally:
        w.stop()
        w.join(5)
    assert not w.is_alive()
    assert job["status"] == "done" and job["step"] == 4 and job["error"] is None
    assert job["title"] == "Downloaded title" and job["duration"] == 120.0 and job["finished_at"]
    clips = sorted(db.list_clips(conn, jid), key=lambda c: c["start"])
    assert [(c["title"], c["score"], c["start"], c["end"]) for c in clips] == [
        ("First", 90, 10.0, 40.0), ("Second", 70, 50.0, 80.0)]
    for c in clips:
        f = job_dir(settings, jid) / "clips" / f"{c['id']}_v1.mp4"
        assert c["file"] == str(f) and f.exists()
        assert c["poster"] == str(f.with_suffix(".jpg")) and c["captions_on"] == 0
    renders = [c for c in fake.calls if c[0] == "render"]
    assert len(renders) == 2 and all(r[4] is None for r in renders)  # captions off -> words=None
    assert names(fake)[:3] == ["download", "transcribe", "find_moments"]


def test_captions_on_passes_source_words(settings, tmp_path, conn):
    fake = FakeSteps(MOMENTS[:1])
    jid = video_job(conn, captions_on=1, caption_style="yellow_pop")
    assert make_worker(settings, tmp_path, fake).run_once()
    (render,) = [c for c in fake.calls if c[0] == "render"]
    assert [w.text for w in render[4]] == ["hello", "world"] and render[5] == "yellow_pop"
    (clip,) = db.list_clips(conn, jid)
    assert clip["captions_on"] == 1 and clip["caption_style"] == "yellow_pop"


def test_upload_job_accepts_upload(settings, tmp_path, conn):
    up = tmp_path / "abc.part"
    up.write_bytes(b"v")
    fake = FakeSteps(MOMENTS[:1])
    jid = video_job(conn, source_url=None, upload_path=str(up), title="My upload")
    assert make_worker(settings, tmp_path, fake).run_once()
    assert ("accept_upload", up) in fake.calls and "download" not in names(fake)
    job = db.get_job(conn, jid)
    assert job["status"] == "done" and job["title"] == "My upload"
    assert (job_dir(settings, jid) / "source.mp4").exists() and not up.exists()


def test_worker_resumes_after_restart(settings, tmp_path, conn):
    jid = video_job(conn, status="running", step=4)
    d = job_dir(settings, jid)
    (d / "clips").mkdir(parents=True)
    (d / "source.mp4").write_bytes(b"v")
    (d / "source.jpg").write_bytes(b"thumb")
    Transcript("en", [], []).save(d / "transcript.json")
    first = db.add_clip(conn, job_id=jid, start=10.0, end=40.0, title="First", score=90, captions_on=0,
                        caption_style="bold_white")
    (d / "clips" / f"{first}_v1.mp4").write_bytes(b"clip")
    fake = FakeSteps()
    w = make_worker(settings, tmp_path, fake)
    w.start()
    try:
        job = wait_status(conn, jid, {"done", "failed"})
    finally:
        w.stop()
        w.join(5)
    assert job["status"] == "done", job["error"]
    assert "download" not in names(fake) and "transcribe" not in names(fake)
    renders = [c for c in fake.calls if c[0] == "render"]
    assert len(renders) == 1 and renders[0][2:4] == (50.0, 80.0)
    assert len(db.list_clips(conn, jid)) == 2


def test_worker_cancel_marks_canceled(settings, tmp_path, conn):
    fake = FakeSteps()
    jid = video_job(conn)

    def cancel_midway(progress, should_stop):
        db.update_job(conn, jid, cancel_requested=1)
        deadline = time.time() + 3
        while time.time() < deadline:
            if should_stop():
                raise Canceled()
            time.sleep(0.05)
        raise AssertionError("should_stop never saw the cancel")

    fake.on_transcribe = cancel_midway
    assert make_worker(settings, tmp_path, fake).run_once()
    job = db.get_job(conn, jid)
    assert job["status"] == "canceled" and job["finished_at"]
    assert "find_moments" not in names(fake) and db.list_clips(conn, jid) == []


def test_cancel_before_start_skips_all_steps(settings, tmp_path, conn):
    fake = FakeSteps()
    jid = video_job(conn, cancel_requested=1)
    assert make_worker(settings, tmp_path, fake).run_once()
    assert db.get_job(conn, jid)["status"] == "canceled" and fake.calls == []


def test_stop_requeues_job_after_current_step(settings, tmp_path, conn):
    fake = FakeSteps()
    jid = video_job(conn)
    w = make_worker(settings, tmp_path, fake)
    fake.on_transcribe = lambda progress, should_stop: w.stop()
    assert w.run_once()
    job = db.get_job(conn, jid)
    assert job["status"] == "queued" and job["step"] == 2 and "find_moments" not in names(fake)


def test_default_steps_are_the_real_pipeline():
    from clipper import ingest, render
    from clipper.worker import Steps
    s = Steps()
    assert s.download is ingest.download and s.render_clip is render.render_clip


def done_job_with_clip(conn, settings, **job_kw):
    jid = video_job(conn, status="done", finished_at=time.time(), **job_kw)
    d = job_dir(settings, jid)
    (d / "clips").mkdir(parents=True)
    (d / "source.mp4").write_bytes(b"v")
    Transcript("en", [Word(11.0, 11.5, "hello"), Word(12.0, 12.5, "world")], []).save(d / "transcript.json")
    cid = db.add_clip(conn, job_id=jid, start=10.0, end=40.0, title="First", score=90, captions_on=0,
                      caption_style="bold_white")
    old = d / "clips" / f"{cid}_v1.mp4"
    old.write_bytes(b"old")
    old.with_suffix(".jpg").write_bytes(b"old")
    db.update_clip(conn, cid, file=str(old), poster=str(old.with_suffix(".jpg")))
    return jid, cid, old


def test_rerender_runs_between_chunks(settings, tmp_path, conn):
    _, cid, _ = done_job_with_clip(conn, settings)
    fake = FakeSteps(MOMENTS[:1])
    jid = video_job(conn)
    rid = []

    def queue_rerender(progress, should_stop):
        rid.append(db.create_job(conn, kind="rerender", status="queued", priority=10, clip_id=cid,
                                 params=json.dumps({"start": 12.0})))

    fake.on_transcribe = queue_rerender
    assert make_worker(settings, tmp_path, fake).run_once()
    assert db.get_job(conn, jid)["status"] == "done"
    assert db.get_job(conn, rid[0])["status"] == "done"
    seq = names(fake)
    assert seq.index("render") < seq.index("find_moments")  # the re-render ran inside step 2
    assert db.get_clip(conn, cid)["start"] == 12.0


def test_rerender_applies_params_to_new_version(settings, tmp_path, conn):
    jid, cid, old = done_job_with_clip(conn, settings)
    params = {"start": 11.0, "end": 30.0, "captions_on": True, "caption_style": "yellow_pop",
              "corrections": {"1": "WORLD!"}}
    rid = db.create_job(conn, kind="rerender", status="queued", priority=10, clip_id=cid, params=json.dumps(params))
    fake = FakeSteps()
    assert make_worker(settings, tmp_path, fake).run_once()
    assert db.get_job(conn, rid)["status"] == "done"
    (render,) = [c for c in fake.calls if c[0] == "render"]
    assert render[1] == f"{cid}_v2.mp4" and render[2:4] == (11.0, 30.0)
    assert [w.text for w in render[4]] == ["hello", "world"] and render[5] == "yellow_pop"
    assert render[6] == {1: "WORLD!"}
    assert ("plan_crop", 11.0, 30.0) in fake.calls  # range changed -> re-planned
    clip = db.get_clip(conn, cid)
    new = old.with_name(f"{cid}_v2.mp4")
    assert (clip["start"], clip["end"], clip["captions_on"], clip["caption_style"], clip["version"]) == (
        11.0, 30.0, 1, "yellow_pop", 2)
    assert json.loads(clip["corrections"]) == {"1": "WORLD!"}
    assert clip["file"] == str(new) and clip["poster"] == str(new.with_suffix(".jpg")) and new.exists()
    assert not old.exists() and not old.with_suffix(".jpg").exists()


def test_rerender_reuses_crop_plan_when_range_unchanged(settings, tmp_path, conn):
    _, cid, _ = done_job_with_clip(conn, settings)
    fake = FakeSteps()
    w = make_worker(settings, tmp_path, fake)
    for style in ("yellow_pop", "bold_white"):
        db.create_job(conn, kind="rerender", status="queued", clip_id=cid,
                      params=json.dumps({"caption_style": style}))
        assert w.run_once()
    assert len([c for c in fake.calls if c[0] == "plan_crop"]) == 1
    assert db.get_clip(conn, cid)["version"] == 3


def test_rerender_failure_keeps_clip_and_files(settings, tmp_path, conn):
    _, cid, old = done_job_with_clip(conn, settings)
    before = dict(db.get_clip(conn, cid))
    rid = db.create_job(conn, kind="rerender", status="queued", clip_id=cid, params=json.dumps({"start": 12.0}))
    fake = FakeSteps()
    fake.fail["render"] = RenderError("ffmpeg exploded")
    assert make_worker(settings, tmp_path, fake).run_once()
    job = db.get_job(conn, rid)
    assert job["status"] == "failed" and job["error"] == "ffmpeg exploded"
    assert dict(db.get_clip(conn, cid)) == before and old.exists() and old.with_suffix(".jpg").exists()


def test_rerender_fails_cleanly_after_source_cleanup(settings, tmp_path, conn):
    jid, cid, old = done_job_with_clip(conn, settings)
    (job_dir(settings, jid) / "source.mp4").unlink()
    db.update_job(conn, jid, source_deleted=1)
    before = dict(db.get_clip(conn, cid))
    rid = db.create_job(conn, kind="rerender", status="queued", clip_id=cid, params=json.dumps({"start": 12.0}))
    fake = FakeSteps()
    assert make_worker(settings, tmp_path, fake).run_once()
    job = db.get_job(conn, rid)
    assert job["status"] == "failed" and job["error"] == GONE
    assert fake.calls == [] and dict(db.get_clip(conn, cid)) == before and old.exists()


def test_cleanup_sources_respects_retention(settings, conn):
    now = time.time()
    old = video_job(conn, status="done", finished_at=now - 15 * DAY)
    recent = video_job(conn, status="done", finished_at=now - 13 * DAY)
    for jid in (old, recent):
        d = job_dir(settings, jid)
        d.mkdir(parents=True)
        (d / "source.mp4").write_bytes(b"v")
    assert cleanup_sources(conn, settings, now) == 1
    assert not (job_dir(settings, old) / "source.mp4").exists()
    assert db.get_job(conn, old)["source_deleted"] == 1
    assert (job_dir(settings, recent) / "source.mp4").exists()
    assert db.get_job(conn, recent)["source_deleted"] == 0
    assert cleanup_sources(conn, settings, now) == 0  # already cleaned


def test_failed_job_records_plain_error(settings, tmp_path, conn):
    fake = FakeSteps()
    fake.fail["download"] = IngestError("This video is private.")
    jid = video_job(conn)
    assert make_worker(settings, tmp_path, fake).run_once()
    job = db.get_job(conn, jid)
    assert job["status"] == "failed" and job["error"] == "This video is private." and job["finished_at"]


def test_unexpected_error_is_logged_and_loop_survives(settings, tmp_path, conn):
    fake = FakeSteps(MOMENTS[:1])
    fake.fail["transcribe"] = ValueError("boom")
    bad = video_job(conn)
    good = video_job(conn, source_url="https://example.com/ok")
    w = make_worker(settings, tmp_path, fake)
    assert w.run_once()
    job = db.get_job(conn, bad)
    assert job["status"] == "failed" and job["error"] == "Something went wrong: ValueError"
    log = (settings.data_dir / "logs" / "worker.log").read_text(encoding="utf-8")
    assert "ValueError: boom" in log and "Traceback" in log
    del fake.fail["transcribe"]
    assert w.run_once()
    assert db.get_job(conn, good)["status"] == "done"
    assert w.run_once() is False  # queue empty
