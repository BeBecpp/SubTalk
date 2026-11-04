"""Failure-injection + soak harness (mock Claude, mock Zoom, no paid calls, no audio).

Runs the REAL pipeline, guards, composer, output controller and Zoom sender against
the strict mock Zoom endpoint over HTTP, for a given time, while injecting faults on a
seeded random schedule:

  Claude   timeout, rate limit (429), invalid JSON, one language missing, slow answers
  ASR      wrong-language / uncertain segments, garbled Mongolian, gaps (crash/restart),
           late segments (speech that ended long ago)
  Zoom     5xx / 429 / 403, timeouts, lost responses (accepted, answer never arrives),
           same-seq retry answered with 403 (real Zoom behaviour, 4 Nov)
  Operator pause/resume, stop/go live, E-STOP during in-flight requests + reset,
           reconnect (new URL), output mode and layout switches

Safety invariants are checked continuously; any violation fails the run:

  * Zoom seq strictly +1, never skipped or reused for different text (mock violations == [])
  * nothing reaches Zoom while output is not LIVE (allowing one request already in flight)
  * nothing reaches Zoom after E-STOP (beyond the request in flight at that moment)
  * no automatic caption older than TTL + its part pacing when it reaches Zoom
  * output queue never exceeds its bound; output never becomes LIVE by itself

Metrics: queue depth, sends, retries, drops by reason, caption age, memory (RSS), CPU.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI

from subtalk.asr.langid import Decision
from subtalk.config import Config
from subtalk.glossary.guards import Guards
from subtalk.glossary.service import GlossaryService
from subtalk.glossary.store import load_glossary
from subtalk.output.compose import reading_time_s
from subtalk.output.controller import OutputConfig, OutputController
from subtalk.pipeline.core import ContextState, Pipeline
from subtalk.state import OutputState, Target
from subtalk.translate.base import TranslationResult, TranslatorError, TranslatorTimeout
from subtalk.translate.mock import MockTranslator
from subtalk.util.stats import Rolling
from subtalk.zoom.mock import MockZoomCaptionService
from subtalk.zoom.sender import ZoomCaptionSender
from subtalk.zoom.seqstore import SeqStore

ROOT = Path(__file__).resolve().parents[2]


class _TimeoutTransport(httpx.AsyncBaseTransport):
    """ASGITransport ignores timeouts; real clients do not."""

    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        t = request.extensions.get("timeout", {}).get("read")
        if t is None:
            return await self.inner.handle_async_request(request)
        try:
            return await asyncio.wait_for(self.inner.handle_async_request(request), t)
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout("mock timeout", request=request) from None


class FaultyTranslator(MockTranslator):
    """Mock translator with injected Claude failures."""

    def __init__(self, translations, rng: random.Random, rates: dict[str, float]):
        super().__init__(translations, latency_s=(0.3, 1.6), seed=rng.randint(0, 10**6))
        self.frng = rng
        self.rates = rates
        self.injected: dict[str, int] = {}

    def _pick(self) -> str | None:
        r = self.frng.random()
        acc = 0.0
        for k, p in self.rates.items():
            acc += p
            if r < acc:
                self.injected[k] = self.injected.get(k, 0) + 1
                return k
        return None

    async def _faulted(self, call, req):
        f = self._pick()
        if f == "timeout":
            await asyncio.sleep(1.0)
            raise TranslatorTimeout("injected timeout")
        if f == "rate_limit":
            raise TranslatorError("injected 429 rate limit")
        if f == "invalid_json":
            raise TranslatorError("injected invalid JSON")
        if f == "slow":
            await asyncio.sleep(self.frng.uniform(3.0, 6.0))
        res: TranslationResult = await call(req)
        if f == "missing_en" and res.display:
            res.english, res.display_en, res.error_en = "", None, "missing_language"
        return res

    async def translate(self, req):
        return await self._faulted(super().translate, req)

    async def interpret(self, req):
        return await self._faulted(super().interpret, req)


@dataclass
class SoakReport:
    seconds: float = 0.0
    seed: int = 0
    segments: int = 0
    faults: dict = field(default_factory=dict)
    zoom_sent: int = 0
    zoom_retries: int = 0
    zoom_abandoned: int = 0
    zoom_confirmed_by_seq: int = 0
    output_counts: dict = field(default_factory=dict)
    pipeline_counts: dict = field(default_factory=dict)
    queue_max: int = 0
    pending_max: int = 0
    caption_age_ms: dict = field(default_factory=dict)
    rss_mb_start: float | None = None
    rss_mb_end: float | None = None
    rss_mb_max: float | None = None
    cpu_percent_avg: float | None = None
    violations: list = field(default_factory=list)
    alerts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations


def _translations() -> dict:
    doc = yaml.safe_load((ROOT / "resources/mock/session.yaml").read_text(encoding="utf-8")) or {}
    out = {}
    for s in doc.get("segments", []):
        if s.get("text"):
            out[s["text"]] = {k: s[k] for k in ("tr", "en", "fail_en", "relation", "quality") if k in s}
    return out, [s for s in doc.get("segments", []) if s.get("lang") in ("mn", "ja")]


async def run_soak(seconds: float = 60.0, seed: int = 1, tmp: Path | None = None,
                   fault_scale: float = 1.0) -> SoakReport:
    rng = random.Random(seed)
    rep = SoakReport(seed=seed)
    tmp = Path(tmp or (ROOT / "state" / "soak"))
    tmp.mkdir(parents=True, exist_ok=True)

    cfg = Config()
    cfg.interp.wait_pending_s = 1.0
    glossary = load_glossary(ROOT / "resources/glossary", ROOT / "resources/bible",
                             en_path=ROOT / "resources/glossary_en/en_glossary.json")
    zoom = MockZoomCaptionService(stall_seconds=4.0)
    app = FastAPI()
    app.include_router(zoom.router())
    http = httpx.AsyncClient(transport=_TimeoutTransport(httpx.ASGITransport(app=app)), base_url="http://127.0.0.1")
    sender = ZoomCaptionSender(http, SeqStore(tmp / "seq.json"), attempt_timeout=cfg.zoom.attempt_timeout_s,
                               retry_budget=cfg.zoom.retry_budget_s, backoff_base=cfg.zoom.backoff_base_s)
    alerts: dict[str, int] = {}
    z = cfg.zoom
    out = OutputController(sender, OutputConfig(
        caption_ttl_s=z.caption_ttl_s, max_chars=z.max_chars, min_interval_s=z.min_interval_s,
        queue_max=z.queue_max, auto_pause_after_failures=z.auto_pause_after_failures, part_read_cps=z.part_read_cps,
    ), on_alert=lambda lvl, code, msg: alerts.__setitem__(code, alerts.get(code, 0) + 1))
    translations, segs = _translations()
    s = fault_scale
    tr = FaultyTranslator(translations, rng, {"timeout": 0.04 * s, "rate_limit": 0.04 * s, "invalid_json": 0.03 * s,
                                             "missing_en": 0.06 * s, "slow": 0.05 * s})
    ctx = ContextState(output_mode="BILINGUAL", caption_layout="single_line", main_speech=True)
    pipe = Pipeline(cfg, GlossaryService(glossary), Guards(glossary), tr, out, ctx,
                    on_alert=lambda lvl, code, msg: alerts.__setitem__(code, alerts.get(code, 0) + 1))
    run_task = asyncio.create_task(out.run())

    url = zoom.mock_url("http://127.0.0.1", meeting_id="soak1")
    await out.connect(url, Target.TEST)
    out.start_live()

    # ---- observation: wrap the sender to see every POST attempt with the state at that moment
    estop_marks: list[float] = []
    notlive_since: list[float] = []
    real_send = sender.send
    age = Rolling(10_000)

    async def watched_send(text, deadline=None):
        t0 = time.monotonic()
        state = out.state
        if state is not OutputState.LIVE and not (estop_marks and t0 - estop_marks[-1] < 0.05):
            # the gate allows TEST / manual captions outside LIVE; this harness sends none
            rep.violations.append(f"POST started while output {state.value}")
        return await real_send(text, deadline=deadline)

    sender.send = watched_send

    proc = None
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        proc.cpu_percent(None)
        rep.rss_mb_start = proc.memory_info().rss / 2**20
    except Exception:  # noqa: BLE001 - metrics are optional
        proc = None

    cpu, rss = [], []
    end = time.monotonic() + seconds
    meeting_no = 1
    next_op = time.monotonic() + rng.uniform(4, 8)

    async def feeder():
        while time.monotonic() < end:
            seg = rng.choice(segs)
            r = rng.random()
            now = time.monotonic()
            if r < 0.06 * s:
                d = Decision("UNCERTAIN", text=seg["text"], quality="low")  # wrong / unsure language
            elif r < 0.10 * s:
                d = Decision("MN", text="ммм ... бүрхэг", quality="low")  # garbled
            elif r < 0.13 * s:
                await asyncio.sleep(rng.uniform(2, 5))  # ASR crash / restart gap
                continue
            else:
                d = Decision("MN" if seg["lang"] == "mn" else "JA", text=seg["text"])
            t_end = now - (rng.uniform(15, 30) if rng.random() < 0.03 * s else rng.uniform(0.3, 1.2))  # late segment
            pipe.submit_decision(d, t_end - seg.get("dur", 3), t_end, asr_ms=rng.uniform(300, 1200))
            rep.segments += 1
            await asyncio.sleep(rng.uniform(0.8, 2.5))

    feed = asyncio.create_task(feeder())
    try:
        while time.monotonic() < end:
            await asyncio.sleep(0.2)
            pipe.tick()
            # Zoom faults
            if rng.random() < 0.05 * s and len(zoom.script) < 3:
                zoom.script.append(rng.choice([500, 503, 429, 403, "timeout", "accept_then_timeout"]))
                rep.faults["zoom"] = rep.faults.get("zoom", 0) + 1
            # operator actions
            if time.monotonic() >= next_op:
                next_op = time.monotonic() + rng.uniform(5, 12)
                op = rng.choice(["pause", "stop", "estop", "reconnect", "mode", "layout", "none", "none"])
                rep.faults[f"op_{op}"] = rep.faults.get(f"op_{op}", 0) + 1
                st = out.state
                if op == "pause" and st is OutputState.LIVE:
                    out.pause()
                    await asyncio.sleep(rng.uniform(1, 3))
                    out.resume()
                elif op == "stop" and st in (OutputState.LIVE, OutputState.PAUSED):
                    out.stop()
                    await asyncio.sleep(rng.uniform(1, 3))
                    if out.state is not OutputState.CONNECTED:
                        rep.violations.append(f"after stop: {out.state.value}")
                    out.start_live()
                elif op == "estop":
                    estop_marks.append(time.monotonic())
                    before = len(zoom.meeting(f"soak{meeting_no}").captions)
                    out.estop()
                    await asyncio.sleep(max(3.5, cfg.zoom.attempt_timeout_s + 0.5))
                    after = len(zoom.meeting(f"soak{meeting_no}").captions)
                    if after - before > 1:  # at most the request already in flight
                        rep.violations.append(f"{after - before} captions reached Zoom after E-STOP")
                    if out.state is not OutputState.E_STOPPED:
                        rep.violations.append(f"E-STOP not latched: {out.state.value}")
                    out.reset_estop()
                    if out.state is OutputState.LIVE:
                        rep.violations.append("reset_estop resumed LIVE by itself")
                    out.start_live()
                elif op == "reconnect" and st in (OutputState.LIVE, OutputState.PAUSED):
                    out.pause()
                    meeting_no += 1
                    await out.connect(zoom.mock_url("http://127.0.0.1", meeting_id=f"soak{meeting_no}"), Target.TEST)
                    if out.state is OutputState.LIVE:
                        rep.violations.append("reconnect made output LIVE by itself")
                    out.resume()
                elif op == "mode":
                    ctx.output_mode = rng.choice(["BILINGUAL", "BILINGUAL", "TR", "EN"])
                elif op == "layout":
                    ctx.caption_layout = rng.choice(["single_line", "two_line"])
            # metrics + invariants
            rep.queue_max = max(rep.queue_max, out.queue_len)
            rep.pending_max = max(rep.pending_max, pipe.emitter.pending)
            if out.queue_len > out.cfg.queue_max:
                rep.violations.append(f"queue {out.queue_len} > bound {out.cfg.queue_max}")
            if proc:
                cpu.append(proc.cpu_percent(None))
                rss.append(proc.memory_info().rss / 2**20)
    finally:
        feed.cancel()
        out.estop()
        await asyncio.sleep(0.2)
        run_task.cancel()
        await http.aclose()

    for rec in out.history:
        if rec.status == "sent" and rec.e2e_ms is not None:
            age.add(rec.e2e_ms)
    for n in range(1, meeting_no + 1):
        m = zoom.meeting(f"soak{n}")
        if m.violations:
            rep.violations.append(f"meeting soak{n}: seq violations {m.violations[:3]}")
        seqs = [c.seq for c in m.captions]
        if seqs != list(range(seqs[0], seqs[0] + len(seqs))) if seqs else False:
            rep.violations.append(f"meeting soak{n}: non-contiguous seq {seqs[:10]}")
    # caption age bound: TTL + worst-case part pacing (4 parts x 4 s); attempts never outlive the deadline
    bound = (cfg.zoom.caption_ttl_s + 16 + 0.5) * 1000
    worst = max((r.e2e_ms or 0) for r in out.history) if out.history else 0
    if worst > bound:
        rep.violations.append(f"caption reached Zoom {worst:.0f} ms after speech end (bound {bound:.0f})")
    st = sender.status()
    rep.seconds = seconds
    rep.zoom_sent = st.sent_total
    rep.zoom_abandoned = st.abandoned_total
    rep.zoom_confirmed_by_seq = st.confirmed_by_seq_total
    rep.zoom_retries = sum(m.duplicate_retries for m in zoom.meetings.values())
    rep.output_counts = dict(out.counts)
    rep.pipeline_counts = dict(pipe.stats.counts)
    rep.caption_age_ms = {"n": age.summary()["n"], "p50": age.pct(50), "p95": age.pct(95), "max": worst or None}
    rep.faults.update({f"claude_{k}": v for k, v in tr.injected.items()})
    rep.alerts = alerts
    if proc and rss:
        rep.rss_mb_end, rep.rss_mb_max = rss[-1], max(rss)
        rep.cpu_percent_avg = round(sum(cpu) / len(cpu), 1)
    return rep


def write_report(rep: SoakReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"soak_{time.strftime('%Y%m%d-%H%M%S')}_{int(rep.seconds)}s_seed{rep.seed}.json"
    d = {k: v for k, v in rep.__dict__.items()}
    d["ok"] = rep.ok
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return p
