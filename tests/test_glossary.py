"""Glossary validation, terminology selection, citation parsing and guard
regressions - many cases come straight from SubTalk JW Glossary v1
evaluation_cases.json (synthetic)."""
import json
import shutil
from pathlib import Path

import pytest

from subtalk.glossary.citations import format_tr
from subtalk.glossary.guards import Guards
from subtalk.glossary.numerals import kanji_to_int, mn_token_number, parse_mn_numbers
from subtalk.glossary.match import mn_tokens
from subtalk.glossary.service import GlossaryService
from subtalk.glossary.store import GlossaryError, load_glossary

ROOT = Path(__file__).resolve().parents[1]
GDIR = ROOT / "resources" / "glossary"
BDIR = ROOT / "resources" / "bible"


@pytest.fixture(scope="module")
def glossary():
    return load_glossary(GDIR, BDIR)


@pytest.fixture(scope="module")
def svc(glossary):
    return GlossaryService(glossary)


@pytest.fixture(scope="module")
def guards(glossary):
    return Guards(glossary)


def cases(glossary):
    return {c["id"]: c for c in glossary.eval_cases}


# --------------------------------------------------------------- validation
def test_pack_validates(glossary):
    s = glossary.summary()
    assert s["books"] == 66
    assert s["terms"] == 86
    assert s["terms_by_status"] == {"tr_attested": 48, "review_required": 38}
    assert len(glossary.eval_cases) == 31
    assert all(not n.approved for n in glossary.names)  # example names are never approved


def test_review_status_is_preserved(glossary):
    by_id = {t.id: t for t in glossary.terms}
    assert by_id["amen"].status == "review_required" and not by_id["amen"].reviewed
    assert by_id["jehovah"].status == "tr_attested" and by_id["jehovah"].reviewed


def test_approvals_promote_terms(tmp_path):
    ap = tmp_path / "approvals.json"
    ap.write_text(json.dumps({"reviewer": "Test Reviewer", "approved_term_ids": ["amen"]}), encoding="utf-8")
    g = load_glossary(GDIR, BDIR, approvals_path=ap)
    assert {t.id: t for t in g.terms}["amen"].status == "human_approved"


def test_unknown_approval_id_rejected(tmp_path):
    ap = tmp_path / "approvals.json"
    ap.write_text(json.dumps({"approved_term_ids": ["no_such_term"]}), encoding="utf-8")
    with pytest.raises(GlossaryError, match="unknown term"):
        load_glossary(GDIR, BDIR, approvals_path=ap)


def test_event_names_file_can_approve(tmp_path):
    p = tmp_path / "names.json"
    p.write_text(json.dumps({"entries": [
        {"id": "groom_galkhuu", "mn": ["Галхүү"], "ja": [], "tr": "Galkhuu", "role": "groom", "approved": True}
    ]}), encoding="utf-8")
    g = load_glossary(GDIR, BDIR, event_names_path=p)
    assert g.names[0].approved and g.names_source == "event"


