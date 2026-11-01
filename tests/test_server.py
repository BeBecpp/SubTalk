"""Server, auth, roles, lease and a full mock run through the HTTP API."""
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from subtalk.config import Config
from subtalk.engine import Engine
from subtalk.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path, mode="mock", session=None):
    cfg = Config(mode=mode)
    cfg.root = ROOT
    cfg.paths.state_dir = str(tmp_path / "state")
    cfg.paths.secrets_dir = str(tmp_path / "secrets")
    cfg.paths.event_dir = str(tmp_path / "event")
    cfg.paths.log_dir = str(tmp_path / "logs")
    cfg.zoom.min_interval_s = 0.0
    if session is not None:
        p = tmp_path / "session.yaml"
        p.write_text(yaml.safe_dump({"segments": session}, allow_unicode=True), encoding="utf-8")
        cfg.mock.session_file = str(p)
        cfg.mock.loop = False
    return cfg


def make_client(cfg):
    engine = Engine(cfg)
    if engine.mock_zoom:  # route the engine's Zoom traffic to the in-process mock
        mz = FastAPI()
        mz.include_router(engine.mock_zoom.router())
        engine.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mz), base_url="http://127.0.0.1")
        engine.sender._http = engine.http
    app = create_app(cfg, engine)
    return TestClient(app, base_url="http://127.0.0.1:8000"), engine


def login(c, role="operator", code=None, name="Op"):
    return c.post("/api/login", json={"role": role, "passcode": code or f"dev-{role}", "name": name})


def cmd(c, type_, args=None, **kw):
    return c.post("/api/command", json={"command_id": f"{type_}-{time.monotonic_ns()}", "type": type_,
                                        "args": args or {}, **kw}).json()


