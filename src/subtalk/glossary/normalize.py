"""Text normalisation used for deterministic glossary/citation matching."""
from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s:\-]", re.UNICODE)
_WS = re.compile(r"\s+")


def norm_mn(text: str) -> str:
    """Mongolian Cyrillic: NFC, casefold, punctuation -> space.

    ё/е are NOT merged automatically; variants must be listed explicitly as
    aliases (glossary integration rule)."""
    text = unicodedata.normalize("NFC", text or "").casefold()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def norm_ja(text: str) -> str:
    """Japanese: NFKC (width variants, half-width ･ -> ・), no whitespace."""
    text = unicodedata.normalize("NFKC", text or "")
    return _WS.sub("", text)


def turkish_lower(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    return text.replace("I", "ı").replace("İ", "i").lower()


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF


def is_kana(ch: str) -> bool:
    o = ord(ch)
    return 0x3040 <= o <= 0x30FF


def is_cyrillic(ch: str) -> bool:
    return 0x0400 <= ord(ch) <= 0x04FF


def script_profile(text: str) -> dict[str, float]:
    """Fraction of letters that are Cyrillic / Japanese (kana+kanji) / Latin."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return {"cyrillic": 0.0, "japanese": 0.0, "latin": 0.0, "letters": 0}
    n = len(letters)
    cyr = sum(is_cyrillic(c) for c in letters)
    jap = sum(is_cjk(c) or is_kana(c) for c in letters)
    lat = sum(c.isascii() for c in letters)
    return {"cyrillic": cyr / n, "japanese": jap / n, "latin": lat / n, "letters": n}