@pytest.mark.parametrize("mutation,message", [
    (lambda d: d["books"].pop(), "66 Bible books"),
    (lambda d: d["books"][1].update(id="GEN"), "duplicate book id"),
])
def test_broken_books_rejected(tmp_path, mutation, message):
    g2 = tmp_path / "g"
    shutil.copytree(GDIR, g2)
    data = json.loads((g2 / "bible_books.json").read_text(encoding="utf-8"))
    mutation(data)
    (g2 / "bible_books.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GlossaryError, match=message):
        load_glossary(g2, BDIR)


def test_bad_term_status_rejected(tmp_path):
    g2 = tmp_path / "g"
    shutil.copytree(GDIR, g2)
    data = json.loads((g2 / "jw_terms.json").read_text(encoding="utf-8"))
    data["items"][0]["status"] = "approved_by_me"
    (g2 / "jw_terms.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GlossaryError, match="invalid status"):
        load_glossary(g2, BDIR)


# --------------------------------------------------------------- numerals
@pytest.mark.parametrize("text,values", [
    ("тавдугаар бүлэг гучин гуравдугаар зүйл", [5, 33]),
    ("хоёрын хорин дөрөв", [2, 24]),
    ("арван гурвын дөрөв", [13, 4]),
    ("зургаадугаар бүлэг дөрөвдүгээр зүйл", [6, 4]),
    ("хоёр мянга хорин зургаа", [2026]),
    ("хоёр гурав", [2, 3]),
    ("Гучин хүн биш, арван хүн ирнэ", [30, 10]),
])
def test_mn_numbers(text, values):
    assert [s.value for s in parse_mn_numbers(mn_tokens(text))] == values


def test_mn_digit_tokens():
    assert mn_token_number("5-р")[0] == 5 and mn_token_number("5-р")[2] is True


@pytest.mark.parametrize("s,v", [("三十三", 33), ("五", 5), ("十三", 13), ("百二十", 120), ("21", 21)])
def test_kanji(s, v):
    assert kanji_to_int(s) == v


# --------------------------------------------------------------- citations
def cite(svc, text, lang="mn"):
    return svc.citations.detect(text, lang)


@pytest.mark.parametrize("case_id,expected", [
    ("ephesians_alias", ("EPH", 5, 33, "valid", "Efesliler 5:33")),
    ("ephesians_current", ("EPH", 5, 33, "valid", "Efesliler 5:33")),
    ("first_corinthians", ("1CO", 13, 4, "valid", "1. Korintliler 13:4")),
    ("genesis", ("GEN", 2, 24, "valid", "Başlangıç 2:24")),
    ("deuteronomy", ("DEU", 6, 4, "valid", "Kanun’un Tekrarı 6:4")),
    ("revelation", ("REV", 21, 4, "valid", "Vahiy 21:4")),
])
def test_eval_case_citations(glossary, svc, case_id, expected):
    c = cases(glossary)[case_id]
    found = cite(svc, c["source_text"])
    assert len(found) == 1
    got = found[0]
    assert (got.book_id, got.chapter, got.verse, got.status) == expected[:4]
    assert format_tr(glossary, got) == expected[4]


def test_invalid_reference_flagged_not_corrected(glossary, svc):
    c = cite(svc, cases(glossary)["invalid_reference"]["source_text"])[0]
    assert (c.book_id, c.chapter, c.verse, c.status) == ("EPH", 5, 34, "invalid")
    assert format_tr(glossary, c) is None  # never rendered, never "fixed" to 5:33


def test_book_without_numbers_has_no_reference(glossary, svc):
    c = cite(svc, cases(glossary)["book_no_verse"]["source_text"])
    assert len(c) == 1 and c[0].status == "book_only" and c[0].chapter is None
    assert format_tr(glossary, c[0]) == "Efesliler"


def test_policy_format_examples(glossary, svc):
    for ex in glossary.policy["bible_ref_handling"]["reference_examples"]:
        c = cite(svc, ex["mn"])
        assert len(c) == 1, ex
        assert format_tr(glossary, c[0]) == ex["tr"]


def test_ordinary_words_are_not_books(svc):
    # "Эхлэл" (beginning) and "Дуулал" (song) without numbers or Bible cues
    assert cite(svc, "Шинэ амьдралын эхлэл өнөөдөр") == []
    assert cite(svc, "Энэ дуулал маш сайхан байна") == []
    assert cite(svc, "Бид гэрлэлтийн тухай Библийн зарчим үзнэ.") == []  # title_reference case


@pytest.mark.parametrize("text,lang,expected,tr,en", [
    ("Ефэс 5:33", "mn", ("EPH", 5, 33, None), "Efesliler 5:33", "Ephesians 5:33"),       # е/э swap from ASR
    ("1 Коринт 13:4–8", "mn", ("1CO", 13, 4, 8), "1. Korintliler 13:4-8", "1 Corinthians 13:4-8"),  # en dash
    ("Эхлэл 2:24", "mn", ("GEN", 2, 24, None), "Başlangıç 2:24", "Genesis 2:24"),
    ("Матай 19:6", "mn", ("MAT", 19, 6, None), "Matta 19:6", "Matthew 19:6"),
    ("エフェソス 5:33", "ja", ("EPH", 5, 33, None), "Efesliler 5:33", "Ephesians 5:33"),
])
def test_owner_reference_examples_both_languages(glossary, svc, text, lang, expected, tr, en):
    from subtalk.glossary.citations import format_ref
    c = cite(svc, text, lang)
    assert len(c) == 1 and (c[0].book_id, c[0].chapter, c[0].verse, c[0].verse_end) == expected
    assert format_ref(glossary, c[0], "tr") == tr and format_ref(glossary, c[0], "en") == en


def test_unverified_when_verse_count_unknown(svc):
    c = cite(svc, "Матай 5:9")[0]
    assert c.status == "unverified"


def test_chapter_out_of_range_invalid(svc):
    assert cite(svc, "Эфэс 7:1")[0].status == "invalid"  # Ephesians has 6 chapters


def test_japanese_citations(glossary, svc):
    c = cite(svc, "エフェソス5章33節を読みましょう", "ja")[0]
    assert (c.book_id, c.chapter, c.verse, c.status) == ("EPH", 5, 33, "valid")
    c2 = cite(svc, "コリント第一 十三章四節", "ja")[0]
    assert (c2.book_id, c2.chapter, c2.verse) == ("1CO", 13, 4)
    assert cite(svc, "ヨナさんが来ました", "ja") == []  # name, no Bible cue


# --------------------------------------------------------------- term selection
def ids(hints):
    return {h.id for h in hints}


def test_short_terms_do_not_overmatch(svc):
    assert "brother_jw" not in ids(svc.select_terms("Хурлын ахлагч үг хэлнэ.", "mn"))
    assert "elder" in ids(svc.select_terms("Хурлын ахлагч үг хэлнэ.", "mn"))


def test_suffixes_match(svc):
    assert "jehovah" in ids(svc.select_terms("Еховагийн хайр", "mn"))


def test_fleeting_vowel_stems_match(svc):
    assert "marriage_vow" in ids(svc.select_terms("Бид гэрлэлтийн тангаргаа өгнө.", "mn"))
    assert "prayer" in ids(svc.select_terms("Залбирлын дараа", "mn"))


def test_single_kanji_not_inside_words(svc):
    assert "god" not in ids(svc.select_terms("神戸から来ました", "ja"))
    assert "god" in ids(svc.select_terms("神は愛です", "ja"))


def test_only_present_terms_selected(svc):
    hints = svc.select_terms("Эрхэм зочид оо, тавтай морилно уу.", "mn")
    assert "jehovah" not in ids(hints)  # "always" terms only when the referent is present


def test_review_required_labelled_candidate(svc):
    h = {x.id: x for x in svc.select_terms("Амен", "mn")}["amen"]
    assert h.label().startswith("CANDIDATE")


def test_names_selected_and_marked_unapproved(svc):
    h = {x.id: x for x in svc.select_terms("Галхүүд баяр хүргэе.", "mn")}
    assert h["groom_galkhuu"].kind == "name" and h["groom_galkhuu"].status == "unapproved"


# --------------------------------------------------------------- guards
def check(guards, svc, src, out, lang="mn"):
    return guards.check(src, lang, svc.citations.detect(src, lang), out)


def test_guard_passes_faithful_output(guards, svc, glossary):
    src = cases(glossary)["ephesians_alias"]["source_text"]
    r = check(guards, svc, src, "Efesliler 5:33.")
    assert r.ok, r.blocked


def test_guard_rewrites_legacy_book_name(guards, svc):
    r = check(guards, svc, "Эфэс 5:33.", "Efesoslular 5:33.")
    assert r.ok and r.text == "Efesliler 5:33." and "legacy_book_name_rewritten" in r.flags


def test_guard_blocks_silent_correction(guards, svc, glossary):
    src = cases(glossary)["invalid_reference"]["source_text"]
    r = check(guards, svc, src, "Efesliler 5:33.")
    assert not r.ok and any(b.startswith("unsupported_citation") for b in r.blocked)


def test_guard_blocks_inserted_reference(guards, svc, glossary):
    src = cases(glossary)["book_no_verse"]["source_text"]
    assert not check(guards, svc, src, "Efesliler 5:33'ü açın.").ok
    src2 = cases(glossary)["title_reference"]["source_text"]
    assert not check(guards, svc, src2, "Evlilikle ilgili Kutsal Kitap ilkelerini, Efesliler 5:22'yi inceleyeceğiz.").ok


def test_guard_divine_name(guards, svc, glossary):
    src = cases(glossary)["divine_name"]["source_text"]
    assert check(guards, svc, src, "Yehova bizi seviyor.").ok
    assert "divine_name_missing" in check(guards, svc, src, "Tanrı bizi seviyor.").blocked


def test_guard_human_father_not_god(guards, svc, glossary):
    src = cases(glossary)["human_father"]["source_text"]
    assert check(guards, svc, src, "Babam bugün konuşma yapacak.").ok
    assert "divine_reference_invented" in check(guards, svc, src, "Göksel Babamız Tanrı bugün konuşacak.").blocked


def test_guard_god_not_upgraded_to_jehovah(guards, svc, glossary):
    src = cases(glossary)["god_not_jehovah"]["source_text"]
    assert check(guards, svc, src, "Tanrı hakkında birlikte konuşalım.").ok
    assert "divine_name_invented" in check(guards, svc, src, "Yehova hakkında birlikte konuşalım.").blocked


def test_guard_prayer_and_vow(guards, svc, glossary):
    assert check(guards, svc, cases(glossary)["prayer"]["source_text"], "Şimdi birlikte dua edelim.").ok
    src = cases(glossary)["vow_not_inferred"]["source_text"]
    assert check(guards, svc, src, "Biz ikimiz birbirimizi seviyoruz.").ok
    assert "vow_invented" in check(guards, svc, src, "Birbirimize evlilik yemini ediyoruz.").blocked
    assert check(guards, svc, cases(glossary)["explicit_vow"]["source_text"], "Evlilik yeminimizi edeceğiz.").ok


def test_guard_numbers(guards, svc, glossary):
    src = cases(glossary)["numerical_difference"]["source_text"]
    assert check(guards, svc, src, "30 kişi değil, 10 kişi gelecek.").ok
    assert "number_invented:20" in check(guards, svc, src, "20 kişi gelecek.").blocked


def test_guard_name_invented(guards, svc, glossary):
    src = cases(glossary)["mc_welcome"]["source_text"]
    assert check(guards, svc, src, "Değerli misafirler, hoş geldiniz.").ok
    assert "name_invented:bride_akane" in check(guards, svc, src, "Değerli misafirler, Akane hoş geldiniz.").blocked
    r = check(guards, svc, cases(glossary)["groom_name"]["source_text"], "Galkhuu'yu tebrik edelim.")
    assert r.ok and "name_spelling_unapproved:groom_galkhuu" in r.flags


def test_guard_japanese_name(guards, svc, glossary):
    src = cases(glossary)["akane_name"]["source_text"]
    assert check(guards, svc, src, "Akane, tebrikler.", "ja").ok


def test_guard_length_ratio(guards, svc):
    r = check(guards, svc, "Сайн байна уу.", "Merhaba. " + "Evlilik Yehova'nın bir armağanıdır ve " * 5)
    assert "length_ratio" in r.blocked


def test_guard_bible_explanation_not_quote(guards, svc, glossary):
    src = cases(glossary)["bible_explanation"]["source_text"]
    assert check(guards, svc, src, "Bu bir ayet değil, bir açıklamadır.").ok


def test_guard_real_claude_failure_name_and_added_spouse(guards, svc):
    """Seen live (Sonnet 5.5): 'Галхүүд баяр хүргэе.' -> 'Galhuu ve eşine tebrikler.'"""
    r = check(guards, svc, "Галхүүд баяр хүргэе.", "Galhuu ve eşine tebrikler.")
    assert not r.ok and "spouse_invented" in r.blocked
    assert r.text == "Galkhuu ve eşine tebrikler."
    assert "name_spelling_corrected:groom_galkhuu" in r.flags


@pytest.mark.parametrize("bad,good", [
    ("Galhuu'ya tebrikler.", "Galkhuu'ya tebrikler."),
    ("Galkuu'yu tebrik edelim.", "Galkhuu'yu tebrik edelim."),
    ("GALHUU tebrikler.", "Galkhuu tebrikler."),
])
def test_guard_corrects_near_miss_name(guards, svc, bad, good):
    r = check(guards, svc, "Галхүүд баяр хүргэе.", bad)
    assert r.ok and r.text == good


def test_guard_name_fix_leaves_other_words(guards, svc):
    r = check(guards, svc, "Галхүү Галатад очсон.", "Galkhuu Galata'ya gitti.")
    assert r.text == "Galkhuu Galata'ya gitti." and "name_spelling_corrected:groom_galkhuu" not in r.flags
    # Name not spoken -> never "corrected", and an invented name is still blocked.
    r2 = check(guards, svc, "Сайн байна уу.", "Galhuu merhaba.")
    assert r2.text == "Galhuu merhaba."


@pytest.mark.parametrize("src,out,ok", [
    ("Баярлалаа.", "Teşekkür ederiz.", True),                      # 'eş' inside a word
    ("Бүгд тэгш эрхтэй.", "Herkes eşit haklara sahip.", True),       # eşit = equal
    ("Нөхөр эхнэрээ хайрлах ёстой.", "Koca karısını sevmeli.", True),
    ("Сүйт хосод баяр хүргэе.", "Çifti ve eşlerini tebrik edelim.", True),
    ("Галхүүд баяр хүргэе.", "Galkhuu ve karısını tebrik edelim.", False),
    ("あかねさん、おめでとうございます。", "Akane ve kocasına tebrikler.", False),
    ("あかねさんと夫に、おめでとう。", "Akane ve kocasına tebrikler.", True),
])
def test_guard_spouse_not_invented(guards, svc, src, out, ok):
    lang = "ja" if any("぀" <= ch <= "ヿ" for ch in src) else "mn"
    r = check(guards, svc, src, out, lang)
    assert ("spouse_invented" not in r.blocked) == ok, r.blocked


def test_guard_groom_not_invented_but_gelin_allowed(guards, svc):
    src = "Эрхэм зочид оо, тавтай морилно уу."
    assert "groom_invented" in check(guards, svc, src, "Değerli misafirler ve damadımız, hoş geldiniz.").blocked
    assert check(guards, svc, src, "Değerli misafirler, hoş gelin.").ok  # 'gelin' = come (imperative)
    assert check(guards, svc, "Сүйт залуу үг хэлнэ.", "Damat konuşacak.").ok


def test_name_hint_does_not_invite_respelling_or_roles(svc):
    h = {x.id: x for x in svc.select_terms("Галхүүд баяр хүргэе.", "mn")}["groom_galkhuu"]
    assert h.notes == "use exactly 'Galkhuu'"
    assert "approved" not in h.notes and "groom" not in h.notes


# --------------------------------------------------------------- English guards
def en(guards, svc, src, out, lang="mn"):
    return guards.check(src, lang, svc.citations.detect(src, lang), out, target="en")


@pytest.mark.parametrize("src,out,ok,code", [
    ("Ехова биднийг хайрладаг.", "Jehovah loves us.", True, None),
    ("Ехова биднийг хайрладаг.", "God loves us.", False, "divine_name_missing"),
    ("Бурхны тухай хамтдаа ярилцъя.", "Let's talk together about God.", True, None),
    ("Бурхны тухай хамтдаа ярилцъя.", "Let's talk about Jehovah.", False, "divine_name_invented"),
    ("Миний аав өнөөдөр илтгэл тавина.", "My father will give a talk today.", True, None),
    ("Миний аав өнөөдөр илтгэл тавина.", "Our Heavenly Father will speak today.", False, "divine_reference_invented"),
    ("Миний аав өнөөдөр илтгэл тавина.", "The Lord will speak today.", False, "divine_reference_invented"),
    ("Галхүүд баяр хүргэе.", "Congratulations to Galkhuu.", True, None),
    ("Галхүүд баяр хүргэе.", "Congratulations to Galkhuu and his wife.", False, "spouse_invented"),
    ("Эрхэм зочид оо, тавтай морилно уу.", "Dear guests and our groom, welcome.", False, "groom_invented"),
    ("Эрхэм зочид оо, тавтай морилно уу.", "Dear guests and the bride, welcome.", False, "bride_invented"),
    ("Бид хоёр нэг нэгэндээ хайртай.", "We love each other.", True, None),
    ("Бид хоёр нэг нэгэндээ хайртай.", "We vow to love each other.", False, "vow_invented"),
    ("Одоо хамт залбирцгаая.", "Let us pray together now.", True, None),
    ("Эрхэм зочид оо, тавтай морилно уу.", "Welcome, let us pray.", False, "prayer_invented"),
    ("Бид түрээсийн гэрээ байгуулсан.", "We signed a rental agreement.", True, None),
    ("Гучин хүн биш, арван хүн ирнэ.", "Not 30 people, 10 people will come.", True, None),
    ("Гучин хүн биш, арван хүн ирнэ.", "20 people will come.", False, "number_invented:20"),
])
def test_english_guards(guards, svc, src, out, ok, code):
    r = en(guards, svc, src, out)
    assert r.ok == ok, r.blocked
    if code:
        assert code in r.blocked


def test_english_references(guards, svc):
    assert en(guards, svc, "Эфэс 5:33.", "Ephesians 5:33.").ok
    r = en(guards, svc, "Эфэс тавдугаар бүлгийн гучин дөрөвдүгээр зүйл.", "Ephesians 5:33.")
    assert not r.ok and any(b.startswith("unsupported_citation") for b in r.blocked)  # never "corrected"
    assert not en(guards, svc, "Эфэс номыг нээгээрэй.", "Open Ephesians 5:33.").ok
    r = en(guards, svc, "Матай 19:6", "Matthew 19:6")
    assert r.ok
    # Ordinary capitalised-less words with numbers are not books (English book names are case-sensitive).
    assert en(guards, svc, "Гучин хүн биш, арван хүн ирнэ.", "Not 30 people; numbers 10 will come.").ok


def test_english_alias_rewrite_and_psalm(guards, svc):
    r = en(guards, svc, "Сургаалт үгс 3:5", "Proverbs 3:5")
    assert r.ok
    r2 = en(guards, svc, "Дуучны дуу 2:1 библи", "Song of Songs 2:1")
    assert "legacy_book_name_rewritten" in r2.flags and r2.text.startswith("Song of Solomon 2:1")
    r3 = en(guards, svc, "Дуулал 23:1", "Psalm 23:1")
    assert r3.ok and r3.text == "Psalm 23:1"  # singular "Psalm" is correct English, not rewritten


def test_english_name_spelling_corrected(guards, svc):
    r = en(guards, svc, "Галхүүд баяр хүргэе.", "Congratulations to Galhuu.")
    assert r.ok and r.text == "Congratulations to Galkhuu."
    assert "name_spelling_corrected:groom_galkhuu" in r.flags


def test_english_does_not_block_ordinary_words(guards, svc):
    for out in ("Thank you for coming.", "Everyone is equal.", "My older brother Galkhuu will speak.",
                "A godless world", "We are mates from school"):
        src = "Миний төрсөн ах Галхүү үг хэлнэ." if "Galkhuu" in out else "Баярлалаа."
        r = en(guards, svc, src, out)
        if out == "We are mates from school":
            assert "spouse_invented" in r.blocked  # 'mate' = spouse in JW English; conservative by design
        else:
            assert r.ok, (out, r.blocked)


def test_guard_empty(guards, svc):
    assert check(guards, svc, "Сайн уу", "").blocked == ["empty_output"]


# --------------------------------------------------------------- verse lists (S-41 talk, 4 Nov)
@pytest.mark.parametrize("src,tr,en_", [
    ("Эхлэл 2:21,22-ыг нээцгээе.", "Başlangıç 2:21, 22", "Genesis 2:21, 22"),
    ("Эфэс 5:25,28,29-г уншицгаая.", "Efesliler 5:25, 28, 29", "Ephesians 5:25, 28, 29"),
    ("Эфэс 5:25, 28, 29-г уншицгаая.", "Efesliler 5:25, 28, 29", "Ephesians 5:25, 28, 29"),
    ("Эфэс тавдугаар бүлгийн хорин тав, хорин найм, хорин есдүгээр зүйлийг уншъя.",
     "Efesliler 5:25, 28, 29", "Ephesians 5:25, 28, 29"),
    ("Эфэс 5:22-24-ыг харъя.", "Efesliler 5:22-24", "Ephesians 5:22-24"),
])
def test_verse_lists_are_kept_whole(glossary, svc, src, tr, en_):
    from subtalk.glossary.citations import format_ref
    (c,) = svc.citations.detect(src, "mn")
    assert c.status == "valid"
    assert format_ref(glossary, c, "tr") == tr and format_ref(glossary, c, "en") == en_


def test_number_after_reference_that_is_not_a_verse(svc):
    (c,) = svc.citations.detect("Эхлэл 2:24 3 удаа уншсан.", "mn")
    assert (c.verse, c.more) == (24, ())


def test_verse_list_beyond_chapter_is_invalid(svc):
    (c,) = svc.citations.detect("Эфэс 5:25, 40 уншъя.", "mn")
    assert c.status == "invalid"


def test_guard_accepts_verse_list_and_blocks_an_invented_extra(guards, svc):
    src = "Эфэс 5:25,28,29-г уншицгаая."
    assert check(guards, svc, src, "Efesliler 5:25, 28, 29'u okuyalım.").ok
    assert en(guards, svc, src, "Let's read Ephesians 5:25, 28, 29.").ok
    r = en(guards, svc, src, "Let's read Ephesians 5:25, 28, 30.")
    assert not r.ok and "number_invented:30" in r.blocked
    spoken = "Эфэс тавдугаар бүлгийн хорин тав, хорин найм, хорин есдүгээр зүйлийг уншъя."
    assert en(guards, svc, spoken, "Let's read Ephesians 5:25, 28, 29.").ok


# --------------------------------------------------------------- S-41 talk findings (4 Nov)
def test_witness_husband_may_be_rendered_jehovahs_witness(guards, svc):
    src = "Гэрч нөхөр эхнэртээ эелдэг байна."
    assert en(guards, svc, src, "A husband who is one of Jehovah's Witnesses is kind to his wife.").ok
    assert check(guards, svc, src, "Yehova'nın Şahidi olan bir koca eşine nazik davranır.").ok
    # ...but only as that phrase: a plain unspoken "Jehovah" is still invented
    r = en(guards, svc, src, "Jehovah wants a husband to be kind to his wife.")
    assert not r.ok and "divine_name_invented" in r.blocked


def test_jehovahs_witness_without_spoken_witness_is_blocked(guards, svc):
    r = en(guards, svc, "Нөхөр эхнэрээ хайрлах ёстой.", "A husband who is one of Jehovah's Witnesses should love his wife.")
    assert not r.ok and "divine_name_invented" in r.blocked


def test_becoming_a_family_at_the_vows_allows_husband_and_wife(guards, svc):
    src = "Тэд өнөөдөр гэр бүл болно."
    assert en(guards, svc, src, "Today they become husband and wife.").ok
    assert check(guards, svc, src, "Bugün karı koca oluyorlar.").ok
    r = en(guards, svc, "Галхүүд баяр хүргэе.", "Congratulations to Galkhuu and his wife.")
    assert not r.ok and "spouse_invented" in r.blocked  # a name alone still never gets a spouse
