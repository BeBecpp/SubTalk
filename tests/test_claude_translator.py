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


def response(payload, model="claude-sonnet-5-5", stop="end_turn"):
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


def lang(text, display=True, quality="ok"):
    return {"display": display, "text": text, "source_quality": quality, "flags": []}


# Dual contract: one request returns Turkish and English.
OK = {"turkish": lang("Merhaba."), "english": lang("Hello."), "segment_kind": "speech", "bible_mentions": []}


def single(text, display=True):
    return {"caption": lang(text, display), "segment_kind": "speech", "bible_mentions": []}


def req(glossary, text="Сайн байна уу."):
    a = GlossaryService(glossary).analyze(text, "mn")
    return TranslateRequest(1, "mn", text, "ok", a.hints, a.citations, a.citation_renderings)


async def test_request_shape(glossary):
    fake = FakeClient([response(OK)])
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=fake)
    r = await t.translate(req(glossary))
    assert r.display and r.turkish == "Merhaba."
    assert r.display_en and r.english == "Hello."
    assert len(fake.calls) == 1  # one request for both languages (single_request mode)
    call = fake.calls[0]
    schema = call["output_config"]["format"]["schema"]
    assert set(schema["required"]) >= {"turkish", "english"}
    user = json.loads(call["messages"][0]["content"])
    assert user["TARGETS"] == ["Turkish", "English"]
    assert call["beta"] is True and call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["model"] == "claude-sonnet-5-5"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in call and "temperature" not in call
    assert fake.options[0] == {"timeout": 5.0, "max_retries": 0}
    assert t.status()["spent_usd"] > 0


async def test_missing_english_does_not_invalidate_turkish(glossary):
    payload = {"turkish": lang("Merhaba."), "segment_kind": "speech", "bible_mentions": []}
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([response(payload)]))
    r = await t.translate(req(glossary))
    assert r.lang("tr").display and r.lang("tr").text == "Merhaba."
    en = r.lang("en")
    assert not en.display and en.error == "missing_language"


async def test_english_withheld_independently(glossary):
    payload = {"turkish": lang("Merhaba."), "english": lang("", False, "garbled"),
               "segment_kind": "speech", "bible_mentions": []}
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([response(payload)]))
    r = await t.translate(req(glossary))
    assert r.lang("tr").display and not r.lang("en").display and r.lang("en").error is None


async def test_parallel_mode_isolates_failures(glossary):
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    # tr: ok; en: primary + fallback both time out
    fake = FakeClient([response(single("Merhaba.")), timeout, timeout])

    class Ordered(FakeClient):
        pass

    cfg = TranslateCfg(dual_mode="parallel")
    t = ClaudeTranslator(cfg, glossary, "k", client=fake)
    r = await t.translate(req(glossary))
    # The first call is Turkish (gather starts tr first); English timed out twice.
    assert r.lang("tr").text == "Merhaba."
    en = r.lang("en")
    assert not en.display and en.timed_out and en.error == "timeout"
    assert len(fake.calls) == 3
    targets = [json.loads(c["messages"][0]["content"])["TARGETS"] for c in fake.calls]
    assert ["Turkish"] in targets and ["English"] in targets


async def test_total_timeout_is_classified(glossary):
    from subtalk.translate.base import TranslatorTimeout
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([timeout, timeout]))
    with pytest.raises(TranslatorTimeout):
        await t.translate(req(glossary))


async def test_ledger_reserves_commits_and_refuses(glossary, tmp_path):
    from subtalk.budget import TestBudgetLedger
    ledger = TestBudgetLedger(tmp_path / "ledger.json", limit_usd=5.0, stop_usd=4.5)
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([response(OK)]), ledger=ledger,
                         ledger_group="unit")
    await t.translate(req(glossary))
    s = ledger.summary()
    assert s["requests"] == 1 and s["spent_usd"] > 0 and s["by_group"]["unit"]["requests"] == 1
    assert ledger.reserved == 0  # reservation replaced by the actual cost
    # Persisted: a new ledger object (process restart) sees the same spend.
    again = TestBudgetLedger(tmp_path / "ledger.json")
    assert again.spent == ledger.spent
    # Nearly exhausted ledger refuses before any request is sent.
    tight = TestBudgetLedger(tmp_path / "tight.json", limit_usd=0.01, stop_usd=0.005)
    fake = FakeClient([response(OK)])
    t2 = ClaudeTranslator(TranslateCfg(), glossary, "k", client=fake, ledger=tight)
    with pytest.raises(BudgetExceeded):
        await t2.translate(req(glossary))
    assert fake.calls == []


async def test_ledger_releases_reservation_on_retryable_error(glossary, tmp_path):
    from subtalk.budget import TestBudgetLedger
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    ledger = TestBudgetLedger(tmp_path / "l.json")
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([timeout, response(OK)]), ledger=ledger)
    await t.translate(req(glossary))
    assert ledger.reserved == 0 and ledger.summary()["requests"] == 1


