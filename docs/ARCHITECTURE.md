# Architecture

## Components (one engine process + one ASR worker process)

| Module | Responsibility |
|---|---|
| `sender/subtalk_sender.py` | Computer A. Shared-mode capture → mono 16 kHz → 20 ms PCM frames over WebSocket. Bounded buffer, drops audio captured while disconnected. |
| `audio/` | Frame protocol, ingest health (lost / flatline / level / gaps), Silero or energy VAD, streaming segmenter (pre/post-roll, silence end, quiet-point force cut at 9 s, flush on gaps). |
| `asr/worker.py`, `asr/engines.py` | faster-whisper in a **separate process** (crash/OOM isolation, restart, fallback model). |
| `asr/langid.py` | MN / JA / UNCERTAIN / NONSPEECH / OVERLAP decision. |
| `glossary/` | JW Glossary v1 loader + validation, term selection, Bible citation parser, output guards. |
| `translate/` | Claude translator (structured outputs) and a deterministic mock. |
| `pipeline/` | Speech events, ordered caption release, Japanese interpreter matching, Main Speech behaviour, inactive script-context plugin. |
| `output/` | Caption output state machine + safety gate + delivery loop; chunking. |
| `zoom/` | Caption URL validation, Zoom sender (official seq rules), strict mock endpoint. |
| `engine.py` | Wires everything, authoritative state, health, verdict, alerts, command execution. |
| `server/` | FastAPI: login, command API, state WebSocket, audio WebSocket; sessions and control lease. |
| `web/` | Operator dashboard and Speaker page (plain HTML/CSS/JS, no build step). |

## State (three independent dimensions)

* **Engine:** OFF → INITIALIZING → READY ⇄ RUNNING ⇄ DEGRADED, FAULT. Stop/start engine only stops
  audio processing; it never touches Zoom.
* **Caption output:** DISCONNECTED → CONNECTED → LIVE ⇄ PAUSED → (stop) CONNECTED; any state →
  E_STOPPED (latched; reset returns to CONNECTED, never LIVE). Target TEST or EVENT is an attribute.
* **Context:** Main Speech on/off, music mode, Japanese policy (Auto / All / Off), auto matching.

Every output transition bumps an **epoch**. Each speech event records the epoch when it entered the
pipeline; its caption is rejected if the epoch changed since (pause, stop, E-STOP, reset, reconnect,
going live), if output is not LIVE, if its speech ended at or before the moment output became LIVE,
or if it is older than `caption_ttl_s`. The epoch check does not depend on clock resolution: on
Windows with Python ≤ 3.12, `time.monotonic()` advances in 15.6 ms steps, so timestamp comparisons
alone could let same-tick work through (this was caught by CI and fixed). Pause drops; nothing
queues behind it.
E-STOP is synchronous: latch, clear queue, cancel the request in flight. A caption already accepted
by Zoom cannot be withdrawn.

Commands: role check → control lease (E-STOP exempt) → optional `expected_version` (rejects a
command issued from an out-of-date screen) → transition validation → idempotent `command_id`.
The UI marks buttons *requested* → *acknowledged* → *actual* (from the next state snapshot only).

## Caption delivery (Zoom's official rules)

From Zoom's support article *Using a third-party closed captioning service*:
`seq` +1 for each new caption; retries reuse the same `seq`; `GET …/closedcaption/seq` returns the
last received `seq`; body is plain UTF-8 text; retry errors and timeouts with randomized binary
exponential backoff for about 5 s, then move on.

* One request in flight (ordering).
* Retry budget = min(5 s, caption deadline).
* If an attempt timed out and the caption was finally abandoned, Zoom may or may not have accepted it,
  so the sequence becomes **AMBIGUOUS** and is re-read from `/seq` before the next caption. If every
  attempt received a definite HTTP error, the number was not used and is reused for the next caption.
