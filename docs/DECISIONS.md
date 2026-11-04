# Engineering decisions

Newest first. Each entry: decision, evidence, consequences.

## D-019 (2026-11-04) Japanese captions: operator mode TR + EN + JA for non-interpreted parts

- Owner: guests are Turkish friends, American friends and the Japanese family. The talk is
  interpreted into Japanese live; the vows and the MC's words are not, so Japanese guests need
  captions there.
- Zoom's manual caption API is ONE stream (D-011): every guest sees the same caption and reads
  their own label. No per-guest language choice (the viewer that offered it stays disabled, D-014).
- New caption modes: `JA` and `TRILINGUAL` ("TR: ... | EN: ... | JA: ..."). Default stays
  `BILINGUAL`. Japanese is requested from Claude (same single request) only while the mode shows
  it, so TR + EN captions cost and latency are unchanged in BILINGUAL.
- Japanese guards = the same rules on character patterns: エホバ preserved / never invented; no
  invented 神 / 天の父 (精神, 神経, 神聖 excluded); no invented 妻 / 夫 (丈夫, 工夫, 夫人 excluded),
  新郎 / 新婦, 祈, 誓, 契約, 節; references only in render_as_ja form ("エフェソス 5:25, 28, 29",
  "創世記 2章"); digits incl. full-width; names (Latin spelling, or a Japanese form if spoken).
- Composer: Japanese splits only after 。、！？ or after a phrase-final particle followed by a new
  content word; CJK characters count double width. Per-language width in TRILINGUAL: 50.
- Live eval (4 Nov, 52 cases, TRILINGUAL): TR 50/0 FAIL, EN 50/0 FAIL, JA 48 pass + 2 check errors
  (Japanese source kept as あかね; 契約 is also "contract" in Japanese - checks fixed, not model
  failures) + 1 guard-withheld (invalid reference). Claude p50 2.07 s / p95 3.70 s (TR + EN alone:
  1.85 / 2.76 s). Cost $0.19.
- Found by that run: with the longer prompt Claude spelled the non-existent Ephesians 5:34 out in
  words ("verse thirty-four"), which the digit guard missed (4/4 earlier runs withheld it). Fixed
  twice: prompt ("no digits and no number words") and a guard that blocks the invalid number in
  digits, kanji or TR / EN number words. Re-run of the 11 reference cases: all correct, 5:34
  withheld in all three languages.
- Phrase deck: optional `ja:`; Japanese texts in `event/phrases.yaml` are Claude's draft from the
  owner's English (needs a native Japanese check). Names stay Latin until the family gives a
  Japanese spelling (`ja_target` in `event/event_names.json`).

## D-018 (2026-11-04) S-41 talk script: used for checks and phrases, not for live substitution

- The owner supplied the Mongolian S-41 talk and their own TR + EN translation (private, `event/talk/`,
  gitignored). Live captions still translate what is actually spoken (no script matching).