def test_ledger_never_loosens_recorded_limits(tmp_path):
    from subtalk.budget import TestBudgetLedger
    TestBudgetLedger(tmp_path / "l.json", limit_usd=5.0, stop_usd=4.5).commit(
        1, model="m", cost_usd=0.1, input_tokens=1, output_tokens=1, cache_read=0, cache_write=0)
    loose = TestBudgetLedger(tmp_path / "l.json", limit_usd=100.0, stop_usd=90.0)
    assert loose.stop == 4.5 and loose.limit == 5.0


async def test_system_prompt_is_stable(glossary):
    a = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([]))
    b = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([]))
    assert a.system_prompt == b.system_prompt  # byte-identical -> cacheable
    assert "Translate the actual spoken content" in a.system_prompt  # glossary policy included


async def test_timeout_falls_back_once(glossary):
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    fake = FakeClient([timeout, response(OK, model="claude-haiku-5-5")])
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=fake)
    r = await t.translate(req(glossary))
    assert r.turkish == "Merhaba."
    assert [c["model"] for c in fake.calls] == ["claude-sonnet-5-5", "claude-haiku-5-5"]
    assert fake.calls[1]["beta"] is False and "fallbacks" not in fake.calls[1]  # Haiku: no server fallback
    assert fake.options[1]["timeout"] == 4.0 and t.fallback_used == 1


def test_default_budget_is_twenty_dollars():
    cfg = TranslateCfg()
    assert (cfg.model, cfg.fallback_model, cfg.budget_hard_usd) == ("claude-sonnet-5-5", "claude-haiku-5-5", 20.0)


async def test_budget_stops_requests_once_spent(glossary):
    fake = FakeClient([response(OK, model="claude-sonnet-5-5")] * 3)
    t = ClaudeTranslator(TranslateCfg(budget_hard_usd=0.0005), glossary, "k", client=fake)
    await t.translate(req(glossary))  # costs $0.001 (100 in + 50 out + 1500 cache-read tokens at Sonnet prices)
    with pytest.raises(BudgetExceeded):
        await t.translate(req(glossary))
    assert len(fake.calls) == 1


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
    assert msg["LOCKED_REFERENCES"][0]["render_as_tr"] == "Efesliler 5:33"
    assert msg["LOCKED_REFERENCES"][0]["render_as_en"] == "Ephesians 5:33"
    first_cor = json.loads(build_user_message(req(glossary, "1 Коринт 13:4-8."), glossary))
    assert first_cor["LOCKED_REFERENCES"][0]["render_as_en"] == "1 Corinthians 13:4-8"
    assert first_cor["LOCKED_REFERENCES"][0]["render_as_tr"] == "1. Korintliler 13:4-8"
    bad = req(glossary, "Эфэс тавдугаар бүлгийн гучин дөрөвдүгээр зүйл.")
    msg = json.loads(build_user_message(bad, glossary))
    assert msg["LOCKED_REFERENCES"] == [] and msg["INVALID_REFERENCES"][0]["verse"] == 34


def test_interpret_message_lists_candidates(glossary):
    a = GlossaryService(glossary).analyze("ありがとう", "ja")
    r = InterpretRequest(5, "ja", "ありがとう", "ok", a.hints, a.citations, a.citation_renderings,
                         candidates=[MnCandidate(3, "Баярлалаа", "Teşekkürler", "published")])
    msg = json.loads(build_user_message(r, glossary))
    assert msg["TASK"] == "interpret" and msg["CANDIDATES"][0]["id"] == 3


def _live_key() -> str | None:
    """Key from the environment or the repo's .env - read here only, never exported,
    so other tests are not affected by local secrets."""
    key = anthropic_api_key()
    env = ROOT / ".env"
    if key or not env.exists():
        return key
    for line in env.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() in ("ANTHROPIC_API_KEY", "CLAUDE_KEY") and value.strip():
            return value.strip().strip('"').strip("'")
    return None


@pytest.mark.live
@pytest.mark.skipif(not _live_key(), reason="no ANTHROPIC_API_KEY / CLAUDE_KEY in environment or .env")
async def test_live_claude_smoke(glossary):
    """One real request (~$0.004): checks the API accepts the exact production request shape.
    Spend is recorded in the persisted test ledger (state/api_test_budget.json)."""
    from subtalk.budget import TestBudgetLedger
    ledger = TestBudgetLedger(ROOT / "state" / "api_test_budget.json")
    t = ClaudeTranslator(TranslateCfg(), glossary, _live_key(), ledger=ledger, ledger_group="smoke")
    r = await t.translate(req(glossary, "Эрхэм зочид оо, тавтай морилно уу."))
    assert r.display and r.turkish
    assert r.display_en and r.english
    print("LIVE EN:", r.english)
    print("LIVE:", r.model, round(r.latency_ms), "ms", r.turkish, t.status()["spent_usd"])
