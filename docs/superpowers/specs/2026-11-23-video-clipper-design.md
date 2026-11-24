# Video Clipper — Design

Date: 2026-11-23
Status: draft, awaiting owner approval

## 1. Intent

A private, self-hosted tool that turns long videos into short vertical clips with captions, similar to what OpusClip does, built from scratch and used by one person (the owner).

**Why:** the owner wants clips from their own long-form content with **no per-use or API cost**. Every step (download, transcription, picking moments, reframing, captions, rendering) runs on hardware they own.

**Success looks like:** the owner pastes a link from their laptop or phone, walks away, and comes back to a page of playable 9:16 clips with captions. They download the good ones as they are, or make a quick trim or caption fix first.

## 2. Goals, non-goals, anti-goals

**Goals**
- Inputs: YouTube links, links to **past** streams (Twitch VODs, Kick, anything yt-dlp supports) and direct file uploads.
- Automatically find the strongest 30–90 second moments and render each as a 1080×1920 clip, framed to suit the video type, with optional burned-in captions.
- A results page where each clip plays in place and has Download, Edit (trim), Caption and Delete, plus Download all.
- Reachable from the owner's other devices (desktop, laptop, phone) behind a single admin login.
- Runs entirely on a home machine, with no paid services.

**Non-goals**
- Clipping **live** streams while they are happening.
- Multiple users, sign-ups, teams or billing.
- Auto-posting to TikTok, YouTube Shorts or Instagram.
- A full timeline editor (B-roll, overlays, music, transitions).
- Hosting on a cloud server.

**Anti-goals** (failure even if everything else works)
- **A caption word appears on screen before it is spoken.** This is a standing rule from the owner (see §6.5).
- Any call to a paid AI or API service in the processing pipeline.
- The website reachable from the public internet without the login, or reachable at all outside the owner's private network.
- Losing a finished clip the owner didn't delete.

## 3. Constraints

- **Content:** the source videos are **16:9** and of three kinds: **movie clips**, **gaming screen recordings** and **talk shows**. Mostly English.
- **Server machine:** Acer Nitro 5 (2021) laptop, Windows, running always-on with the lid closed. NVIDIA graphics card with **4 GB** of memory (the owner will confirm the exact model later), and **16 GB+** of RAM. **Design for 4 GB.** If it turns out to have more, only config settings change.
- **Development machine:** the owner's desktop (RTX 4060 8 GB, 32 GB RAM, i5-14400F, Windows 11). Built and tested here first, then installed on the Nitro without code changes.
- **Already installed on the desktop:** ffmpeg, yt-dlp, Python 3.12/3.13, Node. Nothing else for AI is installed yet.
- **Graphics memory is tight**, so the GPU stages (transcription, the AI model, face detection) must run **one after another**, each releasing the card before the next starts. Never load two at once.
- **Access** from other devices goes through **Tailscale** (a free private network). The app is never exposed to the public internet.
- **One job at a time.** Other jobs wait in a queue.

## 4. Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| **A** | **All Python, one program:** a FastAPI app serves the pages and runs a background job queue in the same process. Server-rendered HTML with a small amount of plain JavaScript. | One language and one process to install and run; the AI libraries are all Python. The UI is hand-built, which is fine for three screens. | **Chosen by the owner.** |
| B | Next.js (React/Tailwind) website + separate Python worker sharing the database | Nicer UI tooling and 21st.dev components, but two languages and two long-running programs on the Nitro, and more ways to break. | Rejected: not worth it for three screens and one user. |
| C | Adapt an existing open-source clipper (ClipsAI and others) | Could save writing the processing step, but the known options call paid APIs or need gated model accounts and look unmaintained. | Rejected: breaks the no-cost rule or costs more time than it saves. |

## 5. Screens (agreed with the owner via mockups)

Mockups: `.superpowers/brainstorm/697-1795422411/content/` (`results-page-v2.html`, `home-and-login.html`).

