# Video Clipper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A private, self-hosted web app that turns long 16:9 videos (links or uploads) into scored 9:16 clips with optional word-timed captions, running fully locally on a 4 GB NVIDIA laptop.

**Architecture:** One Python process: FastAPI serves server-rendered pages + plain JS, and one background worker thread pulls jobs from SQLite and runs a four-step pipeline (ingest → transcribe → find moments → render). GPU stages run strictly one after another; every step and chunk checkpoints to disk so a restart resumes. Access is via Tailscale; the app binds to localhost only.

**Tech Stack:** Python 3.12, FastAPI + Uvicorn + Jinja2, SQLite (stdlib), yt-dlp (pip, run as `python -m yt_dlp`), ffmpeg/ffprobe (system), faster-whisper, Ollama (HTTP via httpx), MediaPipe, PySceneDetect, NumPy, argon2-cffi, itsdangerous, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-video-clipper-design.md`

## Global Constraints

- No paid API or cloud service anywhere in the pipeline; everything runs on the local machine.
- Python **3.12** exactly (MediaPipe wheels); one virtualenv at `.venv/`.
- Design for **4 GB** graphics memory: Whisper `large-v3-turbo` with `compute_type="int8_float16"`; Ollama default model `qwen3:4b` (config `ollama_model`). Never hold two GPU models at once: free Whisper before Ollama runs; call Ollama unload (`keep_alive: 0`) when moment-finding ends.
- Server binds **127.0.0.1 only**. Every route except `/login` and `/static/*` requires a valid session.
- Caption rule: no caption word appears before its Whisper start time **+ 0.1 s** (`caption_delay`). Two adjacent words share an event only if the second starts within **0.45 s** of the first; otherwise word 1 shows alone, then both from word 2's start.
- Captions are **off by default**; when off, no ASS file is built and no subtitle filter runs.
- Caption house style: 2-word chunks, UPPERCASE, bold, lower third; styles `bold_white` and `yellow_pop`.
- Output clips: 1080×1920, H.264 + AAC MP4; `h264_nvenc` when it works, else `libx264`.
- Video types: `talk` (follow largest face), `movie` (follow faces, jump at scene cuts), `gaming` (fit whole frame over blurred copy). Stacked facecam layout is out of scope.
- Length presets (seconds): `auto` 30–90, `short` 15–30, `medium` 30–60, `long` 60–90.
- Clip selection: keep score ≥ threshold, at most 15 clips per hour of source (`clips_per_hour`), at least 1 clip for sources longer than 120 s.
- Transcription and moment-finding work in **600 s chunks**, each saved as it finishes.
- Source video retention: **14 days** after a job finishes (`source_retention_days`); then Edit/Caption are disabled for that job.
- Login: one password (argon2 hash in config), session cookie 30 days, HttpOnly, SameSite=Lax, Secure; 5 failed attempts → 15-minute lockout; changing the password invalidates sessions.
- Finished files are written to a `.tmp` name and renamed into place, so a crash never leaves a half-written file under a final name.
- Subprocesses (ffmpeg, ffprobe, yt-dlp) are always called with an argument list, never `shell=True`.

## Review Focus

1. **Source with little or no speech** (music-heavy movie scene, gameplay without commentary) → still produces clips from audio-energy peaks instead of crashing or returning nothing. Test: `test_find_moments_falls_back_to_audio_when_no_words` (Task 5).
2. **Source shorter than the preset's minimum, or under 2 minutes** → one clip covering what exists (or none if ≤ 120 s and nothing scores), never an exception or a zero-length clip. Test: `test_select_short_source` (Task 5).
3. **The local model returns broken JSON, or clips with end ≤ start, or times outside the chunk** → retried once, then bad items dropped or clamped; the job continues. Test: `test_ollama_candidates_rejects_bad_items` (Task 6).
4. **Titles and caption text with special characters** (emoji, quotes, `/`, `\`, `{}` in YouTube titles or transcripts) → safe filenames, escaped ASS text. Tests: `test_safe_filename` (Task 3), `test_build_ass_escapes_braces_and_backslashes` (Task 2).
5. **Restart mid-render or mid-upload** → no partial file is treated as finished; the job resumes and re-renders it. Tests: `test_render_clip_leaves_no_final_file_on_failure` (Task 8), `test_worker_resumes_after_restart` (Task 10).

---

## File Structure

```
Video Clipper/
  pyproject.toml            deps, pytest config (marker: slow)
  config.example.toml       documented defaults
  setup.ps1                 one-shot installer for desktop and Nitro
  README.md                 run, Tailscale, Task Scheduler, Nitro laptop notes
  .gitignore                .venv/ data/ config.toml .superpowers/ __pycache__/
  clipper/
    __init__.py
    models.py               shared dataclasses and literals
    config.py               Settings + load_settings()
    db.py                   SQLite schema and queries
    auth.py                 password hashing, sessions, lockout
    captions.py             chunk_words, ASS builder, corrections
    ingest.py               yt-dlp download, ffprobe, upload validation, disk check
    transcribe.py           chunked faster-whisper with checkpoints
    moments.py              snapping, scoring, dedupe, selection (pure)
    moments_llm.py          Ollama calls, audio energy, find_moments orchestration
    reframe.py              face tracking, smoothing, scene cuts → CropPlan
    render.py               ffmpeg filter building, encoding, posters
    worker.py               queue loop, step resume, cancel, re-render, retention
    app.py                  FastAPI app factory and routes
    cli.py                  `find-moments` and `serve` commands
    templates/              base.html login.html home.html results.html
    static/                 app.css app.js
  tests/
    conftest.py             tiny generated test videos (ffmpeg lavfi)
    test_*.py               one per module
    e2e/test_smoke.py       slow, full pipeline on a generated speech video
```

## Shared types (`clipper/models.py`, created in Task 1)

```python
VideoType = Literal["talk", "gaming", "movie"]
LengthPreset = Literal["auto", "short", "medium", "long"]
CaptionStyle = Literal["bold_white", "yellow_pop"]

@dataclass(frozen=True) class Word: start: float; end: float; text: str
@dataclass(frozen=True) class Segment: start: float; end: float; text: str
@dataclass class Transcript: language: str; words: list[Word]; segments: list[Segment]
    # save(path: Path) -> None ; load(path: Path) -> Transcript  (JSON)
@dataclass(frozen=True) class Candidate: start: float; end: float; title: str; ai_score: float; reason: str
@dataclass(frozen=True) class Moment: start: float; end: float; title: str; score: int   # score 0–100
@dataclass(frozen=True) class CaptionEvent: start: float; end: float; text: str
@dataclass(frozen=True) class CropKey: t: float; x: int        # t relative to clip start, x = crop left edge in source px
@dataclass class CropPlan: mode: Literal["follow", "fit"]; src_w: int; src_h: int; keys: list[CropKey]
@dataclass(frozen=True) class MediaInfo: duration: float; width: int; height: int; has_audio: bool
@dataclass(frozen=True) class SourceInfo: path: Path; title: str; duration: float; thumbnail: Path | None
LENGTH_RANGES: dict[LengthPreset, tuple[float, float]] = {"auto": (30, 90), "short": (15, 30), "medium": (30, 60), "long": (60, 90)}
class Canceled(Exception): ...
Progress = Callable[[float], None]      # fraction 0..1 of the current step
ShouldStop = Callable[[], bool]
```

---

### Task 1: Project skeleton, config and shared types

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `config.example.toml`, `clipper/__init__.py`, `clipper/models.py`, `clipper/config.py`, `tests/conftest.py`
- Test: `tests/test_config.py`, `tests/test_models.py`

**Interfaces:**
- Produces: everything in "Shared types" above; `Settings` dataclass with fields `data_dir: Path`, `port: int = 8765`, `password_hash: str`, `session_secret: str`, `whisper_model: str = "large-v3-turbo"`, `whisper_compute_type: str = "int8_float16"`, `ollama_model: str = "qwen3:4b"`, `ollama_host: str = "http://127.0.0.1:11434"`, `source_retention_days: int = 14`, `clips_per_hour: int = 15`, `score_threshold: int = 55`, `caption_delay: float = 0.1`, `default_captions_on: bool = False`, `default_caption_style: CaptionStyle = "bold_white"`, `cookie_secure: bool = True`; `load_settings(path: Path) -> Settings`.
- Produces (fixtures in `tests/conftest.py`): `tiny_video(tmp_path) -> Path` (5 s 1280×720 testsrc + sine audio via ffmpeg lavfi), `tiny_vertical_video(tmp_path) -> Path` (5 s 720×1280).

- [ ] **Step 1: Initialize git** — the owner will provide a repo later; until then run `git init` inside `Video Clipper/` (separate from the parent folder's repo). Write `.gitignore` with `.venv/`, `data/`, `config.toml`, `.superpowers/`, `__pycache__/`, `*.tmp*`.
- [ ] **Step 2: Create the venv and install deps** — `py -3.12 -m venv .venv`, then `.venv\Scripts\pip install -e .[dev]`. Deps in `pyproject.toml`: fastapi, uvicorn[standard], jinja2, python-multipart, itsdangerous, argon2-cffi, httpx, faster-whisper, mediapipe, scenedetect[opencv-headless], numpy, yt-dlp; dev: pytest. Register pytest marker `slow` (needs GPU/models) and default `-m "not slow"`.
- [ ] **Step 3: Write the failing tests**

```python
def test_load_settings_applies_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('data_dir = "D:/clips"\npassword_hash = "x"\nsession_secret = "s"\n')
    s = load_settings(p)
    assert s.ollama_model == "qwen3:4b" and s.caption_delay == 0.1
    assert s.source_retention_days == 14 and s.default_captions_on is False

def test_load_settings_rejects_missing_password_hash(tmp_path):
    p = tmp_path / "config.toml"; p.write_text('data_dir = "D:/clips"\nsession_secret = "s"\n')
    with pytest.raises(ValueError, match="password_hash"):
        load_settings(p)

def test_transcript_roundtrip(tmp_path):
    t = Transcript("en", [Word(0.0, 0.4, "hi")], [Segment(0.0, 0.4, "hi")])
    t.save(tmp_path / "t.json")
    assert Transcript.load(tmp_path / "t.json") == t
```

- [ ] **Step 4: Run** `.venv\Scripts\pytest tests/test_config.py tests/test_models.py -v` — Expected: FAIL (module not found).
- [ ] **Step 5: Implement** `clipper/models.py` and `clipper/config.py` (`tomllib`; unknown keys raise `ValueError`; `data_dir` is created if missing). Write `config.example.toml` listing every field with a one-line comment.
- [ ] **Step 6: Run** the same tests — Expected: PASS.
- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat: project skeleton, config and shared types"`

---

### Task 2: Captions (the timing rule)

**Files:**
- Create: `clipper/captions.py`
- Test: `tests/test_captions.py`

**Interfaces:**
- Consumes: `Word`, `CaptionEvent`, `CaptionStyle` (Task 1).
- Produces:
  - `words_in_range(words: list[Word], start: float, end: float) -> list[Word]` — words fully inside [start, end], times shifted so `start` becomes 0.
  - `apply_corrections(words: list[Word], corrections: dict[int, str]) -> list[Word]` — replaces text by index within the clip's word list, keeps timing; an empty string removes the word.
  - `chunk_words(words: list[Word], delay: float = 0.1, pair_gap: float = 0.45, hold: float = 0.3) -> list[CaptionEvent]`
  - `build_ass(events: list[CaptionEvent], style: CaptionStyle, width: int = 1080, height: int = 1920) -> str`

Algorithm for `chunk_words` (not determined by the signature):
```
pair words in order: (w0,w1), (w2,w3), ... ; a trailing odd word is its own chunk
for chunk (a, b):
    if b is None:                     events: [UPPER(a)] from a.start+delay
    elif b.start - a.start <= pair_gap: events: [UPPER(a b)] from a.start+delay
    else: events: [UPPER(a)] from a.start+delay to b.start+delay,
                  [UPPER(a b)] from b.start+delay
each chunk's last event ends at min(next chunk's first start, last_word.end + delay + hold)
```

- [ ] **Step 1: Write the failing tests**

```python
W = lambda s, e, t: Word(s, e, t)

def test_tight_pair_shows_together_at_first_onset():
    ev = chunk_words([W(1.0, 1.2, "nobody"), W(1.3, 1.6, "tells")])
    assert ev == [CaptionEvent(1.1, 2.0, "NOBODY TELLS")]          # ends at 1.6 + 0.1 + 0.3

def test_gap_pair_reveals_progressively():
    ev = chunk_words([W(1.0, 1.2, "wait"), W(2.0, 2.3, "what")])
    assert ev[0] == CaptionEvent(1.1, 2.1, "WAIT")
    assert ev[1] == CaptionEvent(2.1, 2.7, "WAIT WHAT")             # ends at 2.3 + 0.1 + 0.3

def test_no_word_ever_appears_before_spoken(words_fixture_random):  # 200 random words, seeded
    ev = chunk_words(words_fixture_random, delay=0.1)
    starts = {w.text.upper(): w.start for w in words_fixture_random}  # fixture uses unique words
    for e in ev:
        for token in e.text.split():
            assert e.start >= starts[token] + 0.1 - 1e-9

def test_events_never_overlap(words_fixture_random):
    ev = chunk_words(words_fixture_random)
    assert all(a.end <= b.start + 1e-9 for a, b in zip(ev, ev[1:]))

def test_words_in_range_shifts_to_clip_time():
    assert words_in_range([W(10, 10.5, "a"), W(20, 20.5, "b")], 9.0, 15.0) == [W(1.0, 1.5, "a")]

def test_apply_corrections_keeps_timing_and_removes_empty():
    out = apply_corrections([W(0, 1, "price"), W(1, 2, "in")], {0: "pricing", 1: ""})
    assert out == [W(0, 1, "pricing")]

def test_build_ass_escapes_braces_and_backslashes():
    ass = build_ass([CaptionEvent(0, 1, "A{B}\\C")], "bold_white")
    assert "A(B)/C" in ass and "{B}" not in ass       # ASS has no brace escape: { → (, } → ), \ → /

def test_build_ass_styles_and_resolution():
    ass = build_ass([CaptionEvent(0, 1, "HI")], "yellow_pop")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:01.00" in ass
```

- [ ] **Step 2: Run** `pytest tests/test_captions.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/captions.py`. ASS: one style per name, Bold=-1, Alignment=2, MarginV ≈ 560 (lower third of 1920), white primary for `bold_white`, yellow `&H0042C5F5` for `yellow_pop`, thick black outline. In event text replace `{` → `(`, `}` → `)`, `\` → `/`.
- [ ] **Step 4: Run** the tests — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: word-timed caption chunking and ASS output"`

---

### Task 3: Ingest (download, probe, uploads, disk)

**Files:**
- Create: `clipper/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `MediaInfo`, `SourceInfo`, `Progress`, `ShouldStop`, `Canceled` (Task 1); fixture `tiny_video`.
- Produces:
  - `class IngestError(Exception)` — message is plain English, shown to the owner.
  - `probe(path: Path) -> MediaInfo` — ffprobe JSON; raises `IngestError("This file isn't a video we can read.")` when no video stream.
  - `safe_filename(title: str, max_len: int = 80) -> str` — ASCII-safe, no `\/:*?"<>|`, never empty (`"clip"` fallback).
  - `map_ytdlp_error(stderr: str) -> str` — private → "This video is private.", "Video unavailable"/removed → "This video was removed or is unavailable.", geo → "This video is blocked in your country.", "Unsupported URL" → "This site isn't supported.", else "Download failed: " + last stderr line (trimmed to 200 chars).
  - `check_disk(directory: Path, needed_bytes: int) -> None` — raises `IngestError` if free space < `3 * needed_bytes`.
  - `download(url: str, job_dir: Path, progress: Progress, should_stop: ShouldStop) -> SourceInfo` — runs `[sys.executable, "-m", "yt_dlp", ...]` with format `bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]`, merge to mp4, `--write-thumbnail --convert-thumbnails jpg`, `--newline` progress parsing, output `job_dir/source.mp4`. Gets size estimate via `--print filesize_approx` first and calls `check_disk`. Kills the process and raises `Canceled` when `should_stop()`. Skips download if `source.mp4` already exists (resume).
  - `accept_upload(tmp_file: Path, job_dir: Path) -> SourceInfo` — probes, moves to `job_dir/source.<ext>`, title = original filename stem.

- [ ] **Step 1: Write the failing tests**

```python
def test_probe_reads_tiny_video(tiny_video):
    info = probe(tiny_video)
    assert (info.width, info.height, info.has_audio) == (1280, 720, True) and 4.5 < info.duration < 5.5

def test_probe_rejects_non_video(tmp_path):
    f = tmp_path / "x.mp4"; f.write_text("nope")
    with pytest.raises(IngestError, match="isn't a video"):
        probe(f)

@pytest.mark.parametrize("title,expected", [("Ep 12: \"Pricing\" 🔥/why?", "Ep 12 Pricing why"), ("", "clip"), ("///", "clip")])
def test_safe_filename(title, expected):
    assert safe_filename(title) == expected

@pytest.mark.parametrize("stderr,msg", [("ERROR: Private video", "private"), ("ERROR: Video unavailable", "removed"), ("ERROR: Unsupported URL: x", "isn't supported")])
def test_map_ytdlp_error(stderr, msg):
    assert msg in map_ytdlp_error(stderr)

def test_check_disk_raises_when_too_little_space(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (100, 90, 10))
    with pytest.raises(IngestError):
        check_disk(tmp_path, 5)

def test_download_passes_url_as_argument_not_shell(tmp_path, monkeypatch):
    calls = []  # monkeypatch subprocess.Popen/run to record args and fake output
    ...
    assert all(isinstance(a, list) for a in calls) and "https://x/;rm" in calls[-1]
```

- [ ] **Step 2: Run** `pytest tests/test_ingest.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/ingest.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: ingest via yt-dlp, uploads and probing"`

---

### Task 4: Chunked transcription

**Files:**
- Create: `clipper/transcribe.py`
- Test: `tests/test_transcribe.py`

**Interfaces:**
- Consumes: `Transcript`, `Word`, `Segment`, `Progress`, `ShouldStop`, `Canceled`; `probe` (Task 3).
- Produces:
  - `CHUNK_SECONDS = 600`
  - `merge_chunks(chunks: list[tuple[float, Transcript]]) -> Transcript` — adds each chunk's offset, concatenates, sorts by start, language = most common.
  - `transcribe(source: Path, job_dir: Path, model_name: str, compute_type: str, progress: Progress, should_stop: ShouldStop) -> Transcript` — extracts each 600 s chunk to 16 kHz mono WAV with ffmpeg, runs `WhisperModel(model_name, device="cuda", compute_type=compute_type).transcribe(wav, word_timestamps=True, vad_filter=True)`, saves `job_dir/transcript_chunks/NNNN.json` per chunk (skips chunks already saved), writes `job_dir/transcript.json`, then deletes the model and runs `gc.collect()` so the GPU is free. Returns early with the saved file if `transcript.json` exists. A source with no speech yields an empty `words` list, not an error.
  - shortcut comment: words straddling a 600 s boundary may be cut; upgrade to silence-aligned chunk edges if it shows up in captions.

- [ ] **Step 1: Verify CUDA for faster-whisper** — on Windows install cuBLAS + cuDNN 9 for CUDA 12 per the faster-whisper README (Windows section), then run `.venv\Scripts\python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"` — Expected: `1`.
- [ ] **Step 2: Write the failing tests**

```python
def test_merge_chunks_offsets_and_sorts():
    a = Transcript("en", [Word(1, 2, "a")], [Segment(1, 2, "a")])
    b = Transcript("en", [Word(0.5, 1, "b")], [Segment(0.5, 1, "b")])
    m = merge_chunks([(0.0, a), (600.0, b)])
    assert [w.start for w in m.words] == [1, 600.5] and m.language == "en"

def test_transcribe_resumes_from_saved_chunks(tmp_path, monkeypatch, tiny_video):
    # fake WhisperModel records calls; pre-write transcript_chunks/0000.json
    ...
    assert fake_calls == []          # nothing re-transcribed

def test_transcribe_raises_canceled(tmp_path, monkeypatch, tiny_video):
    with pytest.raises(Canceled):
        transcribe(tiny_video, tmp_path, "x", "int8", lambda f: None, lambda: True)

@pytest.mark.slow
def test_transcribe_real_speech(speech_video, tmp_path):   # fixture added in Task 13
    t = transcribe(speech_video, tmp_path, "large-v3-turbo", "int8_float16", lambda f: None, lambda: False)
    assert len(t.words) > 20 and all(w.end >= w.start for w in t.words)
```

- [ ] **Step 3: Run** `pytest tests/test_transcribe.py -v` — Expected: FAIL.
- [ ] **Step 4: Implement** `clipper/transcribe.py`.
- [ ] **Step 5: Run** — Expected: PASS (slow test skipped by default).
- [ ] **Step 6: Commit** — `git commit -am "feat: chunked, resumable Whisper transcription"`

---

### Task 5: Moment scoring, snapping, dedupe and selection (pure logic)

**Files:**
- Create: `clipper/moments.py`
- Test: `tests/test_moments.py`

**Interfaces:**
- Consumes: `Word`, `Candidate`, `Moment`, `VideoType`, `LENGTH_RANGES`.
- Produces:
  - `sentence_ends(words: list[Word]) -> list[int]` — indices of words ending in `.`, `?` or `!`.
  - `snap(c: Candidate, words: list[Word], lo: float, hi: float) -> Candidate | None` — start moves back to the first word of its sentence; end moves forward to the end of its sentence; if shorter than `lo`, extend by whole sentences; if longer than `hi`, cut at the last sentence end ≤ start+hi, else the last word end ≤ start+hi; return `None` if still < `lo` (source ran out). With no words in range, return `c` clamped to [lo, hi] length.
  - `hook_score(text: str) -> float` (0..1) — +0.4 if the first sentence has `?`, +0.3 if it contains a digit, +0.3 if it contains a strong opener from a fixed list (`"nobody"`, `"never"`, `"secret"`, `"truth"`, `"mistake"`, `"why"`, `"how"`, `"stop"`), capped at 1.
  - `pace_score(words: list[Word]) -> float` (0..1) — words per second; 1.0 between 2.0 and 3.5 wps, falling linearly to 0 at 0.5 and 5.5.
  - `final_score(ai: float, energy: float, pace: float, hook: float, video_type: VideoType) -> int` — `ai` is 0..100, the others 0..1. Weights `talk`: ai .6, energy .2, pace .1, hook .1; `gaming`/`movie`: ai .4, energy .45, pace .05, hook .1. Returns round(100 × weighted sum).
  - `dedupe(moments: list[Moment]) -> list[Moment]` — if two overlap by more than 50% of the shorter, keep the higher score.
  - `select(moments: list[Moment], duration: float, threshold: int, per_hour: int) -> list[Moment]` — keep score ≥ threshold, cap at `max(1, ceil(duration / 3600 * per_hour))`, and if none pass and duration > 120, keep the single best; sorted by score descending.
  - `audio_candidates(energy: np.ndarray, hop: float, lo: float, hi: float, n: int) -> list[Candidate]` — windows of length (lo+hi)/2 centred on the `n` highest non-overlapping energy peaks, `ai_score=50`, title `"Moment at mm:ss"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_snap_extends_to_sentence_boundaries(): ...          # candidate mid-sentence → starts at sentence's first word, ends after "."
def test_snap_drops_when_source_runs_out(): ...             # candidate near the end, lo=30, only 10 s left → None
def test_final_score_weights_energy_more_for_gaming():
    assert final_score(50, 1.0, 0, 0, "gaming") > final_score(50, 1.0, 0, 0, "talk")
def test_dedupe_keeps_higher_score():
    a, b = Moment(0, 40, "a", 70), Moment(10, 50, "b", 80)
    assert dedupe([a, b]) == [b]
def test_select_caps_per_hour():
    ms = [Moment(i * 100, i * 100 + 40, str(i), 90) for i in range(30)]
    assert len(select(ms, 3600, 55, 15)) == 15
def test_select_short_source():
    assert select([Moment(0, 30, "a", 20)], 150, 55, 15) == [Moment(0, 30, "a", 20)]
    assert select([Moment(0, 30, "a", 20)], 100, 55, 15) == []
def test_find_moments_falls_back_to_audio_when_no_words():
    energy = np.zeros(1200); energy[400] = 1.0                  # hop 0.5 s → peak at 200 s
    c = audio_candidates(energy, 0.5, 30, 90, 1)[0]
    assert c.start < 200 < c.end and 30 <= c.end - c.start <= 90
def test_hook_score(): assert hook_score("Why does nobody talk about this?") == 1.0
```

- [ ] **Step 2: Run** `pytest tests/test_moments.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/moments.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: moment snapping, scoring and selection"`

---

### Task 6: Local model, audio energy, find_moments + owner checkpoint

**Files:**
- Create: `clipper/moments_llm.py`, `clipper/cli.py`
- Test: `tests/test_moments_llm.py`

**Interfaces:**
- Consumes: Task 5 functions; `Transcript`, `Settings`, `Progress`, `ShouldStop`, `Canceled`; `download`/`accept_upload` (Task 3); `transcribe` (Task 4).
- Produces:
  - `energy_profile(source: Path, hop: float = 0.5) -> np.ndarray` — RMS per hop from ffmpeg-decoded 16 kHz mono PCM, normalized 0..1.
  - `energy_score(profile: np.ndarray, hop: float, start: float, end: float) -> float` — (95th-percentile in window − global median) / (global max − global median), clamped 0..1.
  - `format_chunk(words: list[Word], start: float, end: float) -> str` — one line per sentence: `[123.4] text`.
  - `ollama_candidates(text: str, chunk_start: float, chunk_end: float, video_type: VideoType, lo: float, hi: float, settings: Settings) -> list[Candidate]` — POST `{host}/api/generate` with `model`, `prompt`, `format: "json"`, `think: false`, `stream: false`, `options: {temperature: 0.3}`, `keep_alive: "5m"`. Expects `{"clips": [{"start", "end", "title", "score", "reason"}]}`. Retries once on invalid JSON; drops items with non-numeric times or end ≤ start; clamps times into [chunk_start, chunk_end]; `score` clamped to 0..100; title trimmed to 80 chars.
  - `unload_model(settings: Settings) -> None` — POST with `keep_alive: 0`.
  - `find_moments(source: Path, transcript: Transcript, video_type: VideoType, preset: LengthPreset, settings: Settings, job_dir: Path, progress: Progress, should_stop: ShouldStop) -> list[Moment]` — per 600 s chunk (60 s overlap) call `ollama_candidates`, save `job_dir/moment_chunks/NNNN.json` (skip if present); if the transcript has fewer than 20 words per 10 minutes, add `audio_candidates`; snap, score with `final_score`, `dedupe`, `select`; write `job_dir/moments.json`; always call `unload_model` in a `finally`. Returns saved `moments.json` if present.
  - CLI: `python -m clipper.cli find-moments <url-or-file> --type talk|gaming|movie --length auto|short|medium|long --config config.toml` — prints a table: score, start–end, title. Work dir under `data_dir/tryout/`.
  - Prompt text per video type (spec §6.4): talk → strong statements, stories, jokes; gaming → reactions, hype, funny moments; movie → dramatic or quotable scenes. Tell the model the length range and that each clip must make sense on its own.

- [ ] **Step 1: Write the failing tests** (httpx mocked with `httpx.MockTransport`)

```python
def test_ollama_candidates_parses_and_clamps(settings): ...      # one valid clip with end beyond chunk → end == chunk_end
def test_ollama_candidates_rejects_bad_items(settings):
    # first response "not json", second {"clips":[{"start":"x"},{"start":50,"end":40},{"start":10,"end":50,"title":"ok","score":140,"reason":"r"}]}
    out = ollama_candidates("...", 0, 600, "talk", 30, 90, settings)
    assert out == [Candidate(10, 50, "ok", 100, "r")] and calls == 2
def test_find_moments_unloads_model_even_on_error(settings, tmp_path): ...
def test_find_moments_resumes_from_chunk_files(settings, tmp_path): ...  # no HTTP call for saved chunks
def test_energy_score_peak_window_scores_higher(): ...
```

- [ ] **Step 2: Run** `pytest tests/test_moments_llm.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/moments_llm.py` and the `find-moments` CLI command.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Install Ollama and pull the model** — `winget install Ollama.Ollama`, then `ollama pull qwen3:4b`.
- [ ] **Step 6: Owner checkpoint** — run `python -m clipper.cli find-moments <one of the owner's real videos> --type talk` (and one gaming or movie source). Show the owner the printed list. **Stop and get the owner's verdict on clip choice before Task 7.** If the picks are poor, adjust the prompt and weights here, not later.
- [ ] **Step 7: Commit** — `git commit -am "feat: local-model moment finding with audio fallback and CLI"`

---

### Task 7: Reframing plans

**Files:**
- Create: `clipper/reframe.py`
- Test: `tests/test_reframe.py`

**Interfaces:**
- Consumes: `CropPlan`, `CropKey`, `MediaInfo`, `VideoType`; `probe` (Task 3).
- Produces:
  - `crop_width(src_h: int) -> int` — `round(src_h * 9 / 16)` made even.
  - `smooth_track(samples: list[tuple[float, float | None]], src_w: int, crop_w: int, cuts: list[float], dead_zone: float = 0.08, alpha: float = 0.15) -> list[CropKey]` — samples are (t, face-centre x in px or None). None holds the last target (centre if none yet). Target moves only when the face leaves a dead zone of `dead_zone * src_w` around the current centre; position eases toward target with EMA `alpha`; at a time in `cuts` the position jumps straight to the new target. x = centre − crop_w/2, clamped to [0, src_w − crop_w], int.
  - `detect_faces(source: Path, start: float, end: float, fps: float = 5) -> list[tuple[float, float | None]]` — MediaPipe face detection (CPU, so it doesn't use the graphics card) on frames read with OpenCV; centre x of the largest box; t relative to `start`.
  - `scene_cuts(source: Path, start: float, end: float) -> list[float]` — PySceneDetect `ContentDetector`, times relative to `start`.
  - `plan_crop(source: Path, start: float, end: float, video_type: VideoType, info: MediaInfo) -> CropPlan` — `gaming`, or any source with width/height ≤ 1.0 (already vertical or square) → `CropPlan("fit", w, h, [])`; `talk` → follow without cuts; `movie` → follow with `scene_cuts`.

- [ ] **Step 1: Write the failing tests**

```python
def test_smooth_track_ignores_jitter_inside_dead_zone():
    keys = smooth_track([(i / 5, 960 + (5 if i % 2 else -5)) for i in range(50)], 1920, 608, [])
    assert len({k.x for k in keys}) == 1
def test_smooth_track_jumps_at_cut():
    s = [(t / 5, 400.0) for t in range(10)] + [(t / 5, 1500.0) for t in range(10, 20)]
    keys = smooth_track(s, 1920, 608, cuts=[2.0])
    assert keys[10].x == 1500 - 304
def test_smooth_track_clamps_to_frame():
    assert smooth_track([(0, 10.0)], 1920, 608, [])[0].x == 0
def test_plan_crop_gaming_and_vertical_use_fit(tiny_video, tiny_vertical_video): ...
@pytest.mark.slow
def test_detect_faces_finds_face(face_video): ...           # fixture: short clip with a face, added in Task 13
```

- [ ] **Step 2: Run** `pytest tests/test_reframe.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/reframe.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: face-following and fit reframing plans"`

---

### Task 8: Rendering

**Files:**
- Create: `clipper/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `CropPlan`; `build_ass`, `chunk_words`, `words_in_range`, `apply_corrections` (Task 2); `probe` (Task 3).
- Produces:
  - `pick_encoder() -> list[str]` — cached; `["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"]` if a 1-second NVENC test encode succeeds, else `["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]`. Audio is always `["-c:a", "aac", "-b:a", "160k"]`.
  - `build_filter(plan: CropPlan, ass_name: str | None) -> tuple[str, str | None]` — returns (filter_complex, sendcmd file text or None). Follow: crop to `crop_width(src_h)`×`src_h` with x driven by the keys (sendcmd suggested; any method that moves x over time is fine), then scale to 1080×1920. Fit: blurred, zoomed copy filling 1080×1920 with the full frame scaled to width 1080 overlaid in the centre. When `ass_name` is set, append `subtitles=<ass_name>` (a bare filename; ffmpeg runs with `cwd` = the clip's folder to avoid Windows drive-letter escaping).
  - `render_clip(source: Path, out_path: Path, start: float, end: float, plan: CropPlan, words: list[Word] | None, style: CaptionStyle | None, delay: float, corrections: dict[int, str] | None = None) -> None` — `words=None` means captions off (no ASS file, no subtitles filter). Writes `out_path.with_suffix(".tmp.mp4")` and renames on success; on ffmpeg failure deletes the temp file and raises `RenderError(stderr tail)`. If NVENC fails mid-run, retry once with libx264. Also writes `<out stem>.jpg` poster (frame at 1 s, via temp + rename).
  - `class RenderError(Exception)`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_filter_fit_has_blur_and_overlay():
    f, cmd = build_filter(CropPlan("fit", 1920, 1080, []), None)
    assert "boxblur" in f and "overlay" in f and "subtitles" not in f and cmd is None
def test_build_filter_adds_subtitles_only_when_given():
    f, _ = build_filter(CropPlan("fit", 1920, 1080, []), "c.ass")
    assert "subtitles=c.ass" in f
def test_render_clip_outputs_vertical_1080x1920(tiny_video, tmp_path):
    out = tmp_path / "c1.mp4"
    render_clip(tiny_video, out, 1, 4, CropPlan("follow", 1280, 720, [CropKey(0, 300)]), None, None, 0.1)
    info = probe(out); assert (info.width, info.height) == (1080, 1920) and 2.5 < info.duration < 3.5
    assert (tmp_path / "c1.jpg").exists()
def test_render_clip_with_captions(tiny_video, tmp_path): ...     # words given → renders, no .ass left beside output except the kept clip ASS
def test_render_clip_leaves_no_final_file_on_failure(tmp_path):
    with pytest.raises(RenderError):
        render_clip(tmp_path / "missing.mp4", tmp_path / "c.mp4", 0, 3, CropPlan("fit", 1920, 1080, []), None, None, 0.1)
    assert not (tmp_path / "c.mp4").exists() and not list(tmp_path.glob("*.tmp*"))
```

- [ ] **Step 2: Run** `pytest tests/test_render.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/render.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: ffmpeg rendering with reframing and optional captions"`

---

### Task 9: Database

**Files:**
- Create: `clipper/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - Tables: `jobs(id INTEGER PK, kind TEXT 'video'|'rerender', status TEXT 'queued'|'running'|'done'|'failed'|'canceled', priority INT DEFAULT 0, source_url TEXT, title TEXT, video_type TEXT, length_preset TEXT, captions_on INT, caption_style TEXT, step INT DEFAULT 0, step_progress REAL DEFAULT 0, step_started_at REAL, error TEXT, cancel_requested INT DEFAULT 0, clip_id INT, created_at REAL, finished_at REAL, source_deleted INT DEFAULT 0, duration REAL)`; `clips(id INTEGER PK, job_id INT, start REAL, end REAL, title TEXT, score INT, captions_on INT, caption_style TEXT, corrections TEXT DEFAULT '{}', file TEXT, poster TEXT, version INT DEFAULT 1, created_at REAL)`.
  - `connect(path: Path) -> sqlite3.Connection` — WAL mode, `row_factory=sqlite3.Row`, `check_same_thread=False`, creates tables.
  - `create_job(conn, **fields) -> int`, `get_job(conn, job_id) -> sqlite3.Row | None`, `update_job(conn, job_id, **fields) -> None`
  - `next_job(conn) -> sqlite3.Row | None` — oldest `queued` by `priority DESC, created_at ASC`; marks it `running` in the same transaction.
  - `next_rerender(conn) -> sqlite3.Row | None` — same, only `kind='rerender'`.
  - `requeue_running(conn) -> int` — `running` → `queued`, returns count.
  - `list_jobs(conn) -> tuple[list[Row], list[Row]]` — (active: queued + running, finished: done + failed + canceled newest first).
  - `add_clip(conn, **fields) -> int`, `get_clip`, `list_clips(conn, job_id) -> list[Row]` (score desc), `update_clip(conn, clip_id, **fields)`, `delete_clip(conn, clip_id)`.
  - `update_*` accept only known column names (raise `ValueError` otherwise) so field names can't be injected into SQL.

- [ ] **Step 1: Write the failing tests**

```python
def test_next_job_prefers_rerender_priority(conn):
    a = create_job(conn, kind="video", status="queued", created_at=1)
    b = create_job(conn, kind="rerender", status="queued", priority=10, created_at=2)
    assert next_job(conn)["id"] == b and get_job(conn, b)["status"] == "running"
def test_requeue_running(conn): ...
def test_update_job_rejects_unknown_column(conn):
    j = create_job(conn, kind="video", status="queued", created_at=1)
    with pytest.raises(ValueError): update_job(conn, j, **{"status = 'x'; --": 1})
def test_list_clips_sorted_by_score(conn): ...
```

- [ ] **Step 2: Run** `pytest tests/test_db.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/db.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: SQLite job and clip storage"`

---

### Task 10: Worker (queue, resume, cancel, re-render, retention)

**Files:**
- Create: `clipper/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 6, 7, 8, 9.
- Produces:
  - `job_dir(settings: Settings, job_id: int) -> Path` — `data_dir/jobs/<id>/`; clips at `clips/<clip_id>_v<version>.mp4`.
  - `class Worker(threading.Thread)` — `__init__(self, settings: Settings, db_path: Path, steps: Steps | None = None)`; `run()` loops: `next_job`, dispatch, sleep 1 s when idle; `stop()` ends the loop after the current step.
  - `class Steps` — holds the step callables (`download`, `accept_upload`, `transcribe`, `find_moments`, `plan_crop`, `render_clip`) so tests can inject fakes.
  - Video job: step 1 ingest (skip if source exists) → 2 transcribe → 3 find moments → 4 for each moment: `plan_crop` + `render_clip` + `add_clip` (skip moments whose clip file already exists). Updates `step`, `step_progress`, `step_started_at`. `should_stop` reads `cancel_requested`; on `Canceled` → status `canceled`. On any other exception → status `failed`, `error` = `IngestError`/`RenderError` message or "Something went wrong: <type>"; log the traceback to `data_dir/logs/worker.log`.
  - Between chunks and between clips, the worker runs any queued re-render jobs (`next_rerender`) before continuing, so re-renders never wait for a whole step.
  - Re-render job (`clip_id` set): loads the clip and job; if `source_deleted` → fail with "The source video was cleaned up, so this clip can't be changed." Otherwise re-plans the crop (if start/end changed) and renders to a new version file, updates the clip row, deletes the old version's files.
  - `cleanup_sources(conn, settings, now: float) -> int` — for `done` jobs finished more than `source_retention_days` ago: delete `source.*`, set `source_deleted=1`. Called at most once an hour from the loop.
  - On start: `requeue_running`.

- [ ] **Step 1: Write the failing tests** (fake `Steps` that record calls and write dummy files)

```python
def test_worker_runs_full_video_job(...): ...           # status done, step 4, one clip row per fake moment
def test_worker_resumes_after_restart(...):
    # job marked running with source.mp4 + transcript.json present and one of two clip files present
    # → after restart: no download/transcribe calls, exactly one render call, 2 clips total
def test_worker_cancel_marks_canceled(...): ...
def test_rerender_runs_between_chunks(...): ...         # fake transcribe calls progress twice; a rerender queued meanwhile runs before step 3
def test_rerender_fails_cleanly_after_source_cleanup(...): ...
def test_cleanup_sources_respects_retention(...):
    # done 15 days ago → source deleted, source_deleted=1; done 13 days ago → untouched
def test_failed_job_records_plain_error(...): ...       # IngestError("This video is private.") → error text equals message
```

- [ ] **Step 2: Run** `pytest tests/test_worker.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/worker.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: resumable job worker with re-render and retention"`

---

### Task 11: Auth

**Files:**
- Create: `clipper/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces:
  - `hash_password(pw: str) -> str`, `verify_password(pw_hash: str, pw: str) -> bool` (argon2-cffi).
  - `SESSION_MAX_AGE = 30 * 86400`
  - `make_session(secret: str, pw_hash: str) -> str` — itsdangerous `TimestampSigner(secret)` signing `sha256(pw_hash)[:16]`.
  - `check_session(token: str | None, secret: str, pw_hash: str, max_age: int = SESSION_MAX_AGE) -> bool` — false if missing, bad signature, expired, or fingerprint differs (password changed).
  - `class Lockout` — `__init__(max_failures=5, lock_seconds=900)`, `record_failure(now: float)`, `is_locked(now: float) -> bool`, `reset()`. In memory; shortcut comment: resets on restart, acceptable for one user behind Tailscale.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_roundtrip_and_password_change():
    h1, h2 = hash_password("a"), hash_password("b")
    tok = make_session("s", h1)
    assert check_session(tok, "s", h1) and not check_session(tok, "s", h2)
def test_session_expires(monkeypatch): ...                  # max_age=0 after a tick → False
def test_lockout_after_five_failures():
    lk = Lockout(); [lk.record_failure(100) for _ in range(5)]
    assert lk.is_locked(100 + 899) and not lk.is_locked(100 + 901)
def test_four_failures_do_not_lock(): ...
```

- [ ] **Step 2: Run** `pytest tests/test_auth.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `clipper/auth.py`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: single-admin auth with lockout"`

---

### Task 12: Web app — login, home, queue

**Files:**
- Create: `clipper/app.py`, `clipper/templates/base.html`, `clipper/templates/login.html`, `clipper/templates/home.html`, `clipper/static/app.css`, `clipper/static/app.js`
- Modify: `clipper/cli.py` (add `serve` command: start `Worker`, run uvicorn on `127.0.0.1:<port>`)
- Test: `tests/test_app_home.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 9, 10, 11.
- Produces:
  - `create_app(settings: Settings, db_path: Path, start_worker: bool = True) -> FastAPI`
  - `require_session` dependency → redirects pages to `/login`, returns 401 for `/api/*` and `/media/*`.
  - Routes:
    - `GET /login`, `POST /login` (form `password`; on success set cookie `session` with HttpOnly, SameSite=Lax, Secure=`settings.cookie_secure`, max_age 30 days; when locked show "Too many tries. Try again in N minutes.")
    - `POST /logout`
    - `GET /` — home page per mockup `home-and-login.html`, with the Video type dropdown (Talk show / Gaming / Movie, default Talk show), clip length dropdown, and a captions toggle (off by default) with a style picker shown when on.
    - `POST /jobs` — form `url`, `video_type`, `length_preset`, `captions_on`, `caption_style`; validates it's `http(s)://` and the option values are in their literals; 400 with a message otherwise.
    - `POST /jobs/upload` — multipart `file` plus the same options; streamed to `data_dir/uploads/<uuid>.part` in 1 MB chunks, then `accept_upload` runs in the worker as step 1.
    - `GET /api/jobs` — JSON `{active: [...], finished: [...]}` with id, title, duration, step, step_progress, status, error, clip count, `eta_seconds` (builder's estimate from step progress and elapsed time).
    - `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry` (failed/canceled → queued, error cleared).
    - `GET /media/thumbs/{job_id}.jpg`
  - `app.js` polls `/api/jobs` every 2 s while any job is active and redraws the queue and finished lists.

- [ ] **Step 1: Write the failing tests** (FastAPI `TestClient`, `start_worker=False`)

```python
def test_pages_redirect_to_login_without_session(client): assert client.get("/", follow_redirects=False).status_code == 303
def test_api_returns_401_without_session(client): assert client.get("/api/jobs").status_code == 401
def test_login_sets_session_and_shows_home(client): ...
def test_login_lockout_message_after_five_wrong(client): ...
def test_create_job_validates_url_and_options(authed):
    assert authed.post("/jobs", data={"url": "file:///etc/passwd", "video_type": "talk", "length_preset": "auto"}).status_code == 400
    assert authed.post("/jobs", data={"url": "https://youtu.be/x", "video_type": "bogus", "length_preset": "auto"}).status_code == 400
def test_create_job_defaults_captions_off(authed): ...
def test_upload_streams_to_part_file_and_queues(authed, tiny_video): ...
def test_cancel_and_retry(authed): ...
def test_server_binds_localhost_only(): ...           # cli serve builds uvicorn config with host == "127.0.0.1"
```

- [ ] **Step 2: Run** `pytest tests/test_app_home.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the routes, templates and JS. Port the layout and CSS from `.superpowers/brainstorm/697-1795422411/content/home-and-login.html` (add the Video type dropdown and captions toggle). Must work at phone width.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Manual check** — `python -m clipper.cli serve --config config.toml`, open `http://127.0.0.1:8765`, log in, queue the tiny test video upload, and watch the progress bar move through all 4 steps.
- [ ] **Step 6: Commit** — `git commit -am "feat: login, home page and live queue"`

---

### Task 13: Web app — results page actions

**Files:**
- Create: `clipper/templates/results.html`
- Modify: `clipper/app.py`, `clipper/static/app.js`, `clipper/static/app.css`
- Test: `tests/test_app_results.py`

**Interfaces:**
- Consumes: Task 12 app; `db` clip functions; `words_in_range` (Task 2); `Transcript.load`.
- Produces routes:
  - `GET /jobs/{id}` — results page per mockup `results-page-v2.html`: header with **Download all (N) · .zip**; cards with player, 🔥 score, 🗑, title, length, "from mm:ss", Download / Edit / Caption. Edit and Caption are disabled with the note "Source cleaned up — download only" when `source_deleted`.
  - `GET /media/clips/{clip_id}.mp4` — must answer `Range` requests with **206** (Starlette `FileResponse` supports this; the test proves it).
  - `GET /media/posters/{clip_id}.jpg`
  - `GET /clips/{id}/download` — `Content-Disposition: attachment; filename="<safe_filename(title)>.mp4"`.
  - `GET /jobs/{id}/zip` — zip of all current clip files (ZIP_STORED, built to a temp file then streamed, filenames `NN_<safe title>.mp4`).
  - `POST /clips/{id}/delete` — removes files and row; JSON `{ok: true}`.
  - `GET /api/clips/{id}/words` — the clip's words (clip time) with indices, current corrections applied, `captions_on`, `caption_style`.
  - `POST /clips/{id}/trim` — JSON `{start, end}` in source seconds; validates `0 ≤ start < end ≤ duration` and `end − start ≥ 3`; queues a `rerender` job with priority 10.
  - `POST /clips/{id}/captions` — JSON `{captions_on, caption_style, corrections: {index: text}}`; validates style and indices; queues a `rerender` job.
  - The card shows "Re-rendering…" while its re-render job is active (from `/api/jobs`) and reloads the video when it's done.
- JS: delete confirm overlay ("Delete this clip? This can't be undone." Cancel / Delete), trim panel with two draggable handles over a strip (start/length/end readout), caption panel with an on/off toggle, word chips (click to edit), and a style picker.

- [ ] **Step 1: Write the failing tests**

```python
def test_clip_media_supports_range(authed, clip_on_disk):
    r = authed.get(f"/media/clips/{clip_on_disk}.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
def test_media_requires_session(client, clip_on_disk): assert client.get(f"/media/clips/{clip_on_disk}.mp4").status_code == 401
def test_download_uses_safe_filename(authed, clip_on_disk): ...
def test_zip_contains_all_clips(authed, job_with_two_clips): ...
def test_delete_removes_file_and_row(authed, clip_on_disk): ...
def test_trim_validates_and_queues_rerender(authed, clip_on_disk):
    assert authed.post(f"/clips/{clip_on_disk}/trim", json={"start": 10, "end": 5}).status_code == 400
    assert authed.post(f"/clips/{clip_on_disk}/trim", json={"start": 1, "end": 4}).status_code == 200
def test_caption_edit_rejects_bad_style(authed, clip_on_disk): ...
def test_edit_blocked_after_source_cleanup(authed, clip_on_disk): ...   # 409 with the cleaned-up message
```

- [ ] **Step 2: Run** `pytest tests/test_app_results.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the routes, `results.html` and JS. Port the layout from `results-page-v2.html` and the panels from `results-page.html` (add the captions on/off toggle to the Caption panel).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Manual check on a phone-width window** — play a clip, trim it, turn captions on, fix a word, delete a clip, Download all.
- [ ] **Step 6: Commit** — `git commit -am "feat: results page with play, download, trim, captions, delete, zip"`

---

### Task 14: Setup script, README and end-to-end smoke test

**Files:**
- Create: `setup.ps1`, `README.md`, `tests/e2e/test_smoke.py`
- Modify: `tests/conftest.py` (add `speech_video` and `face_video` fixtures used by the slow tests in Tasks 4 and 7)

**Interfaces:**
- Consumes: everything.
- Produces:
  - `setup.ps1` (idempotent, safe to re-run). It:
    - checks Python 3.12, NVIDIA driver (`nvidia-smi`) and ffmpeg (`winget install Gyan.FFmpeg` if missing);
    - creates `.venv` and installs the package;
    - installs CUDA libs for faster-whisper and verifies `ctranslate2.get_cuda_device_count() -ge 1`;
    - installs Ollama and pulls the `ollama_model` from config;
    - prompts for the admin password (`Read-Host -AsSecureString`) and writes `config.toml` with the argon2 hash and a random `session_secret` (keeps an existing config unless `-ResetPassword`);
    - registers a Task Scheduler task "Video Clipper" that runs `python -m clipper.cli serve` at boot, whether or not anyone is logged in;
    - prints the Tailscale steps.
  - `README.md`:
    - start/stop;
    - Tailscale (`tailscale serve --bg 8765` → open `https://<machine>.<tailnet>.ts.net` from other devices);
    - moving to the Nitro (copy folder, run `setup.ps1`, adjust models in `config.toml` if needed);
    - Nitro laptop notes (charge limit in NitroSense/Care Center, lid close = do nothing, clear vents);
    - a note that posting movie clips publicly can get them taken down.
  - `speech_video` fixture: about 2.5 minutes of English speech from Windows SAPI (`System.Speech.Synthesis` via PowerShell), several sentences including a question, muxed with ffmpeg testsrc at 1280×720.

- [ ] **Step 1: Write the failing slow test**

```python
@pytest.mark.slow
def test_full_pipeline_smoke(speech_video, tmp_path):
    # settings with data_dir=tmp_path, captions_on=True; enqueue an upload job; run Worker until done (timeout 15 min)
    job = ...; assert job["status"] == "done"
    clips = list_clips(conn, job["id"]); assert len(clips) >= 1
    info = probe(Path(clips[0]["file"])); assert (info.width, info.height) == (1080, 1920)
    # caption rule: every Dialogue line in the clip's .ass starts at or after its words' start + 0.1
    assert_no_early_words(clip_ass_path, words_for_clip, delay=0.1)
```

- [ ] **Step 2: Run** `pytest -m slow tests/e2e -v` — Expected: FAIL before the fixture/script exist.
- [ ] **Step 3: Implement** `setup.ps1`, `README.md` and the fixtures.
- [ ] **Step 4: Run all tests** — `pytest -v` (fast suite) and `pytest -m slow -v` on the desktop — Expected: all PASS.
- [ ] **Step 5: Run `setup.ps1` on the desktop from a clean clone** — Expected: ends with the server reachable at `http://127.0.0.1:8765` after a reboot.
- [ ] **Step 6: Commit** — `git commit -am "feat: setup script, README and end-to-end smoke test"`

---

## Spec coverage check

| Spec section | Task |
|---|---|
| §2 inputs: links, past VODs, uploads | 3, 12 |
| §5.1 login, 30 days, lockout, sign out | 11, 12 |
| §5.2 home, options, queue, finished, retry, cancel | 12 |
| §5.3 results, play, score, delete, download, zip, trim, captions | 13 |
| §6.1 one process, worker, SQLite, resume, re-render priority, long videos | 9, 10 |
| §6.2 ingest, errors, disk check | 3 |
| §6.3 transcription, 4 GB, always runs | 4 |
| §6.4 moments: model, snapping, signals, video type, score, dedupe, selection, unload | 5, 6 |
| §6.5 reframing per type, captions rule, encoding | 2, 7, 8 |
| §6.6 edit, caption, delete, zip, 14-day retention | 10, 13 |
| §6.7 security | 11, 12, 13 |
| §6.8 config, setup, moving to Nitro | 1, 14 |
| §6.9 laptop notes | 14 |
| §7 unit tests, smoke test, early owner check on real content | all; 6 (checkpoint); 14 |
