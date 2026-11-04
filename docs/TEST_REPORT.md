# Test report

Single source of truth for test numbers. Evidence levels as in `docs/IMPLEMENTATION_STATUS.md`.
Nothing below was executed with real mixer audio or a GPU. Real Zoom: see "Zoom" (4 Nov, web client).

## Performance target

Speech end → bilingual caption visible in Zoom: **p50 ≤ 4 s, p95 ≤ 7 s**.

| Stage | Measured | Evidence |
|---|---|---|
| Claude TR + EN (one request) | p50 1.64 s, p95 2.39 s, max 3.14 s (52 cases, 4 Nov) | live-API |
| Zoom POST → 2xx | p50 0.49 s, p95 1.24 s, max 1.84 s (13 POSTs, 4 Nov) | HTTP-accepted, real TEST meeting |
| Zoom 2xx → visible | not separately measurable here; visible by the next screenshot (≤ 2 s) | observed (web client) |
| VAD end-of-speech wait | 650 ms silence + post-roll by configuration | config, not measured on real audio |
| Whisper on GPU | **not measured** (Computer B, Friday) | pending |
| Whisper on this laptop (CPU) | `small` int8: 27.1 s for 12.5 s audio (beam 5), 21.3 s (beam 1) | measured, D-016: unusable live |

Budget arithmetic (not a measurement): 0.65 VAD + ASR(GPU, unknown) + 1.64 Claude + 0.49 Zoom ≈
2.8 s + ASR at p50. The 4 s p50 target therefore needs GPU ASR ≲ 1.2 s per segment. Verify Friday.

## Automated suites (dev laptop, Windows 11)

| Date | Suite | Result |
|---|---|---|
| 4 Nov (evening) | Unit + integration incl. Japanese modes / guards / composer, invalid-reference words | **455 passed on Python 3.12 and on 3.13** |
| 4 Nov (evening) | Browser e2e (Edge), all 11 | 11 passed |
| 4 Nov | Unit + integration incl. composer, sender 403 fix, layout command, 2 soak tests | **409 passed on Python 3.12 and on 3.13** |
| 4 Nov | Soak tests (`tests/test_soak.py`, 2 seeds x 20 s, doubled fault rates) | 2 passed, 0 violations |
| 4 Nov | Browser e2e (Edge), operator/speaker (`tests/e2e/test_browser.py`), single-line default | 7 passed |
| 3 Nov | Browser e2e (Edge, Playwright), 11 tests | 11/11 passed twice in a row |
| 3 Nov | Glossary pack validator | PASS |

Defects found by tests and real runs (all fixed):
- **4 Nov, real Zoom:** a POST timed out (1.5 s) although Zoom had shown the caption; the same-seq
  retry got HTTP 403 and SubTalk recorded a failure (3 in a row would auto-pause). Fixed (D-015);
  the mock now reproduces the 403. Attempt timeout raised to 3 s.
- **4 Nov, live eval:** "Ах эгч нар аа" (addressing the audience) became "Older brothers, older
  sisters" / "Ağabeyler, ablalar". Prompt rule added; re-run 5/5 in both languages.
- **4 Nov, composer tests:** at a very small width a Bible reference or a negation could start a
  new part. References, numbers and negations are now unbreakable units.
- 3 Nov: relay retry timestamp, dashboard render freeze, two test-only timing issues (see git log).

## Live Claude (budget ledger `state/api_test_budget.json`)

