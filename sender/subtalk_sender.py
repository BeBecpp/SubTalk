"""SubTalk audio sender for Computer A (Zoom host).

Reads the mixer input READ-ONLY in Windows shared mode (WASAPI) - the same
device Zoom uses keeps working; nothing is routed, played or changed. Audio
is downmixed to mono, resampled to 16 kHz and streamed to the SubTalk engine
as 20 ms PCM frames over a WebSocket. Reconnects automatically; audio older
than ~2 s is dropped instead of being replayed.

Usage:
    python subtalk_sender.py --list-devices
    python subtalk_sender.py --engine ws://192.168.1.50:8000/ws/audio --device "Mixer" --token XXXX
"""
from __future__ import annotations

import argparse
import asyncio
import os
import queue
import struct
import sys
import time

import numpy as np

# Keep identical to subtalk/audio/protocol.py (checked by tests).
MAGIC = b"STA1"
HEADER = struct.Struct("<4sIIQ")
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms
MAX_BUFFER_FRAMES = 100  # 2 s


def pack_frame(seq: int, pcm16: bytes, sample_rate: int = SAMPLE_RATE, capture_ms: int = 0) -> bytes:
    return HEADER.pack(MAGIC, seq & 0xFFFFFFFF, sample_rate, capture_ms) + pcm16


def _lowpass_taps(cutoff: float, fs: float, n: int = 63) -> np.ndarray:
    m = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / fs * m) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


class Resampler:
    """Streaming resampler to 16 kHz: integer decimation (48k, 32k) or
    low-pass + linear interpolation for other rates (44.1k)."""

    def __init__(self, in_rate: int, out_rate: int = SAMPLE_RATE):
        self.in_rate, self.out_rate = in_rate, out_rate
        self.taps = _lowpass_taps(0.45 * out_rate, in_rate) if in_rate > out_rate else None
        self._hist = np.zeros(len(self.taps) - 1 if self.taps is not None else 0, dtype=np.float32)
        self._phase = 0.0
        self._last = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        x = x.astype(np.float32)
        if self.in_rate == self.out_rate:
            return x
        if self.taps is not None:
            buf = np.concatenate([self._hist, x])
            y = np.convolve(buf, self.taps, mode="valid")
            self._hist = buf[-(len(self.taps) - 1):]
        else:
            y = x
        ratio = self.in_rate / self.out_rate
        if ratio.is_integer():
            r = int(ratio)
            start = int(self._phase)
            out = y[start::r]
            self._phase = (start - len(y)) % r
            return out
        # fractional: linear interpolation on the filtered signal
        positions = np.arange(self._phase, len(y), ratio)
        self._phase = positions[-1] + ratio - len(y) if len(positions) else self._phase - len(y)
        ext = np.concatenate([[self._last], y])
        out = np.interp(positions + 1, np.arange(len(ext)), ext).astype(np.float32)
        if len(y):
            self._last = y[-1]
        return out


def to_mono(block: np.ndarray, channel: int | None) -> np.ndarray:
    if block.ndim == 1:
        return block
    if channel is not None:
        return block[:, channel]
    return block.mean(axis=1)


def list_devices() -> None:
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = hostapis[d["hostapi"]]["name"]
            print(f"[{i:2d}] {d['name']}  ({api}, {d['max_input_channels']} ch, {int(d['default_samplerate'])} Hz)")


def find_device(name_or_index: str | None):
    import sounddevice as sd

    if name_or_index is None:
        return None
    if name_or_index.isdigit():
        return int(name_or_index)
    hostapis = sd.query_hostapis()
    matches = [
        (i, d) for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0 and name_or_index.lower() in d["name"].lower()
    ]
    # Prefer the WASAPI entry (shared mode) when the same device appears under several APIs.
    matches.sort(key=lambda m: "WASAPI" not in hostapis[m[1]["hostapi"]]["name"])
    if not matches:
        raise SystemExit(f"No input device matching {name_or_index!r}. Use --list-devices.")
    return matches[0][0]


