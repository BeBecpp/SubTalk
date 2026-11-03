"""End-to-end mock pipeline: decision -> translation -> guards -> ordered output -> mock Zoom."""
import asyncio
import time
from pathlib import Path

import pytest

from subtalk.asr.langid import Decision
from subtalk.config import Config
from subtalk.glossary.guards import Guards
from subtalk.glossary.service import GlossaryService
from subtalk.glossary.store import load_glossary
from subtalk.output.controller import OutputConfig, OutputController
from subtalk.pipeline.core import ContextState, Pipeline
from subtalk.state import JaPolicy, Target
from subtalk.translate.base import TranslationResult
from subtalk.translate.mock import MockTranslator
from subtalk.zoom.sender import ZoomCaptionSender
from subtalk.zoom.seqstore import SeqStore
from conftest import clock_tick, go_live

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1/mock-zoom/closedcaption?id=mock&ns=mock&expire=86400&signature=mock"


@pytest.fixture(scope="module")
def glossary():
    return load_glossary(ROOT / "resources/glossary", ROOT / "resources/bible")


@pytest.fixture
async def rig(zoom_http, mock_zoom, glossary, tmp_path):
    cfg = Config()
    cfg.interp.wait_pending_s = 1.0
    sender = ZoomCaptionSender(zoom_http, SeqStore(tmp_path / "s.json"), attempt_timeout=0.5)
    out = OutputController(sender, OutputConfig(min_interval_s=0.0, caption_ttl_s=10))
    run = asyncio.create_task(out.run())
    await out.connect(URL, Target.TEST)
    await go_live(out)
    tr = MockTranslator({}, latency_s=(0.01, 0.03), seed=1)
    alerts = []
    p = Pipeline(cfg, GlossaryService(glossary), Guards(glossary), tr, out, ContextState(),
                 on_alert=lambda *a: alerts.append(a))
    p.alerts = alerts
    yield p, tr, mock_zoom
    run.cancel()


def mn(text, quality="ok"):
    return Decision("MN", text=text, quality=quality)


def ja(text):
    return Decision("JA", text=text)


