"""ASR benchmark for M0-B (runs on the production GPU).

For every 16-bit WAV in a folder and every model: VAD-segment the audio the
same way the live engine does, transcribe each segment, apply the language
decision, and write:

* bench_out/<model>/segments.jsonl - per segment: times, decision, text, latency
* bench_out/summary.csv            - per model: latency p50/p95, decisions, CER
* bench_out/fidelity_rating.csv    - blank sheet for the blind Turkish review

CER is computed only when a reference transcript ``<name>.mn.txt`` and/or
``<name>.ja.txt`` sits next to the WAV (hand-written, plain text).
"""
from __future__ import annotations

import csv
import json
import time
import wave
from pathlib import Path

import numpy as np

from subtalk.asr.langid import decide
from subtalk.audio.segmenter import Segmenter
from subtalk.audio.vad import make_detector
from subtalk.util.stats import Rolling


def cer(ref: str, hyp: str) -> float:
    ref = "".join(ref.split())
    hyp = "".join(hyp.split())
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, hc in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1] / len(ref)


def load_wav(path: Path) -> np.ndarray:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sender"))
    from subtalk_sender import Resampler  # type: ignore

    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: only 16-bit PCM WAV supported")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return Resampler(rate).process(x)


def segment(cfg, audio: np.ndarray):
    segs = []
    seg = Segmenter(make_detector(cfg.vad.engine, cfg.vad.silero_model_path, cfg.vad.energy_margin_db), cfg.vad, segs.append)
    for i in range(0, len(audio), 320):
        seg.feed(audio[i:i + 320], (i + 320) / 16000)
    seg.flush()
    return segs


def run_benchmark(cfg, folder: Path, models: list[str], out: Path) -> int:
    from subtalk.asr.engines import WhisperAsrEngine

    wavs = sorted(folder.glob("*.wav"))
    if not wavs:
        print(f"No .wav files in {folder}")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    audio = {w: load_wav(w) for w in wavs}
    segments = {w: segment(cfg, a) for w, a in audio.items()}
    print(f"{len(wavs)} files, {sum(len(s) for s in segments.values())} segments")
    summary = []
    rating_rows = []
    for model in models:
        engine = WhisperAsrEngine(cfg.asr, cfg.langid, model_name=model)
        lat = Rolling(10000)
        kinds: dict[str, int] = {}
        hyp = {"mn": {}, "ja": {}}
        mdir = out / model.replace("/", "_")
        mdir.mkdir(exist_ok=True)
        with open(mdir / "segments.jsonl", "w", encoding="utf-8") as f:
            for w, segs in segments.items():
                for k, s in enumerate(segs):
                    t = time.monotonic()
                    ev = engine.transcribe(s.audio)
                    ms = (time.monotonic() - t) * 1000
                    lat.add(ms)
                    d = decide(ev, cfg.langid)
                    kinds[d.kind] = kinds.get(d.kind, 0) + 1
                    if d.kind in ("MN", "JA"):
                        hyp[d.kind.lower()].setdefault(w.name, []).append(d.text)
                    row = {"file": w.name, "seg": k, "start": round(s.t_start, 2), "end": round(s.t_end, 2),
                           "dur": round(s.duration, 2), "kind": d.kind, "quality": d.quality, "text": d.text,
                           "p_mn": round(ev.p_mn, 3), "top": ev.top_language, "reasons": d.reasons, "asr_ms": round(ms)}
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    if d.kind in ("MN", "JA"):
                        rating_rows.append({"model": model, "file": w.name, "seg": k, "source": d.text,
                                            "turkish": "", "rating": "", "notes": ""})
        cers = {}
        for lang in ("mn", "ja"):
            vals = []
            for w in wavs:
                ref = w.with_suffix(f".{lang}.txt")
                if ref.exists():
                    vals.append(cer(ref.read_text(encoding="utf-8"), " ".join(hyp[lang].get(w.name, []))))
            cers[lang] = round(sum(vals) / len(vals), 4) if vals else None
        s = lat.summary()
        summary.append({"model": model, "load_s": round(engine.load_s, 1), "p50_ms": round(s["p50"] or 0),
                        "p95_ms": round(s["p95"] or 0), "cer_mn": cers["mn"], "cer_ja": cers["ja"], **kinds})
        print(summary[-1])
        del engine
    keys = sorted({k for r in summary for k in r})
    with open(out / "summary.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=keys)
        wr.writeheader()
        wr.writerows(summary)
    with open(out / "fidelity_rating.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=["model", "file", "seg", "source", "turkish", "rating", "notes"])
        wr.writeheader()
        wr.writerows(rating_rows)
    print(f"Results in {out}")
    return 0
