"""Claude API translator (Anthropic Python SDK, structured outputs).

* stateless requests: cached system prompt + one JSON user message;
* ``output_config.format`` constrains the JSON shape (not the truth - the
  deterministic guards run afterwards);
* default model Sonnet 5.5 (owner's choice: middle tier) with adaptive
  thinking; ``output_config.effort`` (default low) bounds latency;
* server-side refusal fallback (``fallbacks: "default"``) when enabled;
* own timeout, then one attempt on ``fallback_model``; SDK retries disabled so
  latency stays bounded;
* spend is tracked; the hard budget stops new requests.
"""
from __future__ import annotations

import json
import logging
import time

import anthropic

from subtalk.config import TranslateCfg
from subtalk.translate.base import (
    BudgetExceeded,
    InterpretRequest,
    TranslateRequest,
    TranslationResult,
    TranslatorError,
    Usage,
)
from subtalk.translate.prompt import INTERPRET_SCHEMA, TRANSLATE_SCHEMA, build_system_prompt, build_user_message
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

    def __init__(self, cfg: TranslateCfg, glossary, api_key: str, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.glossary = glossary
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)
        self.system_prompt = build_system_prompt(glossary.policy)
        self.latency = Rolling()
        self.spent_usd = 0.0
        self.requests = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.fallback_used = 0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        self.cache_read_tokens = 0

    # ----------------------------------------------------------------- public
    async def translate(self, req: TranslateRequest) -> TranslationResult:
        return await self._run(req, TRANSLATE_SCHEMA)

    async def interpret(self, req: InterpretRequest) -> TranslationResult:
        return await self._run(req, INTERPRET_SCHEMA)

    async def warm_cache(self) -> None:
        """Pre-warm the prompt cache with a tiny real request (before going live)."""
        req = TranslateRequest(0, "mn", "Сайн байна уу.", "ok", [], [], {})
        await self.translate(req)

    def status(self) -> dict:
        return {
            "engine": self.name,
            "model": self.cfg.model,
            "fallback_model": self.cfg.fallback_model,
            "effort": self.cfg.effort,
            "requests": self.requests,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "fallback_used": self.fallback_used,
            "spent_usd": round(self.spent_usd, 4),
            "budget_soft_usd": self.cfg.budget_soft_usd,
            "budget_hard_usd": self.cfg.budget_hard_usd,
            "latency_ms": self.latency.summary(),
            "cache_read_tokens": self.cache_read_tokens,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
        }

    # --------------------------------------------------------------- internals
    async def _run(self, req: TranslateRequest, schema: dict) -> TranslationResult:
        if self.spent_usd >= self.cfg.budget_hard_usd:
            raise BudgetExceeded(f"Claude budget exhausted (${self.spent_usd:.2f})")
        user = build_user_message(req, self.glossary)
        start = time.monotonic()
        attempts = [(self.cfg.model, self.cfg.timeout_s)]
        if self.cfg.fallback_model and self.cfg.fallback_model != self.cfg.model:
            attempts.append((self.cfg.fallback_model, self.cfg.fallback_timeout_s))
        last_exc: Exception | None = None
        for i, (model, timeout) in enumerate(attempts):
            self.requests += 1
            try:
                resp = await self._call(model, timeout, user, schema)
            except _RETRYABLE as exc:
                last_exc = exc
                self.failures += 1
                self.last_error = f"{model}: {type(exc).__name__}"
                log.warning("claude.retryable", extra={"model": model, "error": type(exc).__name__})
                continue
            except anthropic.APIStatusError as exc:
                self.failures += 1
                self.consecutive_failures += 1
                self.last_error = f"{model}: HTTP {exc.status_code}"
                raise TranslatorError(self.last_error) from exc
            if i > 0:
                self.fallback_used += 1
            result = self._parse(resp, model)
            result.latency_ms = (time.monotonic() - start) * 1000
            self.latency.add(result.latency_ms)
            self.consecutive_failures = 0
            self.last_ok_at = time.time()
            return result
        self.consecutive_failures += 1
        raise TranslatorError(self.last_error or "translation failed") from last_exc

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

    def _parse(self, resp, model: str) -> TranslationResult:
        usage = self._account(resp, model)
        if getattr(resp, "stop_reason", None) == "refusal":
            return TranslationResult(display=False, turkish="", source_quality="garbled", model=model,
                                     usage=usage, error="refusal")
        if getattr(resp, "stop_reason", None) == "max_tokens":
            return TranslationResult(display=False, turkish="", model=model, usage=usage, error="max_tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return TranslationResult(display=False, turkish="", model=model, usage=usage, error="invalid_json")
        return TranslationResult(
            display=bool(data.get("display")),
            turkish=str(data.get("turkish") or "").strip(),
            source_quality=data.get("source_quality", "ok"),
            segment_kind=data.get("segment_kind", "speech"),
            bible_mentions=list(data.get("bible_mentions") or []),
            flags=list(data.get("flags") or []),
            relation=data.get("relation"),
            linked_ids=[int(x) for x in data.get("linked_ids") or [] if isinstance(x, int)],
            conflict_note=str(data.get("conflict_note") or ""),
            model=getattr(resp, "model", model) or model,
            usage=usage,
        )

    def _account(self, resp, model: str) -> Usage:
        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        price = self.cfg.prices.get(getattr(resp, "model", None) or model) or self.cfg.prices.get(model)
        if price:
            usage.cost_usd = (
                usage.input_tokens * price.input + usage.output_tokens * price.output
                + usage.cache_read * price.cache_read + usage.cache_write * price.cache_write
            ) / 1_000_000
        self.spent_usd += usage.cost_usd
        self.cache_read_tokens += usage.cache_read
        return usage
