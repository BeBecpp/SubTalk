"""Audio frame wire format (sender -> engine), binary WebSocket messages.

    header: <4s I I Q  = magic b"STA1", seq (uint32), sample_rate (uint32),
                         capture time in ms since sender start (uint64)
    payload: PCM 16-bit little-endian mono samples

The standalone sender (sender/subtalk_sender.py) duplicates these constants;
tests/test_audio.py checks both stay identical.
"""
from __future__ import annotations

import struct

import numpy as np

MAGIC = b"STA1"
HEADER = struct.Struct("<4sIIQ")
SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320
MAX_PAYLOAD_SAMPLES = SAMPLE_RATE  # reject frames longer than 1 s


class FrameError(ValueError):
    pass


def pack_frame(seq: int, pcm16: bytes, sample_rate: int = SAMPLE_RATE, capture_ms: int = 0) -> bytes:
    return HEADER.pack(MAGIC, seq & 0xFFFFFFFF, sample_rate, capture_ms) + pcm16


def unpack_frame(data: bytes) -> tuple[int, int, int, np.ndarray]:
    if len(data) < HEADER.size:
        raise FrameError("frame too short")
    magic, seq, sr, cap = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise FrameError("bad magic")
    payload = data[HEADER.size:]
    if len(payload) % 2 or len(payload) // 2 > MAX_PAYLOAD_SAMPLES:
        raise FrameError("bad payload length")
    samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    return seq, sr, cap, samples
