# Network, access and roles

## Paths

| From | To | How |
|---|---|---|
| Computer A (sender) | B `ws://B:8000/ws/audio` | LAN. Requires the shared `SUBTALK_AUDIO_TOKEN` (production refuses senders without it). |
| Computer A (operator browser) | B `http://B:8000/operator` | LAN - ideally a dedicated switch or cable, not guest Wi-Fi. Set `server.host: 0.0.0.0` and run `scripts\firewall-lan.ps1`. |
| Remote operator / speaker phone | B over **Tailscale** | Private HTTPS, see below. |
| B | Claude API, Zoom caption endpoint | Outbound HTTPS only. |

Never expose port 8000 to the internet: no port forwarding, no Tailscale Funnel.

## Tailscale (remote control over the private network)

1. Install Tailscale on Computer B and on each remote device (operator laptop, speaker phone); sign
   them into your tailnet (or share Computer B with the speaker's account).
2. On Computer B: `tailscale serve --bg 8000` - serves `https://<b-name>.<tailnet>.ts.net/` with a
   valid certificate to tailnet devices only (check `tailscale serve status`). SubTalk can stay on
   `server.host: 127.0.0.1` if only Tailscale and local access are needed.
3. Restrict access in the Tailscale admin ACLs to the devices that need it.
4. Speaker page: `https://<b-name>.<tailnet>.ts.net/speaker`.

Session cookies are marked `Secure` automatically when the request arrives over HTTPS.
If buttons on the remote dashboard fail with "bad origin", add the Tailscale name to
`config/local.yaml`: `server: {allowed_origins: ["<b-name>.<tailnet>.ts.net"]}`. This proxy path
has not been tested yet - check it during the dress rehearsal.

## Roles

| | Operator | Speaker |
|---|---|---|
| Start/stop engine, connect Zoom, go LIVE, pause, resume, stop | yes (with control lease) | no |
| E-STOP | yes (any operator, no lease needed) | no |
| Main Speech start/end | yes | yes, unless the operator disables the speaker page |
| See tokens, config, diagnostics | yes (tokens are never shown) | no - status only |

Only one operator browser holds the **control lease**. Others are read-only until they press
*Take control*. A speaker disconnecting, or any browser closing, never changes the engine.

## Sessions without a database

Passcodes are stored as scrypt hashes in `secrets/auth.json` (`subtalk set-passcode`). Session
cookies are HMAC-signed with a key generated at process start, so **every engine restart signs
everyone out** and output always comes back OFF. Dashboards detect a restart or a lost connection
and lock their controls until fresh state arrives.