### 5.1 Login
- A single password field (no username) and a Sign in button.
- Stays signed in on that device for 30 days.
- After 5 wrong attempts, locks out for 15 minutes.
- A Sign out button on every page.

### 5.2 Home
- **Add a video:** a link box with a "Get clips" button, plus a drag-and-drop or browse area for files (MP4, MOV, MKV).
- **Options per job:**
  - **Video type:** Talk show, Gaming, Movie. Default Talk show. This sets how clips are framed (§6.5) and what the moment-picker looks for (§6.4). *(Builder's proposal, added after the mockups. The owner should check it.)*
  - Clip length: Auto (30–90s), Under 30s, 30–60s, 60–90s. Default Auto.
  - **Captions: off by default.** When toggled on, choose a style: Bold White or Yellow Pop. With captions off, no caption work runs at all.
- **Queue:** each running or waiting job shows a thumbnail, title and duration, plus a 4-part progress bar (Downloading → Transcribing → Finding moments → Rendering clips) with a status line ("Step 3 of 4 · Finding the best moments · about 6 min left") and a Cancel button. Progress updates live without reloading the page.
- **Finished:** past jobs, newest first, with date and clip count, and an "Open →" link to Results. Failed jobs show the reason in plain words and a Retry button.

### 5.3 Results (one per source video)
- **Header:** the video title, clip count, source and when it finished, plus a **Download all (N) · .zip** button.
- **A grid of clip cards.** Each card has:
  - A 9:16 video player that plays in place.
  - A 🔥 score (0–100) in the corner.
  - A 🗑 delete button in the opposite corner. It asks for confirmation over the card ("Delete this clip? This can't be undone." with Cancel and Delete).
  - The clip title, its length, and where it starts in the source ("0:48 · from 12:31").
  - Buttons: **⬇ Download** (main action), **✂ Edit**, **Aa Caption**.
- **Edit (trim) panel**, optional: a filmstrip with two drag handles for start and end, showing start, length and end times. "Save & re-render" replaces the clip. Handles may move beyond the clip's original edges, within the source video.
- **Caption panel**, optional:
  - An on/off toggle. A clip made without captions can have them turned on here, and the other way around.
  - When on: the clip's words as clickable chips. Click one to correct its text (timing is kept).
  - When on: style picker, Bold White or Yellow Pop.
  - "Save & re-render".
- Must be usable on a phone screen.

## 6. How it works

### 6.1 Overall shape
One Python program (FastAPI) on the Nitro:
- serves the pages (server-rendered templates plus plain JavaScript; no frontend build step);
- serves clip files with HTTP range support, so videos can seek and play in the browser;
- runs **one background worker** that takes the oldest queued job and moves it through the pipeline.

**Job state lives in SQLite.** On startup, any job left marked as "running" (because of a crash or reboot) goes back to "queued", and resumes from its last completed step. Each step's output is saved to disk, so finished steps aren't redone.

**Re-render requests** from Edit or Caption are small jobs that skip straight to rendering one clip. They go to the front of the queue but **do not interrupt** a running job. The worker picks them up as soon as the current chunk finishes (see below), so the wait is a few minutes at most. *(Builder's call.)*

**Long videos (multi-hour stream VODs) are supported.** Transcription and moment-finding work in ~10-minute chunks, and each chunk's result is saved as it finishes. A reboot resumes from the last saved chunk, not from the start of the step. Before downloading, check that free disk space is at least 3× the expected file size, and fail with a clear message if not. *(Builder's call.)*

### 6.2 Step 1: Download / ingest
- **Links:** yt-dlp downloads the best quality up to 1080p as MP4, with the title and a thumbnail. Failures (private video, removed, geo-blocked, unsupported site) become a plain-English reason on the failed job.
- **Uploads:** the file streams to disk (no size limit beyond disk space), and ffprobe validates it before the job is queued.
- **Storage:** `data/jobs/<job_id>/source.mp4`.

### 6.3 Step 2: Transcription
- faster-whisper, model **large-v3-turbo** at int8 (fits in 4 GB), on the GPU, with word-level timestamps and voice-activity filtering. The language is auto-detected.
- Transcription always runs, even with captions off, because moment-finding needs the transcript.
- The result is saved as `transcript.json` (segments + words with start/end).
- The model is unloaded from the GPU afterwards.

### 6.4 Step 3: Finding moments
Combines a local AI model with simple signals:

1. **Candidates from the AI model.** The transcript (with timestamps) goes in ~10-minute chunks with some overlap to a local model through **Ollama**. The default is a **~3–4B instruct model that fits fully in 4 GB** (the model is a config setting; a 7B model can be used on bigger cards). The model returns JSON: start, end, a short title, a hook score from 0 to 100, and a one-line reason. It is told the target length range and the **video type**: a talk show means strong statements, stories and jokes; gaming means reactions, hype and funny moments; a movie means dramatic or quotable scenes.
   - **Quality is judged with the 4 GB model from day one**, including on the desktop, so the owner never approves clips the Nitro can't reproduce.
2. **Snapping.** Each candidate's start and end move to the nearest sentence or word boundary, so a clip never starts or ends mid-word. Clips outside the length range are adjusted or dropped.
3. **Signals.** Each candidate is also scored on audio energy and loudness changes, speaking pace, and whether it opens with a strong first line (question, number, bold claim). For **Gaming** and **Movie**, audio energy weighs more than the words, because exciting moments there are often loud with little speech (explosions, shouting, music swells).
4. **Final score** = a weighted mix of the AI score and the signals, scaled to 0–100. This is the 🔥 number.
5. **Dedupe.** Overlapping candidates are merged and the higher-scoring one kept.
6. **Selection.** Keep every candidate above a score threshold, roughly 5–15 clips per hour of source, with at least 1 clip for any video longer than 2 minutes.

Ollama is told to unload the model when it finishes (`keep_alive: 0`), to free the GPU for the next step.

### 6.5 Step 4: Rendering
For each selected clip:

- **Reframe from 16:9 to 9:16 (1080×1920), depending on the video type:**
  - **Talk show → follow the speaker.** Sample frames (~5 per second) and detect faces with **MediaPipe**. Pick the main face (the largest; a shortcut until a better "who is speaking" signal is needed). Smooth the crop's horizontal position so it moves gently and doesn't jitter, with a dead zone so small head movements don't move it. With no face, use a center crop.
  - **Movie → follow faces, resetting at shot changes.** Same as Talk show, but detect scene cuts (e.g. PySceneDetect) and jump the crop at each cut instead of sliding across it.
  - **Gaming → fit the whole frame.** Scale the full 16:9 picture to the clip's width, centered, over a blurred and zoomed copy of itself filling the rest of the 9:16 frame. This keeps the HUD and action that a crop would cut off. No face detection.
  - A **stacked layout** (facecam on top, gameplay below) is a **non-goal for now**. It needs the facecam's position, so it's a possible later addition.
- **Captions (only when on for the job or clip).** Build an ASS caption file from the clip's word timestamps, then burn it in with ffmpeg. With captions off, skip this step entirely.
  - **House style:** 2-word chunks in UPPERCASE, bold, positioned in the lower third. Styles: Bold White or Yellow Pop.
  - **Timing rule (non-negotiable):** no word appears before it is spoken. Two adjacent words show together only if the second starts within ~0.45s of the first. Otherwise, show word 1 alone from its start, then add word 2 the moment it is spoken. (The owner's earlier reference, `chunk_words()` in `Rubic+Office/edit/build_ass.py`, no longer exists at that path, so implement it fresh from this rule.)
  - "Spoken" means **Whisper's word start time plus a safety delay** (config setting, default 0.1s), because Whisper's times often run slightly early. This needs no extra GPU step. The end-to-end test checks that every caption event starts at or after its word's start time plus the delay. *(Builder's call.)*
  - Caption corrections from the Caption panel replace a word's text and keep its timing.
- **Encode.** H.264 + AAC MP4, using the NVIDIA hardware encoder when available, with software encoding as a fallback. Also save a poster image for the card.
- **Storage:** `data/jobs/<job_id>/clips/<clip_id>.mp4`.

### 6.6 Edit, Caption, Delete, Download all
- **Edit** re-renders one clip from the source with the new start and end (reframe and captions are recomputed for the new range).
- **Caption** re-renders one clip with captions turned on or off, corrected words, and/or a new style. Reframing can be reused if the range hasn't changed.
- **Delete** removes the clip's file and record. The transcript and other clips stay.
- **Download all** streams a zip of that job's clips.

**Keeping the source video.** Edit and Caption need the source video. It is kept for **14 days** (a config setting) after the job finishes, then deleted to save disk space. After that, Edit and Caption are disabled on that job's cards with a short note, while Download and Delete still work. *(Owner's call to confirm: this replaces an earlier suggestion to delete sources immediately.)*

