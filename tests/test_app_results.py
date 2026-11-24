import json
import time
import zipfile

import pytest

from clipper import db
from clipper.models import Transcript, Word
from clipper.worker import job_dir
from tests.test_app_home import authed, client, conn, ctx  # noqa: F401  (fixtures)


def make_job(conn, s, **kw):
    jid = db.create_job(conn, kind="video", status="done", title="Pod: ep/1", duration=100.0,
                        finished_at=time.time(), **kw)
    d = job_dir(s, jid)
    (d / "clips").mkdir(parents=True)
    Transcript("en", [Word(11.0, 11.5, "hello"), Word(12.0, 12.5, "world"), Word(50.0, 50.5, "far")], []).save(
        d / "transcript.json")
    return jid


def make_clip(conn, s, jid, title="First", score=90, start=10.0, end=40.0, data=b"x" * 500):
    d = job_dir(s, jid)
    cid = db.add_clip(conn, job_id=jid, start=start, end=end, title=title, score=score, captions_on=0,
                      caption_style="bold_white")
    f = d / "clips" / f"{cid}_v1.mp4"
    f.write_bytes(data)
    f.with_suffix(".jpg").write_bytes(b"jpg")
    db.update_clip(conn, cid, file=str(f), poster=str(f.with_suffix(".jpg")))
    return cid


@pytest.fixture
def clip_on_disk(ctx, conn):
    jid = make_job(conn, ctx[0])
    return make_clip(conn, ctx[0], jid, title="Nobody tells you: this?")


def test_clip_media_supports_range(authed, clip_on_disk):
    r = authed.get(f"/media/clips/{clip_on_disk}.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


def test_media_requires_session(client, clip_on_disk):
    assert client.get(f"/media/clips/{clip_on_disk}.mp4").status_code == 401
    assert client.get(f"/media/posters/{clip_on_disk}.jpg").status_code == 401
    assert client.post(f"/clips/{clip_on_disk}/delete").status_code == 401


def test_poster_and_missing_clip(authed, clip_on_disk):
    assert authed.get(f"/media/posters/{clip_on_disk}.jpg").content == b"jpg"
    assert authed.get("/media/clips/999.mp4").status_code == 404
    assert authed.get("/media/clips/abc.mp4").status_code == 422


def test_unrendered_clip_is_never_served_or_counted(authed, ctx, conn):
    jid = make_job(conn, ctx[0])
    cid = db.add_clip(conn, job_id=jid, start=1.0, end=9.0, title="Pending", score=5)
    assert authed.get(f"/media/clips/{cid}.mp4").status_code == 404
    assert authed.get(f"/clips/{cid}/download").status_code == 404
    page = authed.get(f"/jobs/{jid}")
    assert page.status_code == 200 and "Pending" not in page.text
    assert authed.get(f"/jobs/{jid}/zip").status_code == 404  # nothing to zip


def test_path_outside_job_dir_is_refused(authed, ctx, conn, tmp_path):
    jid = make_job(conn, ctx[0])
    cid = make_clip(conn, ctx[0], jid)
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"s")
    db.update_clip(conn, cid, file=str(outside))
    assert authed.get(f"/media/clips/{cid}.mp4").status_code == 404


def test_download_uses_safe_filename(authed, clip_on_disk):
    r = authed.get(f"/clips/{clip_on_disk}/download")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="Nobody tells you this.mp4"'


def test_zip_contains_all_clips(authed, ctx, conn):
    s = ctx[0]
    jid = make_job(conn, s)
    make_clip(conn, s, jid, title="Top", score=90, data=b"aaa")
    make_clip(conn, s, jid, title="Top", score=80, data=b"bbbb")
    r = authed.get(f"/jobs/{jid}/zip")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    zp = job_dir(s, jid) / "dl.zip"
    zp.write_bytes(r.content)
    with zipfile.ZipFile(zp) as z:
        assert z.namelist() == ["01_Top.mp4", "02_Top.mp4"]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
        assert z.read("01_Top.mp4") == b"aaa" and z.read("02_Top.mp4") == b"bbbb"
    zp.unlink()
    assert list(job_dir(s, jid).glob("*.zip*")) == []  # the temp zip was removed after streaming


def test_results_page_lists_clips(authed, ctx, conn):
    s = ctx[0]
    jid = make_job(conn, s)
    make_clip(conn, s, jid, title="<b>Hi</b>")
    html = authed.get(f"/jobs/{jid}").text
    assert "Download all (1)" in html and "&lt;b&gt;Hi&lt;/b&gt;" in html and "<b>Hi</b>" not in html
    assert authed.get("/jobs/999").status_code == 404
    db.update_job(conn, jid, source_deleted=1)
    assert "Source cleaned up" in authed.get(f"/jobs/{jid}").text


def test_delete_removes_files_and_row_by_exact_prefix(authed, ctx, conn):
    s = ctx[0]
    jid = make_job(conn, s)
    a = make_clip(conn, s, jid)
    other = db.add_clip(conn, job_id=jid, start=1.0, end=9.0, title="o", score=1)
    clips = job_dir(s, jid) / "clips"
    # "<a>1_..." stands in for a different clip whose id merely starts with the same digits
    for name in (f"{a}_v2.mp4", f"{a}_plan.json", f"{a}_v1.ass", f"{a}1_v1.mp4", f"{a}2_plan.json"):
        (clips / name).write_bytes(b"z")
    r = authed.post(f"/clips/{a}/delete")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert db.get_clip(conn, a) is None and db.get_clip(conn, other) is not None
    assert sorted(p.name for p in clips.iterdir()) == [f"{a}1_v1.mp4", f"{a}2_plan.json"]


