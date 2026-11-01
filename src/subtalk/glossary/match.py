"""Conservative source-text matching for glossary forms.

Mongolian is agglutinative ("Ехова" -> "Еховагийн"), so a term token may be
followed by a case suffix. Short tokens (< 4 letters, e.g. "ах") only accept a
small list of known case suffixes, so "ах" (brother) never matches "ахлагч"
(elder). Japanese single-kanji terms only match when not embedded in a longer
kanji word (神 must not match 神戸).
"""
from __future__ import annotations

import re

from subtalk.glossary.normalize import is_cjk, norm_ja, norm_mn

MN_CASE_SUFFIXES = {
    "ын", "ийн", "ы", "ий", "ний", "гийн", "н",
    "д", "т", "ад", "эд", "од", "өд", "дээ", "даа", "доо", "дөө",
    "ыг", "ийг", "г", "аа", "ээ", "оо", "өө",
    "аас", "ээс", "оос", "өөс", "гаас", "гээс",
    "тай", "тэй", "той", "аар", "ээр", "оор", "өөр", "гаар", "гээр",
    "руу", "рүү", "луу", "лүү", "хан", "чуул", "чүүл", "ууд", "үүд", "нар",
    "ыгаа", "ийгээ", "тайгаа", "тэйгээ",
}
_LONG_SUFFIX_MAX = 7

_TOKEN = re.compile(r"\d+:\d+(?:-\d+)?|\d+(?:-[а-яөүё]+|\.)?|[^\W\d_]+", re.UNICODE)


def mn_tokens(text: str) -> list[str]:
    return _TOKEN.findall(norm_mn(text))


_VOWELS = set("аэоөуүиыяеёю")


def _stems(term_tok: str) -> list[str]:
    """The token itself plus its fleeting-vowel stem: Mongolian drops the
    last-syllable vowel before a suffix (тангараг -> тангаргаа,
    залбирал -> залбирлын)."""
    stems = [term_tok]
    if len(term_tok) >= 5 and term_tok[-2] in _VOWELS and term_tok[-1] not in _VOWELS:
        stems.append(term_tok[:-2] + term_tok[-1])
    return stems


def _mn_token_matches(term_tok: str, src_tok: str, last: bool) -> bool:
    if src_tok == term_tok:
        return True
    for i, stem in enumerate(_stems(term_tok)):
        if not src_tok.startswith(stem):
            continue
        suffix = src_tok[len(stem):]
        if i == 1 and not suffix:
            continue  # the bare fleeting stem is not a word on its own
        if suffix in MN_CASE_SUFFIXES:
            return True
        if len(stem) >= 4 and len(suffix) <= _LONG_SUFFIX_MAX and last:
            return True
    return False


def find_mn(form: str, tokens: list[str]) -> int | None:
    """Return the index of the first token where ``form`` matches, else None."""
    ftoks = mn_tokens(form)
    if not ftoks:
        return None
    n = len(ftoks)
    for i in range(len(tokens) - n + 1):
        if all(_mn_token_matches(ftoks[k], tokens[i + k], last=(k == n - 1)) for k in range(n)):
            return i
    return None


def find_ja(form: str, text_norm: str) -> int | None:
    f = norm_ja(form)
    if not f:
        return None
    start = 0
    while True:
        i = text_norm.find(f, start)
        if i < 0:
            return None
        if len(f) == 1 and is_cjk(f):
            before = text_norm[i - 1] if i > 0 else ""
            after = text_norm[i + 1] if i + 1 < len(text_norm) else ""
            if (before and is_cjk(before)) or (after and is_cjk(after)):
                start = i + 1
                continue
        return i


def contains_form(forms, text: str, lang: str) -> bool:
    if lang == "ja":
        tn = norm_ja(text)
        return any(find_ja(f, tn) is not None for f in forms)
    toks = mn_tokens(text)
    return any(find_mn(f, toks) is not None for f in forms)