### 6.7 Login and security
- The password is set by the setup script and stored **only as a hash** (argon2 or bcrypt) in the config file.
- The session is a signed, HttpOnly cookie that lasts 30 days. Changing the password invalidates existing sessions.
- Lockout: 5 failed attempts → 15 minutes, tracked on the server.
- The app listens on **localhost only**. Tailscale (`tailscale serve`) publishes it to the owner's private network over HTTPS. It is never bound to a public interface, and no router ports are opened.
- Every page and file URL (clips, zips, thumbnails) requires the session.
- Uploads are validated (ffprobe must recognize them as video). Links are passed to yt-dlp as arguments, never through a shell.

### 6.8 Config and setup
- **One config file** (`config.toml`):
  - data folder
  - port
  - password hash and session secret
  - Whisper model
  - Ollama model
  - source retention days
  - clips-per-hour threshold
  - caption defaults
- **One setup script** (PowerShell) for both machines. It:
  - creates a Python virtual environment and installs dependencies, including CUDA-enabled libraries;
  - checks for ffmpeg, yt-dlp and the NVIDIA driver;
  - installs Ollama and pulls the configured model;
  - prompts for the admin password and writes the config;
  - registers the app to start at boot (Windows Task Scheduler);
  - prints the Tailscale steps.
- **Moving to the Nitro** = copy the project, run the setup script, adjust model sizes in the config if the card has less memory.

