"""Frame-level speech probability (512 samples = 32 ms at 16 kHz).

* EnergyDetector - numpy only, adaptive noise floor. Dev/tests and fallback.
* SileroDetector - Silero VAD v5 ONNX via onnxruntime (production). The
  model's inputs are checked at load time; an unexpected model fails loudly
  instead of silently mis-detecting.
"""
from __future__ import annotations

import glob
import os
from typing import Protocol

import numpy as np

FRAME = 512


def dbfs(x: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0
    return 20 * np.log10(rms) if rms > 1e-9 else -120.0


class SpeechDetector(Protocol):
    name: str

    def prob(self, frame: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class EnergyDetector:
    name = "energy"

    def __init__(self, margin_db: float = 12.0, floor_init: float = -60.0):
        self.margin = margin_db
        self.floor_init = floor_init
        self.floor = floor_init

    def reset(self) -> None:
        self.floor = self.floor_init

    def prob(self, frame: np.ndarray) -> float:
        level = dbfs(frame)
        # Noise floor follows quiet frames quickly and loud frames very slowly.
        if level < self.floor:
            self.floor = 0.7 * self.floor + 0.3 * level
        else:
            self.floor = min(self.floor + 0.02, level)
        excess = level - (self.floor + self.margin)
        return float(1 / (1 + np.exp(-excess / 2.0)))


class SileroDetector:
    name = "silero"
    CONTEXT = 64  # samples of the previous chunk prepended (Silero v5, 16 kHz)

    def __init__(self, model_path: str | None = None):
        import onnxruntime as ort

        path = model_path or find_silero_model()
        if not path or not os.path.exists(path):
            raise FileNotFoundError("Silero VAD ONNX model not found (set vad.silero_model_path)")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        names = {i.name for i in self.session.get_inputs()}
        if names != {"input", "state", "sr"}:
            raise RuntimeError(f"Unsupported Silero model inputs {sorted(names)} (expected input/state/sr, v5)")
        self.path = path
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.astype(np.float32)])[None, :]
        out, self._state = self.session.run(
            None, {"input": x, "state": self._state, "sr": np.array(16000, dtype=np.int64)}
        )
        self._context = x[0, -self.CONTEXT:]
        return float(out.reshape(-1)[0])


def find_silero_model() -> str | None:
    """Look for silero_vad*.onnx shipped with faster-whisper, then ./models."""
    candidates = []
    try:
        import faster_whisper

        base = os.path.join(os.path.dirname(faster_whisper.__file__), "assets")
        candidates += sorted(glob.glob(os.path.join(base, "silero*.onnx")))
    except ImportError:
        pass
    candidates += sorted(glob.glob(os.path.join("models", "silero*.onnx")))
    return candidates[0] if candidates else None


def make_detector(engine: str, model_path: str | None, margin_db: float) -> SpeechDetector:
    if engine == "silero":
        return SileroDetector(model_path)
    return EnergyDetector(margin_db)
