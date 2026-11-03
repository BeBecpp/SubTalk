# Session handoff

Update this file at every milestone. It must make sense with no chat history.

## CURRENT OBJECTIVE
Production hardening for 7 Nov 2026: Turkish + English output from one recognition event,
bounded live Claude QA ($5 cap), real Zoom TEST-meeting integration, browser e2e tests,
failure injection / soak, final readiness report (see `CLAUDE.md`).

## SCHEDULE (owner-confirmed)
- Tue 3 - Wed 4 Nov: finish everything that does not need the meeting/equipment (budget ≤ $5).
- Thu 5 Nov: preliminary real Zoom TEST meeting + remote control + failure tests.
- Fri 6 Nov: full dress rehearsal on production equipment; freeze the build if it passes.
- Sat 7 Nov: wedding with the frozen release. Never claim Thu/Fri tests passed before they run.

## CURRENT GIT COMMIT
`bbb8756` (main, in sync with origin) at session start 2026-11-03. Update on each commit.

## WORKING TREE STATUS
Uncommitted: CLAUDE.md, docs/SESSION_HANDOFF.md, resources/glossary_en/en_glossary.json,
src/subtalk/glossary/store.py (English glossary merge). In progress.

## LAST COMPLETED TASK
Baseline verified 2026-11-03: 231/231 tests on Python 3.12 and 3.13; glossary validator PASS
(66 books, 45 JW terms, 41 wedding terms, 31 QA cases). English book names checked against
https://www.jw.org/en/library/bible/nwt/books/ (NWT 2013).

## VERIFIED TEST RESULTS
- `pytest -m "not live"`: 231 passed (3.12 and 3.13), 2026-11-03.
- Earlier live Claude results (before this $5 budget): see docs/TEST_REPORT.md.

## ACTIVE BUGS / BLOCKERS
- Real Zoom TEST meeting: needs the owner to start a test meeting and paste its caption URL into
  the dashboard (never in chat). Not yet done.
- No CUDA GPU on the dev laptop: Whisper/GPU benchmarks pending on Computer B.
- Tailscale not installed on the dev laptop.

## CURRENT API TEST SPEND
$0.00 of the new $5.00 budget (ledger `state/api_test_budget.json` not created yet).
Prior spend before this budget: about $0.19 (1 Nov 2026).

## REMAINING TEST BUDGET
$5.00 (automatic stop at $4.50).

## EXACT NEXT ACTION
Implement dual-language (TR+EN) translation: per-language status, English guards, output modes.

## COMMAND TO REPRODUCE
`.venv\Scripts\python.exe -m pytest -q -m "not live"`

## REQUIRED HUMAN INPUT, IF ANY
Later: start a separate Zoom TEST meeting, copy the caption API token URL, paste it in the dashboard.
