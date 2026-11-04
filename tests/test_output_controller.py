import asyncio
import time

import pytest

from subtalk.output.chunker import chunk_caption, two_line
from subtalk.output.compose import reading_time_s
from subtalk.output.controller import CommandError, OutputConfig, OutputController
from subtalk.state import OutputState, Target
from subtalk.zoom.sender import ZoomCaptionSender
from subtalk.zoom.seqstore import SeqStore
from conftest import clock_tick, go_live

URL = "http://127.0.0.1/mock-zoom/closedcaption?id=mock&ns=mock&expire=86400&signature=mock"
REAL = "https://wmcc.zoom.us/closedcaption?id=1&ns=a&expire=86400&signature=s"


@pytest.fixture
async def ctl(zoom_http, tmp_path):
    sender = ZoomCaptionSender(zoom_http, SeqStore(tmp_path / "seq.json"), attempt_timeout=0.2,
                               retry_budget=0.3, backoff_base=0.01)
    alerts = []
    c = OutputController(sender, OutputConfig(min_interval_s=0.0, caption_ttl_s=5.0),
                         on_alert=lambda *a: alerts.append(a))
    c.alerts = alerts
    task = asyncio.create_task(c.run())
    yield c
    task.cancel()


async def wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_nothing_sent_unless_live(ctl, mock_zoom):
    assert not ctl.submit("önce", speech_end=time.monotonic())  # DISCONNECTED
    await ctl.connect(URL, Target.TEST)
    assert ctl.state is OutputState.CONNECTED
    assert not ctl.submit("bağlı ama canlı değil", speech_end=time.monotonic())
    await asyncio.sleep(0.1)
    assert mock_zoom.post_count == 0


async def test_live_sends_in_order(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    for i in range(3):
        assert ctl.submit(f"altyazı {i}", speech_end=time.monotonic())
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == 3)
    assert [c.text for c in mock_zoom.meeting().captions] == ["altyazı 0", "altyazı 1", "altyazı 2"]
    assert await wait_for(lambda: ctl.on_air and ctl.on_air.text == "altyazı 2")
    assert [r.seq for r in ctl.history if r.status == "sent"] == [1, 2, 3]


