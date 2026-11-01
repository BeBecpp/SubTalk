"""Deterministic guards applied to every translated caption before output.

Claude's output is untrusted until these checks pass. A failed *block* check
drops the caption (safe omission); a *flag* is logged and shown to the
operator but does not drop it. These checks catch invented references,
numbers, names and religious content - they cannot prove the translation
is faithful.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from subtalk.glossary.citations import Citation
from subtalk.glossary.match import contains_form, mn_tokens
from subtalk.glossary.normalize import norm_ja, turkish_lower
from subtalk.glossary.numerals import digit_numbers, ja_numbers, parse_mn_numbers
from subtalk.glossary.store import Glossary

# Concepts that must never appear in Turkish unless the source contains them.
# Source stems are prefixes (Mongolian verbs conjugate: залбирах -> залбирцгаая).
SENSITIVE = {
    "prayer": {"tr": ("dua",), "mn": ("залбир",), "ja": ("祈",)},
    "vow": {"tr": ("yemin",), "mn": ("тангар", "андга"), "ja": ("誓",)},
    "covenant": {"tr": ("ahit", "ahd"), "mn": ("гэрээ",), "ja": ("契約",)},
    "amen": {"tr": ("âmin", "amin"), "mn": ("амен", "аминь"), "ja": ("アーメン",)},
    "verse": {"tr": ("ayet",), "mn": ("зүйл", "эшлэл", "ишлэл", "шүлэг"), "ja": ("節", "聖句")},
}
DIVINE_TERM_CATEGORIES = ("divine_name", "theology")
_RAB = re.compile(r"rab+(?:[’']\w*|im|in|imiz|iniz|ı|i)?")


def _is_divine_word(w: str) -> bool:
    return w.startswith(("yehova", "tanrı", "allah")) or bool(_RAB.fullmatch(w))


@dataclass
class GuardResult:
    ok: bool
    text: str
    blocked: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _tr_words(text: str) -> list[str]:
    return re.findall(r"[\wâîûçğıöşü’']+", turkish_lower(text))


def _has_tr_stem(words: list[str], stems) -> bool:
    return any(w.startswith(s) for w in words for s in stems)


def _source_has_stem(source: str, lang: str, stems) -> bool:
    if lang == "ja":
        t = norm_ja(source)
        return any(s in t for s in stems)
    return any(tok.startswith(s) for tok in mn_tokens(source) for s in stems)


class Guards:
    def __init__(self, glossary: Glossary, *, max_ratio_mn: float = 3.0, max_ratio_ja: float = 5.0,
                 min_len_allowance: int = 60):
        self.g = glossary
        self.max_ratio = {"mn": max_ratio_mn, "ja": max_ratio_ja}
        self.min_len_allowance = min_len_allowance
        names: list[tuple[str, str]] = []
        self._legacy: list[tuple[re.Pattern, str]] = []
        for b in glossary.books:
            names.append((b.tr, b.id))
            for alias in b.tr_recognition_aliases:
                names.append((alias, b.id))
                # Only rewrite a legacy name in reference position ("Efesoslular 5:33"),
                # never as an ordinary word ("tekrar" = "again").
                self._legacy.append((re.compile(re.escape(alias) + r"(?=\s+\d)"), b.tr))
        names.sort(key=lambda x: -len(x[0]))
        self._book_by_name = {turkish_lower(n): bid for n, bid in names}
        alt = "|".join(re.escape(n) for n, _ in names)
        self._ref_re = re.compile(
            rf"(?<![\w])({alt})\s+(\d+)(?:\s*:\s*(\d+)(?:\s*[-–]\s*(\d+))?|\.\s*bölüm)?", re.IGNORECASE
        )
        self._jehovah = [t for t in glossary.terms if t.id in ("jehovah", "jehovahs_witnesses")]
        self._divine_terms = [t for t in glossary.terms if t.category in DIVINE_TERM_CATEGORIES]

    def check(self, source: str, lang: str, citations: list[Citation], output: str) -> GuardResult:
        text = (output or "").strip()
        blocked: list[str] = []
        flags: list[str] = []
        if not text:
            return GuardResult(False, "", ["empty_output"])

        for pattern, canonical in self._legacy:
            if pattern.search(text):
                text = pattern.sub(canonical, text)
                flags.append("legacy_book_name_rewritten")

        # 1. Bible references in the output must match references detected in the source.
        allowed = {(c.book_id, c.chapter, c.verse, c.verse_end) for c in citations
                   if c.status in ("valid", "unverified")}
        allowed_chapters = {(c.book_id, c.chapter) for c in citations
                            if c.status in ("valid", "unverified") and c.verse is None}
        out_ref_numbers: set[int] = set()
        for m in self._ref_re.finditer(text):
            book_id = self._book_by_name[turkish_lower(m.group(1))]
            ch = int(m.group(2))
            v = int(m.group(3)) if m.group(3) else None
            ve = int(m.group(4)) if m.group(4) else None
            out_ref_numbers.update(x for x in (ch, v, ve) if x is not None)
            if v is None:
                ok = (book_id, ch) in allowed_chapters or any(k[0] == book_id and k[1] == ch for k in allowed)
            else:
                ok = (book_id, ch, v, ve) in allowed
            if not ok:
                blocked.append(f"unsupported_citation:{book_id} {ch}:{v}")
        for c in citations:
            if c.status == "invalid":
                flags.append(f"invalid_reference_in_source:{c.book_id} {c.chapter}:{c.verse}")

        words = _tr_words(text)
        # 2. Divine name: preserved when spoken, never invented.
        src_jehovah = any(contains_form(t.ja if lang == "ja" else t.mn, source, lang) for t in self._jehovah)
        out_jehovah = any(w.startswith("yehova") for w in words)
        if src_jehovah and not out_jehovah:
            blocked.append("divine_name_missing")
        if out_jehovah and not src_jehovah:
            blocked.append("divine_name_invented")
        src_divine = src_jehovah or any(
            contains_form(t.ja if lang == "ja" else t.mn, source, lang) for t in self._divine_terms
        )
        if not src_divine and any(_is_divine_word(w) for w in words):
            blocked.append("divine_reference_invented")

        # 3. Sensitive religious/ceremony concepts must come from the source.
        for concept, spec in SENSITIVE.items():
            if _has_tr_stem(words, spec["tr"]) and not _source_has_stem(source, lang, spec[lang if lang in spec else "mn"]):
                blocked.append(f"{concept}_invented")

        # 4. No invented digits.
        src_numbers = set(digit_numbers(source))
        if lang == "ja":
            src_numbers.update(ja_numbers(norm_ja(source)))
        else:
            src_numbers.update(s.value for s in parse_mn_numbers(mn_tokens(source)))
        for c in citations:
            src_numbers.update(x for x in (c.chapter, c.verse, c.verse_end) if x is not None)
        for n in digit_numbers(text):
            if n not in src_numbers:
                blocked.append(f"number_invented:{n}")
        if src_numbers - set(digit_numbers(text)) - {1}:
            flags.append("source_number_not_as_digits")

        # 5. Event names must be spoken to appear.
        for name in self.g.names:
            if any(w.startswith(turkish_lower(name.tr)) for w in words):
                forms = name.ja if lang == "ja" else name.mn
                if not forms or not contains_form(forms, source, lang):
                    blocked.append(f"name_invented:{name.id}")
                elif not name.approved:
                    flags.append(f"name_spelling_unapproved:{name.id}")

        # 6. Length sanity (catches inserted script/verse text).
        limit = max(self.min_len_allowance, self.max_ratio.get(lang, 3.0) * len(source.strip()))
        if len(text) > limit:
            blocked.append("length_ratio")

        return GuardResult(not blocked, text, blocked, flags)
