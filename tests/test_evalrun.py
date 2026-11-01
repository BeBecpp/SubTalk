"""The live-eval checker itself must be right before it judges real Claude output."""
from pathlib import Path

import pytest

from subtalk.evalrun import CHECKS, NOT_TEXT, CaseResult, check_case, run_eval, summarize, write_report
from subtalk.glossary.store import load_glossary
from subtalk.translate.base import TranslationResult

ROOT = Path(__file__).resolve().parents[1]

# A faithful reference Turkish rendering for every text case (what a correct translator returns).
GOOD = {
    "divine_name": "Yehova bizi seviyor.",
    "human_father": "Babam bugün konuşma yapacak.",
    "god_not_jehovah": "Tanrı hakkında birlikte konuşalım.",
    "brother_family": "Ağabeyim Galkhuu konuşma yapacak.",
    "brother_congregation": "Cemaatimizden bir birader bugün geldi.",
    "sister_not_nurse": "Ablam bugün geldi.",
    "elder_office": "Cemaatin ihtiyarı konuşma yapacak.",
    "ephesians_alias": "Efesliler 5:33.",
    "ephesians_current": "Efesliler 5:33.",
    "first_corinthians": "1. Korintliler 13:4.",
    "genesis": "Başlangıç 2:24.",
    "deuteronomy": "Kanun’un Tekrarı 6:4.",
    "revelation": "Vahiy 21:4.",
    "negation": "Bu sözü vermedik.",
    "obligation_negation": "Bunu şimdi yapmamalıyız.",
    "vow_not_inferred": "Biz ikimiz birbirimizi seviyoruz.",
    "explicit_vow": "Evlilik yeminimizi edeceğiz.",
    "bible_explanation": "Bu bir ayet değil, bir açıklamadır.",
    "invalid_reference": "Efesliler'i okuyalım.",
    "book_no_verse": "Efesliler kitabını açın.",
    "prayer": "Şimdi birlikte dua edelim.",
    "title_reference": "Evlilikle ilgili Kutsal Kitap ilkelerini inceleyeceğiz.",
    "mc_welcome": "Değerli misafirler, hoş geldiniz.",
    "groom_name": "Galkhuu'yu tebrik edelim.",
    "akane_name": "Akane, tebrikler.",
    "japanese_independent": "Herkese bugün geldiğiniz için teşekkür ederim.",
    "plain_contract": "Bir kira sözleşmesi yaptık.",
    "numerical_difference": "30 kişi değil, 10 kişi gelecek.",
}
# Typical wrong renderings the checker must flag.
BAD = {
    "divine_name": "Tanrı bizi seviyor.",
    "human_father": "Göksel Babamız Tanrı bugün konuşacak.",
    "god_not_jehovah": "Yehova hakkında konuşalım.",
    "brother_family": "Biraderim Galkhuu konuşacak.",
    "sister_not_nurse": "Hemşiremiz bugün geldi.",
    "ephesians_alias": "Efesoslular 5:33.",
    "invalid_reference": "Efesliler 5:33'ü okuyalım.",
    "book_no_verse": "Efesliler 5:33'ü açın.",
    "negation": "Bu sözü verdik.",
    "vow_not_inferred": "Birbirimize yemin ediyoruz.",
    "plain_contract": "Bir ahit yaptık.",
    "numerical_difference": "30 kişi gelecek.",
}


@pytest.fixture(scope="module")
def glossary():
    return load_glossary(ROOT / "resources/glossary", ROOT / "resources/bible")


def res(cid, text, display=True, relation=None):
    return CaseResult(cid, "mn", "", turkish=text, display=display, relation=relation)


def test_every_glossary_case_has_a_check(glossary):
    ids = {c["id"] for c in glossary.eval_cases}
    assert ids - NOT_TEXT == set(CHECKS)


@pytest.mark.parametrize("cid", sorted(GOOD))
def test_faithful_output_passes(cid):
    assert check_case(cid, res(cid, GOOD[cid])) == ("pass", [])


@pytest.mark.parametrize("cid", sorted(BAD))
def test_wrong_output_fails(cid):
    status, problems = check_case(cid, res(cid, BAD[cid]))
    assert status == "FAIL" and problems


def test_withheld_is_reported_separately():
    assert check_case("divine_name", res("divine_name", "", display=False))[0] == "withheld"
    assert check_case("japanese_independent", res("japanese_independent", "", display=False))[0] == "FAIL"


def test_repetition_relation():
    assert check_case("japanese_repetition", res("japanese_repetition", "", False, "covered"))[0] == "pass"
    assert check_case("japanese_repetition", res("japanese_repetition", "x", True, "independent"))[0] == "FAIL"


class IdealTranslator:
    """Returns GOOD for each source text; marks the repetition case as covered."""

    def __init__(self, glossary):
        self.by_source = {c["source_text"]: GOOD.get(c["id"], "") for c in glossary.eval_cases}

    async def translate(self, req):
        return TranslationResult(True, self.by_source[req.text], model="ideal", latency_ms=10)

    async def interpret(self, req):
        return TranslationResult(False, "", relation="covered", linked_ids=[1], model="ideal", latency_ms=10)


async def test_run_eval_end_to_end_with_ideal_translator(glossary, tmp_path):
    results = await run_eval(IdealTranslator(glossary), glossary)
    s = summarize(results)
    bad = [(r.id, r.problems) for r in results if r.auto not in ("pass", "n/a")]
    assert bad == []
    assert s["cases"] == 31 and s["n_a"] == 2 and s["pass"] == 29 and s["guard_withheld"] == 0
    jl, md = write_report(results, tmp_path)
    assert jl.exists() and "| divine_name | pass |" in md.read_text(encoding="utf-8")
