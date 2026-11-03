"""The live-eval checker itself must be right (in both languages) before it judges real Claude output."""
from pathlib import Path

import pytest

from subtalk.evalrun import (
    CHECKS,
    EXTRA_CASES,
    NOT_TEXT,
    CaseResult,
    LangCase,
    all_cases,
    check_lang,
    run_eval,
    summarize,
    write_report,
)
from subtalk.glossary.store import load_glossary
from subtalk.translate.base import TranslationResult

ROOT = Path(__file__).resolve().parents[1]

# Faithful reference renderings (what a correct translator returns).
GOOD = {
    "divine_name": ("Yehova bizi seviyor.", "Jehovah loves us."),
    "human_father": ("Babam bugün konuşma yapacak.", "My father will give a talk today."),
    "god_not_jehovah": ("Tanrı hakkında birlikte konuşalım.", "Let's talk together about God."),
    "brother_family": ("Ağabeyim Galkhuu konuşma yapacak.", "My own older brother Galkhuu will speak."),
    "brother_congregation": ("Cemaatimizden bir birader bugün geldi.", "A brother from our congregation came today."),
    "sister_not_nurse": ("Ablam bugün geldi.", "Our older sister came today."),
    "elder_office": ("Cemaatin ihtiyarı konuşma yapacak.", "A congregation elder will speak."),
    "ephesians_alias": ("Efesliler 5:33.", "Ephesians 5:33."),
    "ephesians_current": ("Efesliler 5:33.", "Ephesians 5:33."),
    "first_corinthians": ("1. Korintliler 13:4.", "1 Corinthians 13:4."),
    "genesis": ("Başlangıç 2:24.", "Genesis 2:24."),
    "deuteronomy": ("Kanun’un Tekrarı 6:4.", "Deuteronomy 6:4."),
    "revelation": ("Vahiy 21:4.", "Revelation 21:4."),
    "negation": ("Bu sözü vermedik.", "We did not make this promise."),
    "obligation_negation": ("Bunu şimdi yapmamalıyız.", "This must not be done now."),
    "vow_not_inferred": ("Biz ikimiz birbirimizi seviyoruz.", "The two of us love each other."),
    "explicit_vow": ("Evlilik yeminimizi edeceğiz.", "We will make our marriage vows."),
    "bible_explanation": ("Bu bir ayet değil, bir açıklamadır.", "This is not a Bible verse; it is an explanation."),
    "invalid_reference": ("Efesliler'i okuyalım.", "Let's read Ephesians."),
    "book_no_verse": ("Efesliler kitabını açın.", "Open the book of Ephesians."),
    "prayer": ("Şimdi birlikte dua edelim.", "Let us pray together now."),
    "title_reference": ("Evlilikle ilgili Kutsal Kitap ilkelerini inceleyeceğiz.",
                        "We will look at Bible principles about marriage."),
    "mc_welcome": ("Değerli misafirler, hoş geldiniz.", "Dear guests, welcome."),
    "groom_name": ("Galkhuu'yu tebrik edelim.", "Congratulations to Galkhuu."),
    "akane_name": ("Akane, tebrikler.", "Congratulations, Akane!"),
    "japanese_independent": ("Herkese bugün geldiğiniz için teşekkür ederim.", "Thank you all for coming today."),
    "plain_contract": ("Bir kira sözleşmesi yaptık.", "We signed a rental agreement."),
    "numerical_difference": ("30 kişi değil, 10 kişi gelecek.", "Not 30 people, but 10 people will come."),
    "adv_father_is_not_god": ("Babam beni her zaman sevdi.", "My father always loved me."),
    "adv_prohibition": ("Tartışmaları asla sürdürme.", "Never continue quarrels."),
    "adv_numbers_years": ("12 yıl önce ilk kez tanıştık.", "We first met 12 years ago."),
    "adv_reference_mention_not_quote": ("Efesliler 5:33'te evlilikle ilgili öğüt var.",
                                        "Ephesians 5:33 has advice about marriage."),
    "adv_no_vow_from_love": ("Birbirlerine gerçekten saygı duyuyorlar.", "They truly respect each other."),
    "adv_name_genitive": ("Akane'nin ailesi Japonya'dan geldi.", "Akane's family came from Japan."),
    "adv_japanese_new_detail": ("Özellikle her gün minnettarlığınızı ifade edin.",
                                "Especially, express your thanks every day."),
    "adv_jehovah_witnesses_org": ("Yehova'nın Şahitleri cemaatinden birçok birader ve hemşire geldi.",
                                  "Many brothers and sisters came from a congregation of Jehovah's Witnesses."),
}
# Typical wrong renderings the checker must flag, per language.
BAD = {
    ("divine_name", "tr"): "Tanrı bizi seviyor.",
    ("divine_name", "en"): "God loves us.",
    ("human_father", "tr"): "Göksel Babamız Tanrı bugün konuşacak.",
    ("human_father", "en"): "Our Heavenly Father will speak today.",
    ("god_not_jehovah", "en"): "Let's talk about Jehovah.",
    ("brother_family", "tr"): "Biraderim Galkhuu konuşacak.",
    ("brother_family", "en"): "Brother Galkhuu from the congregation will speak.",
    ("sister_not_nurse", "tr"): "Hemşiremiz bugün geldi.",
    ("sister_not_nurse", "en"): "Our nurse came today.",
    ("ephesians_alias", "tr"): "Efesoslular 5:33.",
    ("ephesians_alias", "en"): "Ephesians 5:32.",
    ("revelation", "en"): "Revelations 21:4.",
    ("invalid_reference", "en"): "Let's read Ephesians 5:33.",
    ("book_no_verse", "en"): "Open Ephesians 5:33.",
    ("negation", "tr"): "Bu sözü verdik.",
    ("negation", "en"): "We made this promise.",
    ("vow_not_inferred", "en"): "We vow to love each other.",
    ("plain_contract", "en"): "We made a covenant.",
    ("numerical_difference", "en"): "30 people will come.",
    ("groom_name", "tr"): "Galkhuu ve eşine tebrikler.",
    ("groom_name", "en"): "Congratulations to Galkhuu and his wife.",
    ("akane_name", "en"): "Congratulations to Akane and the groom.",
    ("adv_father_is_not_god", "en"): "God the Father always loved me.",
    ("adv_prohibition", "en"): "Always continue the discussion.",
    ("adv_numbers_years", "en"): "We first met 10 years ago.",
    ("adv_reference_mention_not_quote", "en"): (
        "Ephesians 5:33 says: each one of you must love his wife as he does himself, and the wife should have "
        "deep respect for her husband."),
    ("adv_no_vow_from_love", "en"): "They promised to respect each other.",
}


