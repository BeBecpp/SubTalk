# SubTalk

Live Mongolian / Japanese → Turkish captions for an existing Zoom meeting, built for the
wedding of Galkhuu & Akane (7 November 2026).

```
Mixer ─► Computer A (Zoom host) ── audio sender ──LAN──► Computer B (SubTalk engine)
                                                         VAD → Whisper → language decision
                                                         → JW glossary + citation guard → Claude
                                                         → guards → caption gate ──HTTPS──► Zoom captions
```

* **Never touches the meeting.** SubTalk only holds the caption URL (API token). It has no Zoom
  credentials, cannot change the invitation, attendees, audio, video or Spotlight.
* **Safe by default.** Real Zoom output and the wedding (EVENT) target are disabled in config until
  you enable them. Output starts OFF after every restart. E-STOP halts output immediately.
* **Prefers silence to invention.** Uncertain language, garbled recognition, invented Bible
  references, names, numbers or religious terms are withheld, never guessed.
* **No database.** In-memory state, YAML/JSON files, JSONL logs.

## Quick start (development laptop, no GPU, no paid calls)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
scripts\start.ps1
```

Open <http://127.0.0.1:8000/operator> (passcode `dev-operator`), connect captions with URL `mock`,
hold **go LIVE** and watch the mock ceremony flow to the built-in mock Zoom endpoint.
Speaker page: <http://127.0.0.1:8000/speaker> (`dev-speaker`).

Run the tests: `.venv\Scripts\python.exe -m pytest -q`

## Documentation

| | |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Install on the laptop, Computer B (engine) and Computer A (sender) |
| [docs/VALIDATION.md](docs/VALIDATION.md) | **Do first:** Zoom caption probe (M0-A) and Mongolian/Japanese ASR benchmark (M0-B) |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Event-day checklist and what to do when something fails |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design, state machines, safety rules, implementation decisions |
| [docs/NETWORK.md](docs/NETWORK.md) | LAN access, Tailscale, roles and sessions |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | JW glossary integration and the review workflow |

## Status

Implemented and tested in mock mode (163 automated tests, plus one live Claude test that runs
only when an API key is present). **Not yet verified:** real Zoom
caption delivery (M0-A), Whisper accuracy on real Mongolian/Japanese audio (M0-B), live Claude
latency/quality, and the audio sender on Computer A's actual mixer input. See docs/VALIDATION.md.
