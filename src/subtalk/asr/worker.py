"""ASR worker process.

Whisper runs in a child process so a CUDA crash or out-of-memory error can
never take down the control plane (output gate, E-STOP, dashboard). The
parent restarts a dead worker, and after repeated failures switches to the
configured fallback model.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import threading
import time
import traceback
from dataclasses import asdict

import numpy as np

from subtalk.asr.langid import DecodeStats, LangEvidence
from subtalk.config import AsrCfg, LangIdCfg
from subtalk.util.stats import Rolling

log = logging.getLogger("subtalk.asr")


class AsrUnavailable(Exception):
    pass


def _evidence_to_dict(ev: LangEvidence) -> dict:
    return asdict(ev)


def _evidence_from_dict(d: dict) -> LangEvidence:
    def ds(x):
        return DecodeStats(**x) if x else None
    return LangEvidence(d["duration_s"], d["p_mn"], d["p_ja"], d.get("top_language"), ds(d.get("unforced")),
                        {k: ds(v) for k, v in (d.get("forced") or {}).items()})


def build_engine(kind: str, asr: dict, langid: dict, model_name: str | None, script: list | None):
    if kind == "whisper":
        from subtalk.asr.engines import WhisperAsrEngine
        return WhisperAsrEngine(AsrCfg(**asr), LangIdCfg(**langid), model_name)
    if kind == "crash_test":
        return _CrashEngine()
    from subtalk.asr.engines import ScriptedAsrEngine
    return ScriptedAsrEngine(script or [{"lang": "uncertain", "text": "..."}])


class _CrashEngine:
    model_name, load_s = "crash_test", 0.0

    def transcribe(self, audio):
        if len(audio) == 1600:  # 0.1 s of audio = crash trigger in tests
            import os
            os._exit(3)
        return LangEvidence(len(audio) / 16000, 0.9, 0.1, "mn", DecodeStats("тест", -0.2, 0.0, 1.0))


def worker_main(conn, kind: str, asr: dict, langid: dict, model_name: str | None, script: list | None) -> None:
    try:
        engine = build_engine(kind, asr, langid, model_name, script)
        conn.send(("ready", {"model": engine.model_name, "load_s": engine.load_s}))
    except BaseException as exc:  # noqa: BLE001 - report everything to the parent
        conn.send(("fatal", f"{type(exc).__name__}: {exc}"))
        return
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg[0] == "stop":
            return
        _, job_id, audio = msg
        t = time.monotonic()
        try:
            ev = engine.transcribe(np.asarray(audio, dtype=np.float32))
            conn.send(("result", job_id, _evidence_to_dict(ev), (time.monotonic() - t) * 1000))
        except BaseException as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}"
            conn.send(("error", job_id, text, traceback.format_exc(limit=3)))
            if "out of memory" in text.lower() or "CUDA" in text:
                return  # exit so the parent restarts with a clean GPU context


class AsrWorkerClient:
    def __init__(self, kind: str, asr: AsrCfg, langid: LangIdCfg, script: list | None = None,
                 job_timeout_s: float = 20.0):
        self.kind = kind
        self.asr_cfg, self.langid_cfg, self.script = asr, langid, script
        self.model_name = asr.model
        self.job_timeout_s = job_timeout_s
        self.state = "stopped"  # stopped | starting | ready | dead | fatal
        self.restarts = 0
        self.restart_times: list[float] = []
        self.last_error: str | None = None
        self.latency = Rolling()
        self.loaded_model: str | None = None
        self._proc = None
        self._conn = None
        self._reader = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._futures: dict[int, asyncio.Future] = {}
        self._ready: asyncio.Event | None = None
        self._next = 0
        self._stopping = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._spawn()
        await asyncio.wait_for(self._ready.wait(), timeout=600)  # first model load can be slow

    def _spawn(self) -> None:
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        self._conn = parent
        self._ready = asyncio.Event()
        self.state = "starting"
        self._proc = ctx.Process(
            target=worker_main, name="subtalk-asr", daemon=True,
            args=(child, self.kind, self.asr_cfg.model_dump(), self.langid_cfg.model_dump(), self.model_name, self.script),
        )
        self._proc.start()
        child.close()
        self._reader = threading.Thread(target=self._read_loop, args=(parent,), daemon=True)
        self._reader.start()

    def _read_loop(self, conn) -> None:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                self._loop.call_soon_threadsafe(self._on_dead)
                return
            self._loop.call_soon_threadsafe(self._on_msg, msg)

    def _on_msg(self, msg) -> None:
        kind = msg[0]
        if kind == "ready":
            self.state = "ready"
            self.loaded_model = msg[1]["model"]
            self._ready.set()
        elif kind == "fatal":
            self.state = "fatal"
            self.last_error = msg[1]
            self._ready.set()
        elif kind == "result":
            fut = self._futures.pop(msg[1], None)
            self.latency.add(msg[3])
            if fut and not fut.done():
                fut.set_result((_evidence_from_dict(msg[2]), msg[3]))
        elif kind == "error":
            self.last_error = msg[2]
            fut = self._futures.pop(msg[1], None)
            if fut and not fut.done():
                fut.set_exception(AsrUnavailable(msg[2]))

    def _on_dead(self) -> None:
        if self._stopping:
            self.state = "stopped"
            return
        self.state = "dead"
        for fut in self._futures.values():
            if not fut.done():
                fut.set_exception(AsrUnavailable("ASR worker died"))
        self._futures.clear()
        now = time.monotonic()
        self.restart_times = [t for t in self.restart_times if now - t < 60] + [now]
        if len(self.restart_times) >= 2 and self.asr_cfg.fallback_model and self.model_name != self.asr_cfg.fallback_model:
            log.error("asr.fallback_model", extra={"model": self.asr_cfg.fallback_model})
            self.model_name = self.asr_cfg.fallback_model
        self.restarts += 1
        self._spawn()

    async def transcribe(self, audio: np.ndarray) -> tuple[LangEvidence, float]:
        if self.state != "ready":
            raise AsrUnavailable(f"ASR worker {self.state}")
        self._next += 1
        job = self._next
        fut = self._loop.create_future()
        self._futures[job] = fut
        try:
            self._conn.send(("job", job, audio.astype(np.float32)))
        except (OSError, BrokenPipeError) as exc:
            self._futures.pop(job, None)
            raise AsrUnavailable(str(exc)) from None
        try:
            return await asyncio.wait_for(fut, self.job_timeout_s)
        finally:
            self._futures.pop(job, None)

    async def stop(self) -> None:
        self._stopping = True
        try:
            self._conn.send(("stop",))
        except Exception:  # noqa: BLE001
            pass
        if self._proc:
            await asyncio.to_thread(self._proc.join, 5)
            if self._proc.is_alive():
                self._proc.kill()
        self.state = "stopped"

    def status(self) -> dict:
        return {
            "engine": self.kind, "state": self.state, "model": self.loaded_model or self.model_name,
            "restarts": self.restarts, "last_error": self.last_error, "latency_ms": self.latency.summary(),
            "pending": len(self._futures),
        }
