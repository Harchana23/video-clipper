import threading

import pytest

from clipper.db import (
    add_clip, connect, create_job, delete_clip, get_clip, get_job, list_clips,
    list_jobs, next_job, next_rerender, requeue_running, update_clip, update_job,
)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def test_next_job_prefers_rerender_priority(conn):
    create_job(conn, kind="video", status="queued", created_at=1)
    b = create_job(conn, kind="rerender", status="queued", priority=10, created_at=2)
    assert next_job(conn)["id"] == b and get_job(conn, b)["status"] == "running"


def test_next_job_fifo_and_empty(conn):
    assert next_job(conn) is None
    a = create_job(conn, kind="video", status="queued", created_at=1)
    b = create_job(conn, kind="video", status="queued", created_at=2)
    assert next_job(conn)["id"] == a
    assert next_job(conn)["id"] == b
    assert next_job(conn) is None


def test_next_rerender_skips_video(conn):
    create_job(conn, kind="video", status="queued", created_at=1)
    assert next_rerender(conn) is None
    r = create_job(conn, kind="rerender", status="queued", created_at=2)
    assert next_rerender(conn)["id"] == r


def test_requeue_running(conn):
    a = create_job(conn, kind="video", status="running", created_at=1)
    create_job(conn, kind="video", status="done", created_at=2)
    assert requeue_running(conn) == 1
    assert get_job(conn, a)["status"] == "queued"


def test_update_job_rejects_unknown_column(conn):
    j = create_job(conn, kind="video", status="queued", created_at=1)
    with pytest.raises(ValueError):
        update_job(conn, j, **{"status = 'x'; --": 1})
    with pytest.raises(ValueError):
        create_job(conn, bogus=1)
    with pytest.raises(ValueError):
        add_clip(conn, bogus=1)
    with pytest.raises(ValueError):
        update_clip(conn, 1, bogus=1)


def test_defaults_and_params(conn):
    j = create_job(conn, kind="rerender", status="queued", upload_path="u.mp4", params='{"a": 1}')
    row = get_job(conn, j)
    assert row["created_at"] > 0 and row["params"] == '{"a": 1}' and row["upload_path"] == "u.mp4"
    assert get_job(conn, create_job(conn, kind="video", status="queued"))["params"] == "{}"
    assert get_job(conn, 999) is None


def test_list_jobs_split(conn):
    q = create_job(conn, kind="video", status="queued", created_at=1)
    r = create_job(conn, kind="video", status="running", created_at=2)
    d1 = create_job(conn, kind="video", status="done", created_at=3, finished_at=10)
    d2 = create_job(conn, kind="video", status="failed", created_at=4, finished_at=20)
    active, finished = list_jobs(conn)
    assert {x["id"] for x in active} == {q, r}
    assert [x["id"] for x in finished] == [d2, d1]


def test_list_clips_sorted_by_score(conn):
    j = create_job(conn, kind="video", status="done", created_at=1)
    a = add_clip(conn, job_id=j, start=0, end=5, score=40)
    b = add_clip(conn, job_id=j, start=5, end=9, score=90)
    assert [c["id"] for c in list_clips(conn, j)] == [b, a]
    assert get_clip(conn, a)["corrections"] == "{}" and get_clip(conn, a)["version"] == 1
    update_clip(conn, a, score=99, version=2)
    assert get_clip(conn, a)["score"] == 99
    delete_clip(conn, a)
    assert get_clip(conn, a) is None


def test_concurrent_next_job_claims_once(conn):
    create_job(conn, kind="video", status="queued", created_at=1)
    results, barrier = [], threading.Barrier(2)

    def claim():
        barrier.wait()
        results.append(next_job(conn))

    ts = [threading.Thread(target=claim) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r is None for r in results) == [False, True]
