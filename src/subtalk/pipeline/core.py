"""Translation pipeline: recognised segments -> events -> Claude -> guards -> ordered captions.

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
from subtalk.output.controller import OutputController
from subtalk.pipeline.emitter import OrderedEmitter
from subtalk.pipeline.events import EventStore, SpeechEvent
from subtalk.pipeline.script_context import NullScriptContext, ScriptContextProvider
from subtalk.state import JaPolicy
from subtalk.translate.base import (
    BudgetExceeded,
    ContextPair,
    InterpretRequest,
    MnCandidate,
    TranslateRequest,
    TranslatorError,
)

log = logging.getLogger("subtalk.pipeline")


@dataclass
class ContextState:
    main_speech: bool = False
    main_speech_by: str | None = None
    ja_policy: JaPolicy = JaPolicy.AUTO
    auto_matching_enabled: bool = True
    music_mode: bool = False
    speaker_control_enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "main_speech": self.main_speech, "main_speech_by": self.main_speech_by,
            "ja_policy": self.ja_policy.value, "auto_matching_enabled": self.auto_matching_enabled,
            "music_mode": self.music_mode, "speaker_control_enabled": self.speaker_control_enabled,
        }


@dataclass
class PipelineStats:
    counts: dict = field(default_factory=dict)

    def inc(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1


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
                         main_speech=self.ctx.main_speech, reasons=list(d.reasons), asr_ms=asr_ms)
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

    # ------------------------------------------------------------------ Mongolian
    def _request(self, ev: SpeechEvent, lang: str, cls=TranslateRequest, **extra):
        analysis = self.gsvc.analyze(ev.text, lang)
        ev.citations = [r for r in analysis.citation_renderings.values() if r]
        req = cls(
            event_id=ev.id, lang=lang, text=ev.text, asr_quality=ev.quality, hints=analysis.hints,
            citations=analysis.citations, citation_renderings=analysis.citation_renderings,
            recent=list(self.recent_pairs[-self.cfg.translate.recent_context:]),
            main_speech=self.ctx.main_speech,
            script_context=self.script.context_for(ev.text, lang) if self.script.is_active() else None,
            **extra,
        )
        return req, analysis

    async def _call(self, ev: SpeechEvent, fn, req):
        ev.status = "translating"
        self._on_change()
        try:
            async with self._sem:
                result = await fn(req)
        except BudgetExceeded as exc:
            self._on_alert("critical", "claude_budget", str(exc))
            raise
        except TranslatorError as exc:
            ev.reasons.append(f"translate_error:{exc}")
            self._on_alert("warn", "claude_error", f"Translation failed: {exc}")
            raise
        ev.translate_ms = result.latency_ms
        ev.model = result.model
        ev.flags = list(result.flags)
        return result

    async def _translate_mn(self, ev: SpeechEvent, slot: int) -> None:
        done = self._pending_mn[ev.id]
        try:
            req, analysis = self._request(ev, "mn")
            try:
                result = await self._call(ev, self.translator.translate, req)
            except (TranslatorError, BudgetExceeded):
                ev.status = "failed"
                self.emitter.skip(slot)
                return
            if not result.display or result.source_quality == "garbled" or not result.turkish:
                self.emitter.skip(slot)
                self._hold_or_drop(ev, f"translator:{result.source_quality}{':' + result.error if result.error else ''}")
                return
            if result.source_quality == "degraded":
                ev.flags.append("degraded_source")
            self._finish(ev, slot, "mn", analysis, result.turkish)
        finally:
            done.set()
            self._pending_mn.pop(ev.id, None)
            self._on_change()

    def _finish(self, ev: SpeechEvent, slot: int, lang: str, analysis, turkish: str, source_text: str | None = None) -> bool:
        g = self.guards.check(source_text or ev.text, lang, analysis.citations, turkish)
        ev.flags.extend(g.flags)
        if not g.ok:
            ev.status = "blocked"
            ev.reasons.extend(g.blocked)
            ev.turkish = g.text
            self.stats.inc("blocked")
            self._on_alert("info", "guard_blocked", f"Caption withheld ({', '.join(g.blocked)})")
            self.emitter.skip(slot)
            return False
        ev.turkish = g.text
        ev.status = "translated"
        self.recent_pairs.append(ContextPair(lang, ev.text, g.text))
        self.recent_pairs = self.recent_pairs[-20:]
        self.emitter.complete(slot, g.text, ev.t_end, (ev.id,))
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
        try:
            result = await self._call(ev, self.translator.translate, req)
        except (TranslatorError, BudgetExceeded):
            ev.status = "failed"
            self.emitter.skip(slot)
            return
        ev.relation = "independent"
        if not result.display or not result.turkish or result.source_quality == "garbled":
            ev.status = "dropped"
            self.emitter.skip(slot)
            return
        self._finish(ev, slot, "ja", analysis, result.turkish)
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
            and e.status in ("translated", "published", "held", "dropped", "blocked")
        ]
        if not cands:
            if self.ctx.main_speech:
                ev.reasons.append("no_recent_mongolian")
            await self._translate_ja_independent(ev, slot)
            return
        candidates = [
            MnCandidate(e.id, e.text, e.turkish if e.status in ("translated", "published") else None,
                        "published" if e.status in ("translated", "published") else
                        ("held" if e.status == "held" else "dropped"))
            for e in cands[-8:]
        ]
        req, analysis = self._request(ev, "ja", InterpretRequest, candidates=candidates)
        try:
            result = await self._call(ev, self.translator.interpret, req)
        except (TranslatorError, BudgetExceeded):
            ev.status = "failed"
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
            for i in links:
                mn = self.events.get(i)
                if mn and mn.status in ("translated", "published"):
                    mn.flags.append("confirmed_by_ja")
            self.stats.inc("ja_covered")
            self.emitter.skip(slot)
        elif relation == "conflict":
            ev.status = "conflict"
            self.stats.inc("ja_conflict")
            note = result.conflict_note or "Japanese interpretation differs from a shown caption"
            self._on_alert("warn", "ja_conflict", f"Possible caption error (cannot be undone): {note}")
            self.emitter.skip(slot)
        elif not result.display or not result.turkish:
            ev.status = "dropped"
            self.emitter.skip(slot)
        else:
            if self._finish(ev, slot, "ja", analysis, result.turkish) and relation == "resolves_held":
                for i in links:
                    mn = self.events.get(i)
                    if mn and mn.status == "held":
                        mn.status = "recovered"
                        mn.links.append(ev.id)
                self.stats.inc("recovered")
        self._on_change()

    # ------------------------------------------------------------------ output
    def _emit(self, text: str, speech_end: float, source_ids: tuple) -> None:
        accepted = self.output.submit(text, speech_end=speech_end, source_event_ids=source_ids)
        for i in source_ids:
            ev = self.events.get(i)
            if ev:
                ev.status = "published" if accepted else "not_sent"
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
