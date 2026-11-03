"""Guest caption viewer: output-gate integration, local feed and relay publisher."""
import asyncio
import json
import time

import httpx
import pytest

from subtalk.output.controller import CommandError, OutputConfig, OutputController
from subtalk.output.viewer import ViewerFeed
from subtalk.state import OutputState, Target
from subtalk.zoom.sender import ZoomCaptionSender
from subtalk.zoom.seqstore import SeqStore
from conftest import clock_tick, go_live

URL = "http://127.0.0.1/mock-zoom/closedcaption?id=mock&ns=mock&expire=86400&signature=mock"
BOTH = {"tr": "Evliliği Yehova kurdu.", "en": "Jehovah created marriage."}


def make(zoom_http, tmp_path, *, enabled=True, **feed_kw):
    sender = ZoomCaptionSender(zoom_http, SeqStore(tmp_path / "seq.json"), attempt_timeout=0.2,
                               retry_budget=0.3, backoff_base=0.01)
    feed = ViewerFeed(enabled=enabled, **feed_kw)
    alerts = []
    c = OutputController(sender, OutputConfig(min_interval_s=0.0, caption_ttl_s=5.0),
                         on_alert=lambda *a: alerts.append(a), viewer=feed)
    c.alerts = alerts
    return c, feed


@pytest.fixture
async def ctl(zoom_http, tmp_path):
    c, feed = make(zoom_http, tmp_path)
    q = feed.subscribe()
    task = asyncio.create_task(c.run())
    c.q = q
    yield c
    task.cancel()


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def captions(q):
    return [m for m in drain(q) if m["type"] == "caption"]


async def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_viewer_only_live_without_zoom(ctl, mock_zoom):
    assert ctl.state is OutputState.DISCONNECTED
    await go_live(ctl)  # no caption URL: allowed because the viewer is on
    assert ctl.state is OutputState.LIVE
    assert ctl.submit("", speech_end=time.monotonic(), source_event_ids=(7,), texts=BOTH)
    caps = captions(ctl.q)
    assert len(caps) == 1 and caps[0]["tr"] == BOTH["tr"] and caps[0]["en"] == BOTH["en"]
    assert caps[0]["event_id"] == 7 and caps[0]["speech_end"] <= caps[0]["t"]
    await asyncio.sleep(0.1)
    assert mock_zoom.post_count == 0
    ctl.stop()
    assert ctl.state is OutputState.DISCONNECTED  # no URL attached: back to DISCONNECTED


async def test_viewer_disabled_keeps_old_rules(zoom_http, tmp_path):
    c, feed = make(zoom_http, tmp_path, enabled=False)
    with pytest.raises(CommandError):
        c.start_live()
    assert feed.publish_caption(BOTH) is None


async def test_nothing_published_unless_live(ctl):
    assert not ctl.submit("x", speech_end=time.monotonic(), texts=BOTH)
    await ctl.connect(URL, Target.TEST)
    assert not ctl.submit("x", speech_end=time.monotonic(), texts=BOTH)
    await go_live(ctl)
    ctl.pause()
    assert not ctl.submit("x", speech_end=time.monotonic(), texts=BOTH)
    assert captions(ctl.q) == []


async def test_status_messages_follow_output_state(ctl):
    await go_live(ctl)
    ctl.pause()
    ctl.resume()
    ctl.stop()
    states = [m["state"] for m in drain(ctl.q) if m["type"] == "status"]
    assert states == ["live", "paused", "live", "stopped"]


async def test_stale_epoch_and_before_live_not_published(ctl):
    await go_live(ctl)
    epoch = ctl.epoch
    spoken = time.monotonic()
    ctl.pause()
    await clock_tick(spoken)
    ctl.resume()
    assert not ctl.submit("x", speech_end=time.monotonic(), origin_epoch=epoch, texts=BOTH)
    assert not ctl.submit("x", speech_end=spoken, texts=BOTH)
    assert not ctl.submit("x", speech_end=time.monotonic() - 60, texts=BOTH)  # older than TTL
    assert captions(ctl.q) == []


async def test_estop_clears_guest_screens(ctl):
    await go_live(ctl)
    ctl.submit("", speech_end=time.monotonic(), texts=BOTH)
    assert len(ctl.viewer.recent) == 1
    ctl.estop()
    msgs = drain(ctl.q)
    assert [m["type"] for m in msgs][-2:] == ["status", "clear"]
    assert msgs[-2]["state"] == "stopped"
    assert list(ctl.viewer.recent) == [] and ctl.viewer.hello()["recent"] == []
    assert not ctl.submit("", speech_end=time.monotonic(), texts=BOTH)


async def test_zoom_and_viewer_together_each_get_their_part(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    # Zoom mode TR: Zoom gets Turkish only; the viewer still gets both languages.
    assert ctl.submit(BOTH["tr"], speech_end=time.monotonic(), parts=[BOTH["tr"]], texts=BOTH)
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == 1)
    assert mock_zoom.meeting().captions[0].text == BOTH["tr"]
    caps = captions(ctl.q)
    assert caps[0]["tr"] and caps[0]["en"]


