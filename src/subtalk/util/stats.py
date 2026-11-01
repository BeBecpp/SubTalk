from __future__ import annotations

from collections import deque


class Rolling:
    """Rolling window of latency samples with percentile helpers."""

    def __init__(self, maxlen: int = 200):
        self._v: deque[float] = deque(maxlen=maxlen)

    def add(self, value: float) -> None:
        self._v.append(float(value))

    def pct(self, p: float) -> float | None:
        if not self._v:
            return None
        s = sorted(self._v)
        k = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
        return s[k]

    def summary(self) -> dict:
        return {
            "n": len(self._v),
            "p50": self.pct(50),
            "p95": self.pct(95),
            "last": self._v[-1] if self._v else None,
        }