### 6.9 Server-laptop notes (documented in the README, not code)
- Turn on the battery charge limit (NitroSense / Acer Care Center, if the model has it).
- Set closing the lid to "do nothing".
- Keep the vents clear.

## 7. Testing

- **Unit tests:**
  - caption chunking and the timing rule (the anti-goal);
  - boundary snapping and dedupe of moments;
  - login lockout and session expiry;
  - crash-recovery requeueing.
- **One end-to-end smoke test** on a short sample video (2–3 minutes): download or ingest → transcript → at least one clip rendered with captions, with no word displayed before its spoken start time.
- **Early check on the owner's real content:** run one of the owner's own videos through the moment-picker before polishing the UI. The local model's clip choice is the weakest link and should be judged first.

## 8. Open items from the owner

- **Exact Nitro graphics card:** to be confirmed. Designed for 4 GB, and any other size only changes config.
- **Git repository:** the owner will provide one. Until then, nothing is committed (this folder currently sits inside an unrelated repo's `claims-demo` branch).
- **Movie clips and copyright:** clipping is fine for personal use. Posting movie clips publicly can get them taken down. That's the owner's call, not something the tool enforces.

## 9. Left to the builder

- Exact Python libraries beyond those named (templating, zip streaming, password hashing).
- The default Ollama model and prompt wording, as long as it fits in 4 GB of graphics memory and returns valid JSON.
- Signal weights and the score threshold (start simple, tune on the owner's videos).
- How the smoothed crop is applied in ffmpeg (per-segment crops, `sendcmd`, or frame-by-frame).
- How progress and time estimates are computed and pushed to the page (polling or server-sent events).
- Visual styling of the pages, within the agreed layouts.
- Project folder layout and test framework.
