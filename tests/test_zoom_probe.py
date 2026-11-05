"""zoom-probe against the strict mock endpoint (no network): line-break variants are posted raw,
in seq order, and the probe never writes the caption URL into its report."""
import argparse
import json

import httpx
from fastapi import FastAPI

from subtalk import cli
from subtalk.zoom.mock import MockZoomCaptionService

URL = "http://127.0.0.1/mock-zoom/closedcaption?id=mock&ns=mock&expire=86400&signature=mock"


def test_linebreak_payloads_keep_separators_raw():
    payloads = dict(cli.linebreak_payloads("12:00:00"))
    assert [n for n, _ in cli.LINEBREAK_VARIANTS] == list(payloads)
    assert b"!\r\nEN: [crlf]" in payloads["crlf"]
    assert b"!\rEN: [cr]" in payloads["cr"]
    assert "! EN: [u2028]".encode() in payloads["u2028"]
    assert "! EN: [u2029]".encode() in payloads["u2029"]
    assert "Hepinize hoş geldiniz!".encode() in payloads["lf"]
    for name, body in payloads.items():
        text = body.decode("utf-8")
        assert text.startswith(f"TR: [{name}]") and f"EN: [{name}]" in text


def test_probe_linebreak_and_gap_against_mock(tmp_path, monkeypatch):
    svc = MockZoomCaptionService()
    app = FastAPI()
    app.include_router(svc.router())
    real_client = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = httpx.ASGITransport(app=app)
        return real_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("SUBTALK_TEST_CAPTION_URL", URL)
    args = argparse.Namespace(home=str(tmp_path), config=None, url_file=None, i_confirm_test_meeting=True,
                              interval=0.0, retry_test=True, gap_test=True, linebreak_test=True,
                              linebreak_pause=0.0)
    assert cli.cmd_zoom_probe(args) == 0

    m = svc.meeting("mock")
    texts = [c.text for c in m.captions]
    n_base = 6
    assert len(texts) == n_base + len(cli.LINEBREAK_VARIANTS)  # gap post is rejected by the strict mock
    sent = texts[n_base:]
    assert "\r\n" in sent[1] and "\r" in sent[2] and " " in sent[3] and " " in sent[4]
    # Only the deliberate gap post is a sequence violation.
    assert len(m.violations) == 1 and m.violations[0]["got"] == m.last_seq + 2

    report_file = next((tmp_path / "test-artifacts").glob("zoom_probe_*.json"))
    raw = report_file.read_text(encoding="utf-8")
    assert "signature" not in raw and "mock-zoom" not in raw
    report = json.loads(raw)
    assert [p["variant"] for p in report["linebreak_posts"]] == [n for n, _ in cli.LINEBREAK_VARIANTS]
    assert all(p["http"] == 200 for p in report["linebreak_posts"])
    assert report["same_seq_retry"]["status"] == 403
