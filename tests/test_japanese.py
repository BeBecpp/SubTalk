"""Japanese captions (owner, 4 Nov): JA only / TR + EN + JA for the parts of the ceremony the
Japanese interpreter does not cover (vows, MC). Japanese is requested only while the caption
mode shows it, and gets the same guard rules as Turkish and English."""
import json
from pathlib import Path

import pytest

from subtalk.config import TranslateCfg, ZoomCfg
from subtalk.glossary.guards import Guards
from subtalk.glossary.service import GlossaryService
from subtalk.glossary.store import load_glossary
from subtalk.output.compose import compose_parts, min_parts, mode_max_chars, reading_time_s, split_into, width
from subtalk.translate.base import TranslateRequest
from subtalk.translate.claude import ClaudeTranslator
from test_claude_translator import FakeClient, lang, response
from test_pipeline import mn, now, rig, sent, settle  # noqa: F401  (rig is a fixture)

ROOT = Path(__file__).resolve().parents[1]
TRI = {"tr": "Yehova bizi seviyor.", "en": "Jehovah loves us.", "ja": "エホバは私たちを愛しておられます。"}


@pytest.fixture(scope="module")
def glossary():
    return load_glossary(ROOT / "resources/glossary", ROOT / "resources/bible")


# ------------------------------------------------------------------ composer
def test_trilingual_one_post_tr_en_ja_order():
    assert compose_parts(TRI, "TRILINGUAL", 50, layout="single_line") == [
        "TR: Yehova bizi seviyor. | EN: Jehovah loves us. | JA: エホバは私たちを愛しておられます。"]
    assert compose_parts(TRI, "JA", 84) == ["エホバは私たちを愛しておられます。"]
    assert compose_parts(TRI, "BILINGUAL", 70, layout="single_line") == [
        "TR: Yehova bizi seviyor. | EN: Jehovah loves us."]  # Japanese never leaks into TR + EN


def test_cjk_counts_double_width():
    assert width("abc") == 3 and width("エホバ") == 6 and width("ç ğ ı") == 5


def test_japanese_split_at_punctuation_and_particles_nothing_lost():
    ja = "Galkhuuさん、あなたはAkaneさんを愛し、敬い、大切にすることを約束しますか？"
    parts = split_into(ja, 3, 30, "ja")
    assert "".join(parts) == ja and len(parts) == 3
    assert all(width(p) <= 30 for p in parts)


def test_japanese_reference_and_numbers_never_split():
    ja = "エフェソス 5:25, 28, 29には、夫は妻を愛さなければならないと書かれています。"
    parts = split_into(ja, min_parts(ja, 24, "ja"), 24, "ja")
    assert "".join(parts) == ja
    assert any("エフェソス 5:25, 28, 29" in p for p in parts)


def test_trilingual_long_vow_parts_aligned_and_complete():
    t = {"tr": "Galkhuu, Akane'yi sevmeye, ona saygı göstermeye ve onunla ilgilenmeye söz veriyor musun?",
         "en": "Galkhuu, do you promise to love Akane, to respect her and to care for her?",
         "ja": "Galkhuuさん、あなたはAkaneさんを愛し、敬い、大切にすることを約束しますか？"}
    parts = compose_parts(t, "TRILINGUAL", 50, layout="single_line")
    assert len(parts) >= 2
    for p in parts:
        assert p.startswith("TR: ") and " | EN: " in p and " | JA: " in p
    assert "".join(p.split(" | JA: ")[1] for p in parts) == t["ja"]
    assert all(1.5 <= reading_time_s(p) <= 4.0 for p in parts)


def test_missing_japanese_sends_other_lines():
    assert compose_parts({"tr": "Merhaba.", "en": "Hello."}, "TRILINGUAL", 50, layout="single_line") == [
        "TR: Merhaba. | EN: Hello."]
    assert compose_parts({"tr": "Merhaba.", "en": "Hello."}, "JA", 84) == []


