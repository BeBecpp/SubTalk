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

# Spouse/partner words: never added unless the source mentions a spouse or the couple.
# Whole-word forms only ("eşit", "eşya", "teşekkür" must not match).
_SPOUSE_TR = re.compile(
    r"(eş|eşi|eşin|eşine|eşini|eşinde|eşinden|eşiyle|eşim|eşime|eşimi|eşimiz|eşimize|eşler|eşleri|eşlerine"
    r"|karı|karısı|karısına|karısını|karısıyla|karım|koca|kocası|kocasına|kocasını|kocasıyla|kocam)([’'].*)?"
)
_SPOUSE_SRC = {
    "mn": ("эхнэр", "нөхөр", "нөхр", "хань", "гэргий", "хос", "сүйт"),
    "ja": ("妻", "夫", "奥さん", "旦那", "配偶", "夫婦", "新婦", "新郎", "花嫁", "花婿", "嫁"),
}
_GROOM_TR = re.compile(r"dama[dt]\w*([’'].*)?")
_GROOM_SRC = {"mn": ("сүйт залуу", "залуу", "хүргэн", "сүйт"), "ja": ("新郎", "花婿", "婿")}
_WORD = re.compile(r"[\wâîûçğıöşüÇĞİÖŞÜ]+(?:[’'][\wâîûçğıöşü]+)?")


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def fix_name_spelling(text: str, name: str) -> tuple[str, bool]:
    """Replace near-miss spellings of ``name`` (edit distance <= 2, same first letter,
    names of 5+ letters) with the approved spelling, keeping a Turkish suffix after an
    apostrophe: Galhuu'ya -> Galkhuu'ya. Only called when the source names the person."""
    target = turkish_lower(name)
    if len(target) < 5:
        return text, False
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        word = m.group(0)
        parts = re.match(r"^(.*?)([’'])(.*)$", word)
        core, sep, suffix = parts.groups() if parts else (word, "", "")
        low = turkish_lower(core)
        if low == target or not low or low[0] != target[0] or abs(len(low) - len(target)) > 2:
            return word
        if _edit_distance(low, target) <= 2:
            changed = True
            return name + (sep + suffix if sep else "")
        return word

    return _WORD.sub(repl, text), changed


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

        # Names spoken in the source: correct near-miss spellings (Galhuu -> Galkhuu).
        for name in self.g.names:
            forms = name.ja if lang == "ja" else name.mn
            if forms and contains_form(forms, source, lang):
                text, changed = fix_name_spelling(text, name.tr)
                if changed:
                    flags.append(f"name_spelling_corrected:{name.id}")

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

        # 3b. No added spouse / partner (e.g. "Galkhuu ve eşine" for "Галхүүд").
        if any(_SPOUSE_TR.fullmatch(w) for w in words) and not _source_has_stem(
                source, lang, _SPOUSE_SRC.get(lang, _SPOUSE_SRC["mn"])):
            blocked.append("spouse_invented")
        # "damat" (groom; damadı, damada...) is unambiguous. "gelin" is not checked: it is also
        # the everyday imperative "come".
        if any(_GROOM_TR.fullmatch(w) for w in words) and not _source_has_stem(
                source, lang, _GROOM_SRC.get(lang, _GROOM_SRC["mn"])):
            blocked.append("groom_invented")

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
