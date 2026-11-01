"""Split a Turkish caption into Zoom-sized chunks at natural boundaries."""
from __future__ import annotations

import re

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE = re.compile(r"(?<=[,;:—–])\s+")


def _split_words(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        while len(word) > limit:  # pathological long token
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:limit])
            word = word[limit:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= limit:
            cur += " " + word
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out


def _pack(pieces: list[str], limit: int) -> list[str]:
    out, cur = [], ""
    for p in pieces:
        if not cur:
            cur = p
        elif len(cur) + 1 + len(p) <= limit:
            cur += " " + p
        else:
            out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


def chunk_caption(text: str, max_chars: int = 84) -> list[str]:
    text = " ".join((text or "").split())
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    for sentence in _SENTENCE.split(text):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        for clause in _CLAUSE.split(sentence):
            if len(clause) <= max_chars:
                pieces.append(clause)
            else:
                pieces.extend(_split_words(clause, max_chars))
    return _pack(pieces, max_chars)


def two_line(text: str, line_chars: int = 42) -> str:
    """Optionally break a chunk into two balanced lines with "\\n".

    Only used when config zoom.line_breaks is true (pending M0-A verification
    that Zoom renders the newline as documented).
    """
    if len(text) <= line_chars:
        return text
    words = text.split()
    best, best_cost = text, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if len(a) > line_chars or len(b) > line_chars:
            continue
        cost = abs(len(a) - len(b))
        if best_cost is None or cost < best_cost:
            best, best_cost = a + "\n" + b, cost
    return best