class Capture:
    """Shared-mode capture into a bounded queue (oldest audio dropped when full)."""

    def __init__(self, device, channel: int | None):
        import sounddevice as sd

        info = sd.query_devices(device, "input")
        self.rate = int(info["default_samplerate"])
        self.channels = min(2, int(info["max_input_channels"]))
        self.channel = channel
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self.dropped = 0
        extra = None
        if "WASAPI" in sd.query_hostapis(info["hostapi"])["name"]:
            extra = sd.WasapiSettings(exclusive=False)  # never take exclusive control
        self.stream = sd.InputStream(
            device=device, samplerate=self.rate, channels=self.channels, dtype="float32",
            blocksize=int(self.rate * 0.02), extra_settings=extra, callback=self._cb,
        )
        self.name = info["name"]

    def _cb(self, indata, frames, t, status):
        try:
            self.q.put_nowait(indata.copy())
        except queue.Full:
            try:
                self.q.get_nowait()
                self.dropped += 1
                self.q.put_nowait(indata.copy())
            except queue.Empty:
                pass


async def stream(args) -> None:
    import websockets

    device = find_device(args.device)
    cap = Capture(device, args.channel)
    res = Resampler(cap.rate)
    print(f"Capturing '{cap.name}' at {cap.rate} Hz, {cap.channels} ch (shared mode) -> {args.engine}")
    cap.stream.start()
    seq = 0
    t0 = time.monotonic()
    pending = np.zeros(0, dtype=np.float32)
    backoff = 0.5
    headers = {"X-SubTalk-Token": args.token or "", "X-SubTalk-Device": cap.name[:80]}
    while True:
        try:
            try:
                ws_cm = websockets.connect(args.engine, additional_headers=headers, max_size=2**20,
                                           ping_interval=5, ping_timeout=5)
            except TypeError:  # older websockets
                ws_cm = websockets.connect(args.engine, extra_headers=headers, max_size=2**20)
            async with ws_cm as ws:
                print("Connected.")
                backoff = 0.5
                # Drop anything captured while disconnected - never replay stale speech.
                while not cap.q.empty():
                    cap.q.get_nowait()
                pending = np.zeros(0, dtype=np.float32)
                while True:
                    block = await asyncio.to_thread(cap.q.get, True, 1.0)
                    mono = res.process(to_mono(block, cap.channel))
                    pending = np.concatenate([pending, mono])
                    while len(pending) >= FRAME_SAMPLES:
                        frame, pending = pending[:FRAME_SAMPLES], pending[FRAME_SAMPLES:]
                        pcm = (np.clip(frame, -1, 1) * 32767).astype("<i2").tobytes()
                        await ws.send(pack_frame(seq, pcm, SAMPLE_RATE, int((time.monotonic() - t0) * 1000)))
                        seq += 1
        except queue.Empty:
            print("No audio from the device for 1 s - check the mixer input.", file=sys.stderr)
        except (OSError, Exception) as exc:  # noqa: BLE001 - keep running whatever happens
            print(f"Disconnected ({type(exc).__name__}: {exc}); retrying in {backoff:.1f}s", file=sys.stderr)
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 2)


def main() -> None:
    ap = argparse.ArgumentParser(description="SubTalk audio sender (read-only shared-mode capture)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--engine", default=os.environ.get("SUBTALK_ENGINE_WS", "ws://127.0.0.1:8000/ws/audio"))
    ap.add_argument("--device", default=os.environ.get("SUBTALK_AUDIO_DEVICE"))
    ap.add_argument("--channel", type=int, default=None, help="use one input channel instead of a downmix")
    ap.add_argument("--token", default=os.environ.get("SUBTALK_AUDIO_TOKEN"))
    args = ap.parse_args()
    if args.list_devices:
        list_devices()
        return
    try:
        asyncio.run(stream(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
