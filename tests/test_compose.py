"""Bilingual Zoom caption composer: one POST per part, TR line first, nothing lost."""
import re

import pytest

from subtalk.output.compose import compose_parts, min_parts, reading_time_s, split_into

TR = ("Kutsal Kitap Efesliler 5:22-33 ayetlerinde evlilik hakkında güzel öğütler verir. "
      "Yehova eşlerin birbirini sevmesini ve saymasını ister.")
EN = ("The Bible gives fine counsel on marriage at Ephesians 5:22-33. "
      "Jehovah wants husband and wife to love and respect each other.")


def words(parts, label):
    out = []
    for p in parts:
        for line in re.split(r"\n| \| (?=EN: )", p):
            if line.startswith(label):
                out.extend(line[len(label):].split())
    return out


def test_short_event_is_one_post_tr_first_real_newline():
    assert compose_parts({"tr": "Yehova bizi seviyor.", "en": "Jehovah loves us."}, "BILINGUAL") == [
        "TR: Yehova bizi seviyor.\nEN: Jehovah loves us."]


def test_single_line_fallback_layout():
    (p,) = compose_parts({"tr": "Yehova bizi seviyor.", "en": "Jehovah loves us."}, "BILINGUAL",
                         layout="single_line")
    assert p == "TR: Yehova bizi seviyor. | EN: Jehovah loves us." and "\n" not in p


@pytest.mark.parametrize("layout", ["two_line", "single_line"])
@pytest.mark.parametrize("limit", [30, 50, 70, 84])
def test_long_text_split_never_truncated_and_aligned(layout, limit):
    parts = compose_parts({"tr": TR, "en": EN}, "BILINGUAL", limit, layout=layout)
    assert words(parts, "TR: ") == TR.split()  # every word, in order, nothing dropped
    assert words(parts, "EN: ") == EN.split()
    for p in parts:  # every part carries both languages, TR first
        assert p.startswith("TR: ") and "EN: " in p
        lines = p.split("\n") if layout == "two_line" else p.split(" | ")
        assert all(len(x) - 4 <= limit for x in lines)


def test_bible_reference_numbers_and_negation_kept_together():
    text = "Bugün 1. Korintliler 13:4-8 okunacak ve 3 gün sonra tekrar edilmeyecek değil, unutulmayacak."
    for limit in (12, 18, 25, 40):
        parts = split_into(text, min_parts(text, limit), limit)
        assert " ".join(parts) == text
        assert any("1. Korintliler 13:4-8" in p for p in parts) and any("3 gün" in p for p in parts)
        for part in parts:
            assert not part.startswith("değil"), part
    en = "We will never forget the promise we made before Jehovah God."
    for limit in (15, 20, 30):
        for part in split_into(en, min_parts(en, limit), limit):
            assert not part.startswith("never"), part


def test_prefers_sentence_boundary_when_it_fits():
    parts = split_into("Yehova bizi seviyor. Biz de onu seviyoruz ve ona hizmet ediyoruz.", 2, 50)
    assert parts == ["Yehova bizi seviyor.", "Biz de onu seviyoruz ve ona hizmet ediyoruz."]


def test_withheld_language_is_omitted_not_replaced():
    assert compose_parts({"en": "Jehovah loves us."}, "BILINGUAL") == ["EN: Jehovah loves us."]
    assert compose_parts({"en": "Jehovah loves us."}, "TR") == []  # never English in Turkish-only mode
    assert compose_parts({}, "BILINGUAL") == []


def test_unknown_mode_or_layout_rejected():
    with pytest.raises(ValueError):
        compose_parts({"tr": "x"}, "DE")
    with pytest.raises(ValueError):
        compose_parts({"tr": "x"}, "BILINGUAL", layout="three_line")


def test_reading_time_uses_the_longest_line_and_is_bounded():
    assert reading_time_s("TR: kısa\nEN: short") == 1.5
    assert reading_time_s("TR: " + "a" * 41 + "\nEN: b") == pytest.approx(45 / 15)
    assert reading_time_s("x" * 500) == 4.0
