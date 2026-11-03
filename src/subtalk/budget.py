"""Persistent spend ledger for live Claude API *test* runs (no database).

The owner authorised $5.00 of additional test spend; requests stop
automatically at $4.50. The ledger lives in ``state/api_test_budget.json``
(atomic writes), so it survives process restarts and context resets. It is
never reset by code.

Before each request an *upper-bound* cost is reserved (input tokens at the
most expensive input rate incl. cache writes + max_tokens at the output rate).
A request whose reservation would cross the stop threshold is refused.
After the response the reservation is replaced by the actual cost computed
from the response's usage. These are local estimates - not the provider's
billing; set a spend limit in the Anthropic Console as the real cap.
"""
from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path

from subtalk.util.atomic import read_json, write_json_atomic

LIMIT_USD = 5.00
STOP_USD = 4.50


class BudgetRefused(Exception):
    pass


class TestBudgetLedger:
    def __init__(self, path: Path, limit_usd: float = LIMIT_USD, stop_usd: float = STOP_USD):
        self.path = Path(path)
        self.limit = limit_usd
        self.stop = stop_usd
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._reserved: dict[int, float] = {}
        data = read_json(self.path, default=None)
        self.data = data or {
            "limit_usd": limit_usd, "stop_usd": stop_usd, "spent_usd": 0.0, "requests": 0,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "by_model": {}, "by_group": {}, "log": [],
        }
        # Limits recorded in the file win over constructor defaults (never loosened silently).
        self.limit = min(self.limit, float(self.data.get("limit_usd", self.limit)))
        self.stop = min(self.stop, float(self.data.get("stop_usd", self.stop)))

    @property
    def spent(self) -> float:
        return float(self.data["spent_usd"])

    @property
    def reserved(self) -> float:
        return sum(self._reserved.values())

    @property
    def remaining(self) -> float:
        return max(0.0, self.stop - self.spent - self.reserved)

    def reserve(self, upper_bound_usd: float) -> int:
        with self._lock:
            if self.spent + self.reserved + upper_bound_usd > self.stop:
                raise BudgetRefused(
                    f"test budget: spent ${self.spent:.4f} + reserved ${self.reserved:.4f} + this request "
                    f"(up to ${upper_bound_usd:.4f}) would exceed the ${self.stop:.2f} stop threshold"
                )
            rid = next(self._ids)
            self._reserved[rid] = upper_bound_usd
            return rid

    def release(self, rid: int) -> None:
        with self._lock:
            self._reserved.pop(rid, None)

    def commit(self, rid: int, *, model: str, cost_usd: float, input_tokens: int, output_tokens: int,
               cache_read: int, cache_write: int, group: str = "") -> None:
        with self._lock:
            self._reserved.pop(rid, None)
            d = self.data
            d["spent_usd"] = round(d["spent_usd"] + cost_usd, 6)
            d["requests"] += 1
            m = d["by_model"].setdefault(model, {"requests": 0, "cost_usd": 0.0, "input": 0, "output": 0,
                                                 "cache_read": 0, "cache_write": 0})
            m["requests"] += 1
            m["cost_usd"] = round(m["cost_usd"] + cost_usd, 6)
            m["input"] += input_tokens
            m["output"] += output_tokens
            m["cache_read"] += cache_read
            m["cache_write"] += cache_write
            if group:
                g = d["by_group"].setdefault(group, {"requests": 0, "cost_usd": 0.0})
                g["requests"] += 1
                g["cost_usd"] = round(g["cost_usd"] + cost_usd, 6)
            d["log"] = (d["log"] + [{"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model,
                                     "cost": round(cost_usd, 6), "group": group}])[-500:]
            write_json_atomic(self.path, d)

    def summary(self) -> dict:
        return {
            "limit_usd": self.limit, "stop_usd": self.stop, "spent_usd": round(self.spent, 4),
            "remaining_before_stop_usd": round(max(0.0, self.stop - self.spent), 4),
            "requests": self.data["requests"], "by_model": self.data["by_model"],
            "by_group": self.data["by_group"], "file": str(self.path),
        }


def upper_bound_cost(price, input_chars: int, max_tokens: int) -> float:
    """Pessimistic: ~1 token per 2 characters (Cyrillic/Japanese are dense), all input billed at
    the higher of the input and cache-write rates, and the full max_tokens of output."""
    input_tokens = input_chars / 2 + 50
    rate_in = max(price.input, price.cache_write)
    return (input_tokens * rate_in + max_tokens * price.output) / 1_000_000