async def settle(p, mock_zoom, n=None, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        await p.drain()
        p.tick()
        if n is None or len(mock_zoom.meeting().captions) >= n:
            if p.output.queue_len == 0:
                await asyncio.sleep(0.05)
                return
        await asyncio.sleep(0.02)


def sent(mock_zoom):
    return [c.text for c in mock_zoom.meeting().captions]


def now():
    return time.monotonic()


async def test_mongolian_translated_in_speech_order(rig):
    p, tr, mz = rig

    async def slow_first(req):
        await asyncio.sleep(0.3 if req.text == "Нэг" else 0.01)
        return TranslationResult(True, {"Нэг": "Bir.", "Хоёр": "İki."}[req.text])

    tr.translate = slow_first
    p.submit_decision(mn("Нэг"), now() - 1, now())
    p.submit_decision(mn("Хоёр"), now() - 1, now())
    await settle(p, mz, 2)
    assert sent(mz) == ["Bir.", "İki."]  # second finished first but is released second


async def test_interpreter_repetition_suppressed(rig):
    p, tr, mz = rig
    tr.translations = {
        "Гэрлэлтэд хайр хүндлэл чухал.": {"tr": "Evlilikte sevgi ve saygı önemlidir."},
        "結婚生活では愛と尊敬が大切です。": {"relation": "covered"},
    }
    p.ctx.main_speech = True
    a = p.submit_decision(mn("Гэрлэлтэд хайр хүндлэл чухал."), now() - 2, now())
    await settle(p, mz, 1)
    b = p.submit_decision(ja("結婚生活では愛と尊敬が大切です。"), now() - 2, now())
    await settle(p, mz)
    assert sent(mz) == ["Evlilikte sevgi ve saygı önemlidir."]
    assert b.status == "covered" and b.links == [a.id]
    assert "confirmed_by_ja" in a.flags


async def test_garbled_mongolian_held_then_recovered_from_japanese(rig):
    p, tr, mz = rig
    tr.translations = {
        "ммм ... бүрхэг": {"quality": "garbled", "tr": None},
        "お互いに忍耐強くありなさい。": {"relation": "resolves_held", "tr": "Birbirinize karşı sabırlı olun."},
    }
    p.ctx.main_speech = True
    a = p.submit_decision(mn("ммм ... бүрхэг", "low"), now() - 2, now())
    await settle(p, mz)
    assert a.status == "held" and sent(mz) == []
    b = p.submit_decision(ja("お互いに忍耐強くありなさい。"), now() - 2, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["Birbirinize karşı sabırlı olun."]
    assert a.status == "recovered" and b.relation == "resolves_held"


async def test_garbled_outside_main_speech_is_dropped_not_guessed(rig):
    p, tr, mz = rig
    tr.translations = {"ммм бүрхэг": {"quality": "garbled", "tr": None}}
    a = p.submit_decision(mn("ммм бүрхэг", "low"), now() - 2, now())
    await settle(p, mz)
    assert a.status == "dropped" and sent(mz) == []


async def test_independent_japanese_translated(rig):
    p, tr, mz = rig
    tr.translations = {"皆さん、今日は来てくださってありがとうございます。":
                       {"relation": "independent", "tr": "Herkese bugün geldiğiniz için teşekkürler."}}
    b = p.submit_decision(ja("皆さん、今日は来てくださってありがとうございます。"), now() - 2, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["Herkese bugün geldiğiniz için teşekkürler."]
    assert b.relation == "independent"


async def test_japanese_policy_suppress_and_caption_all(rig):
    p, tr, mz = rig
    tr.translations = {"ありがとう": {"tr": "Teşekkürler."}}
    p.ctx.ja_policy = JaPolicy.SUPPRESS
    a = p.submit_decision(ja("ありがとう"), now() - 2, now())
    await settle(p, mz)
    assert a.status == "suppressed" and sent(mz) == []
    p.ctx.ja_policy = JaPolicy.CAPTION_ALL
    p.submit_decision(ja("ありがとう"), now() - 2, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["Teşekkürler."]


async def test_main_speech_never_stops_mongolian_translation(rig):
    p, tr, mz = rig
    tr.translations = {"Сайн байна уу.": {"tr": "Merhaba."}, "Баярлалаа зочид оо.": {"tr": "Teşekkürler misafirler."}}
    p.ctx.main_speech = True
    p.submit_decision(mn("Сайн байна уу."), now() - 1, now())
    await settle(p, mz, 1)
    p.ctx.main_speech = False
    p.submit_decision(mn("Баярлалаа зочид оо."), now() - 1, now())
    await settle(p, mz, 2)
    assert sent(mz) == ["Merhaba.", "Teşekkürler misafirler."]


async def test_music_mode_drops_everything(rig):
    p, tr, mz = rig
    p.ctx.music_mode = True
    ev = p.submit_decision(mn("ла ла ла"), now() - 3, now())
    await settle(p, mz)
    assert ev.kind == "MUSIC" and ev.status == "dropped" and tr.requests == 0


SRC_REF = "Бид гэрлэлтийн тухай Библийн зарчим үзнэ."


async def test_guard_blocks_invented_reference_in_both_languages(rig):
    p, tr, mz = rig
    tr.translations = {SRC_REF: {"tr": "Evlilikle ilgili Kutsal Kitap ilkelerini, Efesliler 5:22'yi inceleyeceğiz.",
                                 "en": "We will look at Bible principles on marriage, Ephesians 5:22."}}
    p.ctx.output_mode = "BILINGUAL"
    ev = p.submit_decision(mn(SRC_REF), now() - 2, now())
    await settle(p, mz)
    assert ev.status == "blocked" and sent(mz) == []
    assert ev.out["tr"]["status"] == "WITHHELD" and ev.out["en"]["status"] == "WITHHELD"
    assert any(r.startswith("guard:unsupported_citation") for r in ev.out["en"]["reasons"])
    assert any(a[1] == "guard_blocked" for a in p.alerts)


async def test_one_language_blocked_other_still_sent(rig):
    """A problem in Turkish must not remove the English caption (and vice versa)."""
    p, tr, mz = rig
    tr.translations = {SRC_REF: {"tr": "Kutsal Kitap ilkelerini, Efesliler 5:22'yi inceleyeceğiz.",
                                 "en": "We will look at Bible principles about marriage."}}
    p.ctx.output_mode = "BILINGUAL"
    ev = p.submit_decision(mn(SRC_REF), now() - 2, now())
    await settle(p, mz, 1)
    assert ev.out["tr"]["status"] == "WITHHELD" and ev.out["en"]["status"] == "PASS"
    assert sent(mz) == ["EN: We will look at Bible principles about marriage."]
    # In Turkish-only mode the same event sends nothing (Turkish was withheld) - never English instead.
    p.ctx.output_mode = "TR"
    ev2 = p.submit_decision(mn(SRC_REF), now() - 2, now())
    await settle(p, mz)
    assert ev2.status == "not_sent" and len(sent(mz)) == 1


@pytest.mark.parametrize("mode,expected", [
    ("TR", ["Merhaba."]),
    ("EN", ["Hello."]),
    ("BILINGUAL", ["TR: Merhaba.\nEN: Hello."]),
])
async def test_output_modes(rig, mode, expected):
    p, tr, mz = rig
    tr.translations = {"Сайн байна уу.": {"tr": "Merhaba.", "en": "Hello."}}
    p.ctx.output_mode = mode
    ev = p.submit_decision(mn("Сайн байна уу."), now() - 1, now())
    await settle(p, mz, 1)
    assert sent(mz) == expected and ev.sent_mode == mode
    assert ev.out == {"tr": {"status": "PASS", "reasons": []}, "en": {"status": "PASS", "reasons": []}}
    assert len(mz.meeting().captions) == 1  # one POST per event, never two separate language posts


async def test_english_missing_marks_failed_turkish_sent(rig):
    p, tr, mz = rig
    tr.translations = {"Сайн байна уу.": {"tr": "Merhaba.", "fail_en": True}}
    p.ctx.output_mode = "BILINGUAL"
    ev = p.submit_decision(mn("Сайн байна уу."), now() - 1, now())
    await settle(p, mz, 1)
    assert ev.out["en"]["status"] == "FAILED" and ev.out["tr"]["status"] == "PASS"
    assert sent(mz) == ["TR: Merhaba."]


async def test_timeout_marks_both_timed_out(rig):
    from subtalk.translate.base import TranslatorTimeout
    p, tr, mz = rig

    async def boom(req):
        raise TranslatorTimeout("sonnet: APITimeoutError")

    tr.translate = boom
    ev = p.submit_decision(mn("Сайн байна уу."), now() - 1, now())
    await settle(p, mz)
    assert ev.status == "failed" and {o["status"] for o in ev.out.values()} == {"TIMED_OUT"}
    assert sent(mz) == [] and any(a[1] == "claude_timeout" for a in p.alerts)


async def test_stale_marks_language_stale(rig):
    p, tr, mz = rig
    p.output.cfg.caption_ttl_s = 0.05

    async def slow(req):
        await asyncio.sleep(0.2)
        return TranslationResult(True, "Geç.", english="Late.", display_en=True)

    tr.translate = slow
    p.ctx.output_mode = "BILINGUAL"
    ev = p.submit_decision(mn("Удаан"), now() - 1, now())
    await settle(p, mz)
    assert sent(mz) == [] and ev.out["tr"]["status"] == "STALE" and ev.out["en"]["status"] == "STALE"


async def test_interpreter_repeat_suppressed_in_both_languages(rig):
    p, tr, mz = rig
    tr.translations = {"Гэрлэлтэд хайр хүндлэл чухал.": {"tr": "Evlilikte sevgi ve saygı önemlidir.",
                                                         "en": "Love and respect matter in marriage."},
                       "結婚生活では愛と尊敬が大切です。": {"relation": "covered"}}
    p.ctx.main_speech, p.ctx.output_mode = True, "BILINGUAL"
    p.submit_decision(mn("Гэрлэлтэд хайр хүндлэл чухал."), now() - 2, now())
    await settle(p, mz, 1)
    b = p.submit_decision(ja("結婚生活では愛と尊敬が大切です。"), now() - 2, now())
    await settle(p, mz)
    assert sent(mz) == ["TR: Evlilikte sevgi ve saygı önemlidir.\nEN: Love and respect matter in marriage."]
    assert b.status == "covered" and {o["status"] for o in b.out.values()} == {"WITHHELD"}


async def test_bible_reference_passes_with_locked_rendering(rig):
    p, tr, mz = rig
    src = "Эфэс тавдугаар бүлэг гучин гуравдугаар зүйлийг уншъя."
    tr.translations = {src: {"tr": "Efesliler 5:33'ü okuyalım."}}
    ev = p.submit_decision(mn(src), now() - 2, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["Efesliler 5:33'ü okuyalım."]
    assert ev.citations == ["Efesliler 5:33"]


async def test_uncertain_language_never_captioned(rig):
    p, tr, mz = rig
    ev = p.submit_decision(Decision("UNCERTAIN", text="??", reasons=["lid_margin_low"]), now() - 2, now())
    await settle(p, mz)
    assert ev.status == "dropped" and sent(mz) == [] and tr.requests == 0


async def test_held_expires(rig):
    p, tr, mz = rig
    p.cfg.interp.hold_timeout_s = 0.05
    p.ctx.main_speech = True
    ev = p.submit_decision(Decision("UNCERTAIN", text="??"), now() - 1, now())
    assert ev.status == "held"
    await asyncio.sleep(0.1)
    p.tick()
    assert ev.status == "dropped" and "hold_expired" in ev.reasons


async def test_slow_translation_does_not_block_following_captions(rig):
    p, tr, mz = rig
    p.emitter._head_timeout = 0.2

    async def maybe_hang(req):
        if req.text == "Удаан":
            await asyncio.sleep(5)
        return TranslationResult(True, "Hızlı.")

    tr.translate = maybe_hang
    p.submit_decision(mn("Удаан"), now() - 1, now())
    p.submit_decision(mn("Хурдан"), now() - 1, now())
    end = time.monotonic() + 2
    while time.monotonic() < end and not sent(mz):
        p.tick()
        await asyncio.sleep(0.02)
    assert sent(mz) == ["Hızlı."]
    for t in list(p._tasks):
        t.cancel()


@pytest.mark.parametrize("interrupt", ["estop_reset_live", "pause_resume", "stop_live"])
async def test_translation_in_flight_across_output_change_is_dropped(rig, interrupt):
    """Speech recognised before an E-STOP / pause / stop must never be sent afterwards,
    even when output is LIVE again by the time its translation finishes."""
    p, tr, mz = rig
    out = p.output

    async def slow(req):
        await asyncio.sleep(0.4)
        return TranslationResult(True, "Eski cümle.")

    tr.translate = slow
    ev = p.submit_decision(mn("Хуучин өгүүлбэр"), now() - 1, now())
    await asyncio.sleep(0.05)  # translation is now in flight
    if interrupt == "estop_reset_live":
        out.estop()
        out.reset_estop()
    elif interrupt == "pause_resume":
        out.pause()
        out.resume()
        await clock_tick(out.live_since)
    else:
        out.stop()
    if out.state.value != "LIVE":
        await go_live(out)
    await settle(p, mz)
    assert sent(mz) == []
    assert out.history[-1].status in ("dropped_epoch", "dropped_before_live")
    assert ev.status == "not_sent"
    # Fresh speech after the change is captioned normally.
    tr.translate = lambda req: asyncio.sleep(0.01, result=TranslationResult(True, "Yeni cümle."))
    p.submit_decision(mn("Шинэ өгүүлбэр"), now() - 1, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["Yeni cümle."]


async def test_translation_failure_is_reported_not_guessed(rig):
    p, tr, mz = rig
    tr.fail_next = 1
    ev = p.submit_decision(mn("Сайн уу"), now() - 1, now())
    await settle(p, mz)
    assert ev.status == "failed" and sent(mz) == []
    assert any(a[1] == "claude_error" for a in p.alerts)


async def test_guest_viewer_gets_every_passed_language_whatever_the_zoom_mode(rig):
    from subtalk.output.viewer import ViewerFeed
    p, tr, mz = rig
    p.output.viewer = ViewerFeed(enabled=True)
    q = p.output.viewer.subscribe()
    tr.translations = {SRC_REF: {"tr": "Kutsal Kitap ilkelerini, Efesliler 5:22'yi inceleyeceğiz.",
                                 "en": "We will look at Bible principles about marriage."}}
    p.ctx.output_mode = "TR"
    ev = p.submit_decision(mn(SRC_REF), now() - 2, now())
    await settle(p, mz)
    assert sent(mz) == []  # Turkish withheld: Zoom (TR mode) gets nothing, never English instead
    msgs = [q.get_nowait() for _ in range(q.qsize())]
    caps = [m for m in msgs if m["type"] == "caption"]
    assert len(caps) == 1 and caps[0]["tr"] is None
    assert caps[0]["en"] == "We will look at Bible principles about marriage."
    assert caps[0]["event_id"] == ev.id and ev.status == "published" and ev.sent_mode == "VIEWER"
