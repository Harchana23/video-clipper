# Video Clipper

Turns long videos (links, past streams, uploads) into short vertical clips, entirely on your own PC. No cloud, no paid API.
Whisper transcribes, a local Ollama model picks the best moments, ffmpeg cuts and reframes to 1080x1920.

## Set up

In PowerShell, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

It installs what is missing (Python 3.12, ffmpeg, Ollama), builds `.venv`, pulls the model, asks for the admin password, writes `config.toml`, and registers two scheduled tasks, "Video Clipper Ollama" and "Video Clipper", that both start at boot (needs an elevated PowerShell; add `-SkipTask` to skip them). The tasks run whether or not you are logged in, and no Windows password is stored (S4U logon). Re-running is safe. `-ResetPassword` changes the password.

## Start and stop

- Manually: `.venv\Scripts\python -m clipper.cli serve`, then open http://127.0.0.1:8765. Stop with Ctrl+C.
- Scheduled tasks (start at boot): `Start-ScheduledTask "Video Clipper Ollama"; Start-ScheduledTask "Video Clipper"`, stop with `Stop-ScheduledTask`.
  Remove them with `Unregister-ScheduledTask "Video Clipper", "Video Clipper Ollama" -Confirm:$false`.

The server only listens on 127.0.0.1. Logs for failed jobs are in `<data_dir>\logs\worker.log`.

## Use it from your phone or another PC (Tailscale)

Install Tailscale on this PC and on the other device, sign in to the same account, then on this PC:

```powershell
tailscale serve --bg 8765
```

Open `https://<machine>.<tailnet>.ts.net` from the other device and sign in. Only your devices can reach it.

Keep `cookie_secure = true` in `config.toml` (Tailscale serve is HTTPS). For plain-http local use, Chrome and Edge accept the secure cookie on 127.0.0.1; in any other browser set `cookie_secure = false`.

## Use it from anywhere with your own address (Cloudflare Tunnel)

This gives Clipper a public HTTPS address such as `https://clips.yourdomain.com`, with no ports opened on your router. Do every step **on the PC that runs Clipper**: the tunnel points at `127.0.0.1:8765` on that same machine.

You need a domain whose DNS is managed by Cloudflare (Cloudflare Registrar sells them at cost). The tunnel itself is free.

1. **Create the tunnel.** At dash.cloudflare.com go to **Zero Trust > Networks > Tunnels > Create a tunnel**, choose **Cloudflared**, name it `video-clipper`, and save. On the install screen pick **Windows** and keep the page open; it shows a command with a long token.
2. **Install the connector.** In an **elevated** PowerShell:
   ```powershell
   winget install --id Cloudflare.cloudflared -e
   ```
   Open a new elevated PowerShell, then paste the command from the dashboard. It looks like `cloudflared.exe service install <token>`. This installs a Windows service that starts at boot and reconnects by itself. The dashboard should show the connector as **Healthy**.
   Treat the token like a password: don't save it in this folder or commit it.
3. **Point your address at Clipper.** In the tunnel, open **Public Hostname > Add a public hostname**:
   - Subdomain `clips`, Domain `yourdomain.com`
   - Type `HTTP`, URL `127.0.0.1:8765`

   Save; Cloudflare creates the DNS record.
4. **Add Cloudflare Access (recommended).** The address is reachable from the whole internet. Without this, anyone who finds it sees the login page and can lock you out for 15 minutes with 5 wrong guesses. In **Zero Trust > Access > Applications > Add an application > Self-hosted**:
   - Name `Video Clipper`, Domain `clips.yourdomain.com`
   - Policy: Action **Allow**, Include **Emails**, your own email address

   Visitors then get an email code first, before Clipper's own login.

Keep `cookie_secure = true`; Cloudflare serves HTTPS.

**Limits:**
- Uploads through the tunnel are capped at **100 MB** by Cloudflare's free plan; bigger files fail with error 413. Paste links instead (Clipper downloads them itself), or upload on the server PC at http://127.0.0.1:8765 or over Tailscale.
- Playing and downloading clips is not limited.

