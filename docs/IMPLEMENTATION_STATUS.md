# Implementation status

Evidence levels: **mock** (automated, in-process mocks) · **live-API** (real Claude) ·
**HTTP-accepted** (Zoom answered 2xx) · **observed** (seen in a Zoom client) · **human-read**
(read and judged by a person) · **pending** (not executed yet).

Production path (owner, 3 Nov): audio → local Whisper → Claude TR + EN (one request) → guards →
bilingual composer → Zoom manual caption API. Guest viewer / Cloudflare relay: disabled, experimental.

## Schedule

| Date | Work | Status |
|---|---|---|
| Tue 3 - Wed 4 Nov | Implementation, mock integration, TR+EN Claude eval (≤ $5), JW terminology, browser tests, offline stress | in progress (failure/soak harness left) |
| Thu 5 Nov | Real Zoom TEST meeting: desktop + phone participants, network loss, rejoin | pending (needs owner); preliminary web-client run done 4 Nov |
| Fri 6 Nov | Dress rehearsal on production equipment (mixer, Computer B GPU Whisper, Claude, Zoom) | pending (needs owner + Computer B) |
| Sat 7 Nov | Wedding, frozen release | - |

## Performance target

Speech end → bilingual caption visible in Zoom: **p50 ≤ 4 s, p95 ≤ 7 s**. Per-stage timings are
recorded (`util/latency.py`, dashboard diagnostics). Measured so far: Claude p50 1.7 s / p95 2.7 s
(live-API), Zoom POST p50 0.49 s / p95 1.24 s (HTTP-accepted), Zoom display ~0 s after POST
(observed, eye). **Not measured yet: Whisper on the GPU, VAD on real audio** → Friday.

## Done (verified)

| Area | Evidence |
|---|---|
| Zoom caption sender (seq rules, retries), output state machine, E-STOP, epochs | mock + observed (4 Nov) |
| Bilingual caption TR + EN in one POST; Turkish characters | **observed** in Zoom web client (4 Nov) |
| Single-line layout `TR: … \| EN: …` (default; `\n` not rendered by the web client) | **observed** (4 Nov) |
| Pause / Resume / Stop / E-STOP / reset never resumes LIVE | **observed** + mock (4 Nov) |
| Same-seq retry 403 after a timeout counted as delivered (D-015) | mock (real behaviour observed once) |
| Composer: aligned parts, refs/numbers/negations kept whole, reading-time pacing | mock (test_compose) |
| Glossary v1 + English extension, citation parser, TR + EN guards | mock + source-checked (jw.org NWT) |
| TR + EN live eval incl. 13 English-focused high-risk cases | live-API (see TEST_REPORT) |
| Persistent $5 test ledger | used live |
| Dashboard, Speaker page, auth, lease, restart sign-out, layout switch | browser e2e (Edge) |
| CI on Python 3.12 + 3.13 | GitHub Actions |
| Guest viewer + Cloudflare relay (experimental, disabled) | mock + local relay only |

## Next

1. Failure-injection + soak harness (mock).
2. Runbook / rehearsal checklist update for Zoom-first bilingual.
3. Thursday: desktop + phone participants; check whether `\n` renders; network loss; rejoin.

## Pending (needs equipment or people)

- GPU Whisper benchmark on Computer B (the dev laptop cannot run Whisper live: D-016).
- Real mixer audio through the sender on Computer A.
- Tailscale access from a real phone; LAN access from a second computer.
- Turkish and English human review of translations, glossary terms and phrase deck.
