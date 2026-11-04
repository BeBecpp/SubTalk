"""Deterministic mock translator for development (no paid API calls).

Produces Turkish and English (and Japanese when requested). ``translations`` maps a source
text to a spec: {"tr": ..., "en": ..., "ja": ..., "relation": ..., "quality": ..., "fail_en": bool}.
"""
from __future__ import annotations

import asyncio
import random

from subtalk.translate.base import InterpretRequest, TranslateRequest, TranslationResult, TranslatorError
from subtalk.util.stats import Rolling


class MockTranslator:
    name = "mock"

    def __init__(self, translations: dict[str, dict] | None = None, latency_s: tuple[float, float] = (0.3, 0.9),
                 seed: int | None = None):
        self.translations = translations or {}
        self.latency_s = latency_s
        self._rng = random.Random(seed)
        self.latency = Rolling()
        self.requests = 0
        self.fail_next = 0

    async def _delay(self) -> float:
        d = self._rng.uniform(*self.latency_s)
        await asyncio.sleep(d)
        return d * 1000

    @staticmethod
    def _english(spec: dict, text: str) -> tuple[bool | None, str, str | None]:
        if spec.get("fail_en"):
            return None, "", "missing_language"
        return True, spec.get("en") or f"[mock EN] {text}", None

    @staticmethod
    def _with_ja(res: TranslationResult, req: TranslateRequest, spec: dict | None) -> TranslationResult:
        if "ja" in req.targets and res.display:
            res.japanese = (spec or {}).get("ja") or f"[mock JA] {req.text}"
            res.display_ja = True
        elif "ja" in req.targets:
            res.display_ja, res.source_quality_ja = False, res.source_quality
        return res

    async def translate(self, req: TranslateRequest) -> TranslationResult:
        return self._with_ja(await self._translate(req), req, self.translations.get(req.text))

    async def _translate(self, req: TranslateRequest) -> TranslationResult:
        self.requests += 1
        ms = await self._delay()
        if self.fail_next:
            self.fail_next -= 1
            raise TranslatorError("mock failure")
        spec = self.translations.get(req.text)
        self.latency.add(ms)
        if spec is None:
            return TranslationResult(True, f"[mock TR] {req.text}", model="mock", latency_ms=ms,
                                     english=f"[mock EN] {req.text}", display_en=True)
        if spec.get("quality") == "garbled" or spec.get("tr") is None:
            return TranslationResult(False, "", source_quality="garbled", model="mock", latency_ms=ms,
                                     english="", display_en=False, source_quality_en="garbled")
        d_en, en, err_en = self._english(spec, req.text)
        return TranslationResult(True, spec["tr"], source_quality=spec.get("quality", "ok"), model="mock",
                                 latency_ms=ms, english=en, display_en=d_en, error_en=err_en)

    async def interpret(self, req: InterpretRequest) -> TranslationResult:
        return self._with_ja(await self._interpret(req), req, self.translations.get(req.text))

    async def _interpret(self, req: InterpretRequest) -> TranslationResult:
        self.requests += 1
        ms = await self._delay()
        self.latency.add(ms)
        spec = self.translations.get(req.text, {})
        relation = spec.get("relation", "independent")
        cand_ids = [c.id for c in req.candidates]
        if relation in ("covered", "resolves_held", "new_content", "conflict"):
            wanted = spec.get("link_status", "published" if relation != "resolves_held" else "held")
            links = [c.id for c in req.candidates if c.status == wanted][-1:]
        else:
            links = []
        display = relation in ("new_content", "resolves_held", "independent")
        d_en, en, err_en = self._english(spec, req.text) if display else (False, "", None)
        return TranslationResult(
            display=display, turkish=spec.get("tr", f"[mock TR] {req.text}") if display else "",
            relation=relation, linked_ids=[i for i in links if i in cand_ids],
            conflict_note=spec.get("note", ""), model="mock", latency_ms=ms,
            english=en, display_en=d_en, error_en=err_en,
        )

    def status(self) -> dict:
        return {"engine": self.name, "model": "mock", "requests": self.requests,
                "latency_ms": self.latency.summary(), "spent_usd": 0.0, "consecutive_failures": 0}
