# Session handoff

Update this file at every milestone. It must make sense with no chat history.

## CURRENT OBJECTIVE
Production hardening for 7 Nov 2026 (see `CLAUDE.md`). Latest owner request (3 Nov): guests
choose Turkish or English independently via a SubTalk caption viewer behind a Cloudflare
Worker relay; Zoom manual captions stay as the fallback (D-013, `docs/VIEWER.md`).

## SCHEDULE (owner-confirmed)
- Tue 3 - Wed 4 Nov: finish everything that does not need the meeting/equipment (budget ≤ $5).
- Thu 5 Nov: preliminary real Zoom TEST meeting + remote control + failure tests.
- Fri 6 Nov: full dress rehearsal on production equipment; freeze the build if it passes.
- Sat 7 Nov: wedding with the frozen release. Never claim Thu/Fri tests passed before they run.

## CURRENT GIT COMMIT
`1d14d5f` + guest viewer work (commit "Guest caption viewer with Cloudflare relay" follows; see `git log`).

## WORKING TREE STATUS
Guest viewer milestone: `output/viewer.py`, controller/pipeline/engine/server integration,
`web/viewer.html`, `relay/` (Worker + Durable Object, wrangler 4.151.2), tests, docs.
`relay/.dev.vars` holds a LOCAL dev token only (gitignored).

## LAST COMPLETED TASK
Guest viewer built and tested locally (3 Nov):
- Captions reach guests only through the output gate; E-STOP clears guest screens; viewer-only
  LIVE allowed; Zoom failures suspend only Zoom while the viewer is on.
- Relay under `wrangler dev`: 2 browser guests (TR mobile + EN desktop) saw the same events in their
  own language; reconnect after relay restart works; bad token 401; admin paths 404.
- Local load: 1,000 sockets, 30,000/30,000 captions, p50 309 ms, p95 1.09 s (LOCAL ONLY).

## VERIFIED TEST RESULTS
See `docs/TEST_REPORT.md` (single source of truth for numbers).

## ACTIVE BUGS / BLOCKERS
- Relay not deployed: needs the owner's Cloudflare login (`relay/README.md`). Until deployed and
  load-tested from outside, the public viewer and 1,000-viewer capacity are UNVERIFIED.
- Real Zoom TEST meeting (Thu): owner starts it and pastes the caption URL into the dashboard.
- No CUDA GPU on the dev laptop: Whisper/GPU benchmarks pending on Computer B.
- Turkish viewer UI strings and phrase-deck `en:` lines need a native-speaker review.
- Failure-injection / soak harness (Claude timeout, 429, bad JSON, audio gaps, memory) not built yet.

## CURRENT API TEST SPEND
$0.2595 of the $5.00 budget (ledger `state/api_test_budget.json`, 110 requests). Nothing spent on 3 Nov
evening (viewer work is mock-only). Spend before this budget: about $0.19 (1 Nov).

## REMAINING TEST BUDGET
$4.2405 before the automatic stop at $4.50.

## EXACT NEXT ACTION
1. Failure-injection + soak harness (mock): Claude timeout / 429 / invalid JSON / missing language,
   Zoom 400/403/5xx/timeouts, audio gaps, relay outage; record memory + queue metrics.
2. Owner: deploy relay (relay/README.md steps 1-4), then run `relay/loadtest.py` against the
   deployed URL from another network (step 5).
3. Thursday: `docs/ZOOM_TEST_THURSDAY.md` (add viewer rows: guests on phones with TR/EN).

## COMMAND TO REPRODUCE
```
.venv\Scripts\python.exe -m pytest -q -m "not live and not e2e"
.venv312\Scripts\python.exe -m pytest -q -m "not live and not e2e"
.venv\Scripts\python.exe -m pytest -q -m e2e          # needs Edge + relay/node_modules
.venv\Scripts\python.exe relay\loadtest.py --clients 1000   # with `npx wrangler dev` running in relay/
```

## REQUIRED HUMAN INPUT, IF ANY
- Cloudflare account login + `wrangler deploy` + `wrangler secret put PUBLISH_TOKEN` (owner).
- Choose Workers Free vs Paid ($5/month) for the event (owner; check current Cloudflare limits).
- Thursday: Zoom TEST meeting caption URL pasted in the dashboard (never in chat).
- Turkish/English reviewers for translations, glossary terms and viewer UI text.
