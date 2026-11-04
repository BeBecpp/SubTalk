"""Compose the Zoom caption payload(s) for one speech event from its per-language texts.

Zoom's Manual Caption API is a single caption stream (no documented per-attendee
language channel), so one event becomes ONE sequence of POSTs:

* BILINGUAL (production default) - Turkish line, then English line, in the same POST:
    two_line    "TR: ...\\nEN: ..."   (Zoom documents newline support)
    single_line "TR: ... | EN: ..."   (fallback if a Zoom client strips the newline)
  If one language was withheld, only the other line is sent (never a stand-in).
* TR / EN - one language only.

Long texts are split, never truncated. Both languages are split into the SAME number
of parts at the best phrase boundaries (sentence > clause > word), so part i of the
Turkish and part i of the English cover roughly the same stretch of speech. A Bible
reference is never separated from its book name, a negation never from its verb,
and a number never from what it counts.
"""
from __future__ import annotations

import re

MODES = ("TR", "EN", "BILINGUAL")
LAYOUTS = ("two_line", "single_line")
SINGLE_LINE_SEP = " | "

_SENT_END = re.compile(r"[.!?…]['\"”’»)]*$")
_CLAUSE_END = re.compile(r"[,;:—–]$")
_REF = re.compile(r"^\d+[:.,]\d")                       # 5:22, 3.16 -> belongs to the previous word (book)
_NUM = re.compile(r"^\d+([.,]\d+)?[.,]?$")              # 7, 16:00, "1." (as in "1. Korintliler")
_NEG_NEXT = {"not", "never", "no", "değil", "yok", "hiç", "asla", "n't"}
_SOFT_NEXT = {"and", "or", "but", "ve", "veya", "ama", "fakat", "of", "the", "a", "an", "to", "ile", "için"}
_INF = float("inf")


def _penalty(prev: str, nxt: str) -> float:
    """Cost of breaking between word ``prev`` and word ``nxt``."""
    if _REF.match(nxt) or _NUM.match(prev) or (_NUM.match(nxt) and len(nxt) <= 4):
        return _INF  # "Efesliler | 5:22", "1. | Korintliler", "3 | gün"
    if nxt.lower().strip(",.;:") in _NEG_NEXT:
        return _INF  # a part must never start with "not" / "değil" (reads as the opposite)
    if _SENT_END.search(prev):
        return 0.0
    if _CLAUSE_END.search(prev):
        return 120.0
    if prev.lower() in _SOFT_NEXT:
        return 1500.0  # do not end a part on "and" / "ve"
    return 500.0


def _split_k(words: list[str], k: int, max_chars: int) -> list[str] | None:
    """Best split of ``words`` into exactly ``k`` parts of <= max_chars, or None."""
    n = len(words)
    if k > n:
        return None
    lens = [len(w) for w in words]
    total = sum(lens) + n - 1
    target = total / k

    def seg_len(i, j):  # words[i:j]
        return sum(lens[i:j]) + (j - i - 1)

    # dp[p][j]: best cost to put words[:j] into p parts; back[p][j]: start of the last part.
    dp = [[_INF] * (n + 1) for _ in range(k + 1)]
    back = [[-1] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for p in range(1, k + 1):
        for j in range(p, n + 1):
            for i in range(p - 1, j):
                if dp[p - 1][i] == _INF:
                    continue
                L = seg_len(i, j)
                if L > max_chars and j - i > 1:  # a single oversize unit may stand alone
                    continue
                brk = 0.0 if i == 0 else _penalty(words[i - 1].rsplit(" ", 1)[-1], words[i].split(" ", 1)[0])
                c = dp[p - 1][i] + (L - target) ** 2 + brk
                if c < dp[p][j]:
                    dp[p][j], back[p][j] = c, i
    if dp[k][n] == _INF:
        return None
    parts, j = [], n
    for p in range(k, 0, -1):
        i = back[p][j]
        parts.append(" ".join(words[i:j]))
        j = i
    return parts[::-1]


def _words(text: str, max_chars: int) -> list[str]:
    """Words, with unbreakable runs ("1. Korintliler 13:4-8", "3 gün") glued into one unit.
    A unit longer than ``max_chars`` stays whole (a slightly long line beats a split reference)."""
    out: list[str] = []
    for w in " ".join((text or "").split()).split(" "):
        if not w:
            continue
        if out and _penalty(out[-1].rsplit(" ", 1)[-1], w) == _INF:
            out[-1] += " " + w
        else:
            out.append(w)
    return out


def min_parts(text: str, max_chars: int) -> int:
    words = _words(text, max_chars)
    if not words:
        return 0
    for k in range(1, len(words) + 1):
        if _split_k(words, k, max_chars) is not None:
            return k
    return len(words)


def split_into(text: str, k: int, max_chars: int) -> list[str]:
    """Split ``text`` into ``k`` balanced parts (fewer only if it has fewer words)."""
    words = _words(text, max_chars)
    if not words:
        return []
    for kk in range(min(k, len(words)), len(words) + 1):
        parts = _split_k(words, kk, max_chars)
        if parts is not None:
            return parts
    return words  # unreachable: one word per part always fits


def compose_parts(texts: dict[str, str], mode: str, max_chars: int = 84, labels: bool = True,
                  layout: str = "two_line") -> list[str]:
    if layout not in LAYOUTS:
        raise ValueError(f"unknown caption layout {layout!r}")
    tr = (texts.get("tr") or "").strip()
    en = (texts.get("en") or "").strip()
    if mode == "TR":
        return split_into(tr, min_parts(tr, max_chars), max_chars) if tr else []
    if mode == "EN":
        return split_into(en, min_parts(en, max_chars), max_chars) if en else []
    if mode != "BILINGUAL":
        raise ValueError(f"unknown output mode {mode!r}")
    k = max(min_parts(tr, max_chars), min_parts(en, max_chars))
    if k == 0:
        return []
    tr_parts = split_into(tr, k, max_chars) if tr else []
    en_parts = split_into(en, k, max_chars) if en else []
    sep = "\n" if layout == "two_line" else SINGLE_LINE_SEP
    out = []
    for i in range(max(len(tr_parts), len(en_parts))):
        lines = []
        if i < len(tr_parts):
            lines.append(("TR: " if labels else "") + tr_parts[i])
        if i < len(en_parts):
            lines.append(("EN: " if labels else "") + en_parts[i])
        out.append(sep.join(lines))
    return out


def reading_time_s(part: str, cps: float = 15.0, lo: float = 1.5, hi: float = 4.0) -> float:
    """How long a part should stay on screen before the NEXT part of the same event
    replaces it: guests read one language, so the longest line counts."""
    longest = max((len(x) for x in re.split(r"\n|\s\|\s", part)), default=0)
    return max(lo, min(hi, longest / cps))
