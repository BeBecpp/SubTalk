# Event-day runbook - Saturday 7 November 2026, ceremony 16:00 (Ulaanbaatar)

SubTalk can fail completely and the wedding continues untouched. When in doubt: **E-STOP**.

## Before the ceremony

| Time | Step |
|---|---|
| 13:00 | Computer B on, `scripts\start.ps1` (or autostart). `subtalk doctor` all PASS. |
| 13:15 | Computer A: start the sender. Dashboard: Audio chip green, level moves when someone speaks. |
| 13:30 | Sign in on the dashboard (Computer A) and, if used, the Speaker page (phone via Tailscale). |
| 14:30 | Quick test in the **TEST meeting**: connect (TEST), go LIVE, speak, see Turkish, Stop, disconnect. |
| 15:00 | `config/local.yaml`: `zoom.allow_event_target: true`, restart SubTalk, sign in again. |
| 15:15 | Diagnostics ▸ **Warm Claude**. |
| 15:20 | Wedding meeting host: Captions ▸ Set up manual captioner ▸ Copy the API token → Dashboard ▸ Connect ▸ **Wedding (EVENT)** ▸ tick confirmation. Header shows WEDDING badge. Not live yet. |
| 15:30 | **GO / NO-GO** (below). |
| 15:45 | Hold **go LIVE**. Phrase deck ▸ *Welcome / captions start*. |

GO only if: Audio, Whisper, Claude, Zoom chips are green; Zoom caption number is known; the
welcome caption appeared in the meeting (ask a Turkish guest or check a second device).

## During the ceremony

* Songs, processional, long music: switch **Music / ceremony mode** on; optionally send *[İlahi]* / *[Müzik]*. Switch it off when speech resumes.
* Father's interpreted talk: **Main Speech** on (or the speaker page). It never stops normal translation.
* Someone speaks Japanese on their own (not interpreting): Japanese ▸ **All**, back to **Auto** afterwards.
* Watch the verdict pill: CAPTIONING / LISTENING are fine. Anything red: see below.

## When something goes wrong

| Symptom | Do this | Never |
|---|---|---|
| Wrong or inappropriate captions appearing | **E-STOP** (one click). It cannot remove a caption Zoom already showed. Fix the cause, hold reset, go LIVE again. | Keep going hoping it improves |
| Verdict NOT SENDING, Zoom chip red | Check the meeting is running. If the token changed (host rejoined): Stop → Connect with the new URL → go LIVE. Output auto-pauses after 3 rejected captions. | Paste a URL from anywhere but the host's Zoom menu |
| "Caption number unknown" | Diagnostics ▸ **Re-read seq**. | Guess a number |
| Audio chip red / "No audio" | Check the sender window on Computer A and the mixer input. The sender reconnects by itself. | Change Zoom's microphone settings |
| Claude chip red | Internet on Computer B? Captions stop (no guessing). Use the phrase deck (*Captions paused*). | |
| Whisper chip red | The worker restarts by itself (about 10-60 s); after repeated crashes it switches to the fallback model. | |
| Japanese repeats appear as duplicates | Japanese ▸ **Off** during the interpreted talk, or Diagnostics ▸ Auto matching off. | |
| Dashboard shows "Connection lost" | The engine keeps running. Refresh the browser. | Assume output stopped |
| "Engine restarted" | Output is OFF. Sign in, check chips, Connect (if needed), hold go LIVE. | |
| Total failure | Phrase deck if possible (*Captions paused*), otherwise nothing - the ceremony continues. | Touch Zoom/camera settings for SubTalk |

## After the ceremony

Stop → Disconnect. Logs (`logs\subtalk.jsonl`) contain transcripts: keep them private, delete when
no longer needed.
