import io
import shutil
import subprocess

import pytest

from clipper import ingest
from clipper.ingest import (IngestError, accept_upload, check_disk, download,
                            map_ytdlp_error, probe, safe_filename)
from clipper.models import Canceled, MediaInfo


def test_probe_reads_tiny_video(tiny_video):
    info = probe(tiny_video)
    assert (info.width, info.height, info.has_audio) == (1280, 720, True) and 4.5 < info.duration < 5.5


def test_probe_rejects_non_video(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_text("nope")
    with pytest.raises(IngestError, match="isn't a video"):
        probe(f)


@pytest.mark.parametrize("title,expected", [("Ep 12: \"Pricing\" 🔥/why?", "Ep 12 Pricing why"), ("", "clip"), ("///", "clip")])
def test_safe_filename(title, expected):
    assert safe_filename(title) == expected


@pytest.mark.parametrize("title,expected", [(r"a\b{x}", "abx"), ("..", "clip"), ("CON", "clip"), ("con.txt", "clip"), ("hi.", "hi"), ("a/b", "ab")])
def test_safe_filename_hostile(title, expected):
    assert safe_filename(title) == expected


def test_safe_filename_truncates():
    assert len(safe_filename("a" * 200)) == 80


@pytest.mark.parametrize("stderr,msg", [
    ("ERROR: Private video", "private"),
    ("ERROR: Video unavailable", "removed"),
    ("ERROR: Unsupported URL: x", "isn't supported"),
    ("ERROR: not available in your country", "blocked in your country"),
    ("boom\nsomething odd", "Download failed: something odd"),
])
def test_map_ytdlp_error(stderr, msg):
    assert msg in map_ytdlp_error(stderr)


def test_check_disk_raises_when_too_little_space(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (100, 90, 10))
    with pytest.raises(IngestError):
        check_disk(tmp_path, 5)


def test_check_disk_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (100, 70, 30))
    check_disk(tmp_path, 5)


class FakePopen:
    def __init__(self, args, job_dir, lines, rc=0, write=True, **kw):
        self.args, self.rc, self.killed, self.done, self.pid = args, rc, False, False, 4242
        self.stdout = io.StringIO("".join(lines))
        if write:
            (job_dir / "dl.mp4").write_bytes(b"v")
            (job_dir / "dl.jpg").write_bytes(b"t")

    def terminate(self):
        self.killed = True

    kill = terminate

    def wait(self, timeout=None):
        self.done = True
        return self.rc

    def poll(self):
        return self.rc if (self.done or self.killed) else None


def _patch(monkeypatch, job_dir, lines=(), rc=0, write=True, size="NA", title="My Title"):
    calls, procs = [], []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=f"{title}\n{size}\n", stderr="")

    def popen(args, **kw):
        calls.append(args)
        p = FakePopen(args, job_dir, lines, rc, write, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(ingest, "probe", lambda p: MediaInfo(10.0, 1280, 720, True))
    return calls, procs


def test_download_passes_url_as_argument_not_shell(tmp_path, monkeypatch):
    url = "https://x/;rm"
    calls, _ = _patch(monkeypatch, tmp_path, ["[download]  50.0% of 1MiB\n", "[download] 100% of 1MiB\n"])
    seen = []
    info = download(url, tmp_path, seen.append, lambda: False)
    assert len(calls) >= 2
    assert all(isinstance(a, list) for a in calls)
    assert all(a.count(url) == 1 for a in calls) and url in calls[-1]
    assert info.path == tmp_path / "source.mp4" and info.path.exists()
    assert info.title == "My Title" and info.duration == 10.0
    assert info.thumbnail == tmp_path / "source.jpg" and info.thumbnail.exists()
    assert not (tmp_path / "dl.mp4").exists()
    assert seen and seen[0] == 0.5 and seen[-1] == 1.0


def test_download_skips_when_source_exists(tmp_path, monkeypatch):
    (tmp_path / "source.mp4").write_bytes(b"v")
    (tmp_path / "title.txt").write_text("Old Title", encoding="utf-8")
    calls, _ = _patch(monkeypatch, tmp_path)
    info = download("https://x/y", tmp_path, lambda f: None, lambda: False)
    assert calls == [] and info.title == "Old Title"


def test_download_cancel_kills_and_cleans(tmp_path, monkeypatch):
    _, procs = _patch(monkeypatch, tmp_path, ["[download] 10% of 1MiB\n"] * 3)
    with pytest.raises(Canceled):
        download("https://x/y", tmp_path, lambda f: None, lambda: True)
    assert procs[0].killed
    assert not (tmp_path / "source.mp4").exists() and not (tmp_path / "dl.mp4").exists()


def test_download_failure_maps_error(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path, ["ERROR: Private video\n"], rc=1, write=False)
    with pytest.raises(IngestError, match="private"):
        download("https://x/y", tmp_path, lambda f: None, lambda: False)


def test_download_checks_disk(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path, size="1000000")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (100, 90, 10))
    with pytest.raises(IngestError, match="space"):
        download("https://x/y", tmp_path, lambda f: None, lambda: False)