def test_delete_requires_done_job(authed, conn, clip_on_disk):
    jid = db.get_clip(conn, clip_on_disk)["job_id"]
    db.update_job(conn, jid, status="running")
    r = authed.post(f"/clips/{clip_on_disk}/delete")
    assert r.status_code == 409 and "still processing" in r.text
    assert db.get_clip(conn, clip_on_disk) is not None
    assert authed.post("/clips/999/delete").status_code == 404


def test_delete_works_after_source_cleanup(authed, conn, clip_on_disk):
    db.update_job(conn, db.get_clip(conn, clip_on_disk)["job_id"], source_deleted=1)
    assert authed.post(f"/clips/{clip_on_disk}/delete").status_code == 200


def test_trim_validates_and_queues_rerender(authed, conn, clip_on_disk):
    c = clip_on_disk
    for body in ({"start": 10, "end": 5}, {"start": -1, "end": 10}, {"start": 1, "end": 3.5},
                 {"start": 1, "end": 101}, {"start": "a", "end": 9}, {"start": 1}, {"start": True, "end": 9},
                 {"start": "NaN", "end": 9}):
        assert authed.post(f"/clips/{c}/trim", json=body).status_code == 400, body
    for raw in (b"nope", b'{"start": NaN, "end": 9}', b"[1]"):
        assert authed.post(f"/clips/{c}/trim", content=raw).status_code == 400, raw
    before = dict(db.get_clip(conn, c))
    r = authed.post(f"/clips/{c}/trim", json={"start": 1, "end": 4})
    assert r.status_code == 200 and r.json()["ok"] is True
    job = db.get_job(conn, r.json()["job_id"])
    assert (job["kind"], job["status"], job["priority"], job["clip_id"]) == ("rerender", "queued", 10, c)
    assert json.loads(job["params"]) == {"start": 1.0, "end": 4.0}
    assert dict(db.get_clip(conn, c)) == before  # the route never touches the clip row
    r2 = authed.post(f"/clips/{c}/trim", json={"start": 1, "end": 5})
    assert r2.status_code == 409 and "already being re-rendered" in r2.text


def test_edit_blocked_when_processing_or_cleaned(authed, conn, clip_on_disk):
    c, jid = clip_on_disk, db.get_clip(conn, clip_on_disk)["job_id"]
    cap = {"captions_on": True, "caption_style": "bold_white", "corrections": {}}
    db.update_job(conn, jid, source_deleted=1)
    for url, body in ((f"/clips/{c}/trim", {"start": 1, "end": 9}), (f"/clips/{c}/captions", cap)):
        r = authed.post(url, json=body)
        assert r.status_code == 409 and "source video was cleaned up, so this clip can't be changed" in r.text
    db.update_job(conn, jid, source_deleted=0, status="running")
    assert "still processing" in authed.post(f"/clips/{c}/trim", json={"start": 1, "end": 9}).text
    assert authed.post("/clips/999/trim", json={"start": 1, "end": 9}).status_code == 404


def test_caption_edit_validation(authed, conn, clip_on_disk):
    c = clip_on_disk  # clip 10..40 holds 2 words
    ok = {"captions_on": True, "caption_style": "yellow_pop", "corrections": {"0": "  Hi\n  there "}}
    bad = [{**ok, "caption_style": "neon"}, {**ok, "captions_on": "yes"}, {**ok, "corrections": {"2": "x"}},
           {**ok, "corrections": {"-1": "x"}}, {**ok, "corrections": {"a": "x"}}, {**ok, "corrections": {"0": 5}},
           {**ok, "corrections": {"0": "x" * 41}}, {**ok, "corrections": [1]}, {"captions_on": True}]
    for body in bad:
        assert authed.post(f"/clips/{c}/captions", json=body).status_code == 400, body
    assert not [j for j in db.list_jobs(conn)[0] if j["kind"] == "rerender"]
    r = authed.post(f"/clips/{c}/captions", json={**ok, "corrections": {"0": "  Hi\n  there ", "1": ""}})
    assert r.status_code == 200
    p = json.loads(db.get_job(conn, r.json()["job_id"])["params"])
    assert p == {"captions_on": True, "caption_style": "yellow_pop", "corrections": {"0": "Hi there", "1": ""}}


def test_words_endpoint(authed, conn, clip_on_disk):
    c = clip_on_disk
    d = authed.get(f"/api/clips/{c}/words").json()
    assert d["captions_on"] is False and d["caption_style"] == "bold_white"
    assert d["words"] == [{"i": 0, "start": 1.0, "end": 1.5, "orig": "hello", "text": "hello"},
                          {"i": 1, "start": 2.0, "end": 2.5, "orig": "world", "text": "world"}]
    db.update_clip(conn, c, captions_on=1, corrections=json.dumps({"1": "WORLD!"}))
    d = authed.get(f"/api/clips/{c}/words").json()
    assert d["captions_on"] is True and d["words"][1]["text"] == "WORLD!"
    assert authed.get("/api/clips/999/words").status_code == 404


def test_words_missing_transcript_is_404(authed, ctx, conn, clip_on_disk):
    (job_dir(ctx[0], db.get_clip(conn, clip_on_disk)["job_id"]) / "transcript.json").unlink()
    assert authed.get(f"/api/clips/{clip_on_disk}/words").status_code == 404


def test_api_jobs_lists_rerendering_clip_ids(authed, conn, clip_on_disk):
    assert authed.get("/api/jobs").json()["rerendering"] == []
    j = db.create_job(conn, kind="rerender", status="running", clip_id=clip_on_disk, priority=10)
    d = authed.get("/api/jobs").json()
    assert d["rerendering"] == [clip_on_disk] and all(x["id"] != j for x in d["active"] + d["finished"])
    db.update_job(conn, j, status="done")
    assert authed.get("/api/jobs").json()["rerendering"] == []
