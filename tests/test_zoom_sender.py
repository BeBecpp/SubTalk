import asyncio

import httpx
import pytest

from subtalk.zoom.sender import (
    SeqState,
    ZoomCaptionSender,
    _Clock,
    normalize_caption_text,
    parse_seq_response,
)
from subtalk.zoom.seqstore import SeqStore
from subtalk.zoom.url import parse_caption_url

URL = "http://127.0.0.1/mock-zoom/closedcaption?id=mock&ns=mock&expire=86400&signature=mock"


def make_sender(http, tmp_path, **kw):
    store = SeqStore(tmp_path / "seq.json")
    return ZoomCaptionSender(http, store, attempt_timeout=0.1, retry_budget=1.0, **kw), store


def url():
    return parse_caption_url(URL, allow_real_zoom=False, allow_mock=True)


async def test_seq_advances_by_exactly_one(zoom_http, mock_zoom, tmp_path):
    s, _ = make_sender(zoom_http, tmp_path)
    assert await s.attach(url())
    for i in range(1, 6):
        r = await s.send(f"Altyazı {i} — çğıİöşü")
        assert r.outcome == "sent" and r.seq == i
    m = mock_zoom.meeting()
    assert [c.seq for c in m.captions] == [1, 2, 3, 4, 5]
    assert m.violations == []
    assert m.captions[0].text == "Altyazı 1 — çğıİöşü"  # UTF-8 Turkish intact


async def test_attach_resumes_from_zoom_seq(zoom_http, mock_zoom, tmp_path):
    mock_zoom.meeting().last_seq = 41  # e.g. captions sent before an engine restart
    s, _ = make_sender(zoom_http, tmp_path)
    assert await s.attach(url())
    assert s.seq_source == "zoom" and s.last_confirmed == 41
    r = await s.send("devam")
    assert r.seq == 42 and mock_zoom.meeting().violations == []


async def test_retry_reuses_same_seq_after_http_errors(zoom_http, mock_zoom, tmp_path):
    s, _ = make_sender(zoom_http, tmp_path, backoff_base=0.001)
    await s.attach(url())
    mock_zoom.script.extend([503, 500])  # two failures, third attempt succeeds
    r = await s.send("merhaba")
    assert r.outcome == "sent" and r.seq == 1 and r.attempts == 3
    assert mock_zoom.meeting().violations == []


async def test_definite_errors_do_not_consume_seq(zoom_http, mock_zoom, tmp_path):
    s, _ = make_sender(zoom_http, tmp_path, backoff_base=0.001)
    s.retry_budget = 0.05
    await s.attach(url())
    mock_zoom.script.extend([500] * 200)
    r = await s.send("kaybolan")
    assert r.outcome == "abandoned"
    assert s.seq_state is SeqState.KNOWN  # every attempt got a definite error
    mock_zoom.script.clear()
    r2 = await s.send("sonraki")
    assert r2.outcome == "sent" and r2.seq == 1  # the unused number is reused
    assert mock_zoom.meeting().violations == []


async def test_lost_response_becomes_ambiguous_then_resyncs(zoom_http, mock_zoom, tmp_path):
    """Zoom accepted seq 1 but the response never arrived: must not resend as 1
    with new text, and must not skip to 3."""
    s, _ = make_sender(zoom_http, tmp_path, backoff_base=0.001)
    s.retry_budget = 0.05
    await s.attach(url())
    mock_zoom.script.append("accept_then_timeout")
    mock_zoom.script.extend([500] * 50)  # following retries fail definitively
    r = await s.send("ilk")
    # Zoom's own seq shows it was accepted: delivered, not a failure (no step towards auto-pause).
    assert r.outcome == "sent" and r.seq == 1 and s.seq_state is SeqState.KNOWN
    assert s.confirmed_by_seq_total == 1 and s.consecutive_failures == 0
    mock_zoom.script.clear()
    r2 = await s.send("ikinci")
    assert r2.outcome == "sent" and r2.seq == 2
    assert mock_zoom.meeting().violations == []
    assert [c.text for c in mock_zoom.meeting().captions] == ["ilk", "ikinci"]


async def test_real_zoom_timeout_then_403_on_same_seq_retry(zoom_http, mock_zoom, tmp_path):
    """Observed in a real TEST meeting (4 Nov): the first POST timed out although Zoom had
    shown the caption; the same-seq retry got HTTP 403. Must count as delivered, shown once."""
    s, _ = make_sender(zoom_http, tmp_path, backoff_base=0.001)
    s.retry_budget = 0.5
    await s.attach(url())
    mock_zoom.script.append("accept_then_timeout")  # following attempts hit the 403 duplicate rule
    r = await s.send("TR: test | EN: test")
    assert r.outcome == "sent" and r.seq == 1 and s.consecutive_failures == 0
    assert mock_zoom.meeting().duplicate_retries >= 1
    r2 = await s.send("sonraki")
    assert r2.outcome == "sent" and r2.seq == 2
    assert [c.text for c in mock_zoom.meeting().captions] == ["TR: test | EN: test", "sonraki"]
    assert mock_zoom.meeting().violations == []


