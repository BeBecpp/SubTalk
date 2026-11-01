"""ASR engines producing LangEvidence for one segment.

* WhisperAsrEngine - faster-whisper (CTranslate2) on the production GPU.
* ScriptedAsrEngine - development stand-in: returns scripted lines in order,
  so real audio + VAD timing can be exercised without inference.
"""
from __future__ import annotations

import itertools
import time
import zlib

import numpy as np

from subtalk.asr.langid import DecodeStats, LangEvidence
from subtalk.config import AsrCfg, LangIdCfg


def _compression_ratio(text: str) -> float:
    b = text.encode("utf-8")
    return len(b) / max(1, len(zlib.compress(b))) if b else 0.0


class WhisperAsrEngine:
    """Decode strategy (docs/ARCHITECTURE.md, "Language decision"):

    1. unforced decode -> Whisper's language probabilities + text;
    2. restrict probabilities to {mn, ja}; if the unforced language is not the
       restricted winner (Mongolian is often detected as ru/kk), decode again
       forced to the winner;
    3. if the mn/ja margin is small, also decode forced to the other language
       so the decision can compare log-probabilities.
    """

    def __init__(self, cfg: AsrCfg, langid: LangIdCfg, model_name: str | None = None):
        from faster_whisper import WhisperModel  # production dependency (extra "asr")

        self.cfg, self.langid = cfg, langid
        self.model_name = model_name or cfg.model
        t = time.monotonic()
        self.model = WhisperModel(self.model_name, device=cfg.device, compute_type=cfg.compute_type,
                                  download_root=cfg.model_dir)
        self.load_s = time.monotonic() - t

    def _decode(self, audio: np.ndarray, language: str | None) -> tuple[DecodeStats, object]:
        prompt = {"mn": self.cfg.initial_prompt_mn, "ja": self.cfg.initial_prompt_ja}.get(language or "", "") or None
        segments, info = self.model.transcribe(
            audio, language=language, task="transcribe", beam_size=self.cfg.beam_size,
            temperature=0.0, condition_on_previous_text=False, vad_filter=False,
            without_timestamps=True, initial_prompt=prompt,
        )
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        if segs:
            total = sum(max(1e-3, s.end - s.start) for s in segs)
            lp = sum(s.avg_logprob * max(1e-3, s.end - s.start) for s in segs) / total
            ns = max(s.no_speech_prob for s in segs)
            cr = max(s.compression_ratio for s in segs)
        else:
            lp, ns, cr = -10.0, 1.0, 0.0
        return DecodeStats(text=text, avg_logprob=lp, no_speech_prob=ns, compression_ratio=cr), info

    def transcribe(self, audio: np.ndarray) -> LangEvidence:
        unforced, info = self._decode(audio, None)
        probs = dict(info.all_language_probs or [])
        p_mn, p_ja = probs.get("mn", 0.0), probs.get("ja", 0.0)
        s = p_mn + p_ja
        p_mn, p_ja = (p_mn / s, p_ja / s) if s > 0 else (0.5, 0.5)
        top = info.language
        winner = "mn" if p_mn >= p_ja else "ja"
        forced: dict[str, DecodeStats] = {}
        forced[winner] = unforced if top == winner else self._decode(audio, winner)[0]
        if abs(p_mn - p_ja) < self.langid.second_pass_margin:
            other = "ja" if winner == "mn" else "mn"
            forced[other] = self._decode(audio, other)[0]
        return LangEvidence(duration_s=len(audio) / 16000, p_mn=p_mn, p_ja=p_ja, top_language=top,
                            unforced=unforced, forced=forced)


class ScriptedAsrEngine:
    """Returns scripted (lang, text) items in order - for dev audio tests only."""

    def __init__(self, items: list[dict], loop: bool = True):
        self._items = itertools.cycle(items) if loop else iter(items)
        self.model_name = "scripted"
        self.load_s = 0.0

    def transcribe(self, audio: np.ndarray) -> LangEvidence:
        dur = len(audio) / 16000
        item = next(self._items, None)
        if item is None or item.get("lang") in (None, "music", "none"):
            return LangEvidence(dur, 0.5, 0.5, None, DecodeStats("", -5.0, 0.95, 0.0))
        text = item.get("text", "")
        lang = item["lang"]
        if lang == "uncertain":
            return LangEvidence(dur, 0.52, 0.48, "mn", DecodeStats(text, -0.9, 0.2, _compression_ratio(text)))
        lp = -1.2 if item.get("quality") in ("low", "garbled") else -0.3
        stats = DecodeStats(text, lp, 0.05, _compression_ratio(text))
        p_mn = 0.95 if lang == "mn" else 0.05
        return LangEvidence(dur, p_mn, 1 - p_mn, lang, stats, {lang: stats})
