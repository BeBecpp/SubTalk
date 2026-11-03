"""Claude API translator (Anthropic Python SDK, structured outputs).

* stateless requests: cached, byte-stable system prompt + one JSON user message;
* ``output_config.format`` constrains the JSON shape (not the truth - the
  deterministic guards run afterwards, per target language);
* default model Sonnet 5.5 (owner's choice: middle tier) with adaptive
  thinking; ``output_config.effort`` (default low) bounds latency;
* server-side refusal fallback (``fallbacks: "default"``) when enabled;
* own timeout, then one attempt on ``fallback_model``; SDK retries disabled so
  latency stays bounded;
* Turkish + English: ``dual_mode = "single_request"`` returns both languages
  from one request (default, see docs/DECISIONS.md); ``"parallel"`` sends one
  request per language concurrently. Japanese interpretation always uses one
  request so the duplicate decision is identical for both languages;
* runtime spend is tracked against the configured budget; an optional
  TestBudgetLedger enforces the persisted live-test budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import anthropic

from subtalk.budget import BudgetRefused, TestBudgetLedger, upper_bound_cost
from subtalk.config import TranslateCfg
from subtalk.translate.base import (
    BudgetExceeded,
    InterpretRequest,
    TranslateRequest,
    TranslationResult,
    TranslatorError,
    TranslatorTimeout,
    Usage,
)
from subtalk.translate.prompt import SINGLE_SCHEMA, build_system_prompt, build_user_message, dual_schema
from subtalk.util.stats import Rolling

log = logging.getLogger("subtalk.claude")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_NO_SERVER_FALLBACK = ("claude-haiku",)  # Haiku 5.5 has no server-side fallback

_RETRYABLE = (
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class ClaudeTranslator:
    name = "claude"

    def __init__(self, cfg: TranslateCfg, glossary, api_key: str, client: anthropic.AsyncAnthropic | None = None,
                 ledger: TestBudgetLedger | None = None, ledger_group: str = ""):
        self.cfg = cfg
        self.glossary = glossary
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)
        self.system_prompt = build_system_prompt(glossary.policy)
        self.ledger = ledger
        self.ledger_group = ledger_group
        self.latency = Rolling()
        self.spent_usd = 0.0
        self.requests = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.fallback_used = 0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.usage_by_model: dict[str, Usage] = {}

    # ----------------------------------------------------------------- public
    async def translate(self, req: TranslateRequest) -> TranslationResult:
        if self.cfg.dual_mode == "parallel":
            return await self._parallel(req)
        return await self._dual(req, interpret=False)

    async def interpret(self, req: InterpretRequest) -> TranslationResult:
        return await self._dual(req, interpret=True)

    async def warm_cache(self) -> None:
        """Pre-warm the prompt cache with a tiny real request (before going live)."""
        await self.translate(TranslateRequest(0, "mn", "Сайн байна уу.", "ok", [], [], {}))

    def status(self) -> dict:
        return {
            "engine": self.name,
            "model": self.cfg.model,
            "fallback_model": self.cfg.fallback_model,
            "effort": self.cfg.effort,
            "dual_mode": self.cfg.dual_mode,
            "requests": self.requests,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "fallback_used": self.fallback_used,
            "spent_usd": round(self.spent_usd, 4),
            "budget_soft_usd": self.cfg.budget_soft_usd,
            "budget_hard_usd": self.cfg.budget_hard_usd,
            "latency_ms": self.latency.summary(),
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
        }

    # --------------------------------------------------------------- modes
    async def _dual(self, req: TranslateRequest, interpret: bool) -> TranslationResult:
        user = build_user_message(req, self.glossary, ("tr", "en"))
        start = time.monotonic()
        resp, model, usage = await self._attempts(user, dual_schema(interpret))
        result = self._parse_dual(resp, model, usage, interpret)
        result.latency_ms = (time.monotonic() - start) * 1000
        result.latency_ms_en = result.latency_ms
        self.latency.add(result.latency_ms)
        return result

    async def _parallel(self, req: TranslateRequest) -> TranslationResult:
        async def one(target: str):
            t0 = time.monotonic()
            user = build_user_message(req, self.glossary, (target,))
            resp, model, usage = await self._attempts(user, SINGLE_SCHEMA)
            return self._parse_single(resp, model, usage), (time.monotonic() - t0) * 1000

        start = time.monotonic()
        tr_res, en_res = await asyncio.gather(one("tr"), one("en"), return_exceptions=True)
        if isinstance(tr_res, BudgetExceeded) or isinstance(en_res, BudgetExceeded):
            raise tr_res if isinstance(tr_res, BudgetExceeded) else en_res
        if isinstance(tr_res, Exception) and isinstance(en_res, Exception):
            raise tr_res
        result = TranslationResult(display=False, turkish="", model=self.cfg.model)
        if isinstance(tr_res, Exception):
            result.error = "timeout" if isinstance(tr_res, TranslatorTimeout) else f"api_error: {tr_res}"
        else:
            (lang, shared, usage, model), ms = tr_res
            result.display, result.turkish = lang["display"], lang["text"]
            result.source_quality, result.flags = lang["source_quality"], lang["flags"]
            result.error = lang.get("error")
            result.segment_kind, result.bible_mentions = shared["segment_kind"], shared["bible_mentions"]
            result.usage.add(usage)
            result.model = model
        if isinstance(en_res, Exception):
            result.display_en = None
            result.error_en = "timeout" if isinstance(en_res, TranslatorTimeout) else f"api_error: {en_res}"
            result.timed_out_en = isinstance(en_res, TranslatorTimeout)
        else:
            (lang, shared, usage, model), ms = en_res
            result.display_en, result.english = lang["display"], lang["text"]
            result.source_quality_en, result.flags_en = lang["source_quality"], lang["flags"]
            result.error_en = lang.get("error")
            result.latency_ms_en = ms
            result.usage.add(usage)
        result.latency_ms = (time.monotonic() - start) * 1000
        self.latency.add(result.latency_ms)
        return result

    # --------------------------------------------------------------- transport
    async def _attempts(self, user: str, schema: dict):
        if self.spent_usd >= self.cfg.budget_hard_usd:
            raise BudgetExceeded(f"Claude budget exhausted (${self.spent_usd:.2f})")
        attempts = [(self.cfg.model, self.cfg.timeout_s)]
        if self.cfg.fallback_model and self.cfg.fallback_model != self.cfg.model:
            attempts.append((self.cfg.fallback_model, self.cfg.fallback_timeout_s))
        last_exc: Exception | None = None
        timed_out = True
        for i, (model, timeout) in enumerate(attempts):
            rid = None
            if self.ledger is not None:
                price = self.cfg.prices.get(model)
                bound = upper_bound_cost(price, len(self.system_prompt) + len(user), self.cfg.max_tokens) if price else 0.05
                try:
                    rid = self.ledger.reserve(bound)
                except BudgetRefused as exc:
                    raise BudgetExceeded(str(exc)) from None
            self.requests += 1
            try:
                resp = await self._call(model, timeout, user, schema)
            except _RETRYABLE as exc:
                if rid is not None:
                    self.ledger.release(rid)  # nothing billable came back
                last_exc = exc
                timed_out = timed_out and isinstance(exc, anthropic.APITimeoutError)
                self.failures += 1
                self.last_error = f"{model}: {type(exc).__name__}"
                log.warning("claude.retryable", extra={"model": model, "error": type(exc).__name__})
                continue
            except anthropic.APIStatusError as exc:
                if rid is not None:
                    self.ledger.release(rid)
                self.failures += 1
                self.consecutive_failures += 1
                self.last_error = f"{model}: HTTP {exc.status_code}"
                raise TranslatorError(self.last_error) from exc
            usage = self._account(resp, model)
            if rid is not None:
                self.ledger.commit(rid, model=getattr(resp, "model", model) or model, cost_usd=usage.cost_usd,
                                   input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                                   cache_read=usage.cache_read, cache_write=usage.cache_write,
                                   group=self.ledger_group)
            if i > 0:
                self.fallback_used += 1
            self.consecutive_failures = 0
            self.last_ok_at = time.time()
            return resp, model, usage
        self.consecutive_failures += 1
        err = self.last_error or "translation failed"
        raise (TranslatorTimeout(err) if timed_out else TranslatorError(err)) from last_exc

    async def _call(self, model: str, timeout: float, user: str, schema: dict):
        params = dict(
            model=model,
            max_tokens=self.cfg.max_tokens,
            system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        client = self.client.with_options(timeout=timeout, max_retries=0)
        if self.cfg.server_fallbacks and not model.startswith(_NO_SERVER_FALLBACK):
            return await client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        return await client.messages.create(**params)

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _lang(obj, missing_error: str) -> dict:
        if not isinstance(obj, dict) or not isinstance(obj.get("display"), bool):
            return {"display": False, "text": "", "source_quality": "garbled", "flags": [], "error": missing_error}
        return {"display": obj["display"], "text": str(obj.get("text") or "").strip(),
                "source_quality": obj.get("source_quality", "ok"), "flags": list(obj.get("flags") or []),
                "error": None}

    def _decode(self, resp) -> tuple[dict | None, str | None]:
        reason = getattr(resp, "stop_reason", None)
        if reason == "refusal":
            return None, "refusal"
        if reason == "max_tokens":
            return None, "max_tokens"
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None, "invalid_json"
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None

    @staticmethod
    def _shared(data: dict) -> dict:
        return {"segment_kind": data.get("segment_kind", "speech"),
                "bible_mentions": list(data.get("bible_mentions") or [])}

    def _parse_dual(self, resp, model: str, usage: Usage, interpret: bool) -> TranslationResult:
        served = getattr(resp, "model", model) or model
        data, err = self._decode(resp)
        if err:
            return TranslationResult(display=False, turkish="", source_quality="garbled", model=served,
                                     usage=usage, error=err, display_en=False, error_en=err)
        tr = self._lang(data.get("turkish"), "missing_language")
        en = self._lang(data.get("english"), "missing_language")
        shared = self._shared(data)
        return TranslationResult(
            display=tr["display"], turkish=tr["text"], source_quality=tr["source_quality"], flags=tr["flags"],
            error=tr["error"], english=en["text"], display_en=en["display"] if en["error"] is None else None,
            source_quality_en=en["source_quality"], flags_en=en["flags"], error_en=en["error"],
            segment_kind=shared["segment_kind"], bible_mentions=shared["bible_mentions"],
            relation=data.get("relation") if interpret else None,
            linked_ids=[int(x) for x in data.get("linked_ids") or [] if isinstance(x, int)] if interpret else [],
            conflict_note=str(data.get("conflict_note") or "") if interpret else "",
            model=served, usage=usage,
        )

    def _parse_single(self, resp, model: str, usage: Usage):
        served = getattr(resp, "model", model) or model
        data, err = self._decode(resp)
        if err:
            return ({"display": False, "text": "", "source_quality": "garbled", "flags": [], "error": err},
                    {"segment_kind": "speech", "bible_mentions": []}, usage, served)
        return self._lang(data.get("caption"), "missing_language"), self._shared(data), usage, served

    def _account(self, resp, model: str) -> Usage:
        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        served = getattr(resp, "model", None) or model
        price = self.cfg.prices.get(served) or self.cfg.prices.get(model)
        if price:
            usage.cost_usd = (
                usage.input_tokens * price.input + usage.output_tokens * price.output
                + usage.cache_read * price.cache_read + usage.cache_write * price.cache_write
            ) / 1_000_000
        self.spent_usd += usage.cost_usd
        self.cache_read_tokens += usage.cache_read
        self.cache_write_tokens += usage.cache_write
        self.usage_by_model.setdefault(served, Usage()).add(usage)
        return usage
