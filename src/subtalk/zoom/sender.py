"""Zoom Manual Caption API sender.

Implements the rules from Zoom's support article "Using a third-party closed
captioning service":

* every POST carries ``seq``; ``seq`` increases by exactly one between posts of
  *new* caption data and is NOT increased for retries;
* ``GET .../closedcaption/seq`` returns the last successfully received ``seq``;
* body is plain text, UTF-8, not form-encoded;
* retry error codes and timeouts with randomized binary exponential backoff
  (0-100 ms, 0-200 ms, 0-400 ms, ...) for about 5 s, then move on.

Design (see docs/ARCHITECTURE.md, "Caption delivery"):

* exactly one request in flight (a lock) so ordering is guaranteed;
* if any attempt for a caption ended in a timeout/transport error and the
  caption was finally abandoned, we cannot know whether Zoom accepted it, so
  the sequence becomes AMBIGUOUS and is re-read from Zoom before the next new
  caption. If every attempt got a definite HTTP error, Zoom did not accept the
  caption and the same number is reused for the next caption;
* cancellation (E-STOP) also makes the sequence AMBIGUOUS.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import httpx

from subtalk.zoom.seqstore import SeqStore
from subtalk.zoom.url import CaptionUrl

log = logging.getLogger("subtalk.zoom")

CONTENT_TYPE = "text/plain; charset=utf-8"


class SeqState(str, Enum):
    UNKNOWN = "unknown"      # never synchronised for this URL
    KNOWN = "known"          # last_confirmed is trustworthy
    AMBIGUOUS = "ambiguous"  # a post may or may not have been accepted


@dataclass
class DeliveryResult:
    outcome: str  # sent | abandoned | blocked | cancelled
    seq: int | None = None
    attempts: int = 0
    http_status: int | None = None
    latency_ms: float | None = None
    zoom_timestamp: str | None = None
    error: str | None = None


@dataclass
class SenderStatus:
    attached: bool
    host: str | None
    fingerprint: str | None
    seq_state: str
    last_confirmed: int | None
    seq_source: str | None
    consecutive_failures: int
    last_http_status: int | None
    last_error: str | None
    last_success_at: float | None
    sent_total: int
    abandoned_total: int


def normalize_caption_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    # Zoom documents "\n" as the line break; never send other control characters.
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return text.strip()


def parse_seq_response(body: str) -> int:
    """Parse the body of GET /closedcaption/seq.

    Zoom does not document the exact body format, so accept a bare integer or a
    JSON object/number. Anything else raises ValueError (M0-A verifies this).
    """
    body = (body or "").strip()
    if re.fullmatch(r"-?\d+", body):
        return int(body)
    data = json.loads(body)
    if isinstance(data, int):
        return data
    if isinstance(data, dict):
        for key in ("seq", "last_seq", "lastSeq", "sequence"):
            if isinstance(data.get(key), int):
                return data[key]
    raise ValueError("Unrecognised /closedcaption/seq response")


@dataclass
class _Clock:
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep
    rng: Callable[[], float] = random.random


class ZoomCaptionSender:
    def __init__(
        self,
        http: httpx.AsyncClient,
        store: SeqStore,
        *,
        attempt_timeout: float = 1.5,
        retry_budget: float = 5.0,
        backoff_base: float = 0.1,
        clock: _Clock | None = None,
    ):
        self._http = http
        self._store = store
        self.attempt_timeout = attempt_timeout
        self.retry_budget = retry_budget
        self.backoff_base = backoff_base
        self._clock = clock or _Clock()
        self._lock = asyncio.Lock()
        self.url: CaptionUrl | None = None
        self.last_confirmed: int | None = None
        self.seq_state = SeqState.UNKNOWN
        self.seq_source: str | None = None
        self.consecutive_failures = 0
        self.last_http_status: int | None = None
        self.last_error: str | None = None
        self.last_success_at: float | None = None
        self.sent_total = 0
        self.abandoned_total = 0

    # ------------------------------------------------------------------ status
    def status(self) -> SenderStatus:
        return SenderStatus(
            attached=self.url is not None,
            host=self.url.host if self.url else None,
            fingerprint=self.url.fingerprint[:8] if self.url else None,
            seq_state=self.seq_state.value,
            last_confirmed=self.last_confirmed,
            seq_source=self.seq_source,
            consecutive_failures=self.consecutive_failures,
            last_http_status=self.last_http_status,
            last_error=self.last_error,
            last_success_at=self.last_success_at,
            sent_total=self.sent_total,
            abandoned_total=self.abandoned_total,
        )

    # -------------------------------------------------------------- lifecycle
    async def attach(self, url: CaptionUrl) -> bool:
        """Attach a caption URL and synchronise the sequence from Zoom.

        Returns True when the sequence is KNOWN afterwards.
        """
        async with self._lock:
            self.url = url
            self.last_confirmed = None
            self.seq_state = SeqState.UNKNOWN
            self.seq_source = None
            self.consecutive_failures = 0
            self.last_http_status = None
            self.last_error = None
            ok = await self._resync_locked()
            if not ok:
                stored = self._store.get(url.fingerprint)
                if stored is not None:
                    self.last_confirmed = stored
                    self.seq_state = SeqState.KNOWN
                    self.seq_source = "local_mirror"
                    log.warning("zoom.seq from local mirror", extra={"seq": stored})
            return self.seq_state is SeqState.KNOWN

    def detach(self) -> None:
        self.url = None
        self.seq_state = SeqState.UNKNOWN
        self.last_confirmed = None
        self.seq_source = None

    async def resync(self) -> bool:
        async with self._lock:
            return await self._resync_locked()

    def force_last_confirmed(self, value: int) -> None:
        """Explicit operator override when /seq is unavailable (documented)."""
        if self.url is None:
            raise RuntimeError("No caption URL attached")
        self.last_confirmed = int(value)
        self.seq_state = SeqState.KNOWN
        self.seq_source = "operator_override"
        self._store.put(self.url.fingerprint, self.last_confirmed)

    async def _resync_locked(self) -> bool:
        if self.url is None:
            return False
        try:
            resp = await self._http.get(self.url.seq_url(), timeout=self.attempt_timeout)
            self.last_http_status = resp.status_code
            if resp.status_code // 100 != 2:
                self.last_error = f"seq endpoint HTTP {resp.status_code}"
                return False
            value = parse_seq_response(resp.text)
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"seq endpoint: {type(exc).__name__}"
            return False
        self.last_confirmed = value
        self.seq_state = SeqState.KNOWN
        self.seq_source = "zoom"
        self._store.put(self.url.fingerprint, value)
        return True

    # ------------------------------------------------------------------- send
    async def send(self, text: str, deadline: float | None = None) -> DeliveryResult:
        """Send one NEW caption. Retries reuse the same seq.

        ``deadline`` is a monotonic time after which the caption is stale; the
        retry budget never extends past it.
        """
        async with self._lock:
            if self.url is None:
                return DeliveryResult("blocked", error="no_caption_url")
            if self.seq_state is not SeqState.KNOWN:
                await self._resync_locked()
                if self.seq_state is not SeqState.KNOWN:
                    return DeliveryResult("blocked", error="seq_unknown")
            body = normalize_caption_text(text).encode("utf-8")
            if not body:
                return DeliveryResult("blocked", error="empty_caption")
            seq = (self.last_confirmed or 0) + 1
            url = self.url.post_url(seq)
            clk = self._clock
            start = clk.monotonic()
            budget_end = start + self.retry_budget
            if deadline is not None:
                budget_end = min(budget_end, deadline)
            attempts = 0
            uncertain = False
            status: int | None = None
            error: str | None = None
            try:
                while True:
                    attempts += 1
                    try:
                        resp = await self._http.post(
                            url,
                            content=body,
                            headers={"Content-Type": CONTENT_TYPE},
                            timeout=self.attempt_timeout,
                        )
                        status = resp.status_code
                        if status // 100 == 2:
                            self.last_confirmed = seq
                            self.seq_state = SeqState.KNOWN
                            self.seq_source = "zoom_ack"
                            self.consecutive_failures = 0
                            self.last_http_status = status
                            self.last_error = None
                            self.last_success_at = time.time()
                            self.sent_total += 1
                            self._store.put(self.url.fingerprint, seq)
                            return DeliveryResult(
                                "sent",
                                seq=seq,
                                attempts=attempts,
                                http_status=status,
                                latency_ms=(clk.monotonic() - start) * 1000,
                                zoom_timestamp=resp.text.strip()[:64] or None,
                            )
                        error = f"HTTP {status}"
                    except httpx.TimeoutException:
                        uncertain = True
                        status, error = None, "timeout"
                    except httpx.HTTPError as exc:
                        # The request may have reached Zoom before the failure.
                        uncertain = True
                        status, error = None, type(exc).__name__
                    delay = clk.rng() * self.backoff_base * (2 ** (attempts - 1))
                    if clk.monotonic() + delay >= budget_end:
                        break
                    await clk.sleep(delay)
            except asyncio.CancelledError:
                self.seq_state = SeqState.AMBIGUOUS
                self.last_error = "cancelled"
                raise
            # Abandoned after the retry budget.
            self.abandoned_total += 1
            self.consecutive_failures += 1
            self.last_http_status = status
            self.last_error = error
            if uncertain:
                self.seq_state = SeqState.AMBIGUOUS
            return DeliveryResult(
                "abandoned",
                seq=seq,
                attempts=attempts,
                http_status=status,
                latency_ms=(clk.monotonic() - start) * 1000,
                error=error,
            )
