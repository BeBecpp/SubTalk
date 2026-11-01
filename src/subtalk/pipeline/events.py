"""Bounded in-memory speech event history (the MN<->JA matching model)."""
from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class SpeechEvent:
    kind: str  # MN | JA | UNCERTAIN | NONSPEECH | OVERLAP | MUSIC
    text: str
    t_start: float  # monotonic
    t_end: float    # monotonic - used for caption freshness
    quality: str = "ok"
    main_speech: bool = False
    output_epoch: int | None = None  # output epoch when this speech entered the pipeline
    id: int = field(default_factory=lambda: next(_ids))
    wall: float = field(default_factory=time.time)
    status: str = "heard"
    turkish: str | None = None
    reasons: list[str] = field(default_factory=list)
    links: list[int] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    relation: str | None = None
    asr_ms: float | None = None
    translate_ms: float | None = None
    model: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "text": self.text, "quality": self.quality,
            "status": self.status, "turkish": self.turkish, "reasons": self.reasons[-4:],
            "links": self.links, "flags": self.flags, "citations": self.citations,
            "relation": self.relation, "wall": self.wall, "duration": round(self.t_end - self.t_start, 2),
            "asr_ms": self.asr_ms, "translate_ms": self.translate_ms, "main_speech": self.main_speech,
        }


class EventStore:
    def __init__(self, maxlen: int = 40):
        self._events: deque[SpeechEvent] = deque(maxlen=maxlen)

    def add(self, ev: SpeechEvent) -> SpeechEvent:
        self._events.append(ev)
        return ev

    def get(self, event_id: int) -> SpeechEvent | None:
        return next((e for e in self._events if e.id == event_id), None)

    def recent(self, kind: str | None = None, window_s: float | None = None, now: float | None = None) -> list[SpeechEvent]:
        now = time.monotonic() if now is None else now
        return [
            e for e in self._events
            if (kind is None or e.kind == kind) and (window_s is None or now - e.t_end <= window_s)
        ]

    def all(self) -> list[SpeechEvent]:
        return list(self._events)
