# One-command start for the SubTalk engine + dashboard.
# Supervises the process: if the engine exits unexpectedly it is restarted after 3 s.
# A restart always comes back with caption output OFF and requires signing in again.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$exe = ".\.venv\Scripts\subtalk.exe"
if (-not (Test-Path $exe)) { Write-Error "Not installed - run scripts\install.ps1 first"; exit 1 }

while ($true) {
    $started = Get-Date
    & $exe start
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq 2) { break }   # clean stop or configuration error: do not loop
    Write-Warning "SubTalk exited with code $code at $(Get-Date -Format T); restarting in 3 s (Ctrl+C to stop)."
    if (((Get-Date) - $started).TotalSeconds -lt 10) { Start-Sleep -Seconds 10 } else { Start-Sleep -Seconds 3 }
}
