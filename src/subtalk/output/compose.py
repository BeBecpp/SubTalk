"""Compose the Zoom caption payload(s) for one speech event from its per-language texts.

Zoom's Manual Caption API is a single caption stream (no documented per-attendee
language channel), so one event becomes ONE sequence of POSTs:

* BILINGUAL (production default) - Turkish line, then English line, in the same POST:
    two_line    "TR: ...\\nEN: ..."   (Zoom documents newline support)
    single_line "TR: ... | EN: ..."   (fallback if a Zoom client strips the newline)
  If one language was withheld, only the other line is sent (never a stand-in).
* TRILINGUAL - Turkish, English and Japanese in the same POST ("TR: ... | EN: ... | JA: ..."),
  for the parts of the ceremony the Japanese interpreter does not cover (vows, MC).
* TR / EN / JA - one language only.

Long texts are split, never truncated. Both languages are split into the SAME number
of parts at the best phrase boundaries (sentence > clause > word), so part i of the
Turkish and part i of the English cover roughly the same stretch of speech. A Bible
reference is never separated from its book name, a negation never from its verb,
and a number never from what it counts. Japanese has no spaces: it is split only after
punctuation (。、！？) or after a particle that ends a phrase, and a CJK character counts
as two columns of width.
"""
from __future__ import annotations

import re

MODE_LANGS = {"TR": ("tr",), "EN": ("en",), "JA": ("ja",),
              "BILINGUAL": ("tr", "en"), "TRILINGUAL": ("tr", "en", "ja")}
MODES = tuple(MODE_LANGS)
LABELS = {"tr": "TR: ", "en": "EN: ", "ja": "JA: "}
LAYOUTS = ("two_line", "single_line")
SINGLE_LINE_SEP = " | "

_SENT_END = re.compile(r"[.!?…]['\"”’»)]*$")
_CLAUSE_END = re.compile(r"[,;:—–]$")
_REF = re.compile(r"^\d+[:.,]\d")                       # 5:22, 3.16 -> belongs to the previous word (book)
_NUM = re.compile(r"^\d+([.,]\d+)?[.,]?$")              # 7, 16:00, "1." (as in "1. Korintliler")
_NEG_NEXT = {"not", "never", "no", "değil", "yok", "hiç", "asla", "n't"}
_SOFT_NEXT = {"and", "or", "but", "ve", "veya", "ama", "fakat", "of", "the", "a", "an", "to", "ile", "için"}
_INF = float("inf")
# Japanese: split after sentence/clause punctuation, or after a phrase-final particle that is
# followed by the start of a new content word (kanji / katakana / Latin), e.g. "エホバは|私たちを|".
_JA_SPLIT = re.compile(r"(?<=[。！？!?、])|(?<=[はがをにでともへ])(?=[一-鿿゠-ヿA-Za-z])")


def width(s: str) -> int:
    """Display width: CJK / full-width characters count as two columns."""
    return sum(2 if ord(ch) >= 0x2E80 else 1 for ch in s)


def mode_max_chars(zcfg, mode: str) -> int:
    """Per-language line width for a caption mode (from the Zoom config)."""
    n = len(MODE_LANGS[mode])
    return zcfg.max_chars if n == 1 else zcfg.bilingual_max_chars if n == 2 else zcfg.trilingual_max_chars


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


def _ja_penalty(prev: str, nxt: str) -> float:
    if nxt[:1].isdigit() or prev[-1:].isdigit():
        return _INF  # a reference or number stays whole
    if prev[-1:] in "。！？!?":
        return 0.0
    if prev[-1:] == "、":
        return 120.0
    return 500.0


def _unit_penalty(prev: str, nxt: str, ja: bool) -> float:
    if ja:
        return _ja_penalty(prev, nxt)
    return _penalty(prev.rsplit(" ", 1)[-1], nxt.split(" ", 1)[0])


