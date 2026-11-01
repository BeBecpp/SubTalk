# SubTalk installer (Windows). Run from the repository folder:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1            # development laptop
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Engine    # Computer B (GPU, Whisper)
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Sender    # Computer A (audio sender only)
param([switch]$Engine, [switch]$Sender)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "Python 3.11+ is required (https://www.python.org/downloads/)." }
$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.11") { throw "Python $ver found; 3.11 or newer is required." }

if (-not (Test-Path .venv)) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip

if ($Sender) {
    # Computer A stays lightweight: only numpy, sounddevice, websockets.
    & .\.venv\Scripts\python.exe -m pip install --quiet -r sender\requirements.txt
} else {
    & .\.venv\Scripts\python.exe -m pip install --quiet uv
    $extras = @("--extra", "dev")
    if ($Engine) { $extras += @("--extra", "asr") }
    $env:VIRTUAL_ENV = (Resolve-Path .venv).Path
    & .\.venv\Scripts\python.exe -m uv sync --frozen @extras
}
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed" }

foreach ($d in "state", "logs", "secrets", "event", "models") { New-Item -ItemType Directory -Force $d | Out-Null }
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env - fill in the keys." }

if ($Engine) {
    Write-Host "`nNext steps on Computer B:" -ForegroundColor Yellow
    Write-Host "  1. Copy config\production.example.yaml to config\local.yaml and review it"
    Write-Host "  2. Put ANTHROPIC_API_KEY and SUBTALK_AUDIO_TOKEN in .env"
    Write-Host "  3. .\.venv\Scripts\subtalk.exe set-passcode --role operator   (and --role speaker)"
    Write-Host "  4. .\.venv\Scripts\subtalk.exe doctor    (downloads the Whisper model on first run)"
    Write-Host "  5. scripts\start.ps1"
} elseif ($Sender) {
    Write-Host "`nSender ready: scripts\start-sender.ps1 -List   then   scripts\start-sender.ps1 -Engine ws://<B-IP>:8000/ws/audio -Device '<name>'"
} else {
    Write-Host "`nDevelopment setup done. Run: scripts\start.ps1   then open http://127.0.0.1:8000/operator (passcode dev-operator)"
}
