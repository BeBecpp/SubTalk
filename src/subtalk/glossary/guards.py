"""Deterministic guards applied to every translated caption before output.

Claude's output is untrusted until these checks pass. A failed *block* check
drops the caption in that target language (safe omission); a *flag* is logged
and shown to the operator but does not drop it. These checks catch invented
references, numbers, names, people and religious content - they cannot prove
the translation is faithful.

Each target language (Turkish, English) has its own word lists; the same
source checks apply to both. Japanese captions (no spaces between words) get the
same checks on character patterns instead of words (``_check_ja``).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from subtalk.glossary.citations import Citation
from subtalk.glossary.match import contains_form, mn_tokens
from subtalk.glossary.normalize import norm_ja, norm_mn, turkish_lower
from subtalk.glossary.numerals import digit_numbers, ja_numbers, number_in_text, parse_mn_numbers
from subtalk.glossary.store import Glossary

DIVINE_TERM_CATEGORIES = ("divine_name", "theology")

# Source evidence (Mongolian prefixes / Japanese substrings). Mongolian verbs conjugate
# (залбирах -> залбирцгаая) and drop vowels (тангараг -> тангаргаа), hence prefixes.
SRC = {
    "prayer": {"mn": ("залбир",), "ja": ("祈",)},
    "vow": {"mn": ("тангар", "андга"), "ja": ("誓",)},
    "covenant": {"mn": ("гэрээ",), "ja": ("契約",)},
    "amen": {"mn": ("амен", "аминь"), "ja": ("アーメン",)},
    "verse": {"mn": ("зүйл", "эшлэл", "ишлэл", "шүлэг"), "ja": ("節", "聖句")},
    # "гэр бүл бол-" (become a family) / "гэрлэ-" (marry): at a wedding this IS becoming husband and wife.
    "spouse": {"mn": ("эхнэр", "нөхөр", "нөхр", "хань", "гэргий", "хос", "сүйт", "гэр бүлийн хүн",
                      "гэр бүл бол", "гэрлэ"),
               "ja": ("妻", "夫", "奥さん", "旦那", "配偶", "夫婦", "新婦", "新郎", "花嫁", "花婿", "嫁")},
    "groom": {"mn": ("сүйт залуу", "хүргэн", "сүйт"), "ja": ("新郎", "花婿", "婿")},
    "bride": {"mn": ("сүйт бүсгүй", "бэр", "сүйт"), "ja": ("新婦", "花嫁", "嫁")},
}


@dataclass(frozen=True)
class Profile:
    """Target-language word lists. Patterns are full-word regexes on lower-cased words."""
    lower: Callable[[str], str]
    word_re: re.Pattern
    jehovah_prefix: str
    divine_re: re.Pattern
    concepts: dict[str, re.Pattern]  # concept -> invented-word pattern
    chapter_word: str  # "bölüm" / "chapter"
    case_sensitive_books: bool
    # "Jehovah's Witness(es)": the standard rendering of a spoken "Гэрч" (Witness) in JW speech.
    witnesses_re: re.Pattern = re.compile(r"$^")


TR = Profile(
    lower=turkish_lower,
    word_re=re.compile(r"[\wâîûçğıöşü’']+"),
    jehovah_prefix="yehova",
    witnesses_re=re.compile(r"yehova[’']n[ıi]n\s+şahi[dt]\w*"),
    divine_re=re.compile(r"(yehova|tanrı|allah)\w*([’'].*)?|rab+(?:[’']\w*|im|in|imiz|iniz|ı|i)?"),
    concepts={
        "prayer": re.compile(r"dua\w*([’'].*)?"),
        "vow": re.compile(r"yemin\w*([’'].*)?"),
        "covenant": re.compile(r"(ahit|ahd)\w*([’'].*)?"),
        "amen": re.compile(r"(âmin|amin)([’'].*)?"),
        "verse": re.compile(r"ayet\w*([’'].*)?"),
        # Whole-word forms only ("eşit", "eşya", "teşekkür" must not match).
        "spouse": re.compile(
            r"(eş|eşi|eşin|eşine|eşini|eşinde|eşinden|eşiyle|eşim|eşime|eşimi|eşimiz|eşimize|eşler|eşleri|eşlerine"
            r"|karı|karısı|karısına|karısını|karısıyla|karım|koca|kocası|kocasına|kocasını|kocasıyla|kocam)([’'].*)?"),
        # "damat" is unambiguous; "gelin" is also the everyday imperative "come", so no bride check.
        "groom": re.compile(r"dama[dt]\w*([’'].*)?"),
    },
    chapter_word="bölüm",
    case_sensitive_books=False,
)

EN = Profile(
    lower=str.lower,
    word_re=re.compile(r"[\w’']+"),
    jehovah_prefix="jehovah",
    witnesses_re=re.compile(r"jehovah[’']s\s+witness(es)?"),
    divine_re=re.compile(r"(jehovah|god|gods|lord|allah|heavenly)([’']s)?"),
    concepts={
        "prayer": re.compile(r"pray(s|ed|ing|er|ers)?"),
        "vow": re.compile(r"vow(s|ed|ing)?"),
        "covenant": re.compile(r"covenants?"),
        "amen": re.compile(r"amen"),
        "verse": re.compile(r"verses?"),
        "spouse": re.compile(r"(wife|wives|husband|husbands|spouse|spouses|mate|mates)([’']s)?"),
        "groom": re.compile(r"(groom|grooms|bridegroom|bridegrooms)([’']s)?"),
        "bride": re.compile(r"(bride|brides)([’']s)?"),
    },
    chapter_word="chapter",
    case_sensitive_books=True,  # "Numbers", "Acts", "Mark", "Job" are also ordinary words
)
PROFILES = {"tr": TR, "en": EN}

# Japanese output: concept -> invented-content pattern (substring search, no word boundaries).
JA_CONCEPTS = {
    "prayer": re.compile(r"祈"),
    "vow": re.compile(r"誓"),
    "covenant": re.compile(r"契約"),
    "amen": re.compile(r"アーメン"),
    "verse": re.compile(r"聖句|(?<![季調関])節(?![約度目分])"),
    # 丈夫 (sturdy), 工夫 (idea), 夫人 (madam) are not spouses.
    "spouse": re.compile(r"妻|(?<![丈工])夫(?!人)|配偶|旦那|奥さん|伴侶"),
    "groom": re.compile(r"新郎|花婿|お婿"),
    "bride": re.compile(r"新婦|花嫁|お嫁"),
}
JA_JEHOVAH = "エホバ"
JA_WITNESSES = "エホバの証人"
# 精神 (mind), 神経 (nerves), 神聖 (sacred) are not references to God.
JA_DIVINE = re.compile(r"エホバ|(?<!精)神(?![経聖])|天の父|天のお父|創造者|全能者")
# English recognition aliases that are NOT rewritten (a single psalm is "Psalm 23" in English).
_NO_REWRITE = {"Psalm"}


@dataclass
class GuardResult:
    ok: bool
    text: str
    blocked: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _source_has(source: str, lang: str, stems) -> bool:
    if lang == "ja":
        t = norm_ja(source)
        return any(s in t for s in stems)
    norm = norm_mn(source)
    toks = mn_tokens(source)
    return any((" " in s and s in norm) or any(tok.startswith(s) for tok in toks) for s in stems)


def _invalid_shown(citations: list[Citation], text: str, target: str) -> list[str]:
    """A spoken reference that does not exist (e.g. Ephesians 5:34) must not reach a caption in
    any form - digits or words ("verse thirty-four"). Its non-existent number is the verse, or the
    chapter when no verse was spoken."""
    out = []
    for c in citations:
        if c.status != "invalid":
            continue
        bad = c.verse if c.verse is not None else c.chapter
        if bad is not None and number_in_text(bad, text, target):
            out.append(f"invalid_reference_shown:{c.book_id} {c.chapter}:{c.verse}")
    return out


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


_WORD = re.compile(r"[\wâîûçğıöşüÇĞİÖŞÜ]+(?:[’'][\wâîûçğıöşü]+)?")


def fix_name_spelling(text: str, name: str, lower: Callable[[str], str] = turkish_lower) -> tuple[str, bool]:
    """Replace near-miss spellings of ``name`` (edit distance <= 2, same first letter,
    names of 5+ letters) with the approved spelling, keeping a suffix after an
    apostrophe: Galhuu'ya -> Galkhuu'ya. Only called when the source names the person."""
    target = lower(name)
    if len(target) < 5:
        return text, False
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        word = m.group(0)
        parts = re.match(r"^(.*?)([’'])(.*)$", word)
        core, sep, suffix = parts.groups() if parts else (word, "", "")
        low = lower(core)
        if low == target or not low or low[0] != target[0] or abs(len(low) - len(target)) > 2:
            return word
        if _edit_distance(low, target) <= 2:
            changed = True
            return name + (sep + suffix if sep else "")
        return word

    return _WORD.sub(repl, text), changed


class Guards:
    def __init__(self, glossary: Glossary, *, max_ratio_mn: float = 3.0, max_ratio_ja: float = 5.0,
                 min_len_allowance: int = 60):
        self.g = glossary
        self.max_ratio = {"mn": max_ratio_mn, "ja": max_ratio_ja}
        self.min_len_allowance = min_len_allowance
        self._jehovah = [t for t in glossary.terms if t.id in ("jehovah", "jehovahs_witnesses")]
        self._divine_terms = [t for t in glossary.terms if t.category in DIVINE_TERM_CATEGORIES]
        self._books: dict[str, tuple[re.Pattern, dict[str, str], list[tuple[re.Pattern, str]]]] = {}
        for target, prof in PROFILES.items():
            names: list[tuple[str, str]] = []
            legacy: list[tuple[re.Pattern, str]] = []
            for b in glossary.books:
                canonical = (b.en or b.tr) if target == "en" else b.tr
                aliases = b.en_aliases if target == "en" else b.tr_recognition_aliases
                names.append((canonical, b.id))
                for alias in aliases:
                    names.append((alias, b.id))
                    if alias not in _NO_REWRITE:
                        # Only rewrite in reference position ("Efesoslular 5:33"), never as an
                        # ordinary word ("tekrar" = "again").
                        legacy.append((re.compile(r"(?<![\w])" + re.escape(alias) + r"(?=\s+\d)"), canonical))
            names.sort(key=lambda x: -len(x[0]))
            by_name = {prof.lower(n): bid for n, bid in names}
            alt = "|".join(re.escape(n) for n, _ in names)
            if target == "en":
                tail = r"(?:chapter\s+)?(\d+)(?:\s*:\s*(\d+)(?:\s*[-–]\s*(\d+))?)?"
            else:
                tail = r"(\d+)(?:\s*:\s*(\d+)(?:\s*[-–]\s*(\d+))?|\.\s*bölüm)?"
            flags = 0 if prof.case_sensitive_books else re.IGNORECASE
            self._books[target] = (re.compile(rf"(?<![\w])({alt})\s+{tail}", flags), by_name, legacy)
        ja_names = sorted({unicodedata.normalize("NFKC", b.ja): b.id for b in glossary.books}.items(),
                          key=lambda x: -len(x[0]))
        self._ja_book_by_name = dict(ja_names)
        self._ja_ref = re.compile(
            "(" + "|".join(re.escape(n) for n, _ in ja_names) + r")\s*(\d+)(?:\s*[:]\s*(\d+)(?:\s*[-–~〜]\s*(\d+))?|\s*章)?")

    def check(self, source: str, lang: str, citations: list[Citation], output: str,
              target: str = "tr") -> GuardResult:
        if target == "ja":
            return self._check_ja(source, lang, citations, output)
        prof = PROFILES[target]
        text = (output or "").strip()
        blocked: list[str] = []
        flags: list[str] = []
        if not text:
            return GuardResult(False, "", ["empty_output"])
        ref_re, book_by_name, legacy = self._books[target]

        for pattern, canonical in legacy:
            if pattern.search(text):
                text = pattern.sub(canonical, text)
                flags.append("legacy_book_name_rewritten")

        # 1. Bible references in the output must match references detected in the source.
        allowed = {(c.book_id, c.chapter, c.verse, c.verse_end) for c in citations
                   if c.status in ("valid", "unverified")}
        allowed_chapters = {(c.book_id, c.chapter) for c in citations
                            if c.status in ("valid", "unverified") and c.verse is None}
        for m in ref_re.finditer(text):
            book_id = book_by_name[prof.lower(m.group(1))]
            ch = int(m.group(2))
            v = int(m.group(3)) if m.group(3) else None
            ve = int(m.group(4)) if m.group(4) else None
            if v is None:
                ok = (book_id, ch) in allowed_chapters or any(k[0] == book_id and k[1] == ch for k in allowed)
            else:
                ok = (book_id, ch, v, ve) in allowed
            if not ok:
                blocked.append(f"unsupported_citation:{book_id} {ch}:{v}")
        blocked.extend(_invalid_shown(citations, text, target))
        for c in citations:
            if c.status == "invalid":
                flags.append(f"invalid_reference_in_source:{c.book_id} {c.chapter}:{c.verse}")

        # Names spoken in the source: correct near-miss spellings (Galhuu -> Galkhuu).
        for name in self.g.names:
            forms = name.ja if lang == "ja" else name.mn
            if forms and contains_form(forms, source, lang):
                text, changed = fix_name_spelling(text, name.target(target), prof.lower)
                if changed:
                    flags.append(f"name_spelling_corrected:{name.id}")

        words = prof.word_re.findall(prof.lower(text))
        # 2. Divine name: preserved when spoken, never invented.
        src_jehovah = any(contains_form(t.ja if lang == "ja" else t.mn, source, lang) for t in self._jehovah)
        dwords = words  # words checked for divine references
        if not src_jehovah and lang == "mn" and _source_has(source, lang, ("гэрч",)):
            # "Гэрч нөхөр" -> "a husband who is one of Jehovah's Witnesses": allowed only as that phrase.
            dwords = prof.word_re.findall(prof.witnesses_re.sub(" ", prof.lower(text)))
        out_jehovah = any(w.startswith(prof.jehovah_prefix) for w in dwords)
        if src_jehovah and not out_jehovah:
            blocked.append("divine_name_missing")
        if out_jehovah and not src_jehovah:
            blocked.append("divine_name_invented")
        src_divine = src_jehovah or any(
            contains_form(t.ja if lang == "ja" else t.mn, source, lang) for t in self._divine_terms
        )
        if not src_divine and any(prof.divine_re.fullmatch(w) for w in dwords):
            blocked.append("divine_reference_invented")

        # 3. Religious/ceremony concepts and people must come from the source.
        for concept, pattern in prof.concepts.items():
            stems = SRC[concept].get(lang, SRC[concept]["mn"])
            if any(pattern.fullmatch(w) for w in words) and not _source_has(source, lang, stems):
                blocked.append(f"{concept}_invented")

        # 4. No invented digits.
        src_numbers = set(digit_numbers(source))
        if lang == "ja":
            src_numbers.update(ja_numbers(norm_ja(source)))
        else:
            src_numbers.update(s.value for s in parse_mn_numbers(mn_tokens(source)))
        for c in citations:
            src_numbers.update(x for x in (c.chapter, c.verse, c.verse_end, *c.more) if x is not None)
        for n in digit_numbers(text):
            if n not in src_numbers:
                blocked.append(f"number_invented:{n}")
        if src_numbers - set(digit_numbers(text)) - {1}:
            flags.append("source_number_not_as_digits")

        # 5. Event names must be spoken to appear.
        for name in self.g.names:
            if any(w.startswith(prof.lower(name.target(target))) for w in words):
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

    def _check_ja(self, source: str, lang: str, citations: list[Citation], output: str) -> GuardResult:
        """The same rules as ``check`` for a Japanese caption, on character patterns."""
        text = unicodedata.normalize("NFKC", (output or "").strip())  # full-width digits -> ASCII
        blocked: list[str] = []
        flags: list[str] = []
        if not text:
            return GuardResult(False, "", ["empty_output"])

        # 1. Bible references must match references detected in the source.
        allowed = {(c.book_id, c.chapter, c.verse, c.verse_end) for c in citations
                   if c.status in ("valid", "unverified")}
        for m in self._ja_ref.finditer(text):
            book_id, ch = self._ja_book_by_name[m.group(1)], int(m.group(2))
            v = int(m.group(3)) if m.group(3) else None
            ve = int(m.group(4)) if m.group(4) else None
            ok = (any(k[0] == book_id and k[1] == ch for k in allowed) if v is None
                  else (book_id, ch, v, ve) in allowed)
            if not ok:
                blocked.append(f"unsupported_citation:{book_id} {ch}:{v}")
        blocked.extend(_invalid_shown(citations, text, "ja"))
        for c in citations:
            if c.status == "invalid":
                flags.append(f"invalid_reference_in_source:{c.book_id} {c.chapter}:{c.verse}")

        # 2. Divine name preserved when spoken, never invented; no invented reference to God.
        src_jehovah = any(contains_form(t.ja if lang == "ja" else t.mn, source, lang) for t in self._jehovah)
        dtext = text
        if not src_jehovah and lang == "mn" and _source_has(source, lang, ("гэрч",)):
            dtext = text.replace(JA_WITNESSES, " ")  # "Гэрч" -> "エホバの証人" only as that phrase
        out_jehovah = JA_JEHOVAH in dtext
        if src_jehovah and not out_jehovah:
            blocked.append("divine_name_missing")
        if out_jehovah and not src_jehovah:
            blocked.append("divine_name_invented")
        src_divine = src_jehovah or any(
            contains_form(t.ja if lang == "ja" else t.mn, source, lang) for t in self._divine_terms)
        if not src_divine and JA_DIVINE.search(dtext):
            blocked.append("divine_reference_invented")

        # 3. Religious/ceremony concepts and people must come from the source.
        for concept, pattern in JA_CONCEPTS.items():
            stems = SRC[concept].get(lang, SRC[concept]["mn"])
            if pattern.search(text) and not _source_has(source, lang, stems):
                blocked.append(f"{concept}_invented")

        # 4. No invented digits.
        src_numbers = set(digit_numbers(source))
        if lang == "ja":
            src_numbers.update(ja_numbers(norm_ja(source)))
        else:
            src_numbers.update(s.value for s in parse_mn_numbers(mn_tokens(source)))
        for c in citations:
            src_numbers.update(x for x in (c.chapter, c.verse, c.verse_end, *c.more) if x is not None)
        for n in digit_numbers(text):
            if n not in src_numbers:
                blocked.append(f"number_invented:{n}")

        # 5. Event names (Latin spelling or a Japanese form) must be spoken to appear.
        low = text.lower()
        for name in self.g.names:
            shown = [f for f in (name.target("ja"), *name.ja) if f]
            if any(f.lower() in low for f in shown):
                forms = name.ja if lang == "ja" else name.mn
                if not forms or not contains_form(forms, source, lang):
                    blocked.append(f"name_invented:{name.id}")
                elif not name.approved:
                    flags.append(f"name_spelling_unapproved:{name.id}")

        # 6. Length sanity (Japanese is usually shorter than the source).
        limit = max(self.min_len_allowance, self.max_ratio.get(lang, 3.0) * len(source.strip()))
        if len(text) > limit:
            blocked.append("length_ratio")

        return GuardResult(not blocked, text, blocked, flags)
