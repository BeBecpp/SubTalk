"""Translation pipeline: recognised segments -> events -> Claude -> guards -> ordered captions.

One recognition event produces Turkish and English. Each language gets its own
status (PASS / WITHHELD / FAILED / TIMED_OUT / STALE) and its own guard check;
a problem in one language never removes the other. The operator's output mode
(TR / EN / BILINGUAL) decides what is sent to Zoom when the caption is released.

Main Speech Mode only changes interpreter-aware behaviour:
* low-quality / uncertain Mongolian is HELD (up to hold_timeout_s) so the
  interpreter's Japanese can recover its meaning;
* in AUTO policy an ambiguous Japanese segment is treated as interpretation
  (suppressed) rather than independent speech.
Ordinary Mongolian translation is identical with Main Speech on or off.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from subtalk.asr.langid import Decision
from subtalk.config import Config
from subtalk.glossary.guards import Guards
from subtalk.glossary.service import GlossaryService
from subtalk.output.compose import compose_parts
from subtalk.output.controller import OutputController
from subtalk.pipeline.emitter import OrderedEmitter
from subtalk.pipeline.events import EventStore, SpeechEvent
from subtalk.pipeline.script_context import NullScriptContext, ScriptContextProvider
from subtalk.state import JaPolicy
from subtalk.translate.base import (
    FAILED,
    PASS,
    STALE,
    TARGETS,
    TIMED_OUT,
    WITHHELD,
    BudgetExceeded,
    ContextPair,
    InterpretRequest,
    LangOutput,
    MnCandidate,
    TranslateRequest,
    TranslationResult,
    TranslatorError,
    TranslatorTimeout,
)

log = logging.getLogger("subtalk.pipeline")
MODE_LANGS = {"TR": ("tr",), "EN": ("en",), "BILINGUAL": ("tr", "en")}


@dataclass
class ContextState:
    main_speech: bool = False
    main_speech_by: str | None = None
    ja_policy: JaPolicy = JaPolicy.AUTO
    auto_matching_enabled: bool = True
    music_mode: bool = False
    speaker_control_enabled: bool = True
    output_mode: str = "TR"  # TR | EN | BILINGUAL

    def to_dict(self) -> dict:
        return {
            "main_speech": self.main_speech, "main_speech_by": self.main_speech_by,
            "ja_policy": self.ja_policy.value, "auto_matching_enabled": self.auto_matching_enabled,
            "music_mode": self.music_mode, "speaker_control_enabled": self.speaker_control_enabled,
            "output_mode": self.output_mode,
        }


@dataclass
class PipelineStats:
    counts: dict = field(default_factory=dict)

    def inc(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1


def _produced(o: LangOutput) -> bool:
    return bool(o.display and o.text and o.source_quality != "garbled" and not o.error)


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        glossary_service: GlossaryService,
        guards: Guards,
        translator,
        output: OutputController,
        context: ContextState,
        *,
        script_provider: ScriptContextProvider | None = None,
        on_change: Callable[[], None] | None = None,
        on_alert: Callable[[str, str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.gsvc = glossary_service
        self.guards = guards
        self.translator = translator
        self.output = output
        self.ctx = context
        self.script = script_provider or NullScriptContext()
        self._on_change = on_change or (lambda: None)
        self._on_alert = on_alert or (lambda *a: None)
        self._clock = clock
        self.events = EventStore(cfg.interp.history_max)
        self.emitter = OrderedEmitter(self._emit, cfg.ordering.head_timeout_s, clock)
        self._sem = asyncio.Semaphore(cfg.translate.concurrency)
        self._tasks: set[asyncio.Task] = set()
        self._pending_mn: dict[int, asyncio.Event] = {}
        self.stats = PipelineStats()
        self.recent_pairs: list[ContextPair] = []

    # ------------------------------------------------------------------ entry
    def submit_decision(self, d: Decision, t_start: float, t_end: float, asr_ms: float | None = None) -> SpeechEvent:
        kind = "MUSIC" if self.ctx.music_mode else d.kind
        ev = SpeechEvent(kind=kind, text=d.text, t_start=t_start, t_end=t_end, quality=d.quality,
                         main_speech=self.ctx.main_speech, reasons=list(d.reasons), asr_ms=asr_ms,
                         output_epoch=self.output.epoch)
        self.events.add(ev)
        self.stats.inc(f"seg_{kind}")
        if kind == "MUSIC":
            ev.status = "dropped"
            ev.reasons.append("music_mode")
        elif kind in ("NONSPEECH", "OVERLAP"):
            ev.status = "dropped"
        elif kind == "UNCERTAIN":
            self._hold_or_drop(ev, "uncertain_language")
        elif kind == "MN":
            slot = self.emitter.reserve()
            self._pending_mn[ev.id] = asyncio.Event()
            self._spawn(self._translate_mn(ev, slot))
        elif kind == "JA":
            self._route_ja(ev)
        self._on_change()
        return ev

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _hold_or_drop(self, ev: SpeechEvent, reason: str) -> None:
        ev.reasons.append(reason)
        if self.ctx.main_speech and self._ja_matching_on():
            ev.status = "held"
            self.stats.inc("held")
        else:
            ev.status = "dropped"

    def _ja_matching_on(self) -> bool:
        return self.ctx.ja_policy is JaPolicy.AUTO and self.ctx.auto_matching_enabled

    # ------------------------------------------------------------------ requests
    def _request(self, ev: SpeechEvent, lang: str, cls=TranslateRequest, **extra):
        analysis = self.gsvc.analyze(ev.text, lang)
        ev.citations = [r for r in analysis.citation_renderings.values() if r]
        req = cls(
            event_id=ev.id, lang=lang, text=ev.text, asr_quality=ev.quality, hints=analysis.hints,
            citations=analysis.citations, citation_renderings=analysis.citation_renderings,
            recent=list(self.recent_pairs[-self.cfg.translate.recent_context:]),
            main_speech=self.ctx.main_speech,
            script_context=self.script.context_for(ev.text, lang) if self.script.is_active() else None,
            renderings=analysis.renderings,
            **extra,
        )
        return req, analysis

    async def _call(self, ev: SpeechEvent, fn, req) -> TranslationResult | None:
        """Run a translation; on failure mark both languages and return None."""
        ev.status = "translating"
        self._on_change()
        try:
            async with self._sem:
                result = await fn(req)
        except BudgetExceeded as exc:
            self._on_alert("critical", "claude_budget", str(exc))
            self._mark_all(ev, FAILED, "budget")
            return None
        except TranslatorTimeout as exc:
            ev.reasons.append(f"translate_timeout:{exc}")
            self._on_alert("warn", "claude_timeout", f"Translation timed out: {exc}")
            self._mark_all(ev, TIMED_OUT, "timeout")
            return None
        except TranslatorError as exc:
            ev.reasons.append(f"translate_error:{exc}")
            self._on_alert("warn", "claude_error", f"Translation failed: {exc}")
            self._mark_all(ev, FAILED, str(exc)[:80])
            return None
        ev.translate_ms = result.latency_ms
        ev.model = result.model
        ev.flags = list(result.flags)
        return result

    @staticmethod
    def _mark_all(ev: SpeechEvent, status: str, reason: str) -> None:
        for t in TARGETS:
            ev.out[t] = {"status": status, "reasons": [reason]}
        ev.status = "failed"

    @staticmethod
    def _mark_unproduced(ev: SpeechEvent, result: TranslationResult) -> None:
        for t in TARGETS:
            o = result.lang(t)
            if o.timed_out:
                ev.out[t] = {"status": TIMED_OUT, "reasons": [o.error or "timeout"]}
            elif o.error:
                ev.out[t] = {"status": FAILED, "reasons": [o.error]}
            elif not _produced(o):
                ev.out[t] = {"status": WITHHELD, "reasons": [f"model:{o.source_quality}" if o.source_quality != "ok"
                                                              else "model:display_false"]}

    # ------------------------------------------------------------------ Mongolian
    async def _translate_mn(self, ev: SpeechEvent, slot: int) -> None:
        done = self._pending_mn[ev.id]
        try:
            req, analysis = self._request(ev, "mn")
            result = await self._call(ev, self.translator.translate, req)
            if result is None:
                self.emitter.skip(slot)
                return
            self._mark_unproduced(ev, result)
            if not any(_produced(result.lang(t)) for t in TARGETS):
                self.emitter.skip(slot)
                if all(ev.out.get(t, {}).get("status") in (FAILED, TIMED_OUT) for t in TARGETS):
                    ev.status = "failed"
                    return
                self._hold_or_drop(ev, f"translator:{result.source_quality}"
                                       f"{':' + result.error if result.error else ''}")
                return
            if result.source_quality == "degraded":
                ev.flags.append("degraded_source")
            self._finish(ev, slot, "mn", analysis, result)
        finally:
            done.set()
            self._pending_mn.pop(ev.id, None)
            self._on_change()

    def _finish(self, ev: SpeechEvent, slot: int, lang: str, analysis, result: TranslationResult) -> bool:
        """Guard each produced language; release the PASS ones together, in speech order."""
        texts: dict[str, str] = {}
        any_blocked = False
        for t in TARGETS:
            o = result.lang(t)
            if not _produced(o):
                continue  # already marked by _mark_unproduced
            g = self.guards.check(ev.text, lang, analysis.citations, o.text, target=t)
            ev.flags.extend(f"{t}:{f}" for f in g.flags)
            if g.ok:
                texts[t] = g.text
                ev.out[t] = {"status": PASS, "reasons": []}
            else:
                any_blocked = True
                ev.out[t] = {"status": WITHHELD, "reasons": [f"guard:{b}" for b in g.blocked], "text": g.text}
                self.stats.inc(f"blocked_{t}")
                self._on_alert("info", "guard_blocked",
                               f"{t.upper()} caption withheld ({', '.join(g.blocked)})")
        ev.turkish, ev.english = texts.get("tr"), texts.get("en")
        if not texts:
            ev.status = "blocked" if any_blocked else "dropped"
            if any_blocked:
                self.stats.inc("blocked")
                ev.reasons.extend(r for t in TARGETS for r in ev.out.get(t, {}).get("reasons", []))
            self.emitter.skip(slot)
            return False
        ev.status = "translated"
        self.recent_pairs.append(ContextPair(lang, ev.text, texts.get("tr", ""), texts.get("en", "")))
        self.recent_pairs = self.recent_pairs[-20:]
        self.emitter.complete(slot, texts, ev.t_end, (ev.id,), ev.output_epoch)
        return True

    # ------------------------------------------------------------------ Japanese
    def _route_ja(self, ev: SpeechEvent) -> None:
        policy = self.ctx.ja_policy
        if policy is JaPolicy.SUPPRESS:
            ev.status = "suppressed"
            ev.reasons.append("ja_policy_suppress")
            return
        slot = self.emitter.reserve()
        if policy is JaPolicy.CAPTION_ALL:
            self._spawn(self._translate_ja_independent(ev, slot))
        elif not self.ctx.auto_matching_enabled:
            # AUTO with matching disabled (rehearsal showed it unreliable):
            # interpreter context -> suppress; otherwise translate.
            if self.ctx.main_speech:
                ev.status = "suppressed"
                ev.reasons.append("auto_matching_disabled_main_speech")
                self.emitter.skip(slot)
            else:
                self._spawn(self._translate_ja_independent(ev, slot))
        else:
            self._spawn(self._interpret_ja(ev, slot))

    async def _translate_ja_independent(self, ev: SpeechEvent, slot: int) -> None:
        req, analysis = self._request(ev, "ja")
        result = await self._call(ev, self.translator.translate, req)
        if result is None:
            self.emitter.skip(slot)
            return
        ev.relation = "independent"
        self._mark_unproduced(ev, result)
        if not any(_produced(result.lang(t)) for t in TARGETS):
            ev.status = "dropped"
            self.emitter.skip(slot)
            return
        self._finish(ev, slot, "ja", analysis, result)
        self._on_change()

    async def _interpret_ja(self, ev: SpeechEvent, slot: int) -> None:
        window = self.cfg.interp.match_window_s
        pending = [e for e in self.events.recent("MN", window, self._clock()) if e.id in self._pending_mn]
        if pending:  # let in-flight Mongolian translations land first (bounded wait)
            waits = [self._pending_mn[e.id].wait() for e in pending if e.id in self._pending_mn]
            try:
                await asyncio.wait_for(asyncio.gather(*waits), self.cfg.interp.wait_pending_s)
            except asyncio.TimeoutError:
                ev.reasons.append("pending_mn_timeout")
        cands = [
            e for e in self.events.recent(None, window, self._clock())
            if e.kind in ("MN", "UNCERTAIN") and e.id != ev.id
            and e.status in ("translated", "published", "not_sent", "held", "recovered", "dropped", "blocked")
        ]
        if not cands:
            if self.ctx.main_speech:
                ev.reasons.append("no_recent_mongolian")
            await self._translate_ja_independent(ev, slot)
            return
        # "published" = a caption for it was produced (translated); whether output was live
        # is irrelevant for deciding if the Japanese repeats it.
        produced = ("translated", "published", "not_sent")
        candidates = [
            MnCandidate(e.id, e.text, e.turkish if e.status in produced else None,
                        "published" if e.status in produced else ("held" if e.status == "held" else "dropped"),
                        english=e.english if e.status in produced else None)
            for e in cands[-8:]
        ]
        req, analysis = self._request(ev, "ja", InterpretRequest, candidates=candidates)
        result = await self._call(ev, self.translator.interpret, req)
        if result is None:
            self.emitter.skip(slot)
            return
        valid_ids = {c.id for c in candidates}
        links = [i for i in result.linked_ids if i in valid_ids]
        relation = result.relation or "independent"
        if relation in ("covered", "resolves_held", "conflict") and not links:
            # A claimed link to nothing: in interpreter context suppress, else treat as independent.
            ev.reasons.append(f"{relation}_without_valid_link")
            relation = "covered" if self.ctx.main_speech else "independent"
        ev.relation, ev.links = relation, links
        if relation == "covered":
            ev.status = "covered"
            for t in TARGETS:  # the duplicate is suppressed consistently in both languages
                ev.out[t] = {"status": WITHHELD, "reasons": ["interpreter_repeat"]}
            for i in links:
                mn = self.events.get(i)
                if mn and mn.status in ("translated", "published", "not_sent"):
                    mn.flags.append("confirmed_by_ja")
            self.stats.inc("ja_covered")
            self.emitter.skip(slot)
        elif relation == "conflict":
            ev.status = "conflict"
            for t in TARGETS:
                ev.out[t] = {"status": WITHHELD, "reasons": ["interpreter_conflict"]}
            self.stats.inc("ja_conflict")
            note = result.conflict_note or "Japanese interpretation differs from a shown caption"
            self._on_alert("warn", "ja_conflict", f"Possible caption error (cannot be undone): {note}")
            self.emitter.skip(slot)
        else:
            self._mark_unproduced(ev, result)
            if not any(_produced(result.lang(t)) for t in TARGETS):
                ev.status = "dropped"
                self.emitter.skip(slot)
            elif self._finish(ev, slot, "ja", analysis, result) and relation == "resolves_held":
                for i in links:
                    mn = self.events.get(i)
                    if mn and mn.status == "held":
                        mn.status = "recovered"
                        mn.links.append(ev.id)
                self.stats.inc("recovered")
        self._on_change()

    # ------------------------------------------------------------------ output
    def _emit(self, texts: dict[str, str], speech_end: float, source_ids: tuple,
              origin_epoch: int | None = None) -> None:
        mode = self.ctx.output_mode
        parts = compose_parts(texts, mode, self.cfg.zoom.max_chars, self.cfg.zoom.bilingual_labels)
        sent_langs = [t for t in MODE_LANGS[mode] if texts.get(t)]
        accepted = False
        # The guest viewer takes every language, whatever the Zoom mode is.
        viewer = self.output.viewer_on and any(texts.get(t) for t in ("tr", "en"))
        if parts or viewer:
            accepted = self.output.submit("\n".join(parts), speech_end=speech_end, source_event_ids=source_ids,
                                          origin_epoch=origin_epoch, parts=parts, texts=texts)
        reject = self.output.last_reject if (parts or viewer) else f"no_{mode.lower()}_text"
        for i in source_ids:
            ev = self.events.get(i)
            if not ev:
                continue
            ev.status = "published" if accepted else "not_sent"
            ev.sent_mode = (mode if parts else "VIEWER") if accepted else None
            if not accepted:
                ev.reasons.append(reject or "not_sent")
                if reject == "dropped_stale":
                    for t in sent_langs:
                        ev.out[t] = {"status": STALE, "reasons": ["older than caption_ttl"]}
        self._on_change()

    def tick(self) -> None:
        """Called periodically: head-of-line timeout and held-event expiry."""
        self.emitter.tick()
        now = self._clock()
        for ev in self.events.all():
            if ev.status == "held" and now - ev.t_end > self.cfg.interp.hold_timeout_s:
                ev.status = "dropped"
                ev.reasons.append("hold_expired")
                self._on_change()

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
