"""Streaming utterance segmenter.

Speech starts after 3 consecutive speech frames (~96 ms) and includes a
pre-roll. It ends after ``min_silence_ms`` of silence (plus post-roll). A
segment reaching ``max_segment_s`` is force-cut at the quietest frame in its
last 2 s so words are rarely split; the remainder starts the next segment.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from subtalk.audio.vad import FRAME, SpeechDetector, dbfs
from subtalk.config import VadCfg

SR = 16000
FRAME_S = FRAME / SR


@dataclass
class AudioSegment:
    audio: np.ndarray  # float32 mono 16 kHz
    t_start: float     # monotonic
    t_end: float       # monotonic (end of last speech frame)
    forced_cut: bool = False
    t_closed: float = 0.0  # monotonic, when the VAD closed the segment (t_closed - t_end = end-of-speech wait)

    @property
    def duration(self) -> float:
        return len(self.audio) / SR


class Segmenter:
    def __init__(self, detector: SpeechDetector, cfg: VadCfg, on_segment: Callable[[AudioSegment], None]):
        self.det = detector
        self.cfg = cfg
        self.on_segment = on_segment
        self._buf = np.zeros(0, dtype=np.float32)
        self._pre: deque[tuple[np.ndarray, float]] = deque(maxlen=max(1, int(cfg.preroll_ms / 1000 / FRAME_S)))
        self._frames: list[tuple[np.ndarray, float, float]] = []  # (frame, time_end, prob)
        self._in_speech = False
        self._onset = 0
        self._silence = 0
        self._post = max(0, int(cfg.postroll_ms / 1000 / FRAME_S))
        self._min_sil = max(1, int(cfg.min_silence_ms / 1000 / FRAME_S))
        self._max_frames = int(cfg.max_segment_s / FRAME_S)
        self.last_prob = 0.0
        self.segments_emitted = 0
        self.forced_cuts = 0

    def feed(self, samples: np.ndarray, arrival: float) -> None:
        """``arrival`` is the monotonic time the last sample arrived."""
        self._buf = np.concatenate([self._buf, samples.astype(np.float32)])
        n_frames = len(self._buf) // FRAME
        for k in range(n_frames):
            frame = self._buf[k * FRAME:(k + 1) * FRAME]
            t_end = arrival - (len(self._buf) - (k + 1) * FRAME) / SR
            self._process(frame, t_end)
        self._buf = self._buf[n_frames * FRAME:]

    def _process(self, frame: np.ndarray, t_end: float) -> None:
        p = self.det.prob(frame)
        self.last_prob = p
        if not self._in_speech:
            self._pre.append((frame, t_end))
            self._onset = self._onset + 1 if p >= self.cfg.on_threshold else 0
            if self._onset >= 3:
                self._in_speech = True
                self._silence = 0
                self._frames = [(f, t, 1.0) for f, t in self._pre]
                self._pre.clear()
            return
        self._frames.append((frame, t_end, p))
        if p < self.cfg.off_threshold:
            self._silence += 1
            if self._silence >= self._min_sil:
                keep = len(self._frames) - self._silence + self._post
                self._emit(self._frames[:max(1, keep)], forced=False)
                self._frames = []
                self._in_speech = False
                self._onset = 0
                return
        else:
            self._silence = 0
        if len(self._frames) >= self._max_frames:
            self._force_cut()

    def _force_cut(self) -> None:
        window = int(2.0 / FRAME_S)
        lo = max(int(self.cfg.min_segment_s / FRAME_S), len(self._frames) - window)
        energies = [dbfs(f) for f, _, _ in self._frames[lo:]]
        cut = lo + int(np.argmin(energies)) + 1 if energies else len(self._frames)
        self.forced_cuts += 1
        self._emit(self._frames[:cut], forced=True)
        self._frames = self._frames[cut:]
        self._silence = 0

    def _emit(self, frames, forced: bool) -> None:
        if not frames:
            return
        audio = np.concatenate([f for f, _, _ in frames])
        dur = len(audio) / SR
        if dur < self.cfg.min_segment_s:
            return
        speech_end = max((t for _, t, p in frames if p >= self.cfg.off_threshold), default=frames[-1][1])
        seg = AudioSegment(audio=audio, t_start=frames[0][1] - FRAME_S, t_end=speech_end, forced_cut=forced,
                           t_closed=time.monotonic())
        self.segments_emitted += 1
        self.on_segment(seg)

    def flush(self) -> None:
        """End any open segment now (audio discontinuity / disconnect)."""
        if self._in_speech and self._frames:
            self._emit(self._frames, forced=True)
        self._frames = []
        self._in_speech = False
        self._onset = 0
        self._pre.clear()
        self._buf = np.zeros(0, dtype=np.float32)
        self.det.reset()
