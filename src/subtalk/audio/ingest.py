"""Audio ingest: validates sender frames, measures level, detects loss,
flatline and gaps, and feeds the segmenter. Bounded memory throughout."""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

import numpy as np

from subtalk.audio.protocol import SAMPLE_RATE, FrameError, unpack_frame
from subtalk.audio.segmenter import Segmenter
from subtalk.audio.vad import dbfs
from subtalk.config import AudioCfg


class AudioIngest:
    def __init__(self, cfg: AudioCfg, segmenter: Segmenter, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.seg = segmenter
        self._clock = clock
        self.connected = False
        self.sender: dict = {}
        self.frames = 0
        self.bad_frames = 0
        self.gaps = 0
        self.last_seq: int | None = None
        self.last_frame_at: float | None = None
        self._level = deque(maxlen=25)  # ~0.5 s of 20 ms frames
        self._peak = deque(maxlen=25)
        self._nonzero_at: float | None = None
        self.connections = 0
        self.music_mode = False

    def on_connect(self, info: dict) -> None:
        self.connected = True
        self.connections += 1
        self.sender = info
        self.last_seq = None
        self._nonzero_at = self._clock()

    def on_disconnect(self) -> None:
        self.connected = False
        self.seg.flush()

    def handle(self, data: bytes) -> None:
        now = self._clock()
        try:
            seq, sr, _cap, samples = unpack_frame(data)
        except FrameError:
            self.bad_frames += 1
            return
        if sr != SAMPLE_RATE:
            self.bad_frames += 1
            return
        if self.last_seq is not None and seq != (self.last_seq + 1) & 0xFFFFFFFF:
            self.gaps += 1
            self.seg.flush()  # never stitch audio across a gap
        self.last_seq = seq
        self.frames += 1
        self.last_frame_at = now
        if np.any(samples):
            self._nonzero_at = now
        self._level.append(dbfs(samples))
        self._peak.append(float(np.max(np.abs(samples))) if len(samples) else 0.0)
        self.seg.feed(samples, now)

    # ------------------------------------------------------------ health
    def status(self) -> dict:
        now = self._clock()
        age = None if self.last_frame_at is None else now - self.last_frame_at
        lost = (not self.connected) or age is None or age > self.cfg.lost_after_s
        flat = (self._nonzero_at is not None and now - self._nonzero_at > self.cfg.flatline_after_s)
        level = max(self._level) if self._level else -120.0
        peak = max(self._peak) if self._peak else 0.0
        state = "lost" if lost else "flatline" if flat else "ok"
        return {
            "state": state,
            "connected": self.connected,
            "last_frame_age_s": None if age is None else round(age, 2),
            "level_dbfs": round(level, 1),
            "peak": round(20 * np.log10(peak), 1) if peak > 1e-6 else -120.0,
            "clipping": peak >= 0.99,
            "frames": self.frames,
            "gaps": self.gaps,
            "bad_frames": self.bad_frames,
            "connections": self.connections,
            "sender": self.sender,
            "vad_prob": round(self.seg.last_prob, 2),
            "segments": self.seg.segments_emitted,
            "forced_cuts": self.seg.forced_cuts,
        }
