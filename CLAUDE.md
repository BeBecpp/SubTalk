# SubTalk - project instructions

Live captioning for the wedding of Galkhuu & Akane, **Saturday 7 Nov 2026, 16:00 Ulaanbaatar**.
Source speech: Mongolian (primary) and Japanese (interpreter + occasional independent remarks).
Output: Turkish and English captions into an existing Zoom meeting via Zoom's Manual Caption API.

**After any context reset: read `docs/SESSION_HANDOFF.md` and `docs/IMPLEMENTATION_STATUS.md`
first, check `git status`/`git log`, re-run the smallest relevant tests, continue the documented
next action. Do not restart the project and do not re-run paid tests just because context was lost.**

## Schedule (owner-confirmed 2026-11-03)

| Date | Work | Rule |
|---|---|---|
| Tue 3 - Wed 4 Nov | Finish implementation, mock integration tests, TR+EN Claude evaluation (≤ $5 total), JW terminology validation, browser control tests, offline stress tests, all harnesses + docs | Do not wait for Thursday to finish development |
| Thu 5 Nov | Preliminary **real Zoom TEST meeting**: caption API verification, browser remote control, connection/failure tests | Separate test meeting only |
| Fri 6 Nov | **Full dress rehearsal** on production equipment: mixer, both computers, GPU Whisper, real Claude, Zoom; TR+EN output, latency, accuracy, reconnect, emergency recovery | Critical failure => NO-GO for that feature + documented fallback |
| Sat 7 Nov | Wedding. Only the tested, approved release; **build frozen after Friday's successful rehearsal** | No test captions ever into the wedding meeting |

Never claim a Thursday/Friday test passed before it was actually executed. Never hide defects to meet the deadline.

## Confirmed decisions (do not re-ask)

- Two computers: A = Zoom host + lightweight audio sender + dashboard browser; B = local AI engine
  (FastAPI, faster-whisper on GPU, Claude API, Zoom caption sender). Dev laptop runs mocks only.
- Zoom Manual Caption API only (caption URL pasted at runtime). No Zoom OAuth/REST, no meeting changes.
- No database (in-memory state, atomic JSON/YAML, JSONL logs). No OBS, camera, Spotlight, Docker, Redis.
- No prepared speech script in this release (plugin interface `pipeline/script_context.py` stays inactive).
- Main Speech Mode = interpreter-aware routing toggle only; ordinary translation never stops.
- SubTalk JW Glossary v1 (`resources/glossary/`) is the terminology baseline; original data preserved.
- Translation model: `claude-sonnet-5-5` (owner's choice), fallback `claude-haiku-5-5`, effort low.
- Remote access: private LAN or Tailscale Serve only. Never Funnel / port forwarding.
- Turkish + English from one recognition event; see `docs/DECISIONS.md`.

## Architecture map (`src/subtalk/`)

| Path | Role |
|---|---|
| `server/app.py`, `server/auth.py` | FastAPI routes, WebSockets, passcodes (scrypt), boot-bound sessions, control lease |
| `engine.py` | Wires everything; authoritative state, health, verdict, commands |
| `audio/` + `sender/subtalk_sender.py` | Frame protocol, ingest health, VAD, segmenter; shared-mode sender on Computer A |
| `asr/` | faster-whisper worker process, language decision (MN/JA/UNCERTAIN/...) |
| `glossary/` | Glossary loader/validation, term selection, Bible citation parser, output guards |
| `translate/` | Claude translator (structured outputs), prompts, mock |
| `pipeline/` | Speech events, ordered emitter, Japanese interpreter matching, Main Speech |
| `output/controller.py` | Output state machine + safety gate (epochs, TTL, E-STOP) |
| `zoom/` | Caption URL validation, sender (official seq rules), strict mock endpoint |
| `web/` | Operator dashboard + Speaker page (plain HTML/CSS/JS, no build step) |
| `evalrun.py`, `budget.py` | Live Claude evaluation and persistent test-spend ledger |

## Commands (verified on the dev laptop, PowerShell, repo root)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1      # dev install (.venv)
scripts\start.ps1                                                  # mock mode -> http://127.0.0.1:8000/operator (dev-operator)
.venv\Scripts\python.exe -m pytest -q -m "not live and not e2e"    # unit + integration (mock)
.venv312\Scripts\python.exe -m pytest -q -m "not live and not e2e" # same on Python 3.12 (coarse clock, like CI)
.venv\Scripts\python.exe -m pytest -q -m e2e                       # browser tests (Playwright; see docs/TEST_REPORT.md)
.venv\Scripts\python.exe -m pytest -q -m live                      # 1 real Claude request
.venv\Scripts\subtalk.exe eval-claude --yes                        # glossary cases, real Claude, budget-ledgered
.venv\Scripts\subtalk.exe budget                                   # show the persisted test-spend ledger
.venv\Scripts\subtalk.exe zoom-probe --i-confirm-test-meeting      # TEST meeting only; URL read without echo
.venv\Scripts\subtalk.exe doctor | glossary-check                  # diagnostics
.venv\Scripts\python.exe -I resources\glossary\validate.py         # the glossary pack's own validator
```
Production (Computer B): `docs/SETUP.md`. Use `127.0.0.1`, not `localhost` (IPv6 on this machine).

## Security rules

- Keys only in `.env` (gitignored, also `.env.*`). Never print, log or commit keys or caption URLs.
- Before every commit: `git diff --cached` review + check no `sk-ant-` / caption URL / `.env` staged.
- Tests never post to the wedding meeting. Real Zoom only in a separate TEST meeting.
- Output never auto-resumes LIVE after a restart; never switch TEST -> EVENT automatically.

## Testing and budget rules

- Mock-first. Real Claude only when it adds confidence.
- **Live API test budget: $5.00 total, automatic stop at $4.50**, persisted in
  `state/api_test_budget.json` (survives restarts and context resets). Never reset it.
- Report evidence levels honestly: mock / HTTP 200 from Zoom / observed in Zoom / read by a human.
- CI must pass on Python 3.12 and 3.13 (`.github/workflows/ci.yml`). Do not weaken tests to pass.

## Definition of done

Tests pass on 3.12 + 3.13; real integration evidence recorded in `docs/TEST_REPORT.md`;
limitations listed; `docs/RUNBOOK.md` current; manual fallback (phrase deck) available;
`docs/SESSION_HANDOFF.md` updated.

## Status

See `docs/IMPLEMENTATION_STATUS.md` (what is done / pending) and `docs/SESSION_HANDOFF.md` (exact next action).