def test_accept_upload(tiny_video, tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    info = accept_upload(tiny_video, job)
    assert info.path == job / "source.mp4" and info.path.exists() and not tiny_video.exists()
    assert info.title == "tiny" and 4.5 < info.duration < 5.5


def test_accept_upload_rejects_non_video(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_text("nope")
    with pytest.raises(IngestError):
        accept_upload(f, tmp_path)


def test_download_requests_mp4_remux(tmp_path, monkeypatch):
    calls, _ = _patch(monkeypatch, tmp_path)
    download("https://x/y", tmp_path, lambda f: None, lambda: False)
    assert "--remux-video" in calls[-1] and calls[-1][calls[-1].index("--remux-video") + 1] == "mp4"


def test_download_rc0_without_file_gives_explicit_error(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path, ["[download] 100% of 1MiB\n"], rc=0, write=False)
    with pytest.raises(IngestError, match="no video file was produced"):
        download("https://x/y", tmp_path, lambda f: None, lambda: False)


def test_download_clears_stale_partials(tmp_path, monkeypatch):
    (tmp_path / "dl.mp4.part").write_bytes(b"stale")
    (tmp_path / "dl.f137.mp4").write_bytes(b"stale")
    _patch(monkeypatch, tmp_path, write=False)
    seen = {}
    orig = subprocess.Popen

    def popen(args, **kw):
        seen["left"] = sorted(f.name for f in tmp_path.glob("dl.*"))
        return orig(args, **kw)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(IngestError):
        download("https://x/y", tmp_path, lambda f: None, lambda: False)
    assert seen["left"] == []


def test_cancel_kills_process_tree(tmp_path, monkeypatch):
    calls, procs = _patch(monkeypatch, tmp_path, ["[download] 10% of 1MiB\n"])
    monkeypatch.setattr(ingest, "IS_WINDOWS", True)
    with pytest.raises(Canceled):
        download("https://x/y", tmp_path, lambda f: None, lambda: True)
    assert ["taskkill", "/F", "/T", "/PID", "4242"] in calls and procs[0].killed


def test_progress_exception_still_kills_process(tmp_path, monkeypatch):
    _, procs = _patch(monkeypatch, tmp_path, ["[download] 10% of 1MiB\n"])

    def boom(f):
        raise RuntimeError("ui gone")

    with pytest.raises(RuntimeError):
        download("https://x/y", tmp_path, boom, lambda: False)
    assert procs[0].killed


def test_generic_download_error_points_at_ytdlp_update():
    assert map_ytdlp_error("boom\nsomething odd").endswith(" If this keeps happening, update yt-dlp (see README).")
    assert "update yt-dlp" not in map_ytdlp_error("ERROR: Private video")


def test_download_takes_only_first_playlist_item(tmp_path, monkeypatch):
    calls, _ = _patch(monkeypatch, tmp_path)
    download("https://x/@channel", tmp_path, lambda f: None, lambda: False)
    assert len(calls) == 2  # metadata, then download
    for args in calls:
        assert args[args.index("-I") + 1] == "1"
