# Test report

Single source of truth for test numbers. Evidence levels as in `docs/IMPLEMENTATION_STATUS.md`.
Nothing below was executed against a real Zoom meeting, real mixer audio or a GPU unless stated.

## Automated suites (dev laptop, Windows 11)

| Date | Suite | Result |
|---|---|---|
| 3 Nov | Unit + integration, Python 3.13 (`-m "not live and not e2e"`) | 351 passed |
| 3 Nov | Unit + integration, Python 3.12 | 350 passed, 1 failed → test race fixed (coarse clock, see below), then 19/19 viewer + server tests passed 6 runs in a row |
| 3 Nov | Browser e2e (Edge, Playwright), operator/speaker (`tests/e2e/test_browser.py`) | 7 passed |
| 3 Nov | Browser e2e, guest viewer + local relay (`tests/e2e/test_viewer_relay.py`) | 4 passed |
| 3 Nov | Full e2e (11 tests) after the hold-timing fix, run twice in a row | 11/11 passed both times (earlier runs under CPU load: 1 different flaky failure each time, cause below) |
| 3 Nov | Glossary pack validator | PASS |

Defects found by tests on 3 Nov (all fixed):
- Relay retry re-queued captions with a fresh timestamp, so a caption could be delivered later than
  the 20 s limit after repeated relay failures. Now the original queue time is kept.
- Dashboard segment buttons without id made `render()` throw, so the dashboard froze on "Not connected"
  while the backend was LIVE. Fixed, and any render error now shows the stale overlay.
- Test-only: on Python 3.12/Windows the clock ticks in 15.6 ms steps; a test submitted a caption in
  the same tick as going LIVE (correctly rejected as "before LIVE"). The test now waits one tick.
- Test-only: e2e helpers started the LIVE hold before the UI had enabled the button. They now wait
  for the button, as a human would.

## Live Claude (budget ledger `state/api_test_budget.json`)

| Run | Requests | Result | Cost |
|---|---|---|---|
| smoke | 1 | OK | $0.0093 |
| tr_en_regression (single_request) | 37 | TR 36 pass / 0 FAIL / 1 withheld; EN same; p50 1.8 s, p95 3.0 s | $0.1025 |
| mode_b_parallel | 37×2 | TR 1 FAIL (`Хурлын ахлагч`); p95 3.2 s, max 4.6 s | ~$0.148 |
| **Total of the $5 budget** | 110 | | **$0.2595** (stop at $4.50) |

## Guest viewer relay load test

| Date | Target | Clients | Captions | Delivered | Latency p50 / p95 / max | Notes |
|---|---|---|---|---|---|---|
| 3 Nov | local `wrangler dev` (workerd), same laptop as clients | 1,000 | 30 @ 2 s | 30,000 / 30,000 (100 %), 0 early closes | 309 ms / 1.09 s / 1.67 s | connect ramp 17 s at 100 concurrent; **not Cloudflare production** |
| - | deployed Worker, external network | 1,000 | | | | **pending** (owner deploys) |

Report files: `test-artifacts/relay_load_*.json` (gitignored).

## Zoom

| Date | Target | Result |
|---|---|---|
| 3 Nov | local mock Zoom endpoint over real HTTP (`zoom-probe`) | 6 posts, seq 1-6, UTF-8 intact, no URL in report |
| Thu 5 Nov | real Zoom TEST meeting | **pending** (`docs/ZOOM_TEST_THURSDAY.md`) |

## Not yet executed

Failure-injection / soak harness; GPU Whisper benchmark; real audio; Thursday TEST meeting;
Friday rehearsal; deployed relay; human review of TR/EN wording.
