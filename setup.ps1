<#
Video Clipper setup. Safe to re-run. PowerShell 5.1.
  -SkipTask          do not register the "Video Clipper" scheduled task
  -ResetPassword     ask for a new admin password even if config.toml exists
  -PasswordFromEnv   read the admin password from $env:CLIPPER_ADMIN_PASSWORD (non-interactive)
#>
param([switch]$SkipTask, [switch]$ResetPassword, [switch]$PasswordFromEnv)
$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot
Set-Location $Repo

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Winget-Install($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "winget is missing; install '$id' by hand and re-run." }
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
function Find-Ollama {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $p) { return $p }
    return $null
}
function Run-Native { # run an exe and stop the script if it fails
    & $args[0] @($args[1..($args.Count - 1)])
    if ($LASTEXITCODE -ne 0) { throw "failed: $($args -join ' ')" }
}
function Has-Python312 {
    try { return [bool]((& py -0 | Out-String) -match '3\.12') } catch { return $false }
}

Step 'Python 3.12'
if (-not (Has-Python312)) { Winget-Install 'Python.Python.3.12' }
if (-not (Has-Python312)) { throw 'Python 3.12 is still not available; open a new PowerShell window and re-run.' }

Step 'NVIDIA driver'
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { throw 'nvidia-smi not found. Install the NVIDIA driver, then re-run.' }
Run-Native nvidia-smi -L

Step 'ffmpeg'
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Winget-Install 'Gyan.FFmpeg' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'ffmpeg installed but not on PATH yet; open a new PowerShell window and re-run.' }

Step 'Virtualenv and packages'
$Py = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { Run-Native py -3.12 -m venv (Join-Path $Repo '.venv') }
Run-Native $Py -m pip install --upgrade pip
Run-Native $Py -m pip install -e .

Step 'GPU check (faster-whisper)'
Run-Native $Py -c "import ctranslate2, sys; n = ctranslate2.get_cuda_device_count(); print('CUDA devices:', n); sys.exit(0 if n >= 1 else 1)"

Step 'Ollama'
$Ollama = Find-Ollama
if (-not $Ollama) { Winget-Install 'Ollama.Ollama'; $Ollama = Find-Ollama }
if (-not $Ollama) { throw 'Ollama installed but not found; open a new PowerShell window and re-run.' }

Step 'Config'
$Config = Join-Path $Repo 'config.toml'
if ((Test-Path $Config) -and -not $ResetPassword) {
    Write-Host 'config.toml exists; keeping it (use -ResetPassword to change the password).'
} else {
    if ($PasswordFromEnv) {
        $plain = $env:CLIPPER_ADMIN_PASSWORD
        Remove-Item Env:CLIPPER_ADMIN_PASSWORD -ErrorAction SilentlyContinue  # do not leave it in this process's environment
        if (-not $plain) { throw 'CLIPPER_ADMIN_PASSWORD is empty.' }
    } else {
        $a = Read-Host 'Choose the admin password' -AsSecureString
        $b = Read-Host 'Type it again' -AsSecureString
        $pa = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($a)
        $pb = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($b)
        try {
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pa)
            if ($plain -ne [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pb)) { throw 'The passwords did not match.' }
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pa); [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pb)
        }
        if (-not $plain) { throw 'The password is empty.' }
    }
    # the password goes in on stdin as UTF-8 bytes (PS 5.1 would pipe ASCII and turn non-ASCII into '?'), never on a command line
    $OutputEncoding = New-Object Text.UTF8Encoding $false
    $hash = ($plain | & $Py -c "import sys; from clipper.auth import hash_password; print(hash_password(sys.stdin.buffer.read().rstrip(b'
').decode('utf-8')))").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $hash) { throw 'Could not hash the password.' }
    $secret = (& $Py -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    $plain = $null

    if (Test-Path $Config) {  # -ResetPassword: keep the owner's other settings
        $text = [IO.File]::ReadAllText($Config, (New-Object Text.UTF8Encoding $false))
    } else {
        $text = [IO.File]::ReadAllText((Join-Path $Repo 'config.example.toml'), (New-Object Text.UTF8Encoding $false))
        $dataDir = (Join-Path $Repo 'data') -replace '\\', '/'
        $text = $text -replace '(?m)^data_dir\s*=\s*"[^"]*"', ('data_dir = "' + $dataDir + '"')
    }
    # '$' is special in a -replace replacement and argon2 hashes are full of it
    $text = $text -replace '(?m)^password_hash\s*=\s*"[^"]*"', ('password_hash = "' + $hash.Replace('$', '$$') + '"')
    $text = $text -replace '(?m)^session_secret\s*=\s*"[^"]*"', ('session_secret = "' + $secret + '"')
    [IO.File]::WriteAllText($Config, $text, (New-Object Text.UTF8Encoding $false))
    Write-Host "Wrote $Config"
}
$model = 'qwen3:4b'
$m = Select-String -Path $Config -Pattern '^ollama_model\s*=\s*"([^"]+)"'
if ($m) { $model = $m.Matches[0].Groups[1].Value }

Step "Ollama model $model"
Run-Native $Ollama pull $model

Step 'Verify config'
Run-Native $Py -c "from pathlib import Path; from clipper.config import load_settings; s = load_settings(Path(r'$Config')); print('config ok, data in', s.data_dir, 'port', s.port)"

if ($SkipTask) {
    Write-Host "`nSkipping the scheduled task (-SkipTask)."
} else {
    Step 'Scheduled tasks'
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw 'Registering the task needs an elevated PowerShell (Run as administrator), or re-run with -SkipTask.' }
    $action = New-ScheduledTaskAction -Execute $Py -Argument "-m clipper.cli serve --config `"$Config`"" -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    # S4U: runs whether or not the user is logged on, without storing a password
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName 'Video Clipper' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    # Ollama must also be up at boot, or moment-finding fails until someone logs in
    $oAction = New-ScheduledTaskAction -Execute $Ollama -Argument 'serve'
    Register-ScheduledTask -TaskName 'Video Clipper Ollama' -Action $oAction -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host 'Registered "Video Clipper Ollama" and "Video Clipper"; both start at boot. Start now with: Start-ScheduledTask "Video Clipper Ollama"; Start-ScheduledTask "Video Clipper"'
}

Write-Host @"

Done. Next steps:
  1. Start now:  .venv\Scripts\python -m clipper.cli serve     (or: Start-ScheduledTask "Video Clipper")
  2. Open:       http://127.0.0.1:8765
  3. Other devices (Tailscale):
       winget install Tailscale.Tailscale     (then sign in)
       tailscale serve --bg 8765
     then open https://<machine>.<tailnet>.ts.net from your phone or laptop.
"@
