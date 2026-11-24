import shutil
import time
from pathlib import Path

import pytest

from clipper import db
from clipper.captions import words_in_range
from clipper.config import Settings
from clipper.ingest import probe
from clipper.models import Transcript
from clipper.worker import Worker, job_dir

TOL = 0.01  # ASS times are centiseconds


def _secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def assert_no_early_words(ass_path: Path, words, delay: float = 0.1, pair_gap: float = 0.45) -> None:
    """Every Dialogue event shows its newest word no earlier than that word's start + delay.

    `words` are clip-time words. A chunk is 2 words: either one event for both (only legal when the
    second starts within pair_gap of the lead) or a lead-only event followed by a both-words event.
    """
    events = []
    for line in ass_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Dialogue:"):
            f = line.split(",", 9)
            events.append((_secs(f[1]), f[9].split()))
    assert events, "no Dialogue lines"
    i, pending = 0, False  # i: lead word of the next chunk; pending: lead shown alone, second still due
    for start, toks in events:
        if pending:
            assert len(toks) == 2, toks
            w = words[i + 1]
            assert start >= w.start + delay - TOL, f"{toks} shown at {start} before {w.start + delay}"
            i, pending = i + 2, False
            continue
        lead = words[i]
        assert start >= lead.start + delay - TOL, f"{toks} shown at {start} before {lead.start + delay}"
        if len(toks) == 2:
            assert words[i + 1].start - lead.start <= pair_gap + TOL, f"{toks} shown together but not spoken together"
            i += 2
        else:
            pending = i + 1 < len(words)
            i += 0 if pending else 1
    assert i == len(words), f"events cover {i} of {len(words)} words"


@pytest.mark.slow
def test_full_pipeline_smoke(speech_video, tmp_path):
    settings = Settings(data_dir=tmp_path, password_hash="x", session_secret="x", default_captions_on=True)
    db_path = tmp_path / "clipper.db"
    conn = db.connect(db_path)
    upload = tmp_path / "upload.mp4"
    shutil.copy2(speech_video, upload)  # accept_upload moves its input; the fixture is session-shared
    jid = db.create_job(conn, kind="video", status="queued", upload_path=str(upload), title="speech",
                        video_type="talk", length_preset="short", captions_on=1, caption_style="bold_white")
    worker = Worker(settings, db_path)
    worker.start()
    try:
        deadline = time.time() + 15 * 60
        while time.time() < deadline and db.get_job(conn, jid)["status"] in ("queued", "running"):
            time.sleep(2)
    finally:
        worker.stop()
        worker.join(timeout=60)
    job = db.get_job(conn, jid)
    assert job["status"] == "done", f"job ended {job['status']}: {job['error']}"
    clips = db.list_clips(conn, jid)
    assert len(clips) >= 1, "the moment picker returned no clips for the synthetic speech; not a vacuous pass"
    clip = clips[0]
    info = probe(Path(clip["file"]))
    assert (info.width, info.height) == (1080, 1920)
    ass = Path(clip["file"]).with_suffix(".ass")
    assert ass.is_file()
    words = words_in_range(Transcript.load(job_dir(settings, jid) / "transcript.json").words,
                           clip["start"], clip["end"])
    assert words
    assert_no_early_words(ass, words, settings.caption_delay)
