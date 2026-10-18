# Run llm-gateway as the rig controller on a Windows machine (no systemd).
#
#   powershell -ExecutionPolicy Bypass -File deploy\run_windows.ps1
#
# Needs (see docs/MANAGER.md, "Two layers"):
#   - ssh aliases `rig` (commands) and `rig-llm` (forwards the rig's model ports 11434-11441 here);
#     this script opens the rig-llm forward if it is not already up;
#   - only one controller at a time: stop the Pi's gateway first (`ssh pi sudo systemctl stop pi-gateway`).
#     Both would act as owner `llm-gateway` on the rig's lease record.
#   - a venv: py -3 -m venv .venv; .venv\Scripts\python.exe -m pip install -e .[dev]
# Windows OpenSSH has no ControlMaster, so each rig call opens a new ssh connection (about 0.5 s).

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$forwarded = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue
if (-not $forwarded) {
    Write-Host "opening the rig model-port forward (ssh -N rig-llm)"
    Start-Process -FilePath ssh -ArgumentList "-N", "rig-llm" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

$env:PYTHONPATH = Join-Path $repo "src"
$env:RIG_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 rig /usr/local/bin/rig"
$env:PROFILES_PATH = Join-Path $repo "remote_profiles.json"
$env:UPSTREAM_HOST = "127.0.0.1"
$env:MODEL_PREFIX = "gpu-rig"
$env:GATEWAY_BIND = "127.0.0.1"
$env:GATEWAY_PORT = "8080"
$env:RIG_MANAGER = "on"
$env:START_TIMEOUT_SECONDS = "900"
$env:REQUEST_TIMEOUT_SECONDS = "900"

& (Join-Path $repo ".venv\Scripts\python.exe") -m llm_gateway
