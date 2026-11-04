# Session handoff

Update this file at every milestone. It must make sense with no chat history.

## CURRENT OBJECTIVE
Zoom-first bilingual captions for 7 Nov 2026 (owner course correction, 3 Nov): Turkish + English in
ONE Zoom caption stream, faithful JW terminology, p50 ≤ 4 s / p95 ≤ 7 s speech-end → visible,
reliable recovery. Guest viewer / Cloudflare relay = disabled experimental code (do not extend).

## SCHEDULE (owner-confirmed)
- Tue 3 - Wed 4 Nov: implementation + mock/live tests (budget ≤ $5).
- Thu 5 Nov: real Zoom TEST meeting with desktop + phone participants.
- Fri 6 Nov: dress rehearsal on production equipment (Computer B GPU Whisper); freeze if it passes.
- Sat 7 Nov: wedding with the frozen release. Never claim Thu/Fri tests passed before they run.

## CURRENT GIT COMMIT
`11cebc6` + 4 Nov work (Zoom findings, sender 403 fix, prompt fix, soak harness): see `git log`.

## LAST COMPLETED TASK (4 Nov)
- Real Zoom TEST meeting (web client, host view, separate instant meeting): bilingual captions
  shown; `\n` NOT rendered → default layout `single_line` ("TR: … | EN: …"), observed readable;
  Pause/Resume/Stop/E-STOP behaved; Zoom POST p50 0.49 s / p95 1.24 s.
- Found + fixed: timeout then same-seq retry → HTTP 403 was counted as a failure (D-015).
- Live eval 52 cases after a prompt fix ("ах эгч нар аа" → "kardeşler" / "brothers and sisters"):
  TR 49 pass / 0 FAIL / 1 withheld (correct) / 2 n/a; EN same. Claude p50 1.64 s, p95 2.39 s.
- Dev laptop CPU cannot run Whisper live (D-016). faster-whisper `asr` extra now installed in .venv.
- Soak harness `subtalk soak` (mock Claude + mock Zoom, faults + invariants); 60 s run: 0 violations.

- S-41 talk (owner's MN script + TR/EN translation, private in `event/talk/`): verse lists fixed,
  two over-strict guards relaxed (85/87 human paragraphs pass), names file + bilingual phrase deck
  with vows created in `event/` (D-018). Friday checklist: `docs/REHEARSAL_FRIDAY.md`.
- Computer B: RTX 4060 8 GB, 32 GB RAM, i9 (owner, 4 Nov). Rehearsal with the real mic on Friday.
- Japanese captions (owner, 4 Nov evening; D-019): modes JA / TRILINGUAL (TR + EN + JA) for vows
  and MC (the talk has a live Japanese interpreter → TR + EN there). Live eval clean; found and fixed
  an invalid verse spelled out in words. JA phrase deck = Claude draft, needs a native check.

## VERIFIED TEST RESULTS
See `docs/TEST_REPORT.md`.

## ACTIVE BUGS / BLOCKERS
- Whether `\n` renders in the Zoom desktop app / phone app: unknown (Thursday). Single line works.
- GPU Whisper latency unknown until Computer B (Friday). Target needs ASR ≲ 1.2 s per segment.
- 60 English glossary entries still `en_review_required`; Turkish/English human review pending.
- `config/local.yaml` on the dev laptop has `allow_real_zoom: true` (TEST only; EVENT disabled).
- A partial `large-v3` download (~196 MB) is in the HuggingFace cache (harmless; can be deleted).

## CURRENT API TEST SPEND
$0.8727 of $5.00 (301 requests, ledger `state/api_test_budget.json`). Spend before this budget ~$0.19.

## REMAINING TEST BUDGET
$3.6273 before the automatic stop at $4.50.

## EXACT NEXT ACTION
1. Thursday: Zoom TEST meeting with a desktop-app participant and a phone (Show Captions on);
   include row 18 (TR + EN + JA readability).
2. Before Friday: Computer B install + `large-v3` download (`docs/REHEARSAL_FRIDAY.md` section 0).
3. Friday: follow `docs/REHEARSAL_FRIDAY.md`; decide vows (auto vs phrase deck); freeze.

## COMMAND TO REPRODUCE
```
.venv\Scripts\python.exe -m pytest -q -m "not live and not e2e"
.venv\Scripts\python.exe -m pytest -q -m e2e
.venv\Scripts\subtalk.exe soak --seconds 600 --faults 1.5
.venv\Scripts\subtalk.exe budget
```

## REQUIRED HUMAN INPUT, IF ANY
- Thursday: start a TEST meeting, paste its caption API token into the dashboard; a second person
  (desktop app) and a phone join with Show Captions on.
- Friday: Computer B with the NVIDIA GPU, mixer audio, Mongolian test recordings.
- Turkish/English reviewers for translations and glossary terms.
- A Japanese native speaker: JA phrase deck (vows) and the Japanese spelling of the names.