async def test_pause_drops_instead_of_queueing(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    ctl.pause()
    for i in range(20):
        assert not ctl.submit(f"duraklatıldı {i}", speech_end=time.monotonic())
    assert ctl.queue_len == 0
    ctl.resume()
    await asyncio.sleep(0.1)
    assert mock_zoom.post_count == 0


async def test_speech_from_before_resume_is_rejected(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    ctl.pause()
    spoken_while_paused = time.monotonic()
    await asyncio.sleep(0.01)
    ctl.resume()
    assert not ctl.submit("eski konuşma", speech_end=spoken_while_paused)
    assert ctl.history[-1].status == "dropped_before_live"


async def test_stale_caption_never_sent(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    ctl.cfg.caption_ttl_s = 0.05
    spoken = time.monotonic()
    await asyncio.sleep(0.1)  # translation took longer than the TTL
    assert not ctl.submit("çok eski", speech_end=spoken)
    assert ctl.history[-1].status == "dropped_stale"


async def test_queued_caption_expiring_is_dropped_at_gate(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    ctl.cfg.caption_ttl_s = 0.2
    ctl.cfg.min_interval_s = 0.5  # the second caption must wait past its deadline
    ctl.submit("bir", speech_end=time.monotonic())
    ctl.submit("iki", speech_end=time.monotonic())
    assert await wait_for(lambda: any(r.status == "dropped_stale" for r in ctl.history), timeout=2)
    assert [c.text for c in mock_zoom.meeting().captions] == ["bir"]


async def test_estop_is_immediate_and_latched(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    mock_zoom.stall_seconds = 0.15
    mock_zoom.script.append("accept_then_timeout")  # first caption hangs in flight
    for i in range(5):
        ctl.submit(f"x{i}", speech_end=time.monotonic())
    await asyncio.sleep(0.03)
    t0 = time.perf_counter()
    ctl.estop()
    assert (time.perf_counter() - t0) < 0.2
    assert ctl.state is OutputState.E_STOPPED and ctl.queue_len == 0
    posts = mock_zoom.post_count
    await asyncio.sleep(0.3)
    assert mock_zoom.post_count == posts  # nothing new after E-STOP
    for cmd in (ctl.start_live, ctl.resume, ctl.pause):
        with pytest.raises(CommandError):
            cmd()
    with pytest.raises(CommandError):
        ctl.send_manual("manual")
    assert not ctl.submit("yeni", speech_end=time.monotonic())
    ctl.reset_estop()
    assert ctl.state is OutputState.CONNECTED  # never straight back to LIVE


async def test_estop_rejects_work_created_before_it(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    t_before = time.monotonic()
    ctl.estop()
    ctl.reset_estop()
    await go_live(ctl)
    assert not ctl.submit("E-STOP öncesi", speech_end=t_before)


async def test_same_clock_tick_counts_as_before_live(zoom_http, mock_zoom, tmp_path):
    """Frozen clock = worst case of a coarse clock: everything happens in one tick."""
    sender = ZoomCaptionSender(zoom_http, SeqStore(None), attempt_timeout=0.2)
    c = OutputController(sender, OutputConfig(min_interval_s=0.0), clock=lambda: 500.0)
    await c.connect(URL, Target.TEST)
    c.start_live()
    c.estop()
    c.reset_estop()
    c.start_live()
    assert not c.submit("aynı an", speech_end=500.0)
    assert c.history[-1].status == "dropped_before_live"


async def test_work_from_an_older_epoch_is_rejected(ctl, mock_zoom):
    """Clock-independent: anything that entered the pipeline before a pause/stop/E-STOP is stale."""
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    old_epoch = ctl.epoch
    ctl.estop()
    ctl.reset_estop()
    await go_live(ctl)
    assert not ctl.submit("eski iş", speech_end=time.monotonic(), origin_epoch=old_epoch)
    assert ctl.history[-1].status == "dropped_epoch"
    assert ctl.submit("yeni iş", speech_end=time.monotonic(), origin_epoch=ctl.epoch)
    assert await wait_for(lambda: [c.text for c in mock_zoom.meeting().captions] == ["yeni iş"])


async def test_test_caption_only_for_test_target(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.send_test("SubTalk TEST — ç ğ ı İ ö ş ü")  # allowed while CONNECTED (not live) for TEST
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == 1)
    ctl.target = Target.EVENT  # simulate the wedding target
    with pytest.raises(CommandError, match="TEST target"):
        ctl.send_test("must never reach the wedding")
    ctl.target = Target.TEST
    ctl.estop()
    with pytest.raises(CommandError):
        ctl.send_test("not while E-STOPPED")


async def test_queued_test_caption_dropped_if_target_changes(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.cfg.min_interval_s = 0.5
    ctl.send_test("one")
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == 1)
    ctl.send_test("two")  # waits for min_interval in the queue
    ctl.target = Target.EVENT  # e.g. operator reconnects to the wedding while a test is queued
    await asyncio.sleep(0.8)
    assert [c.text for c in mock_zoom.meeting().captions] == ["one"]


async def test_event_target_requires_config_and_confirmation(ctl):
    with pytest.raises(CommandError, match="disabled"):
        await ctl.connect(REAL, Target.EVENT, confirm_event=True)
    ctl.cfg.allow_event_target = True
    with pytest.raises(CommandError, match="confirmation"):
        await ctl.connect(REAL, Target.EVENT)
    with pytest.raises(CommandError, match="mock"):
        await ctl.connect(URL, Target.EVENT, confirm_event=True)


async def test_real_zoom_disabled_by_default(ctl):
    with pytest.raises(CommandError, match="Real Zoom output is disabled"):
        await ctl.connect(REAL, Target.TEST)


async def test_auto_pause_after_repeated_zoom_failures(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    mock_zoom.script.extend([403] * 500)
    for i in range(4):
        ctl.submit(f"f{i}", speech_end=time.monotonic())
    assert await wait_for(lambda: ctl.state is OutputState.PAUSED, timeout=5)
    assert ctl.state_reason == "zoom_failures"
    assert any(a[1] == "zoom_auto_paused" for a in ctl.alerts)


async def test_manual_phrase_allowed_when_paused(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    ctl.pause()
    ctl.send_manual("[Müzik]")
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == 1)
    assert mock_zoom.meeting().captions[0].text == "[Müzik]"


async def test_long_caption_chunked_and_ordered(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    text = ("Sevgili kardeşler, bugün burada Galkhuu ile Akane'nin evliliğine tanıklık etmek için "
            "toplandık. Evlilik Yehova'nın bir düzenlemesidir ve sevgi, saygı ve sadakat ister.")
    ctl.submit(text, speech_end=time.monotonic())
    expected = len(chunk_caption(text, 84))
    assert expected >= 2
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == expected, timeout=12)
    caps = mock_zoom.meeting().captions
    sent = [c.text for c in caps]
    assert all(len(s) <= 84 for s in sent)
    # Follow-up parts stay on screen long enough to read (not the 0 s min interval of this fixture).
    for prev, nxt in zip(caps, list(caps)[1:]):
        assert nxt.received_at - prev.received_at >= reading_time_s(prev.text) - 0.05
    assert " ".join(sent) == " ".join(text.split())


async def test_disconnect_while_live_rejected(ctl):
    await ctl.connect(URL, Target.TEST)
    await go_live(ctl)
    with pytest.raises(CommandError):
        await ctl.disconnect()
    ctl.stop()
    await ctl.disconnect()
    assert ctl.state is OutputState.DISCONNECTED


def test_chunker_respects_limits():
    assert chunk_caption("") == []
    assert chunk_caption("kısa") == ["kısa"]
    long = "kelime " * 40
    chunks = chunk_caption(long, 30)
    assert all(len(c) <= 30 for c in chunks)
    assert " ".join(chunks) == " ".join(long.split())


def test_two_line_balanced():
    out = two_line("Sevgili misafirler hoş geldiniz bugün çok mutluyuz", 30)
    a, b = out.split("\n")
    assert len(a) <= 30 and len(b) <= 30