@pytest.fixture
def client(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    c, engine = make_client(cfg)
    with c:
        yield c, engine


def test_requires_login(client):
    c, _ = client
    assert c.get("/api/state").status_code == 401
    assert c.post("/api/command", json={"type": "estop"}).status_code == 401
    assert login(c, code="wrong").status_code == 401
    assert login(c).status_code == 200
    assert c.get("/api/state").json()["you"]["role"] == "operator"


def test_cross_origin_post_rejected(client):
    c, _ = client
    r = c.post("/api/login", json={"role": "operator", "passcode": "dev-operator"},
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_speaker_permissions(client):
    c, engine = client
    login(c, "speaker")
    assert not cmd(c, "start_live")["accepted"]
    assert not cmd(c, "estop")["accepted"]
    r = cmd(c, "set_main_speech", {"on": True})
    assert r["accepted"] and engine.context.main_speech
    snap = c.get("/api/state").json()
    assert "health" not in snap and "output" in snap and "zoom" not in snap["output"]  # no tokens/config


def test_speaker_disabled_by_operator(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    op, engine = make_client(cfg)
    with op:
        login(op)
        assert cmd(op, "set_speaker_control", {"enabled": False})["accepted"]
        sp = TestClient(op.app, base_url="http://127.0.0.1:8000")
        login(sp, "speaker")
        r = cmd(sp, "set_main_speech", {"on": True})
        assert not r["accepted"] and "disabled" in r["error"]


def test_lease_estop_and_take_control(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    a, engine = make_client(cfg)
    with a:
        login(a, name="Alpha")
        assert cmd(a, "connect_zoom", {"url": "mock", "target": "TEST"})["accepted"]
        b = TestClient(a.app, base_url="http://127.0.0.1:8000")
        login(b, name="Beta")
        cookie = {"cookie": f"subtalk_session={a.cookies.get('subtalk_session')}"}
        with a.websocket_connect("/ws/ui", headers=cookie) as ws:  # Alpha's dashboard is open -> lease alive
            ws.receive_text()
            r = cmd(b, "start_live")
            assert not r["accepted"] and "Alpha" in r["error"]
            assert cmd(b, "estop")["accepted"]  # E-STOP never needs the lease
            assert engine.output.state.value == "E_STOPPED"
            assert cmd(b, "take_control")["accepted"]
            assert cmd(b, "reset_estop")["accepted"]
            assert not cmd(a, "start_live")["accepted"]  # Alpha lost control


def test_stale_version_rejected(client):
    c, engine = client
    login(c)
    cmd(c, "connect_zoom", {"url": "mock", "target": "TEST"})
    v = c.get("/api/state").json()["version"]
    cmd(c, "set_music_mode", {"on": True})  # state changes after the screen was rendered
    r = cmd(c, "start_live", expected_version=v)
    assert not r["accepted"] and "State changed" in r["error"]


def test_idempotent_command_id(client):
    c, engine = client
    login(c)
    body = {"command_id": "same-id", "type": "set_music_mode", "args": {"on": True}}
    r1 = c.post("/api/command", json=body).json()
    r2 = c.post("/api/command", json=body).json()
    assert r1 == r2 and r1["accepted"]


def test_restart_invalidates_sessions(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    c1, _ = make_client(cfg)
    with c1:
        login(c1)
        cookie = c1.cookies.get("subtalk_session")
        assert c1.get("/api/state").status_code == 200
    c2, _ = make_client(cfg)  # "engine restart": new process, new boot id
    with c2:
        c2.cookies.set("subtalk_session", cookie)
        assert c2.get("/api/state").status_code == 401
        assert c2.get("/healthz").json()["output"] == "DISCONNECTED"  # never resumes LIVE


def test_real_zoom_and_event_disabled_in_dev(client):
    c, _ = client
    login(c)
    r = cmd(c, "connect_zoom", {"url": "https://wmcc.zoom.us/closedcaption?id=1&ns=a&expire=1&signature=s", "target": "TEST"})
    assert not r["accepted"] and "disabled" in r["error"]
    r = cmd(c, "connect_zoom", {"url": "mock", "target": "EVENT", "confirm_event": True})
    assert not r["accepted"]


def test_production_audio_requires_token(tmp_path, monkeypatch):
    monkeypatch.delenv("SUBTALK_AUDIO_TOKEN", raising=False)
    cfg = make_cfg(tmp_path, mode="production", session=[])
    cfg.translate.engine = "mock"
    c, engine = make_client(cfg)
    with c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/audio", headers={"x-subtalk-token": "nope"}) as ws:
                ws.send_bytes(b"x")
                ws.receive_bytes()
        assert not engine.ingest.connected


def test_audio_token_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBTALK_AUDIO_TOKEN", "s3cret-token")
    cfg = make_cfg(tmp_path, session=[])
    c, engine = make_client(cfg)
    with c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/audio", headers={"x-subtalk-token": "wrong"}) as ws:
                ws.receive_bytes()
        with c.websocket_connect("/ws/audio", headers={"x-subtalk-token": "s3cret-token"}) as ws:
            ws.send_bytes(b"garbage-frame")
        assert engine.ingest.bad_frames == 1


SESSION = [
    {"gap": 1.0, "dur": 3.0, "lang": "mn", "text": "Эрхэм зочид оо, тавтай морилно уу.", "tr": "Değerli misafirler, hoş geldiniz."},
    {"gap": 0.1, "dur": 3.0, "lang": "ja", "text": "皆さん、ようこそ。", "relation": "covered"},
    {"gap": 0.1, "dur": 3.0, "lang": "music", "text": ""},
    {"gap": 0.1, "dur": 3.0, "lang": "mn", "text": "Ехова гэрлэлтийг бүтээсэн.", "tr": "Evliliği Yehova kurdu."},
    {"gap": 0.1, "dur": 3.0, "lang": "mn", "text": "Бид гэрлэлтийн тухай Библийн зарчим үзнэ.",
     "tr": "Kutsal Kitap ilkelerini, Efesliler 5:22'yi inceleyeceğiz."},
]


def test_full_mock_run_reaches_zoom(tmp_path):
    cfg = make_cfg(tmp_path, session=SESSION)
    c, engine = make_client(cfg)
    with c:
        login(c)
        engine.stop_processing()
        assert cmd(c, "connect_zoom", {"url": "mock", "target": "TEST"})["accepted"]
        r = cmd(c, "start_live")
        assert not r["accepted"] and "start processing" in r["error"]  # engine stopped -> refuse
        assert cmd(c, "engine_start")["accepted"]
        assert cmd(c, "start_live")["accepted"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len(engine.mock_zoom.meeting().captions) >= 2 and engine.pipeline.stats.counts.get("blocked"):
                break
            time.sleep(0.1)
        texts = [x.text for x in engine.mock_zoom.meeting().captions]
        assert texts == ["Değerli misafirler, hoş geldiniz.", "Evliliği Yehova kurdu."]
        assert engine.mock_zoom.meeting().violations == []
        snap = c.get("/api/state").json()
        kinds = [e["status"] for e in snap["events"]]
        assert "covered" in kinds and "blocked" in kinds
        assert snap["verdict"]["code"] in ("CAPTIONING", "LISTENING")
        assert cmd(c, "estop")["accepted"]
        assert snap["output"]["zoom"]["last_confirmed"] == 2
