# Validation (do these first)

Nothing below has been verified yet - it needs the real Zoom account, Computer B and real audio.
Targets are acceptance criteria to measure, not promised performance.

## M0-A - Zoom Manual Caption API in a TEST meeting

**Never against the wedding meeting.** Use a separate test meeting on the organizer's account
(manual captions + "Allow use of caption API token" enabled).

1. Host starts the test meeting. Captions ▸ *Set up manual captioner* ▸ *Copy the API token*.
2. On Computer B, `config/local.yaml` has `zoom.allow_real_zoom: true`.
3. Run the probe:
   ```powershell
   .venv\Scripts\subtalk.exe zoom-probe --url "<pasted URL>" --i-confirm-test-meeting --retry-test --gap-test
   ```
4. Watch the captions in the Zoom **desktop, web and mobile** clients (a second person helps) and record:

| Check | Result |
|---|---|
| GET `/closedcaption/seq` works; body format (the code accepts an integer or JSON) | |
| Turkish characters ç ğ ı İ ö ş ü render correctly | |
| `\n` produces a second line (if yes, set `zoom.line_breaks: true`) | |
| Long caption (~100 chars) display / truncation | |
| Same-seq retry is **not** shown twice | |
| A seq gap (+2) - accepted or rejected? | |
| Delay from POST to visible caption (first caption and later ones) | |
| Does the token survive the host leaving and rejoining? Does a new token start at seq 1? | |
| Guests who did not enable captions see nothing different | |

5. Then do the same through the dashboard: Connect (target TEST) → hold go LIVE → phrase deck →
   E-STOP → reset → stop. Confirm nothing is sent after E-STOP.

**Pass:** captions visible within about 3 s on all clients, no duplicate on retry, seq behaviour known.

## M0-B - Mongolian/Japanese recognition benchmark (Computer B)

1. Record at least 20 minutes through the real mixer, as 16-bit WAV: Mongolian reading and
   spontaneous speech, the Japanese interpreter, alternating turns, overlap, a song, music,
   applause, silence.
2. Write reference transcripts by hand next to each file: `name.mn.txt` and/or `name.ja.txt`.
   Keep a separate held-out set that is never used for tuning.
3. Run:
   ```powershell
   .venv\Scripts\subtalk.exe bench-asr recordings\ --models large-v3,large-v3-turbo,medium
   ```
   Add a converted Mongolian fine-tune (CTranslate2 folder path) to `--models` if one is available.
4. `bench_out\summary.csv`: CER per language, latency p50/p95, decision counts.
   `bench_out\<model>\segments.jsonl`: every segment's decision and reasons - check that music,
   singing and applause produce no MN/JA text.
5. Run the top two models end to end (Claude) on the same held-out segments, fill
   `bench_out\fidelity_rating.csv` with a Turkish reviewer who does not know which model produced
   which line: faithful / minor error / critical error / should have been omitted.

**Decide** by the fewest critical errors in Turkish at acceptable latency, not by CER alone. With
fewer than ~100 rated captions, report the counts; do not claim an error rate.

## Proposed go/no-go thresholds (confirm after M0-B)

| Metric | Target |
|---|---|
| Language decision correct on segments ≥ 2 s | ≥ 95 % |
| Captions produced from music / singing / applause / silence | 0 |
| Speech end → Zoom accepted, p95 (dashboard "Delay") | ≤ 7 s |
| Critical errors (negation, numbers, names, religious content) | ≤ 1 per 100 displayed, on an adequate sample |
| Invented or wrong Bible references | 0 |
| Japanese repetitions captioned twice | ≤ 5 % (else set Japanese to Off/All per segment, or disable Auto matching) |
| Coverage (share of meaningful speech captioned) | reported honestly |

## Live Claude checks

Put `ANTHROPIC_API_KEY=...` in `.env` (gitignored), then:

1. Smoke test, one request (about $0.01): `.venv\Scripts\python.exe -m pytest -m live -q -s`
2. `subtalk doctor` with `translate: {engine: claude}` also makes one request and prints model, latency, cost.
3. Glossary evaluation, 29 requests: `.venv\Scripts\subtalk.exe eval-claude --yes`
   (`--model claude-sonnet-5-5` to compare; `--cases divine_name,negation` for a subset). Each case
   gets an automatic check (pass / FAIL / withheld); the report goes to `bench_out\claude_eval_*.md`.
   The checks are heuristics: a Turkish speaker must still read every line. 29 synthetic sentences
   show behaviour on known traps; they are not a measured error rate.