async def test_definite_403_without_timeout_stays_a_failure(zoom_http, mock_zoom, tmp_path):
    s, _ = make_sender(zoom_http, tmp_path, backoff_base=0.001)
    s.retry_budget = 0.05
    await s.attach(url())
    mock_zoom.script.extend([403] * 50)  # e.g. expired token: never shown
    r = await s.send("reddedildi")
    assert r.outcome == "abandoned" and s.consecutive_failures == 1 and s.confirmed_by_seq_total == 0


async def test_timeout_never_accepted_reuses_number_after_resync(zoom_http, mock_zoom, tmp_path):
    s, _ = make_sender(zoom_http, tmp_path, backoff_base=0.001)
    s.retry_budget = 0.05
    await s.attach(url())
    mock_zoom.script.extend(["timeout"] * 3)
    r = await s.send("zaman aşımı")
    assert r.outcome == "abandoned" and s.seq_state is SeqState.AMBIGUOUS
    mock_zoom.script.clear()
    r2 = await s.send("yeni")
    assert r2.outcome == "sent" and r2.seq == 1


async def test_blocks_when_seq_unknown_and_no_mirror(zoom_http, mock_zoom, tmp_path):
    mock_zoom.seq_endpoint_available = False
    s, _ = make_sender(zoom_http, tmp_path)
    assert not await s.attach(url())
    r = await s.send("x")
    assert r.outcome == "blocked" and r.error == "seq_unknown"
    assert mock_zoom.post_count == 0  # nothing was posted


async def test_local_mirror_used_when_seq_endpoint_down(zoom_http, mock_zoom, tmp_path):
    s, store = make_sender(zoom_http, tmp_path)
    await s.attach(url())
    await s.send("bir")
    mock_zoom.seq_endpoint_available = False
    s2 = ZoomCaptionSender(zoom_http, SeqStore(tmp_path / "seq.json"), attempt_timeout=0.1)
    assert await s2.attach(url())
    assert s2.seq_source == "local_mirror" and s2.last_confirmed == 1
    r = await s2.send("iki")
    assert r.seq == 2 and mock_zoom.meeting().violations == []


async def test_deadline_caps_retry_budget(tmp_path):
    clock_t = [0.0]

    def handler(request):
        return httpx.Response(503)

    async def fake_sleep(d):
        clock_t[0] += d

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = ZoomCaptionSender(
        http, SeqStore(None), retry_budget=5.0, backoff_base=0.1,
        clock=_Clock(monotonic=lambda: clock_t[0], sleep=fake_sleep, rng=lambda: 1.0),
    )
    s.url = url()
    s.seq_state, s.last_confirmed = SeqState.KNOWN, 0
    r = await s.send("eski", deadline=0.25)
    assert r.outcome == "abandoned"
    assert clock_t[0] <= 0.25  # never retried past the staleness deadline


async def test_backoff_is_binary_exponential(tmp_path):
    t = [0.0]
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        t[0] += d

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    s = ZoomCaptionSender(
        http, SeqStore(None), retry_budget=5.0, backoff_base=0.1,
        clock=_Clock(monotonic=lambda: t[0], sleep=fake_sleep, rng=lambda: 1.0),
    )
    s.url = url()
    s.seq_state, s.last_confirmed = SeqState.KNOWN, 0
    await s.send("x")
    assert sleeps[:4] == pytest.approx([0.1, 0.2, 0.4, 0.8])
    assert sum(sleeps) < 5.0


async def test_cancellation_marks_ambiguous(zoom_http, mock_zoom, tmp_path):
    s, _ = make_sender(zoom_http, tmp_path)
    s.attempt_timeout = 5
    await s.attach(url())
    mock_zoom.script.append("accept_then_timeout")
    task = asyncio.create_task(s.send("iptal"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.seq_state is SeqState.AMBIGUOUS
    r = await s.send("sonra")
    assert r.seq == 2 and mock_zoom.meeting().violations == []


async def test_body_is_plain_utf8(tmp_path):
    seen = {}

    def handler(request):
        seen["ctype"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, text="ts")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = ZoomCaptionSender(http, SeqStore(None))
    s.url = url()
    s.seq_state, s.last_confirmed = SeqState.KNOWN, 7
    r = await s.send("  Yehova’nın Şahitleri\u0000 ")
    assert r.seq == 8
    assert seen["ctype"] == "text/plain; charset=utf-8"
    assert seen["body"] == "Yehova’nın Şahitleri".encode("utf-8")


def test_normalize_nfc_and_controls():
    decomposed = "İstanbul"  # I + combining dot -> İ
    assert normalize_caption_text(decomposed) == "İstanbul"
    assert normalize_caption_text("a\tb\nc") == "ab\nc"


@pytest.mark.parametrize("body,expected", [("41", 41), (" 7\n", 7), ('{"seq": 9}', 9), ("12", 12)])
def test_parse_seq_response(body, expected):
    assert parse_seq_response(body) == expected


def test_parse_seq_response_rejects_garbage():
    with pytest.raises(ValueError):
        parse_seq_response("<html>nope</html>")
