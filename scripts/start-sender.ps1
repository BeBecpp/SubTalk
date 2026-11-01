# Audio sender for Computer A. Reads the mixer input read-only (shared mode); Zoom keeps working.
#   scripts\start-sender.ps1 -List
#   scripts\start-sender.ps1 -Engine ws://192.168.1.50:8000/ws/audio -Device "Mixer"
param([switch]$List, [string]$Engine = $env:SUBTALK_ENGINE_WS, [string]$Device = $env:SUBTALK_AUDIO_DEVICE, [int]$Channel = -1)
Set-Location (Split-Path $PSScriptRoot -Parent)
if (Test-Path .env) {
    Get-Content .env | Where-Object { $_ -match '^\s*SUBTALK_AUDIO_TOKEN=(.+)$' } | ForEach-Object { $env:SUBTALK_AUDIO_TOKEN = $Matches[1].Trim() }
}
$py = ".\.venv\Scripts\python.exe"
if ($List) { & $py sender\subtalk_sender.py --list-devices; exit }
if (-not $Engine) { Write-Error "Pass -Engine ws://<Computer-B-IP>:8000/ws/audio"; exit 1 }
$senderArgs = @("sender\subtalk_sender.py", "--engine", $Engine)
if ($Device) { $senderArgs += @("--device", $Device) }
if ($Channel -ge 0) { $senderArgs += @("--channel", "$Channel") }
while ($true) {
    & $py @senderArgs
    Write-Warning "Sender stopped; restarting in 3 s (Ctrl+C to quit)."
    Start-Sleep -Seconds 3
}