| Run | Requests | Turkish | English | Latency p50 / p95 | Cost |
|---|---|---|---|---|---|
| smoke (3 Nov) | 1 | OK | OK | - | $0.0093 |
| tr_en_regression, single_request (3 Nov) | 37 | 36 pass / 0 FAIL / 1 withheld | same | 1.8 / 3.0 s | $0.1025 |
| mode_b_parallel (3 Nov) | 72 | 1 FAIL | 0 FAIL | 1.7 / 3.2 s | $0.148 |
| en_focus_4nov (13 English + 5 high-risk) | 18 | 16 pass / **1 FAIL** / 1 withheld | 16 / **1 FAIL** / 1 | 1.71 / 2.71 s | $0.0545 |
| en_focus_4nov_fix (same 5, OLD prompt by mistake: edit had not applied) | 5 | 4 / 1 FAIL | 4 / 1 FAIL | 1.77 / 2.48 s | $0.0162 |
| en_focus_4nov_fix2 (5 brother/sister cases, fixed prompt) | 5 | 5 pass | 5 pass | 1.82 s | $0.0162 |
| full_regression_4nov (all 52 cases, fixed prompt) | 50 | **49 pass / 0 FAIL / 1 withheld / 2 n/a** | **49 / 0 / 1 / 2** | 1.64 / 2.39 s | $0.1394 |
| trilingual_4nov (52 cases, TR + EN + JA, Japanese prompt) | 50 | 50 pass / 0 FAIL | 50 / 0 | 2.07 / 3.70 s | $0.1878 |
| trilingual_refs_4nov (11 reference cases after the invalid-reference fix) | 11 | 10 pass / 1 withheld (correct) | same; JA same | ~2.0 s | $0.0454 |
| bilingual_after_ja_4nov (52 cases, TR + EN, final prompt) | 50 | **49 pass / 0 FAIL / 1 withheld / 2 n/a** | **49 / 0 / 1 / 2** | 1.85 / 2.76 s (max 7.5 s: one Sonnet timeout → Haiku) | $0.1416 |
| **Total of the $5 budget** | 301 | | | | **$0.8727** (stop at $4.50) |

Japanese in trilingual_4nov: 48 pass, 2 FAIL that were wrong checks (Japanese source repeated as
あかね; 契約 = contract and covenant in Japanese), 1 guard-withheld (invented 5:34, correct). The run
also showed TR / EN spelling the non-existent verse out in words: fixed (D-019), re-run clean.
Japanese wording has not been read by a native speaker yet.

"withheld" = `invalid_reference` (Ephesians 5:34 does not exist): withheld in both languages, as
required. "n/a" = music / applause (no text). Cases are synthetic; not a measured error rate.

## Zoom (real)

| Date | Target | Result |
|---|---|---|
| 4 Nov | separate TEST meeting, web client, host view | bilingual captions shown, TR first, Turkish letters correct; `\n` **not rendered**; single-line layout readable; 13 captions in order; Pause/Resume/Stop/E-STOP behaved; caption visible ~8-10 s. Details: `docs/ZOOM_TEST_THURSDAY.md` |
| Thu 5 Nov | desktop app + phone participants, network loss, rejoin | **pending** |

## S-41 talk: guards vs the owner's human translation (4 Nov, free, no Claude)

87 Mongolian paragraphs checked with SubTalk's guards against the owner's TR + EN translation:
- Before fixes: 81 pass / 6 blocked. Defects found and fixed: verse lists truncated ("5:25,28,29" →
  "5:25"); "Гэрч" → "Jehovah's Witness" and "гэр бүл болох" → "husband and wife" wrongly blocked.
- After fixes: **85 pass / 2 blocked**; both add an unspoken subject or object (by design).
- Not yet: Claude's own translation of the talk compared with the owner's (optional, ~$0.3).

## Failure-injection soak (mock Claude + mock Zoom, real pipeline / controller / sender)

| Date | Duration | Faults | Violations | Notes |
|---|---|---|---|---|
| 4 Nov | 60 s, seed 1 | 13 Zoom, 7 Claude, E-STOP/stop/reconnect/mode/layout | 0 | 2 lost responses confirmed by seq, 0 abandoned; queue max 3; RSS 65→61 MB; CPU 0.5 % |
| 4 Nov | 600 s, seed 3, faults x1.5 | 169 Zoom; 99 Claude (21 timeout, 13 rate limit, 14 invalid JSON, 32 missing EN, 19 slow); 12 E-STOP, 8 stop, 4 pause, 5 reconnect, 24 mode/layout | 0 | 328 segments, 104 sent, 18 confirmed by seq, 8 abandoned; queue max 5; RSS 65 MB flat; CPU 0.7 %. **Finding:** oldest caption reached Zoom 13.6 s after speech end (> 12 s TTL) because a 3 s attempt could outlive the deadline → fixed: attempt timeout capped at the time left |
| 4 Nov | 300 s, seed 3, faults x1.5, after the fix | same mix | 0 | 47 sent, 4 confirmed by seq, 2 abandoned; caption age p50 3.5 s, p95 7.7 s, **max 10.9 s** (within TTL) under heavy injected faults |

## Guest viewer relay (experimental, disabled)

3 Nov local `wrangler dev`: 1,000 sockets, 30,000/30,000 delivered. Not a production dependency.

## Not yet executed

GPU Whisper benchmark; real audio; Thursday desktop/phone participants; Friday rehearsal;
human review of TR/EN wording.
