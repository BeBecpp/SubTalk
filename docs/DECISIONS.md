# Engineering decisions

Newest first. Each entry: decision, evidence, consequences.

## D-012 (2026-11-03) Turkish + English from ONE Claude request ("single_request")

- Options: A = one request returns both languages; B = one request per language, concurrently.
- Measured (live, `claude-sonnet-5-5`, effort low, same 37 text cases, 3 Nov):

  | | A single_request | B parallel |
  |---|---|---|
  | Turkish FAIL | 0 | 1 (`Хурлын ахлагч` -> "Toplantının başkanı", lost the JW "elder" sense) |
  | English FAIL | 0 | 0 |
  | Latency p50 / p95 / max | 1.8 / 3.0 / 3.1 s | 1.7 / 3.2 / 4.6 s |
  | Cost (37 cases) | $0.103 | $0.148 (+44 %) |
- Decision: A is the default (`translate.dual_mode: single_request`). B stays available.
- Japanese interpretation always uses one request, so the duplicate decision (covered / new /
  independent) is identical for both languages.
- Failure isolation in A: the JSON carries an independent display/quality per language; a missing
  or invalid language object fails only that language. A timeout fails both (retried once on Haiku).
- Caveat: one run of 37 synthetic cases; not a measured error rate.

## D-011 (2026-11-03) One Zoom caption stream; output modes TR / EN / BILINGUAL

- Zoom's Manual Caption API documents a single caption stream per URL; no per-attendee language
  channel is documented. SubTalk sends ONE POST sequence per event:
  TR only, EN only, or BILINGUAL ("TR: ...\nEN: ..." in the same POST).
- Never two separate POSTs per event (both would appear sequentially for everyone).
- Bilingual readability on desktop/mobile must be judged in the Thursday TEST meeting.
- Independent per-guest language choice would need a separate private SubTalk caption viewer;
  not built (would add risk to the core pipeline this week).

## D-010 (2026-11-03) English terminology as a separate file

- `resources/glossary_en/en_glossary.json`, keyed by the original pack's IDs; the original pack is
  unchanged. Book names checked against https://www.jw.org/en/library/bible/nwt/books/ (NWT 2013).
  Core JW terms marked `en_core` (standard usage, not individually page-verified); the rest
  `en_review_required` (sent to Claude only as unreviewed candidates).

## D-009 (2026-11-03) Persistent live-test budget ledger

- `state/api_test_budget.json`: $5.00 limit, automatic stop at $4.50, upper-bound reservation
  before each request, actual cost after. Survives restarts and context resets; never reset by code.

## D-008 (2026-11-01) Model: Claude Sonnet 5.5 (owner's choice), Haiku 5.5 fallback, effort low

## D-007 (2026-11-01) Epoch-based stale-work rejection (clock-independent)

- Python <= 3.12 on Windows: `time.monotonic()` advances in 15.6 ms steps. Captions are rejected by
  output epoch, not only by timestamps.

## D-006 (2026-11-01) Zoom sequence handling follows the official article

- seq +1 per new caption, retries reuse seq, ambiguous outcome -> GET /closedcaption/seq before the
  next caption. No block reservation.

## Earlier (see docs/ARCHITECTURE.md "Implementation decisions")

No-build dashboard; Task Scheduler instead of WinSW; quiet-point force cut; no extra LID/music model
unless benchmarks justify it.
