"""Per-stage latency of the caption path, with the production targets.

Stages (milliseconds, per speech event / caption):

  vad_wait    speech end -> segment closed by the VAD (end-of-speech silence wait)
  asr_queue   segment closed -> Whisper starts
  asr         Whisper transcription
  claude      Claude request (TR + EN in one request)
  guards      deterministic TR + EN guards
  order_wait  translation ready -> released by the in-order emitter
  zoom_queue  queued for Zoom -> POST starts (rate limit, earlier captions)
  zoom_post   POST -> Zoom answered 2xx
  e2e         speech end -> Zoom accepted (first part of the caption)

Not measurable in software: Zoom accepted -> visible in a guest's Zoom client. That is
measured by eye / screen recording in the TEST meeting and added to the report.
"""
from __future__ import annotations

from subtalk.util.stats import Rolling

STAGES = ("vad_wait", "asr_queue", "asr", "claude", "guards", "order_wait", "zoom_queue", "zoom_post", "e2e")
TARGET_P50_MS = 4000.0  # speech end -> bilingual caption visible in Zoom (owner, 3 Nov)
TARGET_P95_MS = 7000.0


class LatencyTracker:
    def __init__(self, window: int = 300):
        self.stages = {s: Rolling(window) for s in STAGES}

    def add(self, stage: str, ms: float | None) -> None:
        if ms is not None and ms >= 0:
            self.stages[stage].add(ms)

    def snapshot(self) -> dict:
        out = {s: r.summary() for s, r in self.stages.items()}
        e2e = out["e2e"]
        verdict = "no data"
        if e2e["n"]:
            ok = e2e["p50"] <= TARGET_P50_MS and e2e["p95"] <= TARGET_P95_MS
            verdict = "within target" if ok else "TARGET MISSED"
        return {"stages": out, "target_p50_ms": TARGET_P50_MS, "target_p95_ms": TARGET_P95_MS,
                "verdict": verdict, "note": "e2e = speech end -> Zoom accepted; add Zoom display delay"}
