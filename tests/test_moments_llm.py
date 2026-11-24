import json

import httpx
import numpy as np
import pytest

from clipper import moments_llm as ml
from clipper.config import Settings
from clipper.models import Candidate, Canceled, Moment, Transcript, Word


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, password_hash="", session_secret="x")


def _mock(monkeypatch, handler):
    monkeypatch.setattr(ml, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def _reply(payload):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={"response": body})


def test_ollama_candidates_parses_and_clamps(settings, monkeypatch):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return _reply({"clips": [{"start": 100, "end": 700, "title": "T" * 120, "score": 80, "reason": "r"}]})

    _mock(monkeypatch, handler)
    out = ml.ollama_candidates("[100.0] hi", 0, 600, "talk", 30, 90, settings)
    assert out == [Candidate(100, 600, "T" * 80, 80, "r")]
    b = seen[0]
    assert b["model"] == "qwen3:4b" and b["format"] == "json" and b["think"] is False
    assert b["stream"] is False and b["keep_alive"] == "5m" and b["options"] == {"temperature": 0.3, "num_ctx": 8192}
    assert "30" in b["prompt"] and "90" in b["prompt"]


def test_ollama_candidates_rejects_bad_items(settings, monkeypatch):
    replies = iter(["not json", {"clips": [{"start": "x"}, {"start": 50, "end": 40},
                    {"start": 10, "end": 50, "title": "ok", "score": 140, "reason": "r"}]}])
    calls = []

    def handler(req):
        calls.append(1)
        return _reply(next(replies))

    _mock(monkeypatch, handler)
    out = ml.ollama_candidates("...", 0, 600, "talk", 30, 90, settings)
    assert out == [Candidate(10, 50, "ok", 100, "r")] and len(calls) == 2


def test_ollama_candidates_accepts_bare_list(settings, monkeypatch):
    _mock(monkeypatch, lambda r: _reply([{"start": 1, "end": 40, "title": "a", "score": 5, "reason": ""}]))
    assert ml.ollama_candidates("x", 0, 600, "gaming", 30, 90, settings) == [Candidate(1, 40, "a", 5, "")]


def test_ollama_candidates_gives_up_after_second_bad_json(settings, monkeypatch):
    _mock(monkeypatch, lambda r: _reply("nope"))
    assert ml.ollama_candidates("x", 0, 600, "talk", 30, 90, settings) == []


def test_ollama_not_reachable_is_clear(settings, monkeypatch):
    def handler(req):
        raise httpx.ConnectError("refused")

    _mock(monkeypatch, handler)
    with pytest.raises(ml.MomentsError, match="Ollama is not reachable at http://127.0.0.1:11434"):
        ml.ollama_candidates("x", 0, 600, "talk", 30, 90, settings)


def test_missing_model_is_clear(settings, monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ml.MomentsError, match="ollama pull qwen3:4b"):
        ml.ollama_candidates("x", 0, 600, "talk", 30, 90, settings)


def test_energy_profile_no_audio_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "probe", lambda p: type("I", (), {"has_audio": False})())
    assert len(ml.energy_profile(tmp_path / "x.mp4")) == 0


def test_cli_reports_moments_error_cleanly(monkeypatch, capsys):
    from clipper import cli

    def boom(args):
        raise ml.MomentsError("Ollama is not reachable at h. Start Ollama and try again.")

    monkeypatch.setattr(cli, "find_moments_cmd", boom)
    assert cli.main(["find-moments", "x.mp4"]) == 1
    assert "Ollama is not reachable" in capsys.readouterr().err


def test_unload_model_posts_keep_alive_zero(settings, monkeypatch):
    seen = []
    _mock(monkeypatch, lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json={}))
    ml.unload_model(settings)
    assert seen == [{"model": "qwen3:4b", "keep_alive": 0}]


def test_format_chunk_one_line_per_sentence():
    ws = [Word(1.0, 1.5, "Hello"), Word(1.5, 2.0, "there."), Word(3.0, 3.4, "Next"),
          Word(3.4, 4.0, "one?"), Word(700.0, 701.0, "late")]
    assert ml.format_chunk(ws, 0, 600) == "[1.0] Hello there.\n[3.0] Next one?"


def _words(n, step=0.5):
    return [Word(i * step, i * step + 0.4, "w." if i % 6 == 5 else "w") for i in range(n)]


def _fake(monkeypatch, duration, energy=None):
    monkeypatch.setattr(ml, "probe", lambda p: type("I", (), {"duration": duration})())
    monkeypatch.setattr(ml, "energy_profile",
                        lambda p, hop=0.5: energy if energy is not None else np.zeros(int(duration / 0.5)))


