"""Local mirror of the last Zoom-confirmed caption sequence number.

Zoom's ``GET /closedcaption/seq`` is authoritative. This file is only the
fallback used when that endpoint cannot be reached while (re)attaching.
Keyed by the caption URL fingerprint so TEST and EVENT meetings never mix.
"""
from __future__ import annotations

import time
from pathlib import Path

from subtalk.util.atomic import read_json, write_json_atomic


class SeqStore:
    def __init__(self, path: Path | None):
        self._path = Path(path) if path else None
        self._data: dict[str, dict] = {}
        if self._path:
            self._data = read_json(self._path, default={}) or {}

    def get(self, fingerprint: str) -> int | None:
        entry = self._data.get(fingerprint)
        return int(entry["last_confirmed"]) if entry else None

    def put(self, fingerprint: str, last_confirmed: int) -> None:
        self._data[fingerprint] = {"last_confirmed": int(last_confirmed), "updated": time.time()}
        if self._path:
            write_json_atomic(self._path, self._data)
