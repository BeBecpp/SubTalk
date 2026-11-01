import asyncio

import numpy as np
import pytest

import subtalk_sender as sender
from subtalk.asr.langid import decide
from subtalk.asr.worker import AsrUnavailable, AsrWorkerClient
from subtalk.audio import protocol
from subtalk.audio.ingest import AudioIngest
from subtalk.audio.segmenter import Segmenter
from subtalk.audio.vad import EnergyDetector
from subtalk.config import AsrCfg, AudioCfg, LangIdCfg, VadCfg

SR = 16000


def tone(seconds, amp=0.3, freq=220.0):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds, amp=0.002, seed=0):
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR)) * amp).astype(np.float32)


def test_sender_protocol_matches_engine():
    assert sender.MAGIC == protocol.MAGIC
    assert sender.HEADER.format == protocol.HEADER.format
    assert sender.SAMPLE_RATE == protocol.SAMPLE_RATE
    assert sender.FRAME_SAMPLES == protocol.FRAME_SAMPLES
    pcm = (np.arange(320, dtype=np.int16) - 160).tobytes()
    seq, sr, cap, samples = protocol.unpack_frame(sender.pack_frame(7, pcm, 16000, 1234))
    assert (seq, sr, cap, len(samples)) == (7, 16000, 1234, 320)


def test_protocol_rejects_garbage():
    with pytest.raises(protocol.FrameError):
        protocol.unpack_frame(b"nope")
    with pytest.raises(protocol.FrameError):
        protocol.unpack_frame(b"XXXX" + bytes(16) + bytes(4))


@pytest.mark.parametrize("rate", [48000, 44100, 32000, 16000])
def test_resampler_preserves_tone_and_length(rate):
    r = sender.Resampler(rate)
    t = np.arange(rate * 2) / rate
    x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    out = np.concatenate([r.process(x[i:i + 960]) for i in range(0, len(x), 960)])  # streaming blocks
    assert abs(len(out) - 2 * SR) <= 3
    spec = np.abs(np.fft.rfft(out[2000:2000 + SR]))
    assert abs(np.argmax(spec) - 440) <= 2  # 1 Hz bins over 1 s


def test_downmix_and_channel_select():
    block = np.array([[0.2, 0.4], [0.6, 0.8]], dtype=np.float32)
    assert np.allclose(sender.to_mono(block, None), [0.3, 0.7])
    assert np.allclose(sender.to_mono(block, 1), [0.4, 0.8])


def make_segmenter(**kw):
    segs = []
    cfg = VadCfg(engine="energy", **kw)
    return Segmenter(EnergyDetector(cfg.energy_margin_db), cfg, segs.append), segs


def feed(seg, audio, t0=100.0):
    for i in range(0, len(audio), 320):
        seg.feed(audio[i:i + 320], t0 + (i + 320) / SR)


def test_segmenter_splits_utterances_on_silence():
    seg, out = make_segmenter()
    audio = np.concatenate([noise(1.0), tone(1.5), noise(1.0, seed=1), tone(2.0), noise(1.2, seed=2)])
    feed(seg, audio)
    assert len(out) == 2
    assert 1.4 <= out[0].duration <= 2.4 and 1.9 <= out[1].duration <= 2.9
    assert out[0].t_end < out[1].t_start
    assert not out[0].forced_cut


def test_segmenter_ignores_clicks_and_silence():
    seg, out = make_segmenter()
    audio = noise(3.0)
    audio[16000:16100] = 0.5  # 6 ms click
    feed(seg, audio)
    assert out == []


def test_segmenter_force_cuts_long_speech():
    seg, out = make_segmenter(max_segment_s=4.0)
    speech = np.concatenate([tone(3.0), tone(0.05, amp=0.02), tone(3.0)])  # a quiet dip mid-way
    feed(seg, np.concatenate([noise(0.5), speech, noise(1.0, seed=3)]))
    assert len(out) >= 2 and out[0].forced_cut
    assert all(s.duration <= 4.1 for s in out)


def test_segmenter_flush_on_gap():
    seg, out = make_segmenter()
    feed(seg, np.concatenate([noise(0.5), tone(1.5)]))
    seg.flush()
    assert len(out) == 1


def frames(audio, start_seq=0):
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    return [protocol.pack_frame(start_seq + k, pcm[i:i + 320].tobytes())
            for k, i in enumerate(range(0, len(pcm), 320))]


def test_ingest_health_and_gaps():
    t = [0.0]
    seg, out = make_segmenter()
    ing = AudioIngest(AudioCfg(), seg, clock=lambda: t[0])
    assert ing.status()["state"] == "lost"
    ing.on_connect({"device": "Mixer"})
    fs = frames(np.concatenate([noise(0.5), tone(1.5), noise(1.0, seed=4)]))
    for f in fs[:60]:
        t[0] += 0.02
        ing.handle(f)
    st = ing.status()
    assert st["state"] == "ok" and st["level_dbfs"] > -20
    for f in fs[61:]:  # one frame missing -> gap
        t[0] += 0.02
        ing.handle(f)
    assert ing.status()["gaps"] == 1
    ing.handle(b"garbage")
    assert ing.status()["bad_frames"] == 1
    t[0] += 3
    assert ing.status()["state"] == "lost"


def test_ingest_flatline_detected():
    t = [0.0]
    seg, _ = make_segmenter()
    ing = AudioIngest(AudioCfg(), seg, clock=lambda: t[0])
    ing.on_connect({})
    for f in frames(np.zeros(SR * 3, dtype=np.float32)):
        t[0] += 0.02
        ing.handle(f)
    assert ing.status()["state"] == "flatline"


SCRIPT = [{"lang": "mn", "text": "Сайн байна уу"}, {"lang": "ja", "text": "こんにちは"}, {"lang": "music"}]


async def test_worker_process_scripted_engine():
    client = AsrWorkerClient("scripted", AsrCfg(), LangIdCfg(), script=SCRIPT)
    await client.start()
    try:
        ev, ms = await client.transcribe(tone(2.0))
        assert decide(ev, LangIdCfg()).kind == "MN"
        ev, _ = await client.transcribe(tone(2.0))
        assert decide(ev, LangIdCfg()).kind == "JA"
        ev, _ = await client.transcribe(tone(2.0))
        assert decide(ev, LangIdCfg()).kind == "NONSPEECH"
        assert client.status()["state"] == "ready"
    finally:
        await client.stop()


async def test_worker_crash_is_isolated_and_restarted():
    client = AsrWorkerClient("crash_test", AsrCfg(fallback_model=None), LangIdCfg())
    await client.start()
    try:
        with pytest.raises(AsrUnavailable):
            await client.transcribe(np.zeros(1600, dtype=np.float32))  # triggers os._exit in the child
        for _ in range(200):
            if client.state == "ready":
                break
            await asyncio.sleep(0.05)
        assert client.state == "ready" and client.restarts == 1
        ev, _ = await client.transcribe(tone(1.5))
        assert ev.p_mn > 0.5
    finally:
        await client.stop()
