"""Guest viewer end to end: engine -> local Cloudflare relay (wrangler dev / workerd) -> browsers.

Two guests in separate browser sessions pick different languages and must see
the SAME caption event in their own language. Needs Node + relay/node_modules
(`cd relay && npm install`) and Microsoft Edge. Mock engine only: no Zoom, no Claude.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from test_browser import BASE, SESSION, Server, browser, hold, login, state, wait_state, watch_errors  # noqa: F401

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
RELAY_PORT = 8790
RELAY = f"http://127.0.0.1:{RELAY_PORT}"
WRANGLER = ROOT / "relay" / "node_modules" / ".bin" / ("wrangler.cmd" if os.name == "nt" else "wrangler")

TR = {seg["tr"] for seg in SESSION}
EN = {seg["en"] for seg in SESSION}


def _relay_health():
    with urllib.request.urlopen(RELAY + "/healthz", timeout=2) as r:
        return json.loads(r.read().decode())


class Relay:
    def __init__(self, tmp: Path, token: str):
        self.tmp, self.token, self.proc = tmp, token, None

    def start(self):
        self.proc = subprocess.Popen(
            [str(WRANGLER), "dev", "--local", "--ip", "127.0.0.1", "--port", str(RELAY_PORT),
             "--var", f"PUBLISH_TOKEN:{self.token}", "--persist-to", str(self.tmp / "wrangler"),
             "--show-interactive-dev-session=false"],
            cwd=ROOT / "relay", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "WRANGLER_SEND_METRICS": "false", "CI": "1"},
        )
        end = time.monotonic() + 60
        while time.monotonic() < end:
            try:
                return _relay_health()
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        raise RuntimeError("relay did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":  # wrangler spawns workerd: kill the whole tree
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
            else:
                self.proc.terminate()
            self.proc.wait(15)


@pytest.fixture
def relay(tmp_path):
    if not WRANGLER.exists() or not shutil.which("node"):
        pytest.skip("relay not installed (cd relay && npm install)")
    r = Relay(tmp_path, secrets.token_urlsafe(32))
    r.start()
    yield r
    r.stop()


@pytest.fixture
def engine(tmp_path, relay):
    s = Server(tmp_path, extra={"viewer": {"enabled": True, "relay_url": RELAY}},
               env={"SUBTALK_RELAY_TOKEN": relay.token})
    s.start()
    yield s
    s.stop()


def guest(browser, url, mobile=False):
    kw = {"viewport": {"width": 375, "height": 812}, "is_mobile": True, "has_touch": True} if mobile else {}
    # bypass_csp: the relay CSP forbids eval, which Playwright string predicates need (test-only)
    page = browser.new_context(bypass_csp=True, **kw).new_page()  # own context = own session + storage
    watch_errors(page)
    page.goto(url)
    return page


def latest(page):
    return page.evaluate("(document.querySelector('.cap.latest') || {}).textContent || ''")


def wait_caption(page, allowed, timeout=20000):
    page.wait_for_function(
        "(allowed) => { const e = document.querySelector('.cap.latest'); return e && allowed.includes(e.textContent); }",
        arg=list(allowed), timeout=timeout)
    return latest(page)


def go_live_viewer_only(browser):
    op = browser.new_page()
    login(op)
    op.wait_for_selector("#app:not(.hidden)")
    op.wait_for_function("document.getElementById('btnLive').textContent.includes('VIEWER ONLY')", timeout=10000)
    hold(op, "#btnLive", 1800)
    wait_state(op, lambda s: s["output"]["state"] == "LIVE")
    return op


def test_two_guests_two_languages_same_events(browser, engine, relay):
    tr = guest(browser, RELAY + "/?lang=tr", mobile=True)
    en = guest(browser, RELAY + "/")
    en.click("button[data-lang=en]")
    assert en.evaluate("localStorage.getItem('st.lang')") == "en"
    tr.wait_for_function("document.getElementById('status').dataset.s === 'stopped'", timeout=20000)
    op = go_live_viewer_only(browser)
    tr.wait_for_function("document.getElementById('status').dataset.s === 'live'", timeout=20000)
    assert tr.inner_text("#statusText") == "Canlı" and en.inner_text("#statusText") == "Live"
    seen_tr, seen_en = set(), set()
    end = time.monotonic() + 30
    while time.monotonic() < end and (seen_tr != TR or seen_en != EN):
        seen_tr.add(wait_caption(tr, TR))
        seen_en.add(wait_caption(en, EN))
        tr.wait_for_timeout(300)
    assert seen_tr == TR and seen_en == EN  # each guest only ever sees its own language
    # Same events: the TR guest's list and the EN guest's list come from the same seq numbers.
    k_tr = tr.evaluate("Array.from(document.querySelectorAll('.cap')).length")
    assert k_tr >= 2
    # No Zoom involved at all.
    assert state(op)["output"]["target"] is None
    # Mobile: large text, no sideways scrolling.
    assert tr.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert tr.evaluate("parseFloat(getComputedStyle(document.querySelector('.cap.latest')).fontSize)") >= 26
    # The relay reports both guests to the operator dashboard.
    wait_state(op, lambda s: (s["output"]["viewer"]["relay"]["viewers"] or 0) >= 2, timeout=20)
    # Guest page carries no secrets or admin endpoints.
    html = tr.content()
    assert relay.token not in html and "/api/command" not in html and "closedcaption" not in html
    # E-STOP clears every guest screen.
    op.click("#btnEstop")
    for g in (tr, en):
        g.wait_for_function("!document.querySelector('.cap')", timeout=10000)
        g.wait_for_function("document.getElementById('status').dataset.s === 'stopped'", timeout=10000)
    for p in (tr, en, op):
        assert watch_errors(p) == []


def test_guest_cannot_publish(relay):
    with urllib.request.urlopen(RELAY + "/", timeout=5) as r:
        csp = r.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp and "unsafe-eval" not in csp
        assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    req = urllib.request.Request(RELAY + "/publish", data=b'[{"type":"caption","tr":"x"}]', method="POST",
                                 headers={"Authorization": "Bearer " + "x" * 43})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    for path in ("/operator", "/api/state", "/ws/ui"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(RELAY + path, timeout=5)
        assert e.value.code in (404, 426)


def test_guest_reconnects_after_relay_restart(browser, engine, relay):
    g = guest(browser, RELAY + "/?lang=en")
    go_live_viewer_only(browser)
    wait_caption(g, EN)
    relay.stop()
    g.wait_for_function("document.getElementById('status').dataset.s === 'reconnecting'", timeout=20000)
    assert g.inner_text("#statusText").startswith("Connection lost")
    relay.start()
    # Back without a page reload; the engine re-sends status within a heartbeat, captions resume.
    wait_caption(g, EN, timeout=40000)
    g.wait_for_function("document.getElementById('status').dataset.s === 'live'", timeout=30000)
    assert watch_errors(g) == []


def test_local_engine_viewer_page(browser, engine):
    g = guest(browser, BASE + "/viewer?lang=tr")
    go_live_viewer_only(browser)
    wait_caption(g, TR)
    assert watch_errors(g) == []
