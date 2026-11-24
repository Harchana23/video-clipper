# Video Clipper

Turns long videos (links, past streams, uploads) into short vertical clips, entirely on your own PC. No cloud, no paid API.
Whisper transcribes, a local Ollama model picks the best moments, ffmpeg cuts and reframes to 1080x1920.

## Set up

In PowerShell, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

It installs what is missing (Python 3.12, ffmpeg, Ollama), builds `.venv`, pulls the model, asks for the admin password, writes `config.toml`, and registers the "Video Clipper" scheduled task (needs an administrator PowerShell; add `-SkipTask` to skip it). Re-running is safe. `-ResetPassword` changes the password.

## Start and stop

- Manually: `.venv\Scripts\python -m clipper.cli serve`, then open http://127.0.0.1:8765. Stop with Ctrl+C.
- Scheduled task (starts at boot): `Start-ScheduledTask "Video Clipper"` / `Stop-ScheduledTask "Video Clipper"`.
  Remove it with `Unregister-ScheduledTask "Video Clipper" -Confirm:$false`.

The server only listens on 127.0.0.1. Logs for failed jobs are in `<data_dir>\logs\worker.log`.

## Use it from your phone or another PC (Tailscale)

Install Tailscale on this PC and on the other device, sign in to the same account, then on this PC:

```powershell
tailscale serve --bg 8765
```

Open `https://<machine>.<tailnet>.ts.net` from the other device and sign in. Only your devices can reach it.

## Moving to the Acer Nitro 5

Copy this folder (or `git clone` it), run `setup.ps1`, and you are done. The Nitro has a 4 GB GPU, so the defaults already fit.
If something runs out of memory, edit `config.toml`: use a smaller `whisper_model` (for example `small`) or a smaller `ollama_model`.

Laptop notes:
- Limit charging to 80% in NitroSense / Care Center (a laptop left plugged in 24/7 wears its battery out).
- Set "when I close the lid" to **Do nothing** (Control Panel > Power Options), so it keeps working with the lid shut.
- Keep the vents clear and the laptop on a hard surface; long jobs run the GPU hot.

## A note on movie clips

Posting clips from movies or other people's streams publicly can get them taken down or the account struck. Your own content is safe; for the rest, keep it private or check the rules of where you post.
