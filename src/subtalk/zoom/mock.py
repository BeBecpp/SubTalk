"""In-process mock of Zoom's Manual Caption endpoint for development and tests.

Deliberately strict: any sequence violation is recorded and answered with an
error so tests can assert that SubTalk never produces one. Real Zoom behaviour
for gaps/duplicates is unverified until the M0-A probe runs in a test meeting.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

REQUIRED = ("id", "ns", "expire", "signature", "seq")


@dataclass
class MockCaption:
    seq: int
    text: str
    received_at: float


@dataclass
class MockMeeting:
    last_seq: int = 0
    captions: deque = field(default_factory=lambda: deque(maxlen=500))
    violations: list = field(default_factory=list)
    duplicate_retries: int = 0


class MockZoomCaptionService:
    """Failure injection: push actions onto ``script`` (consumed per POST):

    * ``"ok"`` - normal handling
    * ``500`` / ``503`` / ``400`` ... - answer with that status, do not accept
    * ``"timeout"`` - do not accept, stall longer than the client timeout
    * ``"accept_then_timeout"`` - accept the caption, then stall (lost response)
    """

    def __init__(self, stall_seconds: float = 5.0, duplicate_status: int = 403):
        self.meetings: dict[str, MockMeeting] = {}
        self.started = True
        self.script: deque = deque()
        self.seq_endpoint_available = True
        self.stall_seconds = stall_seconds
        self.post_count = 0
        # Real Zoom answered a same-seq retry of an already accepted caption with 403 (TEST meeting, 4 Nov).
        self.duplicate_status = duplicate_status

    def meeting(self, meeting_id: str = "mock") -> MockMeeting:
        return self.meetings.setdefault(meeting_id, MockMeeting())

    def mock_url(self, base: str, meeting_id: str = "mock") -> str:
        return f"{base.rstrip('/')}/mock-zoom/closedcaption?id={meeting_id}&ns=mock&expire=86400&signature=mock"

    def router(self) -> APIRouter:
        r = APIRouter()

        @r.post("/mock-zoom/closedcaption")
        async def post_caption(request: Request):
            return await self.handle_post(request)

        @r.get("/mock-zoom/closedcaption/seq")
        async def get_seq(request: Request):
            if not self.seq_endpoint_available:
                return PlainTextResponse("not found", status_code=404)
            m = self.meeting(request.query_params.get("id", ""))
            return PlainTextResponse(str(m.last_seq))

        @r.get("/mock-zoom/captions")
        async def list_captions(id: str = "mock"):
            m = self.meeting(id)
            return {
                "last_seq": m.last_seq,
                "violations": m.violations,
                "captions": [c.__dict__ for c in list(m.captions)[-50:]],
            }

        return r

    async def handle_post(self, request: Request):
        self.post_count += 1
        q = request.query_params
        if any(not q.get(k) for k in REQUIRED):
            return PlainTextResponse("Unauthorized", status_code=403)
        ctype = request.headers.get("content-type", "")
        if not ctype.startswith("text/plain"):
            return PlainTextResponse("Bad content type", status_code=400)
        body = (await request.body()).decode("utf-8")
        action = self.script.popleft() if self.script else "ok"
        if isinstance(action, int):
            return PlainTextResponse("error", status_code=action)
        if action == "timeout":
            await asyncio.sleep(self.stall_seconds)
            return PlainTextResponse("late", status_code=504)
        if not self.started:
            return PlainTextResponse("Bad request. The meeting has not started.", status_code=400)
        m = self.meeting(q["id"])
        seq = int(q["seq"])
        if seq == m.last_seq and m.captions and m.captions[-1].text == body:
            m.duplicate_retries += 1  # a retry of an accepted caption: not shown twice
            if self.duplicate_status // 100 != 2:
                return PlainTextResponse("Forbidden", status_code=self.duplicate_status)
        elif seq != m.last_seq + 1:
            m.violations.append({"expected": m.last_seq + 1, "got": seq, "text": body})
            return PlainTextResponse("sequence violation", status_code=400)
        else:
            m.last_seq = seq
            m.captions.append(MockCaption(seq=seq, text=body, received_at=time.time()))
        if action == "accept_then_timeout":
            await asyncio.sleep(self.stall_seconds)
        return PlainTextResponse(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()))
