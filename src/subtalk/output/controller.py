"""Caption output state machine, safety gate and delivery loop.

Safety rules enforced here (independently of the UI):

* Nothing is posted unless the output state is LIVE (operator phrases: LIVE
  or PAUSED). PAUSED drops new automatic captions; it never queues them.
* Every caption carries the output *epoch* at submission. Pause, stop,
  E-STOP, connect, disconnect and reset bump the epoch, so work produced
  before the change is rejected at the gate.
* Captions for speech that ended before output (re)became LIVE are rejected.
* Captions past their deadline (speech end + TTL) are dropped, never sent.
* E-STOP is synchronous: it latches E_STOPPED, clears the queue and cancels
  the request in flight. A caption Zoom already accepted cannot be undone.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from subtalk.output.chunker import chunk_caption, two_line
from subtalk.state import OutputState, Target
from subtalk.util.stats import Rolling
from subtalk.zoom.sender import ZoomCaptionSender
from subtalk.zoom.url import CaptionUrlError, parse_caption_url

log = logging.getLogger("subtalk.output")

_ids = itertools.count(1)


class CommandError(Exception):
    """A command was rejected; the message is shown to the operator."""


@dataclass
class OutputConfig:
    caption_ttl_s: float = 12.0
    manual_ttl_s: float = 30.0
    max_chars: int = 84
    line_breaks: bool = False
    line_chars: int = 42
    min_interval_s: float = 1.0
    queue_max: int = 8
    auto_pause_after_failures: int = 3
    allow_real_zoom: bool = False
    allow_mock: bool = True
    allow_event_target: bool = False


@dataclass
class Caption:
    text: str
    speech_end: float  # monotonic time the source speech ended
    kind: str = "auto"  # auto | manual
    source_event_ids: tuple = ()
    id: int = field(default_factory=lambda: next(_ids))
    epoch: int = -1
    deadline: float = 0.0
    part: int = 1
    parts: int = 1


@dataclass
class CaptionRecord:
    id: int
    text: str
    kind: str
    status: str
    at: float
    seq: int | None = None
    latency_ms: float | None = None
    e2e_ms: float | None = None
    error: str | None = None
    source_event_ids: tuple = ()


class OutputController:
    def __init__(
        self,
        sender: ZoomCaptionSender,
        config: OutputConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[], None] | None = None,
        on_alert: Callable[[str, str, str], None] | None = None,
    ):
        self.sender = sender
        self.cfg = config
        self._clock = clock
        self._on_change = on_change or (lambda: None)
        self._on_alert = on_alert or (lambda level, code, msg: None)
        self.state = OutputState.DISCONNECTED
        self.target: Target | None = None
        self.epoch = 0
        self.live_since: float | None = None
        self.state_reason: str | None = None
        self.last_reject: str | None = None
        self._queue: deque[Caption] = deque()
        self._wakeup = asyncio.Event()
        self._inflight: asyncio.Task | None = None
        self._last_send_end: float = 0.0
        self._consecutive_failures = 0
        self.history: deque[CaptionRecord] = deque(maxlen=60)
        self.on_air: CaptionRecord | None = None
        self.e2e = Rolling()
        self.zoom_latency = Rolling()
        self.counts: dict[str, int] = {}

    # ----------------------------------------------------------- helpers
    def _set_state(self, state: OutputState, reason: str | None = None) -> None:
        self.state = state
        self.state_reason = reason
        self.epoch += 1
        self.live_since = self._clock() if state is OutputState.LIVE else None
        log.info("output.state", extra={"state": state.value, "reason": reason})
        self._on_change()

    def _record(self, cap: Caption, status: str, **kw) -> CaptionRecord:
        rec = CaptionRecord(
            id=cap.id, text=cap.text, kind=cap.kind, status=status, at=time.time(),
            source_event_ids=cap.source_event_ids, **kw,
        )
        self.history.append(rec)
        self.counts[status] = self.counts.get(status, 0) + 1
        if status == "sent":
            self.on_air = rec
        self._on_change()
        return rec

    def _drop_queue(self, status: str) -> None:
        while self._queue:
            self._record(self._queue.popleft(), status)

    def _require(self, *states: OutputState) -> None:
        if self.state not in states:
            allowed = ", ".join(s.value for s in states)
            raise CommandError(f"Not allowed in state {self.state.value} (allowed: {allowed})")

    # ----------------------------------------------------------- commands
    async def connect(self, raw_url: str, target: Target, confirm_event: bool = False) -> None:
        self._require(OutputState.DISCONNECTED, OutputState.CONNECTED, OutputState.PAUSED)
        if target is Target.EVENT:
            if not self.cfg.allow_event_target:
                raise CommandError("EVENT output is disabled in config (zoom.allow_event_target)")
            if not confirm_event:
                raise CommandError("EVENT target requires explicit confirmation")
        try:
            url = parse_caption_url(raw_url, allow_real_zoom=self.cfg.allow_real_zoom, allow_mock=self.cfg.allow_mock)
        except CaptionUrlError as exc:
            raise CommandError(str(exc)) from None
        if target is Target.EVENT and url.is_mock:
            raise CommandError("EVENT target cannot use a mock endpoint")
        prev = self.state
        self._drop_queue("dropped_reconnect")
        ok = await self.sender.attach(url)
        self.target = target
        self._consecutive_failures = 0
        new_state = OutputState.PAUSED if prev is OutputState.PAUSED else OutputState.CONNECTED
        self._set_state(new_state, None if ok else "seq_unknown")
        if not ok:
            self._on_alert("warn", "zoom_seq_unknown",
                           "Could not read the last caption number from Zoom; sending is blocked until it can.")

    async def disconnect(self) -> None:
        self._require(OutputState.CONNECTED, OutputState.PAUSED, OutputState.E_STOPPED)
        self._drop_queue("dropped_disconnect")
        self.sender.detach()
        self.target = None
        if self.state is OutputState.E_STOPPED:
            self.epoch += 1
            self._on_change()
        else:
            self._set_state(OutputState.DISCONNECTED)

    def start_live(self) -> None:
        self._require(OutputState.CONNECTED)
        if self.target is Target.EVENT and not self.cfg.allow_event_target:
            raise CommandError("EVENT output is disabled in config")
        self._consecutive_failures = 0
        self._set_state(OutputState.LIVE)

    def pause(self, reason: str = "operator") -> None:
        self._require(OutputState.LIVE)
        self._drop_queue("dropped_paused")
        self._set_state(OutputState.PAUSED, reason)

    def resume(self) -> None:
        self._require(OutputState.PAUSED)
        self._consecutive_failures = 0
        self._set_state(OutputState.LIVE)

    def stop(self) -> None:
        self._require(OutputState.LIVE, OutputState.PAUSED)
        self._drop_queue("dropped_stopped")
        self._set_state(OutputState.CONNECTED, "stopped")

    def estop(self) -> None:
        """Immediate. Valid from every state; idempotent."""
        self._drop_queue("dropped_estop")
        if self._inflight and not self._inflight.done():
            self._inflight.cancel()
        if self.state is not OutputState.E_STOPPED:
            self._set_state(OutputState.E_STOPPED, "emergency_stop")
        self._on_alert("critical", "estop", "Emergency stop: caption output halted.")

    def reset_estop(self) -> None:
        self._require(OutputState.E_STOPPED)
        self._set_state(OutputState.CONNECTED if self.sender.url else OutputState.DISCONNECTED, "estop_reset")

    # ----------------------------------------------------------- submission
    def submit(self, text: str, *, speech_end: float, source_event_ids: tuple = (),
               origin_epoch: int | None = None, parts: list[str] | None = None) -> bool:
        """Queue an automatic caption. Returns False if rejected at the gate
        (the reason is kept in ``last_reject``).

        ``origin_epoch`` is the output epoch when the source speech entered the
        pipeline. Any state change since then (pause, stop, E-STOP, reset,
        reconnect, going live) makes the work stale. This check does not depend
        on clock resolution (time.monotonic() ticks every ~15.6 ms on some
        Windows/Python versions), unlike the speech_end comparison below.
        ``parts`` are pre-composed payloads (e.g. bilingual); otherwise ``text``
        is chunked here.
        """
        now = self._clock()
        probe = Caption(text=text, speech_end=speech_end, source_event_ids=source_event_ids)
        reason = None
        if self.state is not OutputState.LIVE:
            reason = "dropped_not_live"
        elif origin_epoch is not None and origin_epoch != self.epoch:
            reason = "dropped_epoch"
        # "<=": speech ending in the same clock tick as going live counts as before it.
        elif self.live_since is not None and speech_end <= self.live_since:
            reason = "dropped_before_live"
        elif now > speech_end + self.cfg.caption_ttl_s:
            reason = "dropped_stale"
        self.last_reject = reason
        if reason:
            self._record(probe, reason)
            return False
        self._enqueue(text, "auto", speech_end, speech_end + self.cfg.caption_ttl_s, source_event_ids, parts=parts)
        return True

    def send_test(self, text: str) -> None:
        """Explicitly labelled test caption: only to a TEST target, never to the wedding."""
        if self.target is not Target.TEST:
            raise CommandError("Test captions are only allowed for a TEST target")
        self._require(OutputState.CONNECTED, OutputState.LIVE, OutputState.PAUSED)
        text = (text or "").strip()
        if not text:
            raise CommandError("Empty caption")
        now = self._clock()
        self._enqueue(text, "test", now, now + self.cfg.manual_ttl_s, (), front=True,
                      parts=[p for p in text.split("\f") if p.strip()])

    def send_manual(self, text: str) -> None:
        """Operator phrase deck / emergency manual caption."""
        self._require(OutputState.LIVE, OutputState.PAUSED)
        text = (text or "").strip()
        if not text:
            raise CommandError("Empty caption")
        now = self._clock()
        self._enqueue(text, "manual", now, now + self.cfg.manual_ttl_s, (), front=True)

    def _enqueue(self, text, kind, speech_end, deadline, source_ids, front=False, parts=None) -> None:
        if parts:
            chunks, shape = list(parts), False  # pre-composed (bilingual / test): send as given
        else:
            chunks, shape = chunk_caption(text, self.cfg.max_chars), self.cfg.line_breaks
        caps = [
            Caption(
                text=two_line(c, self.cfg.line_chars) if shape else c,
                speech_end=speech_end, kind=kind, source_event_ids=source_ids,
                epoch=self.epoch, deadline=deadline, part=i + 1, parts=len(chunks),
            )
            for i, c in enumerate(chunks)
        ]
        if front:
            for cap in reversed(caps):
                self._queue.appendleft(cap)
        else:
            self._queue.extend(caps)
        while len(self._queue) > self.cfg.queue_max:
            # Drop the oldest automatic caption; manual phrases are kept.
            victim = next((c for c in self._queue if c.kind == "auto"), None)
            if victim is None:
                break
            self._queue.remove(victim)
            self._record(victim, "dropped_overflow")
        self._wakeup.set()
        self._on_change()

    @property
    def queue_len(self) -> int:
        return len(self._queue)

    # ----------------------------------------------------------- delivery loop
    def _gate(self, cap: Caption) -> str | None:
        if cap.epoch != self.epoch:
            return "dropped_epoch"
        if cap.kind == "test":
            if self.target is not Target.TEST:
                return "dropped_not_test_target"
            allowed = (OutputState.CONNECTED, OutputState.LIVE, OutputState.PAUSED)
        elif cap.kind == "manual":
            allowed = (OutputState.LIVE, OutputState.PAUSED)
        else:
            allowed = (OutputState.LIVE,)
        if self.state not in allowed:
            return "dropped_not_live"
        if self._clock() > cap.deadline:
            return "dropped_stale"
        return None

    async def run(self) -> None:
        while True:
            if not self._queue:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            cap = self._queue[0]
            reason = self._gate(cap)
            if reason:
                self._queue.popleft()
                self._record(cap, reason)
                continue
            wait = self._last_send_end + self.cfg.min_interval_s - self._clock()
            if wait > 0:
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                continue  # re-evaluate the gate after waiting
            self._queue.popleft()
            await self._deliver(cap)

    async def _deliver(self, cap: Caption) -> None:
        self._inflight = asyncio.create_task(self.sender.send(cap.text, deadline=cap.deadline))
        try:
            result = await self._inflight
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the delivery loop itself is shutting down
            self._record(cap, "cancelled", error="emergency_stop")
            return
        finally:
            self._inflight = None
        self._last_send_end = self._clock()
        if result.outcome == "sent":
            self._consecutive_failures = 0
            e2e = (self._clock() - cap.speech_end) * 1000 if cap.kind == "auto" else None
            if e2e is not None:
                self.e2e.add(e2e)
            if result.latency_ms is not None:
                self.zoom_latency.add(result.latency_ms)
            self._record(cap, "sent", seq=result.seq, latency_ms=result.latency_ms, e2e_ms=e2e)
            return
        self._consecutive_failures += 1
        self._record(cap, result.outcome, seq=result.seq, error=result.error)
        if (
            self._consecutive_failures >= self.cfg.auto_pause_after_failures
            and self.state is OutputState.LIVE
        ):
            self._drop_queue("dropped_paused")
            self._set_state(OutputState.PAUSED, "zoom_failures")
            self._on_alert(
                "critical", "zoom_auto_paused",
                f"Zoom rejected {self._consecutive_failures} captions in a row "
                f"({result.error}). Output auto-paused. Check the meeting / replace the caption URL.",
            )

    # ----------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        st = self.sender.status()
        return {
            "state": self.state.value,
            "reason": self.state_reason,
            "target": self.target.value if self.target else None,
            "epoch": self.epoch,
            "queue": len(self._queue),
            "on_air": self.on_air.__dict__ if self.on_air else None,
            "history": [r.__dict__ for r in list(self.history)[-25:]],
            "counts": dict(self.counts),
            "e2e_ms": self.e2e.summary(),
            "zoom_ms": self.zoom_latency.summary(),
            "zoom": st.__dict__,
        }