def test_mode_widths():
    z = ZoomCfg()
    assert mode_max_chars(z, "TR") == z.max_chars and mode_max_chars(z, "JA") == z.max_chars
    assert mode_max_chars(z, "BILINGUAL") == z.bilingual_max_chars
    assert mode_max_chars(z, "TRILINGUAL") == z.trilingual_max_chars


# ------------------------------------------------------------------ guards (synthetic sentences)
@pytest.mark.parametrize("src,out,ok,why", [
    ("Ехова биднийг хайрладаг.", "エホバは私たちを愛しておられます。", True, None),
    ("Ехова биднийг хайрладаг.", "神は私たちを愛しておられます。", False, "divine_name_missing"),
    ("Аав маань илтгэл тавина.", "父が話をします。", True, None),
    ("Аав маань илтгэл тавина.", "天の父が話をします。", False, "divine_reference_invented"),
    ("Аав маань илтгэл тавина.", "エホバが話をします。", False, "divine_name_invented"),
    ("Аав маань илтгэл тавина.", "父と妻が話をします。", False, "spouse_invented"),
    ("Аав маань илтгэл тавина.", "父が祈ります。", False, "prayer_invented"),
    ("Эфэс 5:33-т бичсэн.", "エフェソス 5:33に書かれています。", True, None),
    ("Эфэс 5:33-т бичсэн.", "エフェソス 5:34に書かれています。", False, "unsupported_citation:EPH 5:34"),
    ("Гурван хүүхэдтэй.", "子供が5人います。", False, "number_invented:5"),
    ("Тэр хүчтэй хүн.", "丈夫で、精神的に強い人です。", True, None),  # 丈夫 / 精神 are not spouse / God
    ("Гэрч нөхөртэй болсон.", "エホバの証人の夫を持ちました。", True, None),
])
def test_japanese_guards(glossary, src, out, ok, why):
    a = GlossaryService(glossary).analyze(src, "mn")
    g = Guards(glossary).check(src, "mn", a.citations, out, target="ja")
    assert g.ok is ok, g.blocked
    if why:
        assert why in g.blocked


def test_japanese_reference_rendering(glossary):
    a = GlossaryService(glossary).analyze("Эфэс 5:25,28,29-д бичсэн.", "mn")
    assert list(a.renderings["ja"].values()) == ["エフェソス 5:25, 28, 29"]


def test_full_width_digits_normalised(glossary):
    src = "Эфэс 5:33-т бичсэн."
    a = GlossaryService(glossary).analyze(src, "mn")
    g = Guards(glossary).check(src, "mn", a.citations, "エフェソス ５:３３に書かれています。", target="ja")
    assert g.ok and "5:33" in g.text


# ------------------------------------------------------------------ Claude request
async def test_japanese_requested_only_when_asked(glossary):
    ok3 = {"turkish": lang("Merhaba."), "english": lang("Hello."), "japanese": lang("こんにちは。"),
           "segment_kind": "speech", "bible_mentions": []}
    ok2 = {k: v for k, v in ok3.items() if k != "japanese"}
    fake = FakeClient([response(ok2), response(ok3)])
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=fake)
    a = GlossaryService(glossary).analyze("Сайн байна уу.", "mn")
    base = dict(event_id=1, lang="mn", text="Сайн байна уу.", asr_quality="ok", hints=a.hints,
                citations=a.citations, citation_renderings=a.citation_renderings)
    r2 = await t.translate(TranslateRequest(**base))
    r3 = await t.translate(TranslateRequest(**base, targets=("tr", "en", "ja")))
    s2 = fake.calls[0]["output_config"]["format"]["schema"]
    s3 = fake.calls[1]["output_config"]["format"]["schema"]
    assert "japanese" not in s2["properties"] and "japanese" in s3["required"]
    assert json.loads(fake.calls[1]["messages"][0]["content"])["TARGETS"] == ["Turkish", "English", "Japanese"]
    assert r2.display_ja is None and r2.turkish == "Merhaba."
    assert r3.display_ja and r3.japanese == "こんにちは。" and r3.english == "Hello."
    assert len(fake.calls) == 2  # still ONE request per event


