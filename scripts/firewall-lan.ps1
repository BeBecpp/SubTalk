# Allow the SubTalk dashboard/audio port from the local subnet only (Computer B, elevated PowerShell).
param([int]$Port = 8000)
New-NetFirewallRule -DisplayName "SubTalk (LAN only)" -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress LocalSubnet -Action Allow -Profile Private,Domain | Out-Null
Write-Host "Inbound TCP $Port allowed from LocalSubnet on Private/Domain networks."
Write-Host "Make sure the venue network is set to 'Private' in Windows, or use Tailscale (docs/NETWORK.md)."