async def test_only_english_passed_still_reaches_viewer(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    # Turkish withheld by a guard, Zoom mode TR: nothing for Zoom, English for viewers.
    assert ctl.submit("", speech_end=time.monotonic(), parts=[], texts={"en": BOTH["en"]})
    caps = captions(ctl.q)
    assert caps[0]["tr"] is None and caps[0]["en"] == BOTH["en"]
    await asyncio.sleep(0.1)
    assert mock_zoom.post_count == 0


async def test_zoom_failures_suspend_zoom_but_viewer_continues(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    mock_zoom.script.extend([403] * 500)
    for i in range(4):
        ctl.submit(f"f{i}", speech_end=time.monotonic(), texts={"tr": f"f{i}", "en": f"e{i}"})
    assert await wait_for(lambda: ctl.zoom_suspended, timeout=5)
    assert ctl.state is OutputState.LIVE
    assert any(a[1] == "zoom_suspended" for a in ctl.alerts)
    before = mock_zoom.post_count
    drain(ctl.q)
    assert ctl.submit("later", speech_end=time.monotonic(), texts={"tr": "sonra", "en": "later"})
    assert captions(ctl.q)[0]["en"] == "later"
    await asyncio.sleep(0.2)
    assert mock_zoom.post_count == before  # Zoom no longer hammered
    mock_zoom.script.clear()
    ctl.pause()
    ctl.resume()
    assert not ctl.zoom_suspended


async def test_manual_phrase_reaches_viewer_in_both_languages(ctl, mock_zoom):
    await go_live(ctl)
    ctl.send_manual("[Müzik]", en="[Music]")
    caps = captions(ctl.q)
    assert caps[0]["tr"] == "[Müzik]" and caps[0]["en"] == "[Music]" and caps[0]["kind"] == "manual"
    await asyncio.sleep(0.1)
    assert mock_zoom.post_count == 0


async def test_test_caption_never_reaches_viewer(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.send_test("SubTalk TEST")
    assert await wait_for(lambda: mock_zoom.post_count == 1)
    assert captions(ctl.q) == []


async def test_slow_local_viewer_never_blocks():
    feed = ViewerFeed(enabled=True)
    q = feed.subscribe()
    for i in range(200):
        feed.publish_caption({"tr": f"t{i}"})
    assert q.qsize() == 50 and feed.published == 200


# ------------------------------------------------------------------ relay publisher
class FakeRelay:
    def __init__(self, fail=0):
        self.fail = fail
        self.batches = []
        self.auth = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization"))
        if self.fail:
            self.fail -= 1
            return httpx.Response(503)
        self.batches.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "viewers": 12})


def relay_feed(relay, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(relay.handler))
    return ViewerFeed(enabled=True, relay_url="https://relay.example/", relay_token="t" * 32, http=http, **kw)


async def test_relay_receives_each_message_once_with_token():
    relay = FakeRelay()
    feed = relay_feed(relay)
    task = asyncio.create_task(feed.run_relay())
    feed.publish_status("LIVE")
    feed.publish_caption(BOTH, event_ids=(3,))
    assert await wait_for(lambda: feed.relay.sent == 2)
    task.cancel()
    sent = [m for b in relay.batches for m in b]
    assert [m["type"] for m in sent] == ["status", "caption"]
    assert sent[1]["tr"] == BOTH["tr"] and "_queued" not in sent[1]
    assert relay.auth[0] == "Bearer " + "t" * 32
    assert feed.relay.viewers == 12
    assert feed.relay_url == "https://relay.example"


async def test_relay_outage_retries_then_drops_stale():
    relay = FakeRelay(fail=10**6)
    feed = relay_feed(relay, max_age_s=0.3)
    task = asyncio.create_task(feed.run_relay())
    feed.publish_caption({"tr": "eski"})
    assert await wait_for(lambda: feed.relay.consecutive_failures >= 1)
    relay.fail = 0  # relay back; the first backoff (1 s) outlasts max_age_s (0.3 s)
    assert await wait_for(lambda: feed.relay.dropped_stale >= 1, timeout=6)
    feed.publish_caption({"tr": "yeni"})
    assert await wait_for(lambda: feed.relay.sent >= 1, timeout=6)
    task.cancel()
    texts = [m.get("tr") for b in relay.batches for m in b if m["type"] == "caption"]
    assert texts == ["yeni"]  # the old caption was never delivered late


async def test_relay_queue_is_bounded():
    relay = FakeRelay(fail=10**6)
    feed = relay_feed(relay, queue_max=5)
    for i in range(20):
        feed.publish_caption({"tr": str(i)})
    assert len(feed._relay_q) == 5 and feed.relay.dropped_overflow == 15


async def test_relay_heartbeat_when_idle():
    relay = FakeRelay()
    feed = relay_feed(relay, heartbeat_s=0.1)
    task = asyncio.create_task(feed.run_relay())
    assert await wait_for(lambda: any(m["type"] == "heartbeat" for b in relay.batches for m in b))
    task.cancel()


def test_relay_not_configured_without_token():
    assert not ViewerFeed(enabled=True, relay_url="https://relay.example", relay_token=None).relay.configured