async def test_japanese_missing_does_not_drop_other_languages(glossary):
    bad = {"turkish": lang("Merhaba."), "english": lang("Hello."), "segment_kind": "speech", "bible_mentions": []}
    t = ClaudeTranslator(TranslateCfg(), glossary, "k", client=FakeClient([response(bad)]))
    r = await t.translate(TranslateRequest(1, "mn", "Сайн байна уу.", "ok", [], [], {}, targets=("tr", "en", "ja")))
    assert r.display and r.display_en and r.lang("ja").error == "missing_language"


# ------------------------------------------------------------------ pipeline
async def test_pipeline_trilingual_one_post(rig):  # noqa: F811
    p, tr, mz = rig
    tr.translations = {"Сайн байна уу.": {"tr": "Merhaba.", "en": "Hello.", "ja": "こんにちは。"}}
    p.ctx.output_mode, p.ctx.caption_layout = "TRILINGUAL", "single_line"
    ev = p.submit_decision(mn("Сайн байна уу."), now() - 1, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["TR: Merhaba. | EN: Hello. | JA: こんにちは。"]
    assert ev.targets == ("tr", "en", "ja") and ev.out["ja"]["status"] == "PASS"
    assert ev.japanese == "こんにちは。" and ev.sent_mode == "TRILINGUAL"


async def test_pipeline_bilingual_does_not_request_japanese(rig):  # noqa: F811
    p, tr, mz = rig
    tr.translations = {"Сайн байна уу.": {"tr": "Merhaba.", "en": "Hello.", "ja": "こんにちは。"}}
    p.ctx.output_mode, p.ctx.caption_layout = "BILINGUAL", "single_line"
    ev = p.submit_decision(mn("Сайн байна уу."), now() - 1, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["TR: Merhaba. | EN: Hello."]
    assert ev.targets == ("tr", "en") and "ja" not in ev.out and ev.japanese is None


async def test_pipeline_japanese_guard_blocks_only_japanese(rig):  # noqa: F811
    p, tr, mz = rig
    tr.translations = {"Аав маань илтгэл тавина.": {"tr": "Babam konuşma yapacak.",
                                                    "en": "My father will give a talk.", "ja": "天の父が話をします。"}}
    p.ctx.output_mode, p.ctx.caption_layout = "TRILINGUAL", "single_line"
    ev = p.submit_decision(mn("Аав маань илтгэл тавина."), now() - 1, now())
    await settle(p, mz, 1)
    assert sent(mz) == ["TR: Babam konuşma yapacak. | EN: My father will give a talk."]
    assert ev.out["ja"]["status"] == "WITHHELD" and ev.out["tr"]["status"] == "PASS"


# ------------------------------------------------------------------ invalid reference spelled out (4 Nov live finding)
@pytest.mark.parametrize("target,out", [
    ("en", "Ephesians chapter five, verse thirty-four."),
    ("en", "Ephesians 5, the thirty-fourth verse."),
    ("tr", "Efes beşinci bölüm, otuz dördüncü ayet."),
    ("tr", "Efesliler 5, 34. ayet."),
    ("ja", "エフェソス5章34節。"),
    ("ja", "エフェソス五章三十四節。"),
])
def test_invalid_reference_never_shown_even_in_words(glossary, target, out):
    src = "Эфэс тавдугаар бүлгийн гучин дөрөвдүгээр зүйл."  # Ephesians 5:34 does not exist
    a = GlossaryService(glossary).analyze(src, "mn")
    assert any(c.status == "invalid" for c in a.citations)
    g = Guards(glossary).check(src, "mn", a.citations, out, target=target)
    assert not g.ok and any(b.startswith("invalid_reference_shown") for b in g.blocked)


def test_invalid_reference_surrounding_words_still_pass(glossary):
    src = "Эфэс тавдугаар бүлгийн гучин дөрөвдүгээр зүйлд гэрлэлтийн тухай бичсэн."
    a = GlossaryService(glossary).analyze(src, "mn")
    g = Guards(glossary).check(src, "mn", a.citations, "Efeslilerde evlilik hakkında yazılmıştır.", target="tr")
    assert g.ok, g.blocked