def _run(settings, tmp_path, t, stop=lambda: False, vt="talk"):
    return ml.find_moments(tmp_path / "s.mp4", t, vt, "auto", settings, tmp_path, lambda f: None, stop)


def test_find_moments_unloads_model_even_on_error(settings, tmp_path, monkeypatch):
    _fake(monkeypatch, 100)
    unloaded = []
    monkeypatch.setattr(ml, "unload_model", lambda s: unloaded.append(1))

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(ml, "ollama_candidates", boom)
    with pytest.raises(RuntimeError, match="ollama down"):
        _run(settings, tmp_path, Transcript("en", _words(300), []))
    assert unloaded == [1]


def test_find_moments_unload_failure_does_not_mask_error(settings, tmp_path, monkeypatch):
    _fake(monkeypatch, 100)

    def handler(req):
        raise httpx.ConnectError("gone")  # both generate and unload fail

    _mock(monkeypatch, handler)
    monkeypatch.setattr(ml, "ollama_candidates", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("real")))
    with pytest.raises(RuntimeError, match="real"):
        _run(settings, tmp_path, Transcript("en", _words(300), []))


def test_find_moments_cancel(settings, tmp_path, monkeypatch):
    _fake(monkeypatch, 100)
    unloaded = []
    monkeypatch.setattr(ml, "unload_model", lambda s: unloaded.append(1))
    with pytest.raises(Canceled):
        _run(settings, tmp_path, Transcript("en", _words(300), []), stop=lambda: True)
    assert unloaded == [1]


def test_find_moments_resumes_from_chunk_files(settings, tmp_path, monkeypatch):
    _fake(monkeypatch, 100)
    monkeypatch.setattr(ml, "unload_model", lambda s: None)
    (tmp_path / "moment_chunks").mkdir()
    (tmp_path / "moments_key.json").write_text(json.dumps(ml.cache_key("talk", "auto", settings)))
    (tmp_path / "moment_chunks" / "0000.json").write_text(
        json.dumps([{"start": 0, "end": 40, "title": "saved", "ai_score": 90, "reason": "r"}]))

    def no_http(*a, **k):
        raise AssertionError("must not call the model for a saved chunk")

    monkeypatch.setattr(ml, "ollama_candidates", no_http)
    t = Transcript("en", _words(300), [])  # 150 s of words, dense enough: no audio fallback
    out = _run(settings, tmp_path, t)
    assert [m.title for m in out] == ["saved"]
    assert json.loads((tmp_path / "moments.json").read_text())[0]["title"] == "saved"
    # a second call returns the saved result without any work
    monkeypatch.setattr(ml, "probe", no_http)
    assert _run(settings, tmp_path, t) == out


def test_find_moments_recomputes_when_type_changes(settings, tmp_path, monkeypatch):
    _fake(monkeypatch, 100)
    monkeypatch.setattr(ml, "unload_model", lambda s: None)
    calls = []
    monkeypatch.setattr(ml, "ollama_candidates",
                        lambda text, cs, ce, vt, lo, hi, s: calls.append(vt) or [])
    t = Transcript("en", _words(300), [])
    _run(settings, tmp_path, t, vt="talk")
    _run(settings, tmp_path, t, vt="talk")
    assert calls == ["talk"]  # same key: reused
    _run(settings, tmp_path, t, vt="gaming")
    assert calls == ["talk", "gaming"]


def test_find_moments_chunks_and_audio_fallback(settings, tmp_path, monkeypatch):
    # 1300 s source -> chunks at 0, 540, 1080; empty transcript -> audio candidates only
    energy = np.zeros(2600)
    energy[380:420] = 1.0  # loud stretch around 200 s
    _fake(monkeypatch, 1300, energy)
    monkeypatch.setattr(ml, "unload_model", lambda s: None)
    out = _run(settings, tmp_path, Transcript("en", [], []), vt="gaming")
    assert sorted(p.name for p in (tmp_path / "moment_chunks").iterdir()) == ["0000.json", "0001.json", "0002.json"]
    assert any(m.start <= 200 <= m.end for m in out)


def test_energy_score_peak_window_scores_higher():
    p = np.full(200, 0.1)
    p[100:110] = 1.0
    assert ml.energy_score(p, 0.5, 50, 55) > 0.9
    assert ml.energy_score(p, 0.5, 0, 10) == 0.0
    assert ml.energy_score(np.zeros(0), 0.5, 0, 10) == 0.0


def test_energy_profile_normalised(tiny_video):
    p = ml.energy_profile(tiny_video)
    assert 9 <= len(p) <= 11 and p.max() == pytest.approx(1.0)
