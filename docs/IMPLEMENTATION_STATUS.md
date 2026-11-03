# Implementation status

Evidence levels: **mock** (automated, in-process mocks) · **local-relay** (real Worker code under
`wrangler dev`/workerd on this PC) · **live-API** (real Claude) · **HTTP-accepted** (Zoom answered
2xx) · **observed** (seen in a Zoom client) · **human-read** (read and judged by a person) ·
**pending** (not executed yet).

## Schedule

| Date | Work | Status |
|---|---|---|
| Tue 3 - Wed 4 Nov | Implementation, mock integration, TR+EN Claude eval (≤ $5), JW terminology, browser tests, offline stress | in progress |
| Thu 5 Nov | Real Zoom TEST meeting, caption API, remote control, failure tests, guests' viewer on phones | pending (needs owner) |
| Fri 6 Nov | Dress rehearsal on production equipment (incl. deployed viewer relay) | pending (needs owner + Computer B) |
| Sat 7 Nov | Wedding, frozen release | - |

## Done (verified)

| Area | Evidence |
|---|---|
| Zoom caption sender (official seq/retry rules), output state machine, E-STOP, epochs | mock |
| Glossary v1 + English extension, citation parser, TR + EN guards | mock + source-checked (jw.org NWT) |
| Turkish + English from one recognition event, per-language status | mock + live-API (37 cases, 0 FAIL, 3 Nov) |
| Output modes TR / EN / BILINGUAL, test caption, dashboard previews | mock + browser e2e |
| Persistent $5 test ledger | mock + used live ($0.2595) |
| Zoom probe (URL never on the command line / in reports) | mock over real HTTP |
| Dashboard, Speaker page, auth, lease, restart sign-out | browser e2e (Edge) |
| **Guest viewer** (TR/EN per guest), output-gate publishing, E-STOP clears guests | mock + browser e2e |
| **Cloudflare relay** (Worker + Durable Object), auth, read-only guests, reconnect | local-relay + browser e2e |
| Relay fan-out to 1,000 sockets (30,000/30,000) | local-relay only (not Cloudflare production) |
| CI on Python 3.12 + 3.13 | GitHub Actions |

## In progress / next (4 Nov)

- Failure-injection + soak harness (mock).
- Relay deployment + external load test (needs owner's Cloudflare login).

## Pending (needs equipment or people)

- Real Zoom TEST meeting (Thu 5 Nov): HTTP-accepted / observed / human-read levels.
- Deployed relay load test from outside networks; guests' phones in the rehearsal.
- GPU Whisper benchmark on Computer B: Fri 6 Nov at the latest.
- Real mixer audio through the sender on Computer A.
- Tailscale access from a real phone; LAN access from a second computer.
- Turkish and English human review of translations, glossary terms, viewer UI and phrase deck.