* On connect the sequence comes from `/seq`; if that fails, from the local mirror
  `state/caption_seq.json` (keyed by URL fingerprint); otherwise sending is blocked.
* `lang` is not sent (not documented by Zoom).
* After 3 consecutive failed captions while LIVE, output auto-pauses with an alert.

## Language decision

Whisper's language probabilities (restricted to mn/ja) and forced-decode log-probabilities come from
the same model and are **not independent proof**; the writing system only counts on an unforced
decode. MN/JA requires agreement with margin (bigger margin for segments < 2 s); otherwise UNCERTAIN.
Hallucination patterns, high no-speech probability and repetition loops (compression ratio) are
NONSPEECH. Mixed Cyrillic + Japanese script in one decode is OVERLAP. Only MN and JA are translated.

## Translation and Japanese interpretation

* Mongolian: translated immediately. Garbled/uncertain → in Main Speech **held** (≤ 20 s) for the
  interpreter, otherwise dropped.
* Japanese, policy **Auto** + matching on: one Claude call compares it with recent Mongolian events
  (with their captions) → `covered` (suppressed), `new_content` (caption only the new part),
  `resolves_held` (caption the held meaning from Japanese), `independent`, `conflict` (alert only;
  earlier captions cannot be corrected). Links must refer to real candidate IDs.
* Policy **All**: every Japanese segment translated as independent. **Off**: never captioned.
  Matching disabled: suppressed in Main Speech, translated otherwise.
* Captions are released in speech order (`OrderedEmitter`); a slot that does not finish within 8 s
  is skipped so one slow request never blocks the rest.
* Main Speech never changes ordinary Mongolian translation (tested).

Claude request: byte-stable system prompt (rules + glossary policy) with prompt caching; one JSON
user message with only the source-triggered terms, locked Bible references, invalid references and
the last few caption pairs; `output_config.format` JSON schema; `effort: low`; server-side refusal
fallback (`fallbacks: "default"`); own timeout then one attempt on the fallback model; spend tracked
against soft/hard budgets.

## JW terminology and scripture safeguards

See [GLOSSARY.md](GLOSSARY.md). Guards run on every caption and **block** (omit) on: a Bible
reference not detected in the source, a missing or invented divine name, a divine reference when
none was spoken, invented prayer/vow/covenant/amen/verse concepts, digits not in the source, event
names not spoken, or output far longer than the source. Prepared scripture or script text is never
substituted in this release.

## Script intelligence (inactive)

`pipeline/script_context.py` defines `ScriptContextProvider`; the pipeline asks it for context only
when active, and `TranslateRequest.script_context` carries it. No provider is installed.
Substitution of prepared wording would need its own deterministic matching gate.

## Implementation decisions (deviations from the v4 plan)

| Plan | Built | Why |
|---|---|---|
| Vite + Preact dashboard | Plain HTML/CSS/JS, no build step | No Node on either computer, nothing to compile on Computer B, smaller and simpler to audit. |
| WinSW service | `scripts/start.ps1` supervisor loop + Task Scheduler autostart | Built into Windows, no third-party binary to download. |
| Segment overlap + word-timestamp de-duplication on forced cuts | Force cut at the quietest frame, no overlap | Simpler and avoids duplicated words; words can still be split on long continuous speech - measure in M0-B. |
| Separate LID model / music classifier | Not included | Optional, adopt only if M0-B shows they help (they add PyTorch). Music mode is the operational fallback. |
| Speech-to-Zoom latency from ASR | Measured from speech end (VAD) to Zoom 2xx | This is what guests experience. |

## Known limitations

* Whisper language ID on short segments and overlapping speakers is unreliable - those segments
  are dropped, which reduces coverage.
* Claude's self-reported quality is a hint; guards catch structural errors, not every meaning error.
* A caption shown in Zoom cannot be retracted; conflicts are only alerted.
* Real Zoom, real audio, real GPU and live Claude behaviour are unverified until M0-A/M0-B.
