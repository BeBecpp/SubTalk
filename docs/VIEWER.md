# Guest caption viewer (Turkish / English chosen per guest)

Owner decision 3 Nov 2026 (D-013). Zoom manual captions remain the validated fallback.

## Why a viewer

Zoom's Manual Caption API has one caption stream per caption URL: every attendee sees the same
text. Independent language choice needs a separate page. SubTalk sends each caption event once
to a relay; each guest's browser shows only its chosen language.

## Paths

| Path | Who | Transport | Exposed to the internet |
|---|---|---|---|
| Zoom manual captions (TR / EN / BILINGUAL) | every Zoom attendee | Zoom | n/a (Zoom) |
| Public viewer `https://<worker>.workers.dev/` | guests anywhere | Cloudflare Worker + Durable Object | only the read-only relay |
| Local viewer `http://<computer-b>:8000/viewer` | venue LAN / Tailscale | engine | no |

Computer B makes **outbound** HTTPS calls only. Operator dashboard, speaker page, audio ingest
and the Zoom caption URL stay on LAN/Tailscale. The relay has no route to any of them.

## Safety: the same gate as Zoom

- A caption reaches guests only from `OutputController.submit` after the LIVE / epoch /
  before-LIVE / TTL checks pass, with each language that passed its own guards.
- Withheld / failed / timed-out languages are never shown, and never replaced by the other language.
- Pause: nothing new is published; guests see "Captions paused".
- E-STOP: status "stopped" plus a `clear` message: **captions disappear from guest screens**.
  (Text Zoom has already accepted cannot be undone.)
- Operator TEST captions never go to the viewer.
- Engine restart: output starts DISCONNECTED. Guests see "not on air" / "source unreachable".
  Nothing resumes by itself.
- If Zoom fails 3x in a row while the viewer is on, only Zoom is suspended (dashboard alert
  `zoom_suspended`, Zoom chip red). Re-read seq, or Pause→Resume, retries Zoom.

## Guest page behaviour

- Language: Türkçe / English buttons, remembered per device; `?lang=tr|en` in the link.
- Large text (A− / A+), latest caption largest at the bottom, last 12 shown, dark/light by OS.
- No horizontal scroll at 375 px. Screen kept awake on supported phones after the first tap.
- Status line: Live · Captions paused · Not on air · Connecting · Reconnecting (socket lost)
  · Caption source unreachable (no engine message for 45 s: engine sends a heartbeat every 15 s).
- Reconnect: jittered backoff 0.8 → 15 s; a socket silent for 50 s is closed and reopened.
- Footer: "translated automatically by AI; mistakes are possible".
- Turkish UI strings: **review_required** by a Turkish speaker (same as the phrase deck).

## Sync

Each message carries `seq` (engine-side counter), `event_id` (SubTalk speech event) and `t`
(engine wall time when it passed the gate), plus `speech_end`. TR and EN of one event are in the
SAME message, so two guests can never see different events for the same moment.

## Verified (3 Nov, local only)

| Test | Result |
|---|---|
| Unit: gate, status, E-STOP clear, Zoom-suspend, phrases, relay retry/stale/heartbeat (`tests/test_viewer.py`) | 17 passed |
| Server: local `/ws/viewer` read-only, ping/pong, live→caption→E-STOP | passed |
| Pipeline: TR withheld + Zoom mode TR → Zoom gets nothing, viewer gets EN only | passed |
| Browser (Edge): 2 guests, separate sessions, TR (mobile 375 px) + EN, same events, E-STOP clears both | passed |
| Browser: relay killed + restarted → guest page reconnects by itself, captions resume | passed |
| Relay: bad token 401, admin paths 404, strict CSP (no eval, no framing) | passed |
| Load, local workerd: 1,000 sockets, 30 captions @ 2 s | 30,000/30,000 delivered, p50 309 ms, p95 1.09 s, max 1.67 s |

The load number is **local only** (`wrangler dev`, 1,000 Python clients on the same laptop).
It shows the relay code fans out to 1,000 sockets without loss. It does NOT show Cloudflare
production capacity, mobile networks, or the venue uplink. **Not verified until the deployed
URL is load-tested** (relay/README.md, step 5) and checked in the Friday rehearsal.

## Zoom native translated captions (evaluation)

- They translate Zoom's **own automated captions** (Zoom speech recognition). Zoom documents
  manual captions and automated captions as alternatives, so they cannot translate SubTalk's
  manual feed (sources: vendor and university guides; they disagree on details, so verify in Zoom
  settings on Thursday if wanted).
- Need Business Plus / Enterprise or the Translated Captions add-on (paid).
- Mongolian source speech is not known to be supported, so they are useless for the main speech.
- Possible use: Japanese-only stretches *instead of* SubTalk captions, which means turning SubTalk
  manual captions off in Zoom during that time. Not planned for 7 Nov: operator switching
  between captioning systems mid-ceremony is riskier than SubTalk's own JA→TR/EN path.

## Rollback

The viewer fails rehearsal → `viewer.enabled: false` (or `relay_url: ""`), restart SubTalk,
use Zoom manual captions (TR, EN or BILINGUAL). Nothing else changes.
