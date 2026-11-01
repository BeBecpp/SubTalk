"""ClaudeTranslator against a fake SDK client (no network, no cost).

A live smoke test runs only when ANTHROPIC_API_KEY/CLAUDE_KEY is set:
    pytest -m live tests/test_claude_translator.py
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from subtalk.config import TranslateCfg, anthropic_api_key
from subtalk.glossary.service import GlossaryService
from subtalk.glossary.store import load_glossary
from subtalk.translate.base import BudgetExceeded, InterpretRequest, MnCandidate, TranslateRequest, TranslatorError
from subtalk.translate.claude import FALLBACK_BETA, ClaudeTranslator
from subtalk.translate.prompt import build_user_message

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def glossary():
    return load_glossary(ROOT / "resources/glossary", ROOT / "resources/bible")


def response(payload, model="claude-opus-5-5", stop="end_turn"):
    return SimpleNamespace(
        model=model, stop_reason=stop,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=1500,
                              cache_creation_input_tokens=0),
    )


class FakeMessages:
    def __init__(self, owner, beta):
        self.owner, self.beta = owner, beta

    async def create(self, **kw):
        self.owner.calls.append({"beta": self.beta, **kw})
        action = self.owner.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.options = []
        self.messages = FakeMessages(self, False)
        self.beta = SimpleNamespace(messages=FakeMessages(self, True))

    def with_options(self, **kw):
        self.options.append(kw)
        return self


OK = {"display": True, "turkish": "Merhaba.", "source_quality": "ok", "segment_kind": "speech",
      "bible_mentions": [], "flags": []}


def req(glossary, text="Сайн байна уу."):
    a = GlossaryService(glossary).analyze(text, "mn")
    return TranslateRequest(1, "mn", text, "ok", a.hints, a.citations, a.citation_renderings)


async def test_request_shape(glossary):
    fake = FakeClient([response(OK)])
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=fake)
    r = await t.translate(req(glossary))
    assert r.display and r.turkish == "Merhaba."
    call = fake.calls[0]
    assert call["beta"] is True and call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["model"] == "claude-opus-5-5"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in call and "temperature" not in call
    assert fake.options[0] == {"timeout": 5.0, "max_retries": 0}
    assert t.status()["spent_usd"] > 0


async def test_system_prompt_is_stable(glossary):
    a = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([]))
    b = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([]))
    assert a.system_prompt == b.system_prompt  # byte-identical -> cacheable
    assert "Translate the actual spoken content" in a.system_prompt  # glossary policy included


async def test_timeout_falls_back_once(glossary):
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    fake = FakeClient([timeout, response(OK, model="claude-sonnet-5-5")])
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=fake)
    r = await t.translate(req(glossary))
    assert r.turkish == "Merhaba."
    assert [c["model"] for c in fake.calls] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert fake.options[1]["timeout"] == 4.0 and t.fallback_used == 1


async def test_both_fail_raises(glossary):
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([timeout, timeout]))
    with pytest.raises(TranslatorError):
        await t.translate(req(glossary))
    assert t.consecutive_failures == 1


async def test_refusal_and_bad_json_never_display(glossary):
    bad = response(OK)
    bad.content[1].text = "not json"
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([response(OK, stop="refusal"), bad]))
    assert not (await t.translate(req(glossary))).display
    assert not (await t.translate(req(glossary))).display


async def test_budget_hard_cap(glossary):
    t = ClaudeTranslator(TranslateCfg(budget_hard_usd=0.0), glossary, "k", client=FakeClient([]))
    with pytest.raises(BudgetExceeded):
        await t.translate(req(glossary))


async def test_haiku_has_no_server_fallback(glossary):
    fake = FakeClient([response(OK, model="claude-haiku-5-5")])
    t = ClaudeTranslator(TranslateCfg(model="claude-haiku-5-5", fallback_model=None), glossary, "k", client=fake)
    await t.translate(req(glossary))
    assert fake.calls[0]["beta"] is False and "fallbacks" not in fake.calls[0]


def test_user_message_contains_locked_reference(glossary):
    r = req(glossary, "Эфэс тавдугаар бүлэг гучин гуравдугаар зүйл.")
    msg = json.loads(build_user_message(r, glossary))
    assert msg["LOCKED_REFERENCES"][0]["render_as"] == "Efesliler 5:33"
    bad = req(glossary, "Эфэс тавдугаар бүлгийн гучин дөрөвдүгээр зүйл.")
    msg = json.loads(build_user_message(bad, glossary))
    assert msg["LOCKED_REFERENCES"] == [] and msg["INVALID_REFERENCES"][0]["verse"] == 34


def test_interpret_message_lists_candidates(glossary):
    a = GlossaryService(glossary).analyze("ありがとう", "ja")
    r = InterpretRequest(5, "ja", "ありがとう", "ok", a.hints, a.citations, a.citation_renderings,
                         candidates=[MnCandidate(3, "Баярлалаа", "Teşekkürler", "published")])
    msg = json.loads(build_user_message(r, glossary))
    assert msg["TASK"] == "interpret" and msg["CANDIDATES"][0]["id"] == 3


@pytest.mark.live
@pytest.mark.skipif(not anthropic_api_key(), reason="no ANTHROPIC_API_KEY / CLAUDE_KEY in environment")
async def test_live_claude_smoke(glossary):
    """One real request (~$0.01): checks the request shape is accepted by the API."""
    t = ClaudeTranslator(TranslateCfg(), glossary, anthropic_api_key())
    r = await t.translate(req(glossary, "Эрхэм зочид оо, тавтай морилно уу."))
    assert r.display and r.turkish
    print("LIVE:", r.model, round(r.latency_ms), "ms", r.turkish, t.status()["spent_usd"])
