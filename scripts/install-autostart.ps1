# Start SubTalk automatically when this Windows user logs on (Computer B).
# Uses the built-in Task Scheduler - no third-party service wrapper.
# Run once from an elevated PowerShell:  scripts\install-autostart.ps1
# Remove with:                            Unregister-ScheduledTask -TaskName SubTalk -Confirm:$false
$root = Split-Path $PSScriptRoot -Parent
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$root\scripts\start.ps1`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "SubTalk" -Action $action -Trigger $trigger -Settings $settings `
    -Description "SubTalk caption engine (output always starts OFF)" -Force | Out-Null
Write-Host "Registered scheduled task 'SubTalk' (at logon of $env:USERNAME)."