**Turn it off:** `cloudflared.exe service uninstall` (elevated), then delete the tunnel in the dashboard.

## Full setup on a new server PC (for example the Acer Nitro 5)

1. Check the NVIDIA driver: `nvidia-smi` should print the graphics card's name.
2. Install Git, then open a new PowerShell:
   ```powershell
   winget install --id Git.Git -e
   ```
3. Get the code:
   ```powershell
   git clone https://github.com/Harchana23/video-clipper.git C:\VideoClipper
   ```
4. Run setup in an **elevated** PowerShell. It asks for the admin password; this takes a while the first time because of the model downloads.
   ```powershell
   cd C:\VideoClipper
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```
5. Start it:
   ```powershell
   Start-ScheduledTask "Video Clipper Ollama"; Start-ScheduledTask "Video Clipper"
   ```
6. Optional: set up the Cloudflare Tunnel (above) or Tailscale.
7. Run the checks in **Test that it works** below.

The Nitro has a 4 GB GPU, and the defaults already fit it. If something runs out of memory, edit `config.toml`: use a smaller `whisper_model` (for example `small`) or a smaller `ollama_model`, then restart the "Video Clipper" task.

To update later: stop the "Video Clipper" task, run `git -C C:\VideoClipper pull`, re-run `setup.ps1` (safe), then start the task again.

Laptop notes:
- Limit charging to 80% in NitroSense / Care Center (a laptop left plugged in 24/7 wears its battery out).
- Set "when I close the lid" to **Do nothing** and sleep to **Never** when plugged in (Control Panel > Power Options), so it keeps working with the lid shut.
- Keep the vents clear and the laptop on a hard surface; long jobs run the GPU hot.

## Test that it works

Run these in order. Each one assumes the one before it passed.

1. **Services are running.** On the server PC:
   ```powershell
   Get-ScheduledTask "Video Clipper", "Video Clipper Ollama" | Select-Object TaskName, State
   ```
   Both should say `Running`.
2. **Local page.** On the server PC open http://127.0.0.1:8765, sign in with the admin password, and the Home page appears.
3. **A real job.** Paste a link to a short video (2-10 minutes) with clear speech. Pick Talk show and Auto length, and press **Get clips**. The progress bar should go through all 4 steps, and the job should appear under Finished with at least one clip. Open it, play a clip, and press Download.
4. **Edit tools.** On a clip, drag a trim handle and press **Save & re-render**. The card shows "Re-rendering…", then the new length. Turn captions on in **Aa Caption** and save; the clip comes back with captions.
5. **Remote address.** On your phone, with Wi-Fi off (mobile data only), open `https://clips.yourdomain.com`. If you set up Access, you get the email-code page first, then Clipper's login. Sign in and see the same Home page.
6. **Access blocks strangers.** In a private browser window, enter an email address that is *not* on the policy. Access should refuse it.
7. **Survives a reboot.** Restart the server PC and don't log in. After a couple of minutes the remote address should load again.

**If something fails:**

| What you see | Likely cause | Fix |
|---|---|---|
| Cloudflare page "Error 1033" | The tunnel connector isn't running | Check the "cloudflared" service in services.msc; the dashboard shows it as Down |
| Cloudflare "502 Bad Gateway" | The tunnel is up but Clipper isn't | `Start-ScheduledTask "Video Clipper"`; check http://127.0.0.1:8765 on the server |
| Job fails at step 3 with "Ollama is not reachable" | The Ollama task isn't running | `Start-ScheduledTask "Video Clipper Ollama"` |
| Upload fails with 413 | Over the 100 MB Cloudflare limit | Paste a link, or upload on the server PC |
| Anything else | | `<data_dir>\logs\worker.log` on the server PC |

## If downloads start failing

Video sites change often, and the downloader (yt-dlp) needs updates to keep up. Stop Clipper, then run this in the Clipper folder:

```
.venv\Scripts\python -m pip install -U yt-dlp
```

Start Clipper again and press Retry on the failed job.

## A note on movie clips

Posting clips from movies or other people's streams publicly can get them taken down or the account struck. Your own content is safe; for the rest, keep it private or check the rules of where you post.
