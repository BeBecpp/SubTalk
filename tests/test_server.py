"""Server, auth, roles, lease and a full mock run through the HTTP API."""
import time
import uuid
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
    # uuid4, never a clock: time.monotonic_ns() repeats within a 15.6 ms tick on Windows/Python 3.12.
    return c.post("/api/command", json={"command_id": uuid.uuid4().hex, "type": type_,
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
    r3 = c.post("/api/command", json={**body, "type": "estop"}).json()
    assert not r3["accepted"] and "reused" in r3["error"]  # same id, different command: never replayed
    assert engine.output.state.value != "E_STOPPED"


def test_command_ids_are_scoped_per_session(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    a, engine = make_client(cfg)
    with a:
        login(a, name="Alpha")
        b = TestClient(a.app, base_url="http://127.0.0.1:8000")
        login(b, name="Beta")
        body = {"command_id": "shared-id", "type": "set_music_mode", "args": {"on": True}}
        assert a.post("/api/command", json=body).json()["accepted"]
        r = b.post("/api/command", json={**body, "args": {"on": False}}).json()
        assert not r["accepted"] and "Alpha" in r["error"]  # evaluated for Beta, not a replay of Alpha's result


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


def test_test_caption_and_output_mode_commands(client):
    c, engine = client
    login(c)
    assert not cmd(c, "send_test_caption")["accepted"]  # not connected
    assert cmd(c, "connect_zoom", {"url": "mock", "target": "TEST"})["accepted"]
    r = cmd(c, "send_test_caption")
    assert r["accepted"] and "ç ğ ı İ ö ş ü" in r["data"]["text"]
    assert not cmd(c, "set_output_mode", {"mode": "FR"})["accepted"]
    assert cmd(c, "set_output_mode", {"mode": "BILINGUAL"})["accepted"]
    assert c.get("/api/state").json()["context"]["output_mode"] == "BILINGUAL"
    r = cmd(c, "send_test_caption")  # default layout: one line (Zoom web client drops "\n")
    assert r["data"]["text"].startswith("TR: ") and " | EN: " in r["data"]["text"]
    assert not cmd(c, "set_caption_layout", {"layout": "three_line"})["accepted"]
    assert cmd(c, "set_caption_layout", {"layout": "two_line"})["accepted"]
    r = cmd(c, "send_test_caption")
    assert "\nEN: " in r["data"]["text"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and len(engine.mock_zoom.meeting().captions) < 3:
        time.sleep(0.05)
    texts = [x.text for x in engine.mock_zoom.meeting().captions]
    assert len(texts) == 3 and " | EN: " in texts[1] and "\nEN: " in texts[2]
    login(c, "speaker")
    assert not cmd(c, "set_output_mode", {"mode": "TR"})["accepted"]  # operator only


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


def test_real_audio_path_through_asr_worker(tmp_path):
    """WebSocket audio frames -> VAD segmenter -> ASR worker process (scripted) -> pipeline -> Zoom."""
    import numpy as np

    from subtalk.audio.protocol import pack_frame

    script = [{"lang": "mn", "text": "Сайн байна уу.", "tr": "Merhaba."}]
    cfg = make_cfg(tmp_path, session=script)
    cfg.asr.engine = "scripted"
    c, engine = make_client(cfg)
    with c:
        login(c)
        assert engine.asr is not None and engine.asr.state == "ready"
        assert cmd(c, "connect_zoom", {"url": "mock", "target": "TEST"})["accepted"]
        assert cmd(c, "start_live")["accepted"]
        sr = 16000
        t = np.arange(int(1.5 * sr)) / sr
        speech = 0.3 * np.sin(2 * np.pi * 220 * t)
        rng = np.random.default_rng(0)
        audio = np.concatenate([rng.standard_normal(sr) * 0.002, speech, rng.standard_normal(sr) * 0.002])
        pcm = (audio * 32767).astype("<i2")
        with c.websocket_connect("/ws/audio") as ws:
            for k, i in enumerate(range(0, len(pcm) - 319, 320)):
                ws.send_bytes(pack_frame(k, pcm[i:i + 320].tobytes()))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not engine.mock_zoom.meeting().captions:
                time.sleep(0.1)
        assert [x.text for x in engine.mock_zoom.meeting().captions][0].startswith("TR: Merhaba. | EN: ")
        ev = engine.pipeline.events.all()[-1]
        assert ev.kind == "MN" and ev.asr_ms is not None


SESSION = [
    {"gap": 1.0, "dur": 3.0, "lang": "mn", "text": "Эрхэм зочид оо, тавтай морилно уу.", "tr": "Değerli misafirler, hoş geldiniz.",
     "en": "Dear guests, welcome."},
    {"gap": 0.1, "dur": 3.0, "lang": "ja", "text": "皆さん、ようこそ。", "relation": "covered"},
    {"gap": 0.1, "dur": 3.0, "lang": "music", "text": ""},
    {"gap": 0.1, "dur": 3.0, "lang": "mn", "text": "Ехова гэрлэлтийг бүтээсэн.", "tr": "Evliliği Yehova kurdu.",
     "en": "Jehovah created marriage."},
    {"gap": 0.1, "dur": 3.0, "lang": "mn", "text": "Бид гэрлэлтийн тухай Библийн зарчим үзнэ.",
     "tr": "Kutsal Kitap ilkelerini, Efesliler 5:22'yi inceleyeceğiz.",
     "en": "We will look at Bible principles, Ephesians 5:22."},
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
        # Production default: BILINGUAL, one POST per event, TR first, single-line layout.
        assert texts == ["TR: Değerli misafirler, hoş geldiniz. | EN: Dear guests, welcome.",
                         "TR: Evliliği Yehova kurdu. | EN: Jehovah created marriage."]
        assert engine.mock_zoom.meeting().violations == []
        snap = c.get("/api/state").json()
        kinds = [e["status"] for e in snap["events"]]
        assert "covered" in kinds and "blocked" in kinds
        assert snap["verdict"]["code"] in ("CAPTIONING", "LISTENING")
        assert cmd(c, "estop")["accepted"]
        assert snap["output"]["zoom"]["last_confirmed"] == 2


def test_local_guest_viewer_read_only_and_live(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    cfg.viewer.enabled = True
    c, engine = make_client(cfg)
    with c:
        page = c.get("/viewer")
        assert page.status_code == 200 and "/ws/viewer" in page.text
        assert "sk-ant" not in page.text and "closedcaption" not in page.text
        guest = TestClient(c.app, base_url="http://127.0.0.1:8000")  # no login at all
        with guest.websocket_connect("/ws/viewer") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["status"]["state"] == "stopped"
            ws.send_text("ping")
            assert ws.receive_text() == "pong"
            ws.send_text('{"type":"caption","tr":"hack"}')  # viewers cannot publish
            login(c)
            assert cmd(c, "start_live")["accepted"]  # viewer only: no Zoom URL needed
            m = ws.receive_json()
            assert (m["type"], m["state"]) == ("status", "live")
            while time.monotonic() <= engine.output.live_since:  # coarse clock on 3.12 (see conftest.clock_tick)
                time.sleep(0.005)
            assert engine.output.submit("", speech_end=time.monotonic(), texts={"tr": "Merhaba", "en": "Hello"})
            m = ws.receive_json()
            assert m["type"] == "caption" and (m["tr"], m["en"]) == ("Merhaba", "Hello")
            assert all(x.get("tr") != "hack" for x in engine.viewer.recent)
            assert cmd(c, "estop")["accepted"]
            assert ws.receive_json()["state"] == "stopped"
            assert ws.receive_json()["type"] == "clear"
        assert guest.get("/api/state").status_code == 401  # the viewer grants nothing else


def test_viewer_disabled_by_default(client):
    c, _ = client
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/viewer") as ws:
            ws.receive_text()
    assert e.value.code == 4404


def test_phrase_deck_follows_caption_language_and_keeps_long_vows_whole(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    ev = tmp_path / "event"
    ev.mkdir(parents=True, exist_ok=True)
    # Synthetic long phrase (not event text): long enough to need several paced parts.
    vow_tr = ("Bu uzun bir deneme cümlesidir ve birkaç parçaya bölünmesi gerekir. Her kelime sırasıyla ekranda "
              "görünmeli, hiçbir kelime kaybolmamalı ve Türkçe satır her zaman önce gelmelidir. Sonuna kadar okuyun.")
    vow_en = ("This is a long test sentence that has to be split into several parts. Every word must appear in "
              "order, no word may be lost, and the Turkish line must always come first. Read it to the end.")
    (ev / "phrases.yaml").write_text(yaml.safe_dump({"phrases": [
        {"label": "[Music]", "text": "[Müzik]", "en": "[Music]"},
        {"label": "Vow", "text": vow_tr, "en": vow_en}]}, allow_unicode=True), encoding="utf-8")
    c, engine = make_client(cfg)
    with c:
        login(c)
        assert cmd(c, "connect_zoom", {"url": "mock", "target": "TEST"})["accepted"]
        assert cmd(c, "start_live")["accepted"]
        assert cmd(c, "send_phrase", {"text": "[Müzik]"})["accepted"]
        assert cmd(c, "send_phrase", {"text": vow_tr})["accepted"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and engine.output.queue_len + (1 if not engine.mock_zoom.meeting().captions else 0):
            time.sleep(0.1)
        texts = [x.text for x in engine.mock_zoom.meeting().captions]
        assert texts[0] == "TR: [Müzik] | EN: [Music]"
        vow_parts = texts[1:]
        assert len(vow_parts) >= 2 and all(p.startswith("TR: ") and " | EN: " in p for p in vow_parts)
        tr_words = " ".join(p.split(" | EN: ")[0][4:] for p in vow_parts).split()
        en_words = " ".join(p.split(" | EN: ")[1] for p in vow_parts).split()
        assert tr_words == vow_tr.split() and en_words == vow_en.split()  # nothing lost, order kept
        assert cmd(c, "set_output_mode", {"mode": "TR"})["accepted"]
        assert cmd(c, "send_phrase", {"text": "[Müzik]"})["accepted"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and engine.mock_zoom.meeting().captions[-1].text != "[Müzik]":
            time.sleep(0.1)
        assert engine.mock_zoom.meeting().captions[-1].text == "[Müzik]"


def test_trilingual_mode_test_caption_and_japanese_phrase(tmp_path):
    cfg = make_cfg(tmp_path, session=[])
    ev = tmp_path / "event"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "phrases.yaml").write_text(yaml.safe_dump({"phrases": [
        {"label": "Thanks", "text": "Teşekkürler.", "en": "Thank you.", "ja": "ありがとうございます。"},
        {"label": "[Music]", "text": "[Müzik]", "en": "[Music]"}]}, allow_unicode=True), encoding="utf-8")
    c, engine = make_client(cfg)
    with c:
        login(c)
        assert cmd(c, "connect_zoom", {"url": "mock", "target": "TEST"})["accepted"]
        assert cmd(c, "set_output_mode", {"mode": "TRILINGUAL"})["accepted"]
        r = cmd(c, "send_test_caption")
        assert " | EN: " in r["data"]["text"] and " | JA: " in r["data"]["text"]
        assert cmd(c, "start_live")["accepted"]
        assert cmd(c, "send_phrase", {"text": "Teşekkürler."})["accepted"]
        assert cmd(c, "send_phrase", {"text": "[Müzik]"})["accepted"]  # no Japanese: TR + EN still sent
        assert cmd(c, "set_output_mode", {"mode": "JA"})["accepted"]
        assert not cmd(c, "send_phrase", {"text": "[Müzik]"})["accepted"]  # no Japanese text -> refused
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(engine.mock_zoom.meeting().captions) < 3:
            time.sleep(0.1)
        texts = [x.text for x in engine.mock_zoom.meeting().captions]
        assert texts[1] == "TR: Teşekkürler. | EN: Thank you. | JA: ありがとうございます。"
        assert texts[2] == "TR: [Müzik] | EN: [Music]"
