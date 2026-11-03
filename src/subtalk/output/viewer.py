"""Guest caption viewer feed (Turkish / English chosen by each guest).

The output controller publishes here exactly when a caption passes the output
gate (LIVE, fresh, current epoch), plus status changes (live / paused /
stopped). The feed:

* keeps the last captions in memory and serves local read-only WebSocket
  viewers (LAN / Tailscale testing, or a venue screen);
* forwards every message ONCE to the public relay (Cloudflare Worker +
  Durable Object) with a bearer token, so up to many guests are served by the
  relay, not by the venue uplink.

Bounded queues everywhere; relay messages older than ``max_age_s`` are dropped
instead of being delivered late (no backlog after a network outage).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("subtalk.viewer")
STATUS_FOR_STATE = {"LIVE": "live", "PAUSED": "paused", "E_STOPPED": "stopped",
                    "CONNECTED": "stopped", "DISCONNECTED": "stopped"}


@dataclass
class RelayStats:
    configured: bool = False
    sent: int = 0
    failed: int = 0
    dropped_stale: int = 0
    dropped_overflow: int = 0
    consecutive_failures: int = 0
    last_ok_at: float | None = None
    last_error: str | None = None
    viewers: int | None = None  # as reported by the relay
    latency_ms: deque = field(default_factory=lambda: deque(maxlen=100))


class ViewerFeed:
    def __init__(self, *, enabled: bool = False, relay_url: str | None = None, relay_token: str | None = None,
                 http: httpx.AsyncClient | None = None, recent_max: int = 30, queue_max: int = 100,
                 max_age_s: float = 20.0, timeout_s: float = 3.0, heartbeat_s: float = 15.0):
        self.enabled = enabled
        self.relay_url = (relay_url or "").rstrip("/") or None
        self._token = relay_token
        self._http = http
        self.recent: deque[dict] = deque(maxlen=recent_max)
        self.status: dict = {"type": "status", "state": "stopped", "t": time.time()}
        self._seq = itertools.count(1)
        self._clients: set[asyncio.Queue] = set()
        self._relay_q: deque[dict] = deque()
        self._relay_wakeup = asyncio.Event()
        self.queue_max = queue_max
        self.max_age_s = max_age_s
        self.timeout_s = timeout_s
        self.relay = RelayStats(configured=bool(self.relay_url and relay_token))
        self.published = 0
        self.last_published_at: float | None = None
        self.heartbeat_s = heartbeat_s

    # ---------------------------------------------------------------- publishing
    def publish_caption(self, texts: dict[str, str], *, event_ids: tuple = (), speech_end_wall: float | None = None,
                        kind: str = "auto") -> dict | None:
        if not self.enabled:
            return None
        tr, en = (texts.get("tr") or "").strip(), (texts.get("en") or "").strip()
        if not tr and not en:
            return None
        msg = {"type": "caption", "seq": next(self._seq), "event_id": event_ids[0] if event_ids else None,
               "t": round(time.time(), 3), "speech_end": speech_end_wall, "kind": kind,
               "tr": tr or None, "en": en or None}
        self.recent.append(msg)
        self.published += 1
        self.last_published_at = msg["t"]
        self._fanout(msg)
        return msg

    def publish_status(self, output_state: str) -> None:
        state = STATUS_FOR_STATE.get(output_state, "stopped")
        if state == self.status.get("state"):
            return
        self.status = {"type": "status", "state": state, "t": round(time.time(), 3)}
        if self.enabled:
            self._fanout(self.status)

    def clear(self) -> None:
        """Take captions off guest screens (E-STOP, or between rehearsal and wedding)."""
        self.recent.clear()
        if not self.enabled:
            return
        self._fanout({"type": "clear", "t": round(time.time(), 3)})

    def _fanout(self, msg: dict) -> None:
        for q in list(self._clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # a slow local viewer misses messages; it never blocks the engine
        if self.relay.configured:
            self._relay_q.append({**msg, "_queued": time.monotonic()})
            while len(self._relay_q) > self.queue_max:
                self._relay_q.popleft()
                self.relay.dropped_overflow += 1
            self._relay_wakeup.set()

    # ---------------------------------------------------------------- local viewers
    def hello(self) -> dict:
        return {"type": "hello", "recent": list(self.recent)[-10:], "status": self.status, "t": round(time.time(), 3)}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    @property
    def local_viewers(self) -> int:
        return len(self._clients)

    # ---------------------------------------------------------------- relay
    def heartbeat(self) -> dict:
        """Tells the relay (and through it, every guest) that the engine is alive."""
        return {"type": "heartbeat", "state": self.status.get("state"), "t": round(time.time(), 3)}

    async def run_relay(self) -> None:
        """Deliver queued messages to the relay in order; drop stale ones.
        When idle, send a heartbeat every ``heartbeat_s`` so guests can tell
        "quiet moment" from "caption source offline"."""
        if not self.relay.configured:
            return
        http = self._http or httpx.AsyncClient()
        while True:
            if not self._relay_q:
                self._relay_wakeup.clear()
                try:
                    await asyncio.wait_for(self._relay_wakeup.wait(), timeout=self.heartbeat_s)
                except asyncio.TimeoutError:
                    if self.enabled:
                        self._relay_q.append({**self.heartbeat(), "_queued": time.monotonic()})
                continue
            now = time.monotonic()
            queued = []
            while self._relay_q and len(queued) < 20:
                m = self._relay_q.popleft()
                if now - m["_queued"] > self.max_age_s and m["type"] == "caption":
                    self.relay.dropped_stale += 1
                    continue
                queued.append(m)
            if not queued:
                continue
            ok = await self._post(http, [{k: v for k, v in m.items() if k != "_queued"} for m in queued])
            if not ok:
                # Retry later with the ORIGINAL queue time, so the staleness limit still holds.
                self._relay_q.extendleft(reversed(queued))
                await asyncio.sleep(min(5.0, 0.5 * (2 ** min(self.relay.consecutive_failures, 4))))

    async def _post(self, http: httpx.AsyncClient, batch: list[dict]) -> bool:
        t = time.monotonic()
        try:
            r = await http.post(f"{self.relay_url}/publish", content=json.dumps(batch, ensure_ascii=False),
                                headers={"Authorization": f"Bearer {self._token}",
                                         "Content-Type": "application/json; charset=utf-8"},
                                timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            return self._fail(type(exc).__name__)
        if r.status_code // 100 != 2:
            return self._fail(f"HTTP {r.status_code}")
        self.relay.sent += len(batch)
        self.relay.consecutive_failures = 0
        self.relay.last_ok_at = time.time()
        self.relay.last_error = None
        self.relay.latency_ms.append((time.monotonic() - t) * 1000)
        try:
            self.relay.viewers = int(r.json().get("viewers"))
        except (ValueError, TypeError, AttributeError):
            pass
        return True

    def _fail(self, error: str) -> bool:
        self.relay.failed += 1
        self.relay.consecutive_failures += 1
        self.relay.last_error = error
        log.warning("viewer.relay_failed", extra={"error": error})
        return False

    def snapshot(self) -> dict:
        lat = sorted(self.relay.latency_ms)
        return {
            "enabled": self.enabled,
            "published": self.published,
            "local_viewers": self.local_viewers,
            "status": self.status.get("state"),
            "relay": {
                "configured": self.relay.configured, "sent": self.relay.sent, "failed": self.relay.failed,
                "dropped_stale": self.relay.dropped_stale, "dropped_overflow": self.relay.dropped_overflow,
                "consecutive_failures": self.relay.consecutive_failures, "last_ok_at": self.relay.last_ok_at,
                "last_error": self.relay.last_error, "viewers": self.relay.viewers,
                "queue": len(self._relay_q),
                "latency_p50_ms": round(lat[len(lat) // 2]) if lat else None,
            },
        }