- Found by reading it: verse lists ("Эфэс 5:25,28,29", "Эхлэл 2:21,22", spoken "хорин тав, хорин
  найм, хорин есдүгээр") lost every verse after the first. Parser + guards now keep them:
  "Efesliler 5:25, 28, 29" / "Ephesians 5:25, 28, 29"; non-ascending or out-of-range lists invalid.
- Guards run on all 87 paragraphs against the owner's translation: 81 passed, 6 blocked. Two guard
  rules were too strict and were relaxed narrowly: "Гэрч" may become "Jehovah's Witness(es)" /
  "Yehova'nın Şahidi" (only that phrase); spouse words are allowed when the source says "гэр бүл
  бол-" / "гэрлэ-". Now 85 pass; the 2 left add an unspoken subject/object (Jehovah, "your wife"),
  which SubTalk deliberately never does.
- Names file `event/event_names.json`: Galkhuu, Akane, Bat-Erdene (owner's spellings).
- Phrase deck is bilingual (`en:`), sent in the current caption mode/layout and paced; the event deck
  holds the owner's vow, ring and prayer texts. Vows: see the Friday decision (auto vs phrase deck).

## D-017 (2026-11-04) Strict caption TTL on arrival

- The 10-min soak showed a caption reaching Zoom 13.6 s after speech end (TTL 12 s): the gate
  checks the deadline when an attempt starts, and one 3 s attempt could run past it.
- Each attempt's timeout is now min(attempt_timeout, time left before the deadline), floor 0.3 s.
  Re-run: max 10.9 s under heavy faults. A timed-out late attempt is resolved via GET /seq (D-015).

## D-016 (2026-11-04) Dev laptop cannot run Whisper live; production ASR stays on Computer B's GPU

- Dev laptop: Intel i7-3630QM (2012, 4 cores, AVX, no AVX2), Intel HD 4000, 7.9 GB RAM, no CUDA.
- Measured (faster-whisper 1.2.1, CPU int8, 4 threads, 12.5 s speech clip): `small` beam 5 =
  27.1 s (RTF 2.18), beam 1 = 21.3 s (RTF 1.71). Slower than real time even for `small`;
  `large-v3` (~6x the parameters) would take minutes per segment. Not run (3 GB download).
- Consequence: this laptop is for mocks, dashboard and Zoom tests only. Live captions need an
  NVIDIA GPU (Computer B); GPU latency is measured there (bench.py), not assumed.

## D-015 (2026-11-04) Real Zoom findings: single-line default, same-seq retry 403, 3 s attempt timeout

Observed in a real Zoom TEST meeting (web client, host view, 4 Nov):
- The "\n" between TR and EN is NOT rendered by the Zoom web client: both lines run together
  ("... ö ş ü EN: ..."). Default layout is now `single_line` ("TR: ... | EN: ..."), which was seen
  clearly separated. `two_line` stays a dashboard option until desktop/mobile apps are checked.
- Turkish ç ğ ı İ ö ş ü and curly quotes render correctly. A caption stays visible ~8-10 s.
- A POST timed out (1.5 s) although Zoom had accepted it; the same-seq retry got HTTP 403. The
  sender now, after any uncertain attempt, reads GET /seq: if Zoom already has the seq, the
  caption counts as delivered (no failure, no step towards auto-pause). Mock Zoom answers
  duplicates with 403 like real Zoom. Attempt timeout 1.5 s -> 3 s (POST p50 0.49 s, p95 1.24 s,
  max 1.84 s over 13 POSTs).

## D-014 (2026-11-03) Zoom-first bilingual captions (owner course correction)

- Production delivery = Zoom manual caption API; TR + EN in ONE caption stream, BILINGUAL default.
- Guest viewer + Cloudflare relay (D-013) disabled, off the production path, code kept.
- Composer: both languages split into the same number of parts at sentence/clause boundaries;
  references, numbers, negations never split off; nothing truncated; follow-up parts paced by
  reading time (1.5-4 s) with their deadline extended accordingly.

## D-013 (2026-11-03) Guest caption viewer with per-guest TR/EN choice (owner decision)

- Supersedes the last bullet of D-011. Zoom's manual caption API stays the fallback, unchanged.
- Viewer page (`src/subtalk/web/viewer.html`, one self-contained file) is served in two places:
  locally by the engine (`/viewer`, LAN/Tailscale, `viewer.local_page`) and publicly by a
  Cloudflare Worker + one Durable Object (`relay/`). Same page, same protocol.
- Publish point = the output gate (`OutputController.submit`): a caption reaches guests only if
  it passed LIVE / epoch / before-LIVE / TTL checks, with every language that passed its guards,
  independently of the Zoom mode. Status changes are published; **E-STOP clears guest screens**.
  Operator test captions never reach the viewer. Phrase deck: `en:` field optional.
- Output may go LIVE with the viewer alone (no Zoom URL): "Hold to go LIVE · VIEWER ONLY".
- If the viewer is on and Zoom fails 3x, only the Zoom path is suspended (`zoom_suspended`),
  the viewer stays LIVE; without the viewer the old auto-pause applies.
- Relay publisher: one outbound HTTPS POST per message batch, bearer token from
  `SUBTALK_RELAY_TOKEN`, bounded queue (100), captions older than 20 s are dropped instead of
  delivered late (original queue time kept across retries; bug found by test and fixed), heartbeat
  every 15 s so guests can tell "quiet" from "source offline".
- Relay: token compared in constant time (SHA-256 digests + `timingSafeEqual`); fields
  whitelisted and length-capped; guest sockets are read-only; `ping` auto-answered by the runtime
  (no wake-up); max 3,000 sockets; no storage (memory only; history lost if the object is evicted).
- Zoom native translated captions: they translate Zoom's own automated captions, which cannot run
  while manual captions are on, so they can never translate SubTalk's feed; they also need a paid
  plan or add-on and are not known to support Mongolian. At best they are an alternative for
  Japanese-only remarks, used *instead of* SubTalk captions. Not planned for 7 Nov. Details in
  `docs/VIEWER.md`. Source guides conflict, so verify in the Thursday TEST meeting if wanted.

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
- Independent per-guest language choice: see D-013 (guest viewer, built 3 Nov).

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
