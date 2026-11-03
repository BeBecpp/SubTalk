# Implementation status

Evidence levels: **mock** (automated, in-process mocks) · **live-API** (real Claude) ·
**HTTP-accepted** (Zoom answered 2xx) · **observed** (seen in a Zoom client) · **human-read**
(read and judged by a person) · **pending** (not executed yet).

## Schedule

| Date | Work | Status |
|---|---|---|
| Tue 3 - Wed 4 Nov | Implementation, mock integration, TR+EN Claude eval (≤ $5), JW terminology, browser tests, offline stress | in progress |
| Thu 5 Nov | Real Zoom TEST meeting, caption API, remote control, failure tests | pending (needs owner) |
| Fri 6 Nov | Dress rehearsal on production equipment | pending (needs owner + Computer B) |
| Sat 7 Nov | Wedding, frozen release | - |

## Done (verified)

| Area | Evidence |
|---|---|
| Zoom caption sender (official seq/retry rules), output state machine, E-STOP, epochs | mock (tests) |
| Glossary v1 loader + validation, citation parser, Turkish guards | mock |
| Claude translator (Turkish), live glossary eval 28 pass / 0 fail / 1 withheld | live-API (1 Nov) |
| Audio ingest, VAD segmenter, ASR worker isolation (crash restart) | mock / scripted ASR |
| Dashboard, Speaker page, auth, lease, restart sign-out | mock + manual browser check (1 Nov) |
| CI on Python 3.12 + 3.13 | GitHub Actions |
| English glossary extension (`resources/glossary_en/`), English book names vs jw.org NWT | source-checked 3 Nov |

## In progress (3 Nov)

- Turkish + English from one recognition event, per-language status, English guards.
- Output modes TR / EN / Bilingual; test-caption command; dashboard previews.
- Persistent $5 live-API test ledger; TR+EN live evaluation; single vs parallel request comparison.
- Zoom probe without the URL on the command line; Thursday test procedure.
- Playwright browser e2e tests; failure-injection + soak harness.

## Pending (needs equipment or people)

- Real Zoom TEST meeting (Thu 5 Nov): HTTP-accepted / observed / human-read levels.
- GPU Whisper benchmark on Computer B (large-v3, turbo, Mongolian fine-tunes): Fri 6 Nov at the latest.
- Real mixer audio through the sender on Computer A.
- Tailscale access from a real phone; LAN access from a second computer.
- Turkish and English human review of translations and glossary terms.
