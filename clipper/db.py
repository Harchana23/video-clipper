"""SQLite job queue and clip storage, shared by the worker thread and the web app."""
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()  # one shared connection (check_same_thread=False): serialize every call

_JOB_COLS = {
    "kind": "TEXT", "status": "TEXT", "priority": "INT DEFAULT 0", "source_url": "TEXT",
    "upload_path": "TEXT", "params": "TEXT DEFAULT '{}'", "title": "TEXT", "video_type": "TEXT",
    "length_preset": "TEXT", "captions_on": "INT", "caption_style": "TEXT", "step": "INT DEFAULT 0",
    "step_progress": "REAL DEFAULT 0", "step_started_at": "REAL", "error": "TEXT",
    "cancel_requested": "INT DEFAULT 0", "clip_id": "INT", "created_at": "REAL",
    "finished_at": "REAL", "source_deleted": "INT DEFAULT 0", "duration": "REAL",
}
_CLIP_COLS = {
    "job_id": "INT", "start": "REAL", "end": "REAL", "title": "TEXT", "score": "INT",
    "captions_on": "INT", "caption_style": "TEXT", "corrections": "TEXT DEFAULT '{}'",
    "file": "TEXT", "poster": "TEXT", "version": "INT DEFAULT 1", "created_at": "REAL",
}


def _ddl(table: str, cols: dict) -> str:
    body = ", ".join(f'"{n}" {t}' for n, t in cols.items())
    return f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, {body})"


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: autocommit, so we control transactions explicitly (BEGIN IMMEDIATE)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_ddl("jobs", _JOB_COLS))
    conn.execute(_ddl("clips", _CLIP_COLS))
    return conn


def _check(fields: dict, cols: dict) -> None:
    bad = set(fields) - set(cols)
    if bad:
        raise ValueError(f"unknown columns: {sorted(bad)}")


def _insert(conn, table, cols, fields) -> int:
    _check(fields, cols)
    fields = {"created_at": time.time(), **fields}
    names = ", ".join(f'"{k}"' for k in fields)
    marks = ", ".join("?" * len(fields))
    with _lock:
        return conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(fields.values())).lastrowid


def _update(conn, table, cols, row_id, fields) -> None:
    _check(fields, cols)
    if not fields:
        return
    sets = ", ".join(f'"{k}" = ?' for k in fields)
    with _lock:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*fields.values(), row_id])


def create_job(conn, **fields) -> int:
    return _insert(conn, "jobs", _JOB_COLS, fields)


def get_job(conn, job_id):
    with _lock:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def update_job(conn, job_id, **fields) -> None:
    _update(conn, "jobs", _JOB_COLS, job_id, fields)


def _claim(conn, where: str):
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                f"SELECT * FROM jobs WHERE status = 'queued'{where} ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row:
                conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (row["id"],))
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return row
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def next_job(conn):
    return _claim(conn, "")


def next_rerender(conn):
    return _claim(conn, " AND kind = 'rerender'")


def requeue_running(conn) -> int:
    with _lock:
        return conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'").rowcount


def list_jobs(conn):
    with _lock:
        active = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY priority DESC, created_at ASC, id ASC"
        ).fetchall()
        finished = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('done','failed','canceled') "
            "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC"
        ).fetchall()
    return active, finished


def add_clip(conn, **fields) -> int:
    return _insert(conn, "clips", _CLIP_COLS, fields)


def get_clip(conn, clip_id):
    with _lock:
        return conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()


def list_clips(conn, job_id):
    with _lock:
        return conn.execute("SELECT * FROM clips WHERE job_id = ? ORDER BY score DESC, id ASC", (job_id,)).fetchall()


def update_clip(conn, clip_id, **fields) -> None:
    _update(conn, "clips", _CLIP_COLS, clip_id, fields)


def delete_clip(conn, clip_id) -> None:
    with _lock:
        conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
