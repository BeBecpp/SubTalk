"""Real browser (Microsoft Edge via Playwright) against a real SubTalk server process.

Run:  .venv\\Scripts\\python.exe -m pytest -q -m e2e
Needs the `e2e` extra (playwright, psutil) and Microsoft Edge. Mock mode only: no Zoom, no Claude.
Every check reads backend state (HTTP / mock Zoom), not only what the page shows.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
SESSION = [
    {"gap": 0.6, "dur": 2.0, "lang": "mn", "text": "Эрхэм зочид оо, тавтай морилно уу.",
     "tr": "Değerli misafirler, hoş geldiniz.", "en": "Dear guests, welcome."},
    {"gap": 0.6, "dur": 2.0, "lang": "mn", "text": "Ехова гэрлэлтийг бүтээсэн.",
     "tr": "Evliliği Yehova kurdu.", "en": "Jehovah created marriage."},
]


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=3) as r:
        return json.loads(r.read().decode())


def _wait_health(timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if _get("/healthz")["engine"] in ("RUNNING", "READY"):
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError("server did not start")


class Server:
    def __init__(self, tmp: Path, extra: dict | None = None, env: dict | None = None):
        self.tmp = tmp
        self.env = env or {}
        (tmp / "session.yaml").write_text(json.dumps({"segments": SESSION}, ensure_ascii=False), encoding="utf-8")
        cfg = {
            "mode": "mock",
            "server": {"host": "127.0.0.1", "port": PORT},
            "paths": {"state_dir": str(tmp / "state"), "secrets_dir": str(tmp / "secrets"),
                      "event_dir": str(tmp / "event"), "log_dir": str(tmp / "logs")},
            "mock": {"session_file": str(tmp / "session.yaml"), "loop": True},
            "zoom": {"min_interval_s": 0.2},
            **(extra or {}),
        }
        (tmp / "e2e.yaml").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.proc = None

    def start(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        env.pop("SUBTALK_AUDIO_TOKEN", None)
        env.pop("SUBTALK_RELAY_TOKEN", None)
        env.update(self.env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "subtalk.cli", "--home", str(ROOT), "--config", str(self.tmp / "e2e.yaml"), "start"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_health()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        yield b
        b.close()


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path)
    s.start()
    yield s
    s.stop()


def watch_errors(page):
    """Any uncaught JS error in the dashboard is a test failure (a frozen screen looks current)."""
    if not hasattr(page, "_st_errors"):
        page._st_errors = []
        page.on("pageerror", lambda e: page._st_errors.append(str(e)))
    return page._st_errors


def login(page, role="operator", name="QA", code=None):
    watch_errors(page)
    if role == "operator":
        page.goto(BASE + "/operator")
        page.fill("#loginName", name)
        page.fill("#loginPass", code or "dev-operator")
        page.click("#loginForm button[type=submit]")
    else:
        page.goto(BASE + "/speaker")
        page.fill("#pass", code or "dev-speaker")
        page.click("#loginForm button[type=submit]")


def hold(page, selector, ms):
    box = page.locator(selector).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(ms)
    page.mouse.up()


def state(page):
    return page.evaluate("fetch('/api/state').then(r => r.json())")


def wait_state(page, pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = state(page)
        if pred(s):
            return s
        page.wait_for_timeout(150)
    raise AssertionError(f"state not reached: {json.dumps(state(page))[:400]}")


def mock_captions():
    return _get("/mock-zoom/captions?id=mock")


def connect_mock(page):
    page.click("#btnConnect")
    page.fill("#zoomUrl", "mock")
    page.click("#connectForm button[type=submit]")
    page.wait_for_selector("#connect", state="hidden")
    # A hold on a still-disabled button is ignored (as for a human): wait until the UI enables it.
    page.wait_for_function("!document.getElementById('btnLive').disabled", timeout=10000)


# ------------------------------------------------------------------------------- tests
def test_login_wrong_then_right(browser, server):
    page = browser.new_page()
    login(page, code="wrong-code")
    page.wait_for_function("document.getElementById('loginErr').textContent.length > 0")
    assert "Wrong passcode" in page.inner_text("#loginErr")
    assert page.evaluate("fetch('/api/state').then(r => r.status)") == 401
    page.fill("#loginPass", "dev-operator")
    page.click("#loginForm button[type=submit]")
    page.wait_for_selector("#app:not(.hidden)")
    page.wait_for_function("document.getElementById('verdictLabel').textContent !== '—'")
    assert page.inner_text("#outState") == "Not connected"


def test_operator_flow_bilingual_live_estop(browser, server):
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    login(page)
    page.wait_for_selector("#app:not(.hidden)")
    connect_mock(page)
    wait_state(page, lambda s: s["output"]["state"] == "CONNECTED")
    assert page.is_disabled("#btnLive") is False
    page.click("#outMode button[data-v=BILINGUAL]")
    wait_state(page, lambda s: s["context"]["output_mode"] == "BILINGUAL")
    page.click("#btnLive")  # a plain click must NOT go live
    page.wait_for_timeout(600)
    assert state(page)["output"]["state"] == "CONNECTED"
    hold(page, "#btnLive", 1800)
    wait_state(page, lambda s: s["output"]["state"] == "LIVE")
    page.wait_for_function("document.getElementById('outState').textContent === 'LIVE'", timeout=5000)
    page.wait_for_function("document.getElementById('onairText').textContent.includes('EN:')", timeout=15000)
    caps = mock_captions()["captions"]
    assert caps and all("TR:" in c["text"] and "\nEN:" in c["text"] for c in caps)
    assert mock_captions()["violations"] == []
    page.click("#btnEstop")
    wait_state(page, lambda s: s["output"]["state"] == "E_STOPPED")
    n = len(mock_captions()["captions"])
    page.wait_for_timeout(2500)
    assert len(mock_captions()["captions"]) == n  # nothing after E-STOP
    assert page.is_disabled("#btnLive")
    hold(page, "#btnResetEstop", 3300)
    s = wait_state(page, lambda s: s["output"]["state"] == "CONNECTED")
    assert s["output"]["state"] != "LIVE"  # reset never resumes LIVE by itself
    page.click("#jaPolicy button[data-v=SUPPRESS]")  # segment buttons (no id) must not break rendering
    wait_state(page, lambda s: s["context"]["ja_policy"] == "SUPPRESS")
    page.wait_for_timeout(500)
    assert page.is_hidden("#stale")
    assert watch_errors(page) == []


def test_two_operators_lease_and_estop(browser, server):
    a = browser.new_context().new_page()
    b = browser.new_context().new_page()
    login(a, name="Alpha")
    a.wait_for_selector("#app:not(.hidden)")
    connect_mock(a)
    login(b, name="Beta")
    b.wait_for_selector("#app:not(.hidden)")
    b.wait_for_function("document.getElementById('leaseInfo').textContent.includes('Alpha')")
    assert b.is_disabled("#btnConnect") and b.is_disabled("#btnPause")
    r = b.evaluate("ST.command('start_live', {})")
    assert not r["accepted"] and "Alpha" in r["error"]
    assert not b.is_disabled("#btnEstop")  # E-STOP is never blocked by the lease
    b.click("#btnEstop")
    a.wait_for_function("document.getElementById('outState').textContent === 'E-STOP'", timeout=5000)
    assert watch_errors(a) == [] and watch_errors(b) == []


def test_speaker_page_mobile_permissions(browser, server):
    op = browser.new_page()
    login(op)
    op.wait_for_selector("#app:not(.hidden)")
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True)
    sp = ctx.new_page()
    login(sp, role="speaker")
    sp.wait_for_selector("#app:not(.hidden)")
    assert sp.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no horizontal scroll
    assert sp.query_selector("#btnEstop") is None and sp.query_selector("#btnConnect") is None
    hold(sp, "#msBtn", 1300)
    op.wait_for_function("document.getElementById('tMain').classList.contains('on')", timeout=5000)
    snap = sp.evaluate("fetch('/api/state').then(r => r.json())")
    assert "health" not in snap and "zoom" not in json.dumps(snap.get("output", {}))
    r = sp.evaluate("ST.command('start_live', {})")
    assert not r["accepted"]
    r = sp.evaluate("ST.command('estop', {})")
    assert not r["accepted"]
    assert watch_errors(op) == [] and watch_errors(sp) == []


def test_refresh_during_live_keeps_server_state(browser, server):
    page = browser.new_page()
    login(page)
    page.wait_for_selector("#app:not(.hidden)")
    connect_mock(page)
    hold(page, "#btnLive", 1800)
    wait_state(page, lambda s: s["output"]["state"] == "LIVE")
    page.reload()
    page.wait_for_function("document.getElementById('outState').textContent === 'LIVE'", timeout=8000)
    assert state(page)["output"]["state"] == "LIVE"


def test_keyboard_estop(browser, server):
    page = browser.new_page()
    login(page)
    page.wait_for_selector("#app:not(.hidden)")
    connect_mock(page)
    page.focus("#btnEstop")
    page.keyboard.press("Enter")
    wait_state(page, lambda s: s["output"]["state"] == "E_STOPPED")


def test_backend_restart_requires_new_login_and_output_off(browser, server):
    page = browser.new_page()
    login(page)
    page.wait_for_selector("#app:not(.hidden)")
    connect_mock(page)
    hold(page, "#btnLive", 1800)
    wait_state(page, lambda s: s["output"]["state"] == "LIVE")
    server.stop()
    page.wait_for_selector("#stale:not(.hidden)", timeout=8000)  # stale overlay, controls locked
    assert page.is_disabled("#btnEstop") or page.is_visible("#stale")
    server.start()
    page.wait_for_selector("#login:not(.hidden)", timeout=20000)  # old session rejected
    assert _get("/healthz")["output"] == "DISCONNECTED"  # never resumes LIVE
    page.fill("#loginPass", "dev-operator")
    page.click("#loginForm button[type=submit]")
    page.wait_for_function("document.getElementById('outState').textContent === 'Not connected'", timeout=8000)
    assert watch_errors(page) == []
