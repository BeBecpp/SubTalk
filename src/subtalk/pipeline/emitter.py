"""Releases captions in speech order even though translations finish out of order.

Each segment reserves a slot when it is recognised. A slot is completed with
a caption or skipped. If the head slot is still pending after
``head_timeout_s`` it is skipped so one slow request never blocks the rest.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _Slot:
    reserved_at: float
    done: bool = False
    text: str | None = None
    speech_end: float = 0.0
    source_ids: tuple = ()


class OrderedEmitter:
    def __init__(self, emit: Callable[[str, float, tuple], None], head_timeout_s: float = 8.0,
                 clock: Callable[[], float] = time.monotonic):
        self._emit = emit
        self._head_timeout = head_timeout_s
        self._clock = clock
        self._slots: dict[int, _Slot] = {}
        self._next_reserve = 0
        self._next_emit = 0
        self.head_timeouts = 0

    def reserve(self) -> int:
        n = self._next_reserve
        self._next_reserve += 1
        self._slots[n] = _Slot(reserved_at=self._clock())
        return n

    def complete(self, slot: int, text: str, speech_end: float, source_ids: tuple = ()) -> None:
        s = self._slots.get(slot)
        if s is None:
            return  # already skipped by timeout - a late result is never emitted
        s.done, s.text, s.speech_end, s.source_ids = True, text, speech_end, source_ids
        self._flush()

    def skip(self, slot: int) -> None:
        s = self._slots.get(slot)
        if s is not None:
            s.done = True
            s.text = None
        self._flush()

    def tick(self) -> None:
        head = self._slots.get(self._next_emit)
        if head and not head.done and self._clock() - head.reserved_at > self._head_timeout:
            self.head_timeouts += 1
            del self._slots[self._next_emit]
            self._next_emit += 1
        self._flush()

    def _flush(self) -> None:
        while True:
            head = self._slots.get(self._next_emit)
            if head is None:
                if self._next_emit < self._next_reserve:
                    self._next_emit += 1  # slot removed by timeout
                    continue
                return
            if not head.done:
                return
            del self._slots[self._next_emit]
            self._next_emit += 1
            if head.text:
                self._emit(head.text, head.speech_end, head.source_ids)

    @property
    def pending(self) -> int:
        return sum(1 for s in self._slots.values() if not s.done)