@pytest.fixture(scope="module")
def glossary():
    return load_glossary(ROOT / "resources/glossary", ROOT / "resources/bible")


def spec_for(glossary, cid):
    case = next(c for c in all_cases(glossary) if c["id"] == cid)
    return case.get("checks") or CHECKS[cid]


def run_check(glossary, cid, target, text, display=True, relation=None):
    s = spec_for(glossary, cid)
    return check_lang(s.get(target, {}), s.get("*", {}), target, LangCase(text=text, display=display), relation)


def test_every_case_has_checks_in_both_languages(glossary):
    ids = {c["id"] for c in all_cases(glossary)} - NOT_TEXT
    for cid in ids:
        s = spec_for(glossary, cid)
        assert ("tr" in s and "en" in s) or "*" in s, cid
    assert set(CHECKS) <= ids and len(EXTRA_CASES) >= 8


@pytest.mark.parametrize("cid", sorted(c for c in GOOD if c != "adv_japanese_new_detail"))
@pytest.mark.parametrize("target", ["tr", "en"])
def test_faithful_output_passes(glossary, cid, target):
    text = GOOD[cid][0 if target == "tr" else 1]
    relation = "independent" if cid.startswith("japanese") else None
    assert run_check(glossary, cid, target, text, relation=relation) == ("pass", [])


@pytest.mark.parametrize("cid,target", sorted(BAD))
def test_wrong_output_fails(glossary, cid, target):
    status, problems = run_check(glossary, cid, target, BAD[(cid, target)])
    assert status == "FAIL" and problems


def test_withheld_is_reported_separately(glossary):
    assert run_check(glossary, "divine_name", "en", "", display=False)[0] == "withheld"
    assert run_check(glossary, "japanese_independent", "en", "", display=False,
                     relation="independent")[0] == "FAIL"


def test_relation_checks(glossary):
    assert run_check(glossary, "japanese_repetition", "tr", "", False, "covered")[0] == "pass"
    assert run_check(glossary, "japanese_repetition", "en", "x", True, "independent")[0] == "FAIL"
    assert run_check(glossary, "adv_japanese_new_detail", "en", GOOD["adv_japanese_new_detail"][1],
                     relation="covered")[0] == "FAIL"  # added detail must not be suppressed


class IdealTranslator:
    """Returns the GOOD renderings in both languages."""

    def __init__(self, glossary):
        self.by_source = {c["source_text"]: GOOD.get(c["id"], ("", "")) for c in all_cases(glossary)}

    async def translate(self, req):
        tr, en = self.by_source[req.text]
        return TranslationResult(True, tr, english=en, display_en=True, model="ideal", latency_ms=10)

    async def interpret(self, req):
        tr, en = self.by_source[req.text]
        if not tr:  # japanese_repetition
            return TranslationResult(False, "", relation="covered", linked_ids=[1], display_en=False,
                                     model="ideal", latency_ms=10)
        return TranslationResult(True, tr, english=en, display_en=True, relation="new_content",
                                 linked_ids=[1], model="ideal", latency_ms=10)


async def test_run_eval_end_to_end_with_ideal_translator(glossary, tmp_path):
    results = await run_eval(IdealTranslator(glossary), glossary)
    bad = [(r.id, t, lc.problems) for r in results for t, lc in r.out.items() if lc.auto not in ("pass", "n/a")]
    assert bad == []
    s = summarize(results)
    n = len(all_cases(glossary))
    assert s["cases"] == n
    for lang in ("turkish", "english"):
        assert s[lang]["n/a"] == 2 and s[lang]["pass"] == n - 2 and s[lang]["guard_withheld"] == 0
    jl, md = write_report(results, tmp_path)
    assert jl.exists() and "| divine_name | pass |" in md.read_text(encoding="utf-8")
