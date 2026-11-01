"""Deterministic Bible reference detection (Mongolian and Japanese).

Rules (JW Glossary v1 translation policy):
* a book is resolved by exact name/alias only - never guessed from a topic;
* a precise reference needs the book AND explicitly spoken numbers next to it;
* chapter beyond the book's chapter count, or verse beyond a known verse count
  -> INVALID (numbers are never "corrected");
* verse counts not in the curated table -> UNVERIFIED (allowed only because the
  numbers were explicitly spoken);
* no verse text is ever produced here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from subtalk.glossary.match import _mn_token_matches, mn_tokens
from subtalk.glossary.normalize import norm_ja
from subtalk.glossary.numerals import kanji_to_int, mn_token_number, parse_mn_numbers
from subtalk.glossary.store import Glossary

CHAPTER_WORDS = ("бүлэг", "бүлг")
VERSE_WORDS = ("зүйл", "эшлэл", "ишлэл")
# Words that make a bare book name a Bible-book mention.
MN_CUES = ("библи", "судар", "судр", "уншъя", "уншиж", "унших", "уншина", "эшлэл", "ишлэл", "бүлэг", "бүлг")
JA_CUES = ("聖書", "章", "節", "読み", "読ん", "書を", "書の")
ORDINAL_PREFIX = {"нэгдүгээр": 1, "хоёрдугаар": 2, "гуравдугаар": 3}
_JA_ORDINAL = {"第一": 1, "第二": 2, "第三": 3}


@dataclass(frozen=True)
class Citation:
    book_id: str
    chapter: int | None
    verse: int | None
    verse_end: int | None
    status: str  # valid | unverified | invalid | book_only
    reason: str = ""

    @property
    def has_numbers(self) -> bool:
        return self.chapter is not None

    def key(self) -> tuple:
        return (self.book_id, self.chapter, self.verse, self.verse_end)


def format_tr(glossary: Glossary, c: Citation) -> str | None:
    """Canonical Turkish rendering, or None when numbers must not be shown."""
    name = glossary.book(c.book_id).tr
    if c.status in ("invalid", "book_only") or c.chapter is None:
        return name if c.status == "book_only" else None
    if c.verse is None:
        return f"{name} {c.chapter}. bölüm"
    ref = f"{name} {c.chapter}:{c.verse}"
    if c.verse_end:
        ref += f"-{c.verse_end}"
    return ref


def validate(glossary: Glossary, book_id: str, chapter: int | None, verse: int | None,
             verse_end: int | None) -> Citation:
    if chapter is None:
        return Citation(book_id, None, None, None, "book_only")
    max_ch = glossary.chapter_counts.get(book_id)
    if max_ch is None or not (1 <= chapter <= max_ch):
        return Citation(book_id, chapter, verse, verse_end, "invalid", f"chapter {chapter} > {max_ch}")
    if verse is None:
        return Citation(book_id, chapter, None, None, "valid")
    max_v = glossary.verse_counts.get(f"{book_id} {chapter}")
    last = verse_end or verse
    if verse < 1 or (verse_end is not None and verse_end <= verse):
        return Citation(book_id, chapter, verse, verse_end, "invalid", "bad verse range")
    if max_v is None:
        return Citation(book_id, chapter, verse, verse_end, "unverified", "verse count not curated")
    if last > max_v:
        return Citation(book_id, chapter, verse, verse_end, "invalid", f"verse {last} > {max_v}")
    return Citation(book_id, chapter, verse, verse_end, "valid")


class CitationDetector:
    def __init__(self, glossary: Glossary):
        self.g = glossary
        self._mn_index: list[tuple[int | None, list[str], str]] = []
        self._ja_index: list[tuple[str, str]] = []
        for b in glossary.books:
            for form in (b.mn, *b.mn_aliases):
                num, toks = self._split_number_prefix(mn_tokens(form))
                self._mn_index.append((num, toks, b.id))
            for form in (b.ja, *b.ja_aliases):
                self._ja_index.append((norm_ja(form), b.id))
        # Longest names first so "1 Иохан" wins over "Иохан".
        self._mn_index.sort(key=lambda x: (-(x[0] is not None), -len(x[1])))
        self._ja_index.sort(key=lambda x: -len(x[0]))

    @staticmethod
    def _split_number_prefix(toks: list[str]) -> tuple[int | None, list[str]]:
        if toks and toks[0].isdigit():
            return int(toks[0]), toks[1:]
        return None, toks

    # ------------------------------------------------------------- Mongolian
    def _book_at(self, tokens: list[str], i: int) -> tuple[str, int] | None:
        """Return (book_id, end_index) if a book name starts at token i."""
        prefix_num, j = None, i
        tok = tokens[i]
        m = re.match(r"^(\d)(?:-р|\.)?$", tok)
        if tok in ORDINAL_PREFIX:
            prefix_num, j = ORDINAL_PREFIX[tok], i + 1
        elif m:
            prefix_num, j = int(m.group(1)), i + 1
        for num, name_toks, book_id in self._mn_index:
            if num != prefix_num and not (num is None and prefix_num is None):
                continue
            n = len(name_toks)
            if j + n > len(tokens) or n == 0:
                continue
            if all(_mn_token_matches(name_toks[k], tokens[j + k], last=(k == n - 1)) for k in range(n)):
                return book_id, j + n
        return None

    def _parse_tail(self, tokens: list[str], start: int) -> tuple[int | None, int | None, int | None]:
        """Parse chapter/verse spoken right after the book name."""
        j = start
        if j < len(tokens) and tokens[j].startswith("ном"):  # "Эфэс номын 5..." / "Эхлэл номын"
            j += 1
        if j >= len(tokens):
            return None, None, None
        m = re.fullmatch(r"(\d+):(\d+)(?:-(\d+))?", tokens[j])
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
        window = tokens[j:j + 12]
        spans = parse_mn_numbers(window)
        if not spans or spans[0].start != 0:
            return None, None, None  # numbers must follow the book name immediately
        first = spans[0]
        after_first = window[first.end] if first.end < len(window) else ""
        chapter = first.value
        verse = None
        nxt_index = first.end + (1 if after_first.startswith(CHAPTER_WORDS) else 0)
        second = next((s for s in spans[1:] if s.start == nxt_index), None)
        if second is not None:
            verse = second.value
        return chapter, verse, None

    def detect_mn(self, text: str) -> list[Citation]:
        """Book names such as "Эхлэл" (beginning) or "Дуулал" (song) are also
        ordinary words, so a book counts only with explicit evidence: spoken
        chapter+verse (or a chapter word), digits like 5:33, or a Bible cue."""
        tokens = mn_tokens(text)
        has_cue = any(t.startswith(MN_CUES) for t in tokens)
        out: list[Citation] = []
        i = 0
        while i < len(tokens):
            hit = self._book_at(tokens, i)
            if not hit:
                i += 1
                continue
            book_id, end = hit
            chapter, verse, verse_end = self._parse_tail(tokens, end)
            next_tok = tokens[end] if end < len(tokens) else ""
            book_cue = has_cue or next_tok.startswith("ном")
            if chapter is not None and verse is None and not (book_cue or self._chapter_word_after(tokens, end)):
                chapter = None  # a lone number after an ordinary word is not a reference
            if chapter is None and not book_cue:
                i = end
                continue
            out.append(validate(self.g, book_id, chapter, verse, verse_end))
            i = end
        return self._dedupe(out)

    @staticmethod
    def _chapter_word_after(tokens: list[str], end: int) -> bool:
        return any(t.startswith(CHAPTER_WORDS) for t in tokens[end:end + 4])

    # ------------------------------------------------------------- Japanese
    _JA_TAIL = re.compile(
        r"^\s*(?:第)?([0-9〇一二三四五六七八九十百]+)\s*(?:章|:)\s*([0-9〇一二三四五六七八九十百]+)?\s*(?:節)?"
        r"(?:\s*[-~〜から]+\s*([0-9〇一二三四五六七八九十百]+)\s*節?)?"
    )

    def detect_ja(self, text: str) -> list[Citation]:
        t = norm_ja(text)
        has_cue = any(c in t for c in JA_CUES)
        out: list[Citation] = []
        i = 0
        while i < len(t):
            for form, book_id in self._ja_index:
                if t.startswith(form, i):
                    end = i + len(form)
                    m = self._JA_TAIL.match(t[end:])
                    if m:
                        ch = kanji_to_int(m.group(1))
                        v = kanji_to_int(m.group(2)) if m.group(2) else None
                        ve = kanji_to_int(m.group(3)) if m.group(3) else None
                        out.append(validate(self.g, book_id, ch, v, ve))
                        end += m.end()
                    elif has_cue:
                        out.append(validate(self.g, book_id, None, None, None))
                    i = end - 1
                    break
            i += 1
        return self._dedupe(out)

    def detect(self, text: str, lang: str) -> list[Citation]:
        return self.detect_ja(text) if lang == "ja" else self.detect_mn(text)

    @staticmethod
    def _dedupe(items: list[Citation]) -> list[Citation]:
        seen, out = set(), []
        for c in items:
            if c.key() not in seen:
                seen.add(c.key())
                out.append(c)
        return out
