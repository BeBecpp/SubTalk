"""Zoom caption URL (API token) parsing and validation.

The caption URL is a credential: anyone holding it can post captions into the
meeting. It is never logged; ``repr`` and ``redacted()`` only expose the host
and a short fingerprint.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Zoom answers 403 when any of these is missing (official support article).
REQUIRED_PARAMS = ("id", "ns", "expire", "signature")
# Parameters SubTalk adds itself; any copy pasted by the operator is dropped.
STRIPPED_PARAMS = ("seq", "lang")
MOCK_HOSTS = ("127.0.0.1", "localhost")


class CaptionUrlError(ValueError):
    pass


@dataclass(frozen=True)
class CaptionUrl:
    base: str = field(repr=False)  # full URL without seq/lang - secret
    host: str
    fingerprint: str
    is_mock: bool

    def post_url(self, seq: int) -> str:
        return f"{self.base}&seq={int(seq)}"

    def seq_url(self) -> str:
        parts = urlsplit(self.base)
        path = parts.path.rstrip("/") + "/seq"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def redacted(self) -> str:
        return f"{self.host} [{self.fingerprint[:8]}]"

    def __str__(self) -> str:  # never leak the token through formatting
        return self.redacted()


def parse_caption_url(raw: str, *, allow_real_zoom: bool, allow_mock: bool) -> CaptionUrl:
    raw = (raw or "").strip()
    if not raw:
        raise CaptionUrlError("Caption URL is empty")
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    is_mock = host in MOCK_HOSTS

    if is_mock:
        if not allow_mock:
            raise CaptionUrlError("Mock caption endpoints are disabled in this mode")
        if parts.scheme not in ("http", "https"):
            raise CaptionUrlError("Unsupported URL scheme")
    else:
        if parts.scheme != "https":
            raise CaptionUrlError("Zoom caption URL must use https")
        if not (host == "zoom.us" or host.endswith(".zoom.us")):
            raise CaptionUrlError("Host is not a zoom.us caption endpoint")
        if not allow_real_zoom:
            raise CaptionUrlError(
                "Real Zoom output is disabled (set zoom.allow_real_zoom: true in config/local.yaml)"
            )
    if not parts.path.rstrip("/").endswith("/closedcaption"):
        raise CaptionUrlError("URL path must end with /closedcaption")

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in STRIPPED_PARAMS]
    present = {k for k, v in query if v}
    missing = [p for p in REQUIRED_PARAMS if p not in present]
    if missing:
        raise CaptionUrlError(f"Caption URL is missing required parameters: {', '.join(missing)}")

    base = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), urlencode(query), ""))
    fingerprint = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return CaptionUrl(base=base, host=host, fingerprint=fingerprint, is_mock=is_mock)