def _split_k(words: list[str], k: int, max_chars: int, ja: bool = False) -> list[str] | None:
    """Best split of ``words`` into exactly ``k`` parts of <= max_chars, or None."""
    n = len(words)
    if k > n:
        return None
    sep = "" if ja else " "
    lens = [width(w) for w in words]
    total = sum(lens) + len(sep) * (n - 1)
    target = total / k

    def seg_len(i, j):  # words[i:j]
        return sum(lens[i:j]) + len(sep) * (j - i - 1)

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
                brk = 0.0 if i == 0 else _unit_penalty(words[i - 1], words[i], ja)
                c = dp[p - 1][i] + (L - target) ** 2 + brk
                if c < dp[p][j]:
                    dp[p][j], back[p][j] = c, i
    if dp[k][n] == _INF:
        return None
    parts, j = [], n
    for p in range(k, 0, -1):
        i = back[p][j]
        parts.append(sep.join(words[i:j]))
        j = i
    return parts[::-1]


def _words(text: str, max_chars: int, ja: bool = False) -> list[str]:
    """Words (Japanese: phrase units), with unbreakable runs ("1. Korintliler 13:4-8", "3 gün")
    glued into one unit. A unit longer than ``max_chars`` stays whole (a slightly long line
    beats a split reference)."""
    text = " ".join((text or "").split())
    if ja:
        out: list[str] = []
        for u in _JA_SPLIT.split(text):
            if not u:
                continue
            if out and _ja_penalty(out[-1], u) == _INF:
                out[-1] += u
            else:
                out.append(u)
        return out
    out = []
    for w in text.split(" "):
        if not w:
            continue
        if out and _penalty(out[-1].rsplit(" ", 1)[-1], w) == _INF:
            out[-1] += " " + w
        else:
            out.append(w)
    return out


def min_parts(text: str, max_chars: int, lang: str = "tr") -> int:
    ja = lang == "ja"
    words = _words(text, max_chars, ja)
    if not words:
        return 0
    for k in range(1, len(words) + 1):
        if _split_k(words, k, max_chars, ja) is not None:
            return k
    return len(words)


def split_into(text: str, k: int, max_chars: int, lang: str = "tr") -> list[str]:
    """Split ``text`` into ``k`` balanced parts (fewer only if it has fewer units)."""
    ja = lang == "ja"
    words = _words(text, max_chars, ja)
    if not words:
        return []
    for kk in range(min(k, len(words)), len(words) + 1):
        parts = _split_k(words, kk, max_chars, ja)
        if parts is not None:
            return parts
    return words  # unreachable: one word per part always fits


def compose_parts(texts: dict[str, str], mode: str, max_chars: int = 84, labels: bool = True,
                  layout: str = "two_line") -> list[str]:
    if layout not in LAYOUTS:
        raise ValueError(f"unknown caption layout {layout!r}")
    if mode not in MODE_LANGS:
        raise ValueError(f"unknown output mode {mode!r}")
    langs = MODE_LANGS[mode]
    body = {t: (texts.get(t) or "").strip() for t in langs}
    if len(langs) == 1:
        t = langs[0]
        return split_into(body[t], min_parts(body[t], max_chars, t), max_chars, t) if body[t] else []
    k = max(min_parts(body[t], max_chars, t) for t in langs)
    if k == 0:
        return []
    split = {t: split_into(body[t], k, max_chars, t) if body[t] else [] for t in langs}
    sep = "\n" if layout == "two_line" else SINGLE_LINE_SEP
    out = []
    for i in range(max(len(v) for v in split.values())):
        lines = [(LABELS[t] if labels else "") + split[t][i] for t in langs if i < len(split[t])]
        out.append(sep.join(lines))
    return out


def reading_time_s(part: str, cps: float = 15.0, lo: float = 1.5, hi: float = 4.0) -> float:
    """How long a part should stay on screen before the NEXT part of the same event
    replaces it: guests read one language, so the longest line counts."""
    longest = max((width(x) for x in re.split(r"\n|\s\|\s", part)), default=0)
    return max(lo, min(hi, longest / cps))
