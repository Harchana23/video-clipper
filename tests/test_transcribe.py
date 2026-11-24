from types import SimpleNamespace as NS

import pytest

from clipper import transcribe as tr
from clipper.models import Canceled, Segment, Transcript, Word
from clipper.transcribe import merge_chunks, transcribe


def test_merge_chunks_offsets_and_sorts():
    a = Transcript("en", [Word(1, 2, "a")], [Segment(1, 2, "a")])
    b = Transcript("en", [Word(0.5, 1, "b")], [Segment(0.5, 1, "b")])
    m = merge_chunks([(600.0, b), (0.0, a)])
    assert [w.start for w in m.words] == [1, 600.5] and m.language == "en"
    assert [s.start for s in m.segments] == [1, 600.5]


class FakeModel:
    def __init__(self, calls, segments):
        self.calls, self.segments = calls, segments

    def transcribe(self, wav, **kw):
        assert kw == {"word_timestamps": True, "vad_filter": True}
        self.calls.append(wav)
        return iter(self.segments), NS(language="en")


def _patch(monkeypatch, segments=()):
    calls, loads = [], []

    def load(name, compute_type):
        loads.append((name, compute_type))
        return FakeModel(calls, segments)

    monkeypatch.setattr(tr, "_load_model", load)
    return calls, loads


def test_transcribe_resumes_from_saved_chunks(tmp_path, monkeypatch, tiny_video):
    calls, loads = _patch(monkeypatch)
    chunks = tmp_path / "transcript_chunks"
    chunks.mkdir()
    Transcript("en", [Word(1, 2, "hi")], [Segment(1, 2, "hi")]).save(chunks / "0000.json")
    t = transcribe(tiny_video, tmp_path, "x", "int8", lambda f: None, lambda: False)
    assert calls == [] and loads == []
    assert [w.text for w in t.words] == ["hi"]
    assert (tmp_path / "transcript.json").exists()


def test_transcribe_returns_saved_transcript(tmp_path, monkeypatch, tiny_video):
    calls, _ = _patch(monkeypatch)
    Transcript("fr", [], []).save(tmp_path / "transcript.json")
    assert transcribe(tiny_video, tmp_path, "x", "int8", lambda f: None, lambda: False).language == "fr"
    assert calls == []


def test_transcribe_runs_model_and_reports_progress(tmp_path, monkeypatch, tiny_video):
    seg = NS(start=0.1, end=0.9, text=" hello there", words=[
        NS(start=0.1, end=0.4, word=" hello"), NS(start=0.5, end=0.9, word=" there")])
    calls, loads = _patch(monkeypatch, [seg])
    prog = []
    t = transcribe(tiny_video, tmp_path, "m", "int8", prog.append, lambda: False)
    assert len(calls) == 1 and loads == [("m", "int8")]
    assert [w.text for w in t.words] == ["hello", "there"] and t.segments[0].text == "hello there"
    assert prog == [1.0]
    assert (tmp_path / "transcript_chunks" / "0000.json").exists()
    assert not list((tmp_path / "transcript_chunks").glob("*.wav"))


def test_transcribe_no_speech_is_empty_not_error(tmp_path, monkeypatch, tiny_video):
    _patch(monkeypatch, [])
    t = transcribe(tiny_video, tmp_path, "m", "int8", lambda f: None, lambda: False)
    assert t.words == []


def test_transcribe_raises_canceled(tmp_path, monkeypatch, tiny_video):
    calls, loads = _patch(monkeypatch)
    with pytest.raises(Canceled):
        transcribe(tiny_video, tmp_path, "x", "int8", lambda f: None, lambda: True)
    assert loads == []


@pytest.mark.slow
def test_transcribe_real_speech(speech_video, tmp_path):  # fixture added in Task 13
    t = transcribe(speech_video, tmp_path, "large-v3-turbo", "int8_float16", lambda f: None, lambda: False)
    assert len(t.words) > 20 and all(w.end >= w.start for w in t.words)


def test_transcribe_no_audio_stream_returns_empty(tmp_path, monkeypatch):
    import subprocess
    v = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(v)], check=True)
    calls, loads = _patch(monkeypatch)
    t = transcribe(v, tmp_path, "x", "int8", lambda f: None, lambda: False)
    assert t.words == [] and loads == [] and calls == []
    assert (tmp_path / "transcript.json").exists()


def test_transcribe_multi_chunk_offsets_and_partial_resume(tmp_path, monkeypatch, tiny_video):
    monkeypatch.setattr(tr, "CHUNK_SECONDS", 2)
    seg = NS(start=0.1, end=0.5, text="x", words=[NS(start=0.1, end=0.5, word="x")])
    calls, loads = _patch(monkeypatch, [seg])
    chunks = tmp_path / "transcript_chunks"
    chunks.mkdir()
    Transcript("en", [Word(0.1, 0.5, "saved")], [Segment(0.1, 0.5, "saved")]).save(chunks / "0001.json")
    prog = []
    t = transcribe(tiny_video, tmp_path, "m", "int8", prog.append, lambda: False)
    assert len(calls) == 2  # chunks 0 and 2 only
    assert [(w.start, w.text) for w in t.words] == [(0.1, "x"), (2.1, "saved"), (4.1, "x")]
    assert prog == [1 / 3, 2 / 3, 1.0]


def test_register_cuda_dlls_only_once(tmp_path, monkeypatch):
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    added = []
    monkeypatch.setattr(tr.sys, "platform", "win32")
    monkeypatch.setattr(tr.sys, "path", [str(tmp_path)])
    monkeypatch.setattr(tr.os, "add_dll_directory", added.append, raising=False)
    monkeypatch.setattr(tr, "_dlls_registered", False)
    monkeypatch.setenv("PATH", "base")
    tr._register_cuda_dlls()
    first = tr.os.environ["PATH"]
    assert first.endswith("base") and len(added) == 1
    tr._register_cuda_dlls()
    assert tr.os.environ["PATH"] == first and len(added) == 1
