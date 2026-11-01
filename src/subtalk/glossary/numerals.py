"""Deterministic numeral parsing for Mongolian (words/digits), Japanese
(kanji/arabic) and Turkish digits/words. Used for Bible references and the
"no invented numbers" guard."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Mongolian number word forms -> (value, kind). Ordinal (-дугаар/-дүгээр),
# genitive (-ын/-ийн/-гийн) and dative forms terminate a number phrase.
_MN_FORMS: dict[int, list[str]] = {
    1: ["нэг", "нэгэн", "нэгдүгээр", "нэгийн", "нэгд"],
    2: ["хоёр", "хоёрдугаар", "хоёрын", "хоёрт"],
    3: ["гурав", "гурван", "гуравдугаар", "гурвын", "гуравт"],
    4: ["дөрөв", "дөрвөн", "дөрөвдүгээр", "дөрвийн", "дөрөвт"],
    5: ["тав", "таван", "тавдугаар", "тавын", "тавд"],
    6: ["зургаа", "зургаан", "зургаадугаар", "зургадугаар", "зургаагийн", "зургаад"],
    7: ["долоо", "долоон", "долоодугаар", "долдугаар", "долоогийн", "долоод"],
    8: ["найм", "найман", "наймдугаар", "наймын", "наймд"],
    9: ["ес", "есөн", "есдүгээр", "есийн", "есд"],
    10: ["арав", "арван", "аравдугаар", "аравын", "арвын", "аравт"],
    20: ["хорь", "хорин", "хорьдугаар", "хорийн", "хорьд"],
    30: ["гуч", "гучин", "гучдугаар", "гучийн", "гучид"],
    40: ["дөч", "дөчин", "дөчдүгээр", "дөчийн"],
    50: ["тавь", "тавин", "тавьдугаар", "тавийн"],
    60: ["жар", "жаран", "жардугаар", "жарын"],
    70: ["дал", "далан", "далдугаар", "далын"],
    80: ["ная", "наян", "наядугаар", "наяын"],
    90: ["ер", "ерэн", "ерэдүгээр", "ерийн"],
    100: ["зуу", "зуун", "зуудугаар", "зуугийн"],
    1000: ["мянга", "мянган", "мянгадугаар", "мянгын"],
}
_TERMINAL_SUFFIXES = ("дугаар", "дүгээр", "ын", "ийн", "гийн", "ний", "д", "т")
_ATTRIBUTIVE_EXCEPTIONS = {"нэгэн", "гурван", "дөрвөн", "таван", "зургаан", "долоон", "найман",
                           "есөн", "арван", "хорин", "гучин", "дөчин", "тавин", "жаран",
                           "далан", "наян", "ерэн", "зуун", "мянган"}

MN_WORDS: dict[str, tuple[int, bool]] = {}
for _value, _forms in _MN_FORMS.items():
    for _f in _forms:
        terminal = _f not in _ATTRIBUTIVE_EXCEPTIONS and _f not in (_forms[0],) and _f.endswith(_TERMINAL_SUFFIXES)
        MN_WORDS[_f] = (_value, terminal)

_DIGIT_TOKEN = re.compile(r"^(\d+)(?:-?([а-яөүё]+)|\.)?$")


@dataclass
class NumberSpan:
    value: int
    start: int  # token index
    end: int    # exclusive
    terminal: bool


def mn_token_number(tok: str) -> tuple[int, str, bool] | None:
    """Return (value, kind, terminal) for a single token, or None."""
    m = _DIGIT_TOKEN.match(tok)
    if m:
        return int(m.group(1)), "digits", bool(m.group(2) or tok.endswith("."))
    if tok in MN_WORDS:
        value, terminal = MN_WORDS[tok]
        kind = "hundred" if value == 100 else "thousand" if value == 1000 else "word"
        return value, kind, terminal
    return None


def parse_mn_numbers(tokens: list[str]) -> list[NumberSpan]:
    """Find number phrases, e.g. ["гучин", "гуравдугаар"] -> 33."""
    spans: list[NumberSpan] = []
    i = 0
    while i < len(tokens):
        first = mn_token_number(tokens[i])
        if first is None:
            i += 1
            continue
        start = i
        if first[1] == "digits":
            spans.append(NumberSpan(first[0], i, i + 1, first[2]))
            i += 1
            continue
        total, current, terminal = 0, 0, False
        while i < len(tokens):
            parsed = mn_token_number(tokens[i])
            if parsed is None or parsed[1] == "digits":
                break
            value, kind, terminal = parsed
            if kind == "hundred":
                current = max(current, 1) * 100
            elif kind == "thousand":
                total += max(current, 1) * 1000
                current = 0
            else:
                # A unit after a unit, or a ten after a ten/unit, starts a new
                # number ("хоёр гурав" is 2 and 3, not 5).
                if value < 10 and current % 10 != 0:
                    break
                if 10 <= value < 100 and current % 100 != 0:
                    break
                current += value
            i += 1
            if terminal:
                break
        spans.append(NumberSpan(total + current, start, i, terminal))
    return spans


_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}


def kanji_to_int(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, current = 0, 0
    for ch in text:
        if ch in _KANJI_DIGITS:
            current = _KANJI_DIGITS[ch]
        elif ch in _KANJI_UNITS:
            total += (current or 1) * _KANJI_UNITS[ch]
            current = 0
        else:
            return None
    return total + current


_JA_NUM = re.compile(r"[0-9]+|[〇零一二三四五六七八九十百千]+")


def ja_numbers(text: str) -> list[int]:
    out = []
    for m in _JA_NUM.finditer(text):
        v = kanji_to_int(m.group(0))
        if v is not None:
            out.append(v)
    return out


_DIGITS = re.compile(r"\d+")


def digit_numbers(text: str) -> list[int]:
    return [int(x) for x in _DIGITS.findall(text or "")]


TR_NUMBER_WORDS = {
    "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9,
    "on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "elli": 50, "altmış": 60, "yetmiş": 70,
    "seksen": 80, "doksan": 90, "yüz": 100, "bin": 1000,
}
