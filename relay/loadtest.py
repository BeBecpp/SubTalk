"""Load test for the caption relay: N guest WebSockets + one publisher.

    .venv\\Scripts\\python.exe relay\\loadtest.py --url http://127.0.0.1:8787 --clients 1000

The publish token comes from SUBTALK_RELAY_TOKEN or relay/.dev.vars (never printed).
Every caption carries a unique seq; each guest records when it arrived, so the
report gives delivery (did every guest get every caption?) and fan-out latency
(publish request sent -> guest received; same machine, same clock).

A run against `wrangler dev` measures the relay code on this PC, NOT Cloudflare's
network or guests' phone connections. Label results accordingly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]


def token() -> str:
    t = os.environ.get("SUBTALK_RELAY_TOKEN")
    if t:
        return t
    dev = ROOT / "relay" / ".dev.vars"
    for line in dev.read_text(encoding="utf-8").splitlines() if dev.exists() else []:
        if line.startswith("PUBLISH_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("No token: set SUBTALK_RELAY_TOKEN or create relay/.dev.vars")


class Guest:
    def __init__(self, i: int):
        self.i = i
        self.got: dict[int, float] = {}
        self.connected = False
        self.error: str | None = None
        self.hello_ms: float | None = None
        self.closed_early = False


async def run_guest(g: Guest, ws_url: str, stop: asyncio.Event, ready: asyncio.Semaphore) -> None:
    t0 = time.perf_counter()
    try:
        async with ready:
            ws = await websockets.connect(ws_url, open_timeout=20, ping_interval=None, max_queue=None)
    except Exception as exc:  # noqa: BLE001
        g.error = type(exc).__name__
        return
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 20))
        assert hello["type"] == "hello"
        g.hello_ms = (time.perf_counter() - t0) * 1000
        g.connected = True

        async def pinger():
            while True:
                await asyncio.sleep(20)
                await ws.send("ping")

        pt = asyncio.create_task(pinger())
        try:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), 0.5)
                except asyncio.TimeoutError:
                    continue
                if raw == "pong":
                    continue
                m = json.loads(raw)
                if m.get("type") == "caption" and isinstance(m.get("seq"), int):
                    g.got[m["seq"]] = time.perf_counter()
        finally:
            pt.cancel()
    except websockets.ConnectionClosed:
        g.closed_early = not stop.is_set()
    except Exception as exc:  # noqa: BLE001
        g.error = type(exc).__name__
    finally:
        await ws.close()


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 1)


async def main(a) -> dict:
    base = a.url.rstrip("/")
    ws_url = ("wss://" if base.startswith("https") else "ws://") + base.split("://", 1)[1] + "/ws/viewer"
    tok = token()
    guests = [Guest(i) for i in range(a.clients)]
    stop = asyncio.Event()
    ready = asyncio.Semaphore(a.connect_concurrency)
    t_start = time.perf_counter()
    tasks = [asyncio.create_task(run_guest(g, ws_url, stop, ready)) for g in guests]
    deadline = time.perf_counter() + a.connect_timeout
    while time.perf_counter() < deadline and sum(g.connected or g.error is not None for g in guests) < a.clients:
        await asyncio.sleep(0.2)
    connect_s = time.perf_counter() - t_start
    connected = sum(g.connected for g in guests)
    print(f"connected {connected}/{a.clients} in {connect_s:.1f}s", flush=True)

    sent_at: dict[int, float] = {}
    publish_ms = []
    publish_errors = 0
    async with httpx.AsyncClient(timeout=10) as http:
        for seq in range(1, a.captions + 1):
            body = [{"type": "caption", "seq": seq, "t": time.time(), "kind": "auto",
                     "tr": f"Yük testi {seq}: çğıİöşü", "en": f"Load test {seq}"}]
            t = time.perf_counter()
            sent_at[seq] = t
            try:
                r = await http.post(f"{base}/publish", content=json.dumps(body, ensure_ascii=False),
                                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
                ok = r.status_code == 200
                viewers = r.json().get("viewers") if ok else None
            except httpx.HTTPError:
                ok, viewers = False, None
            publish_ms.append((time.perf_counter() - t) * 1000)
            if not ok:
                publish_errors += 1
            if seq % 10 == 0:
                print(f"caption {seq}: relay reports {viewers} viewers", flush=True)
            await asyncio.sleep(a.interval)
    await asyncio.sleep(a.settle)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)

    lat = [(g.got[s] - sent_at[s]) * 1000 for g in guests if g.connected for s in g.got if s in sent_at]
    expected = connected * a.captions
    delivered = sum(len(g.got) for g in guests if g.connected)
    errors: dict[str, int] = {}
    for g in guests:
        if g.error:
            errors[g.error] = errors.get(g.error, 0) + 1
    report = {
        "target": base, "environment": a.label, "clients": a.clients, "connected": connected,
        "connect_seconds": round(connect_s, 1), "connect_errors": errors,
        "closed_early": sum(g.closed_early for g in guests),
        "hello_ms_p50": pct([g.hello_ms for g in guests if g.hello_ms], 50),
        "hello_ms_p95": pct([g.hello_ms for g in guests if g.hello_ms], 95),
        "captions": a.captions, "interval_s": a.interval, "publish_errors": publish_errors,
        "publish_ms_p50": pct(publish_ms, 50), "publish_ms_max": round(max(publish_ms), 1) if publish_ms else None,
        "delivered": delivered, "expected": expected,
        "delivery_ratio": round(delivered / expected, 5) if expected else None,
        "guests_missing_any": sum(1 for g in guests if g.connected and len(g.got) < a.captions),
        "latency_ms_p50": pct(lat, 50), "latency_ms_p95": pct(lat, 95), "latency_ms_p99": pct(lat, 99),
        "latency_ms_max": round(max(lat), 1) if lat else None,
        "latency_ms_mean": round(statistics.mean(lat), 1) if lat else None,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--clients", type=int, default=1000)
    ap.add_argument("--captions", type=int, default=30)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--connect-concurrency", type=int, default=100)
    ap.add_argument("--connect-timeout", type=float, default=90)
    ap.add_argument("--label", default="local wrangler dev (workerd on this PC)")
    ap.add_argument("--out", default=str(ROOT / "test-artifacts"))
    args = ap.parse_args()
    rep = asyncio.run(main(args))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"relay_load_{args.clients}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(json.dumps(rep, indent=2))
    print(f"report: {path}")
