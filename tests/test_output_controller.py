import asyncio
import time

import pytest

from subtalk.output.chunker import chunk_caption, two_line
from subtalk.output.controller import CommandError, OutputConfig, OutputController
from subtalk.state import OutputState, Target
from subtalk.zoom.sender import ZoomCaptionSender
from subtalk.zoom.seqstore import SeqStore

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
    ctl.start_live()
    for i in range(3):
        assert ctl.submit(f"altyazı {i}", speech_end=time.monotonic())
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == 3)
    assert [c.text for c in mock_zoom.meeting().captions] == ["altyazı 0", "altyazı 1", "altyazı 2"]
    assert await wait_for(lambda: ctl.on_air and ctl.on_air.text == "altyazı 2")
    assert [r.seq for r in ctl.history if r.status == "sent"] == [1, 2, 3]


async def test_pause_drops_instead_of_queueing(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
    ctl.pause()
    for i in range(20):
        assert not ctl.submit(f"duraklatıldı {i}", speech_end=time.monotonic())
    assert ctl.queue_len == 0
    ctl.resume()
    await asyncio.sleep(0.1)
    assert mock_zoom.post_count == 0


async def test_speech_from_before_resume_is_rejected(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
    ctl.pause()
    spoken_while_paused = time.monotonic()
    await asyncio.sleep(0.01)
    ctl.resume()
    assert not ctl.submit("eski konuşma", speech_end=spoken_while_paused)
    assert ctl.history[-1].status == "dropped_before_live"


async def test_stale_caption_never_sent(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
    ctl.cfg.caption_ttl_s = 0.05
    spoken = time.monotonic()
    await asyncio.sleep(0.1)  # translation took longer than the TTL
    assert not ctl.submit("çok eski", speech_end=spoken)
    assert ctl.history[-1].status == "dropped_stale"


async def test_queued_caption_expiring_is_dropped_at_gate(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
    ctl.cfg.caption_ttl_s = 0.2
    ctl.cfg.min_interval_s = 0.5  # the second caption must wait past its deadline
    ctl.submit("bir", speech_end=time.monotonic())
    ctl.submit("iki", speech_end=time.monotonic())
    assert await wait_for(lambda: any(r.status == "dropped_stale" for r in ctl.history), timeout=2)
    assert [c.text for c in mock_zoom.meeting().captions] == ["bir"]


async def test_estop_is_immediate_and_latched(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
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
    ctl.start_live()
    t_before = time.monotonic()
    ctl.estop()
    ctl.reset_estop()
    ctl.start_live()
    assert not ctl.submit("E-STOP öncesi", speech_end=t_before)


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
    ctl.start_live()
    mock_zoom.script.extend([403] * 500)
    for i in range(4):
        ctl.submit(f"f{i}", speech_end=time.monotonic())
    assert await wait_for(lambda: ctl.state is OutputState.PAUSED, timeout=5)
    assert ctl.state_reason == "zoom_failures"
    assert any(a[1] == "zoom_auto_paused" for a in ctl.alerts)


async def test_manual_phrase_allowed_when_paused(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
    ctl.pause()
    ctl.send_manual("[Müzik]")
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == 1)
    assert mock_zoom.meeting().captions[0].text == "[Müzik]"


async def test_long_caption_chunked_and_ordered(ctl, mock_zoom):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
    text = ("Sevgili kardeşler, bugün burada Galkhuu ile Akane'nin evliliğine tanıklık etmek için "
            "toplandık. Evlilik Yehova'nın bir düzenlemesidir ve sevgi, saygı ve sadakat ister.")
    ctl.submit(text, speech_end=time.monotonic())
    expected = len(chunk_caption(text, 84))
    assert expected >= 2
    assert await wait_for(lambda: len(mock_zoom.meeting().captions) == expected)
    sent = [c.text for c in mock_zoom.meeting().captions]
    assert all(len(s) <= 84 for s in sent)
    assert " ".join(sent) == " ".join(text.split())


async def test_disconnect_while_live_rejected(ctl):
    await ctl.connect(URL, Target.TEST)
    ctl.start_live()
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
