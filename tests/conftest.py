import asyncio

import httpx
import pytest
from fastapi import FastAPI

from subtalk.zoom.mock import MockZoomCaptionService


class FakeClock:
    """Deterministic clock for retry/backoff tests (sleep advances time)."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.t += d
        await asyncio.sleep(0)

    def rng(self) -> float:
        return 1.0  # worst-case backoff: makes budget arithmetic predictable


@pytest.fixture
def mock_zoom():
    return MockZoomCaptionService(stall_seconds=0.3)


class TimeoutEnforcingTransport(httpx.AsyncBaseTransport):
    """httpx.ASGITransport ignores timeouts; real network clients do not."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner

    async def handle_async_request(self, request):
        timeout = (request.extensions.get("timeout") or {}).get("read")
        try:
            return await asyncio.wait_for(self.inner.handle_async_request(request), timeout)
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout("mock read timeout", request=request)


@pytest.fixture
def zoom_http(mock_zoom):
    app = FastAPI()
    app.include_router(mock_zoom.router())
    transport = TimeoutEnforcingTransport(httpx.ASGITransport(app=app))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1")
