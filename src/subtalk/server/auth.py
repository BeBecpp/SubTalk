"""Authentication without a database.

* Passcodes per role are stored as scrypt hashes in secrets/auth.json
  (gitignored), set with ``subtalk set-passcode``.
* Sessions are HMAC-signed cookies keyed by a secret generated at process
  start (the boot ID). An engine restart therefore invalidates every browser
  session - old browser state is never silently trusted.
* One operator holds the control lease at a time; other operator browsers
  are read-only until they explicitly take control.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from subtalk.state import Role
from subtalk.util.atomic import read_json, write_json_atomic

SCRYPT = {"n": 2**14, "r": 8, "p": 1}
DEV_PASSCODES = {"operator": "dev-operator", "speaker": "dev-speaker"}


def hash_passcode(passcode: str, salt: bytes | None = None) -> dict:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(passcode.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)
    return {"salt": salt.hex(), "hash": digest.hex(), **SCRYPT}


def verify_passcode(passcode: str, record: dict) -> bool:
    digest = hashlib.scrypt(passcode.encode("utf-8"), salt=bytes.fromhex(record["salt"]), dklen=32,
                            n=record["n"], r=record["r"], p=record["p"])
    return hmac.compare_digest(digest.hex(), record["hash"])


class PasscodeStore:
    def __init__(self, path: Path, allow_dev_defaults: bool):
        self.path = Path(path)
        self.records: dict = read_json(self.path, default={}) or {}
        self.dev_defaults = False
        if allow_dev_defaults:
            for role, code in DEV_PASSCODES.items():
                if role not in self.records:
                    self.records[role] = hash_passcode(code)
                    self.dev_defaults = True

    def configured(self, role: str) -> bool:
        return role in self.records

    def check(self, role: str, passcode: str) -> bool:
        rec = self.records.get(role)
        return bool(rec and passcode and verify_passcode(passcode, rec))

    def set(self, role: str, passcode: str) -> None:
        if len(passcode) < 8:
            raise ValueError("Passcode must be at least 8 characters")
        data = read_json(self.path, default={}) or {}
        data[role] = hash_passcode(passcode)
        write_json_atomic(self.path, data)
        self.records[role] = data[role]


@dataclass
class Session:
    sid: str
    role: Role
    name: str
    created: float
    expires: float
    last_seen: float = field(default_factory=time.monotonic)
    connections: int = 0
    disconnected_at: float | None = None


class SessionManager:
    COOKIE = "subtalk_session"

    def __init__(self, hours: float = 16.0):
        self.boot_id = secrets.token_hex(6)
        self._key = secrets.token_bytes(32)
        self.hours = hours
        self.sessions: dict[str, Session] = {}

    def create(self, role: Role, name: str) -> tuple[Session, str]:
        now = time.time()
        s = Session(sid=secrets.token_hex(8), role=role, name=(name or role.value)[:40], created=now,
                    expires=now + self.hours * 3600)
        self.sessions[s.sid] = s
        return s, self._sign({"sid": s.sid, "role": role.value, "boot": self.boot_id, "exp": s.expires})

    def _sign(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        mac = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def verify(self, token: str | None) -> Session | None:
        if not token or "." not in token:
            return None
        body, mac = token.rsplit(".", 1)
        expected = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None  # includes every cookie issued before a restart
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("boot") != self.boot_id or payload.get("exp", 0) < time.time():
            return None
        s = self.sessions.get(payload.get("sid"))
        if s:
            s.last_seen = time.monotonic()
        return s

    def revoke(self, sid: str) -> None:
        self.sessions.pop(sid, None)


class LoginLimiter:
    def __init__(self, attempts: int = 8, window_s: float = 300.0):
        self.attempts, self.window = attempts, window_s
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = hits
        return len(hits) < self.attempts

    def fail(self, key: str) -> None:
        self._hits.setdefault(key, []).append(time.monotonic())


class ControlLease:
    """Single operator control lease. Released automatically when the holder's
    last dashboard connection has been gone for ``grace_s``."""

    def __init__(self, sessions: SessionManager, grace_s: float = 15.0):
        self.sessions = sessions
        self.grace = grace_s
        self.holder: str | None = None
        self.since: float | None = None

    def _holder_alive(self) -> bool:
        s = self.sessions.sessions.get(self.holder) if self.holder else None
        if s is None:
            return False
        if s.connections > 0:
            return True
        last = max(s.last_seen, s.disconnected_at or 0.0)  # recent dashboard or API activity
        return time.monotonic() - last < self.grace

    def ensure(self, session: Session) -> bool:
        """True if ``session`` holds (or just acquired) the lease."""
        if self.holder == session.sid:
            return True
        if not self._holder_alive():
            self.holder, self.since = session.sid, time.time()
            return True
        return False

    def take(self, session: Session) -> None:
        self.holder, self.since = session.sid, time.time()

    def info(self) -> dict:
        s = self.sessions.sessions.get(self.holder) if self.holder and self._holder_alive() else None
        return {"holder": s.name if s else None, "holder_sid": s.sid if s else None, "since": self.since if s else None}
