"""Language decision for one speech segment: MN / JA / UNCERTAIN / NONSPEECH / OVERLAP.

The evidence sources are NOT independent:
* Whisper language probabilities (E1) and forced-decode log-probabilities (E2)
  come from the same model;
* the writing system of a *forced* decode proves nothing (forcing mn always
  yields Cyrillic), so script is only checked on the unforced decode (E3).
A segment is MN/JA only when E1 and E2 agree with sufficient margin and E3
does not contradict; otherwise it is UNCERTAIN, which is never captioned
directly. Thresholds are tuned on held-out rehearsal audio (docs/BENCHMARK.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from subtalk.config import LangIdCfg
from subtalk.glossary.normalize import script_profile

# Common Whisper hallucinations on silence/music (multilingual).
HALLUCINATION_PATTERNS = [
    r"ご視聴ありがとうございました", r"チャンネル登録", r"字幕", r"おやすみなさい",
    r"subtitles? by", r"amara\.org", r"thank(s| you) for watching", r"please subscribe",
    r"продолжение следует", r"субтитры", r"редактор субтитров", r"сабтайтл",
    r"баярлалаа\.?$",
]
_HALLUCINATION_RE = re.compile("|".join(HALLUCINATION_PATTERNS), re.IGNORECASE)


@dataclass
class DecodeStats:
    text: str
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float


@dataclass
class LangEvidence:
    duration_s: float
    p_mn: float  # restricted to {mn, ja}, renormalised
    p_ja: float
    top_language: str | None = None  # Whisper's unrestricted top language
    unforced: DecodeStats | None = None
    forced: dict[str, DecodeStats] = field(default_factory=dict)


@dataclass
class Decision:
    kind: str  # MN | JA | UNCERTAIN | NONSPEECH | OVERLAP
    text: str = ""
    quality: str = "ok"  # ok | low
    reasons: list[str] = field(default_factory=list)
    margin: float = 0.0


def is_hallucination(text: str) -> bool:
    return bool(_HALLUCINATION_RE.search(text or ""))


def decide(ev: LangEvidence, cfg: LangIdCfg) -> Decision:
    reasons: list[str] = []
    decodes = [d for d in [ev.unforced, *ev.forced.values()] if d is not None]
    if not decodes:
        return Decision("NONSPEECH", reasons=["no_decode"])
    if ev.duration_s < cfg.min_duration_s:
        return Decision("NONSPEECH", reasons=["too_short"])
    if all(d.no_speech_prob >= cfg.nonspeech_prob for d in decodes):
        return Decision("NONSPEECH", reasons=["no_speech_prob"])
    if all(not d.text.strip() for d in decodes):
        return Decision("NONSPEECH", reasons=["empty_text"])
    if all(is_hallucination(d.text) for d in decodes if d.text.strip()):
        return Decision("NONSPEECH", reasons=["hallucination_pattern"])

    margin = ev.p_mn - ev.p_ja
    lang = "mn" if margin >= 0 else "ja"
    need = cfg.margin_short if ev.duration_s < cfg.short_duration_s else cfg.margin_ok
    if abs(margin) < need:
        reasons.append(f"lid_margin_low:{margin:+.2f}")

    # E2: forced decodes for both languages (only present when margin was low).
    if "mn" in ev.forced and "ja" in ev.forced:
        diff = ev.forced["mn"].avg_logprob - ev.forced["ja"].avg_logprob
        e2 = "mn" if diff >= 0 else "ja"
        if abs(diff) < cfg.logprob_conflict:
            reasons.append(f"logprob_close:{diff:+.2f}")
        elif e2 != lang:
            reasons.append("lid_logprob_disagree")
        elif abs(margin) < need:
            # E2 clearly agrees with a weak E1 - accept with a note.
            reasons = [r for r in reasons if not r.startswith("lid_margin_low")]
            reasons.append("accepted_by_forced_logprob")

    # E3: script of the unforced decode (if Whisper chose mn/ja itself).
    if ev.unforced and ev.unforced.text.strip():
        prof = script_profile(ev.unforced.text)
        if prof["cyrillic"] >= cfg.overlap_script_ratio and prof["japanese"] >= cfg.overlap_script_ratio:
            return Decision("OVERLAP", reasons=["mixed_scripts"], margin=margin)
        if lang == "mn" and prof["japanese"] > 0.5:
            reasons.append("script_contradicts_mn")
        if lang == "ja" and prof["cyrillic"] > 0.5:
            reasons.append("script_contradicts_ja")

    chosen = ev.forced.get(lang)
    if chosen is None and ev.unforced is not None and ev.top_language == lang:
        chosen = ev.unforced
    if chosen is None or not chosen.text.strip():
        return Decision("UNCERTAIN", reasons=reasons + ["no_transcript_for_language"], margin=margin)
    if chosen.compression_ratio > cfg.compression_max:
        return Decision("NONSPEECH", reasons=reasons + ["compression_ratio"], margin=margin)
    if is_hallucination(chosen.text):
        return Decision("NONSPEECH", reasons=reasons + ["hallucination_pattern"], margin=margin)

    blocking = [r for r in reasons if r.startswith(("lid_margin_low", "logprob_close", "lid_logprob_disagree", "script_contradicts"))]
    if blocking:
        return Decision("UNCERTAIN", text=chosen.text.strip(), reasons=reasons, margin=margin)
    quality = "ok" if chosen.avg_logprob >= cfg.min_avg_logprob_ok else "low"
    return Decision(lang.upper(), text=chosen.text.strip(), quality=quality, reasons=reasons, margin=margin)
