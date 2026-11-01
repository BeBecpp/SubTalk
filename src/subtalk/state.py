"""Shared state enums. Engine health, caption output and context are
independent dimensions - changing one never changes another implicitly."""
from __future__ import annotations

from enum import Enum


class EngineState(str, Enum):
    OFF = "OFF"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    FAULT = "FAULT"


class OutputState(str, Enum):
    DISCONNECTED = "DISCONNECTED"  # no caption URL attached
    CONNECTED = "CONNECTED"        # URL attached, nothing is sent
    LIVE = "LIVE"                  # automatic captions are sent
    PAUSED = "PAUSED"              # new automatic captions are dropped (never queued)
    E_STOPPED = "E_STOPPED"        # latched; requires explicit reset


class Target(str, Enum):
    TEST = "TEST"    # mock endpoint or a separate Zoom test meeting
    EVENT = "EVENT"  # the real wedding meeting


class JaPolicy(str, Enum):
    AUTO = "AUTO"                # match against recent Mongolian; caption only new content
    CAPTION_ALL = "CAPTION_ALL"  # treat every Japanese segment as independent speech
    SUPPRESS = "SUPPRESS"        # never caption Japanese


class Role(str, Enum):
    OPERATOR = "operator"
    SPEAKER = "speaker"
