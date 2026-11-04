"""SubTalk command line.

  subtalk start                      run the engine + dashboard
  subtalk doctor                     production readiness checks
  subtalk set-passcode --role operator|speaker
  subtalk glossary-check             validate the JW glossary and show review status
  subtalk zoom-probe --url URL --i-confirm-test-meeting   (M0-A: test meeting only)
  subtalk replay FILE.wav            stream a recording to the engine like the sender
  subtalk eval-claude --yes          glossary evaluation cases through real Claude (paid)
  subtalk bench-asr DIR              ASR benchmark on recordings (production GPU)
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
from pathlib import Path


def _cfg(args):
    from subtalk.config import load_config

    return load_config(Path(args.home) if args.home else None, Path(args.config) if args.config else None)


# ---------------------------------------------------------------------- start
def cmd_start(args) -> int:
    import uvicorn

    from subtalk.server.app import create_app
    from subtalk.util.logs import setup_logging

    cfg = _cfg(args)
    setup_logging(cfg.path(cfg.paths.log_dir))
    if cfg.mode == "production":
        from subtalk.server.auth import PasscodeStore

        ps = PasscodeStore(cfg.path(cfg.paths.secrets_dir) / "auth.json", allow_dev_defaults=False)
        if not ps.configured("operator"):
            print("ERROR: no operator passcode. Run:  subtalk set-passcode --role operator", file=sys.stderr)
            return 2
    print(f"SubTalk {cfg.mode} - dashboard: http://{'localhost' if cfg.server.host in ('127.0.0.1', '0.0.0.0') else cfg.server.host}:{cfg.server.port}/operator")
    if cfg.mode == "mock":
        print("Mock mode: passcodes dev-operator / dev-speaker. No real Zoom output, no paid API calls.")
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="warning",
                ws_max_size=2**20, timeout_graceful_shutdown=5)
    return 0


# ---------------------------------------------------------------------- passcode
def cmd_set_passcode(args) -> int:
    from subtalk.server.auth import PasscodeStore

    cfg = _cfg(args)
    code = args.passcode or getpass.getpass(f"New {args.role} passcode (min 8 chars): ")
    if not args.passcode and code != getpass.getpass("Repeat: "):
        print("Passcodes do not match.")
        return 1
    PasscodeStore(cfg.path(cfg.paths.secrets_dir) / "auth.json", allow_dev_defaults=False).set(args.role, code)
    print(f"Passcode for {args.role} saved (scrypt hash) in {cfg.paths.secrets_dir}/auth.json")
    return 0


# ---------------------------------------------------------------------- glossary
def cmd_glossary_check(args) -> int:
    from subtalk.glossary.store import GlossaryError, load_glossary

    cfg = _cfg(args)
    ev = cfg.path(cfg.paths.event_dir)
    try:
        g = load_glossary(cfg.path(cfg.paths.glossary_dir), cfg.path(cfg.paths.bible_dir),
                          event_names_path=ev / "event_names.json", approvals_path=ev / "glossary_approvals.json")
    except GlossaryError as exc:
        print(exc)
        return 1
    s = g.summary()
    print(f"PASS  {s['books']} books, {s['terms']} terms {s['terms_by_status']}, verse tables: {s['verse_tables']}")
    print(f"Names ({s['names_source']}): " + ", ".join(f"{n['tr']}{' (approved)' if n['approved'] else ' (NOT approved)'}" for n in s["names"]))
    pending = [t for t in g.terms if t.status == "review_required"]
    print(f"\n{len(pending)} terms need a Turkish JW reviewer (event/glossary_approvals.json -> approved_term_ids):")
    for t in pending:
        print(f"  {t.id:28s} {', '.join(t.mn)[:34]:36s} -> {t.tr}")
    return 0


# ---------------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    from subtalk.config import anthropic_api_key, audio_token

    cfg = _cfg(args)
    results: list[tuple[str, str, str]] = []

    def check(name, fn):
        try:
            ok, detail = fn()
            results.append(("PASS" if ok else "FAIL", name, detail))
        except Exception as exc:  # noqa: BLE001
            results.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))

    def glossary():
        from subtalk.glossary.store import load_glossary
        g = load_glossary(cfg.path(cfg.paths.glossary_dir), cfg.path(cfg.paths.bible_dir),
                          event_names_path=cfg.path(cfg.paths.event_dir) / "event_names.json")
        names_ok = all(n.approved for n in g.names)
        return True, f"{len(g.terms)} terms; names approved: {names_ok}"

    def passcodes():
        from subtalk.server.auth import PasscodeStore
        ps = PasscodeStore(cfg.path(cfg.paths.secrets_dir) / "auth.json", allow_dev_defaults=False)
        if cfg.mode == "mock" and not ps.configured("operator"):
            return True, "mock mode: dev passcodes (dev-operator / dev-speaker)"
        return ps.configured("operator"), f"operator: {ps.configured('operator')}, speaker: {ps.configured('speaker')}"

    def claude_key():
        return bool(anthropic_api_key()) or cfg.translate.engine == "mock", \
            "set" if anthropic_api_key() else f"missing (translate.engine={cfg.translate.engine})"

    def claude_call():
        if cfg.translate.engine != "claude" or not anthropic_api_key():
            return True, "skipped"
        from subtalk.glossary.store import load_glossary
        from subtalk.translate.base import TranslateRequest
        from subtalk.translate.claude import ClaudeTranslator
        g = load_glossary(cfg.path(cfg.paths.glossary_dir), cfg.path(cfg.paths.bible_dir))
        t = ClaudeTranslator(cfg.translate, g, anthropic_api_key(), ledger=_ledger(cfg), ledger_group="doctor")
        r = asyncio.run(t.translate(TranslateRequest(0, "mn", "Сайн байна уу.", "ok", [], [], {})))
        return bool(r.turkish and r.english), (f"{r.model} {round(r.latency_ms)} ms -> TR {r.turkish!r} / "
                                               f"EN {r.english!r} (${t.spent_usd:.4f})")

    def audio_tok():
        return bool(audio_token()) or cfg.mode == "mock", "set" if audio_token() else "missing"

    def gpu():
        if cfg.asr.engine != "whisper":
            return True, f"skipped (asr.engine={cfg.asr.engine})"
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        return n > 0, f"{n} CUDA device(s); compute types: {sorted(ctranslate2.get_supported_compute_types('cuda')) if n else '-'}"

    def whisper_load():
        if cfg.asr.engine != "whisper":
            return True, "skipped"
        import numpy as np
        from subtalk.asr.engines import WhisperAsrEngine
        e = WhisperAsrEngine(cfg.asr, cfg.langid)
        t = time.monotonic()
        e.transcribe(np.zeros(16000 * 3, dtype=np.float32))
        return True, f"{cfg.asr.model} loaded in {e.load_s:.1f}s, 3 s silence decoded in {time.monotonic() - t:.2f}s"

    def vad():
        from subtalk.audio.vad import make_detector
        d = make_detector(cfg.vad.engine, cfg.vad.silero_model_path, cfg.vad.energy_margin_db)
        return True, f"{d.name}" + (f" ({getattr(d, 'path', '')})" if d.name == "silero" else "")

    def zoom_cfg():
        return True, f"allow_real_zoom={cfg.zoom.allow_real_zoom}, allow_event_target={cfg.zoom.allow_event_target}, line_breaks={cfg.zoom.line_breaks}"

    def disk():
        import shutil
        free = shutil.disk_usage(cfg.root).free / 2**30
        return free > 5, f"{free:.1f} GiB free"

    for name, fn in [("glossary", glossary), ("passcodes", passcodes), ("claude key", claude_key),
                     ("claude request", claude_call), ("audio token", audio_tok), ("gpu", gpu),
                     ("whisper", whisper_load), ("vad", vad), ("zoom config", zoom_cfg), ("disk", disk)]:
        check(name, fn)
    for status, name, detail in results:
        print(f"[{status}] {name:15s} {detail}")
    return 0 if all(r[0] == "PASS" for r in results) else 1


# ---------------------------------------------------------------------- zoom probe
def cmd_zoom_probe(args) -> int:
    """M0-A: validate Zoom caption delivery in a TEST meeting."""
    import httpx

    from subtalk.zoom.sender import ZoomCaptionSender, normalize_caption_text, parse_seq_response
    from subtalk.zoom.seqstore import SeqStore
    from subtalk.zoom.url import parse_caption_url

    if not args.i_confirm_test_meeting:
        print("Refusing: add --i-confirm-test-meeting (this tool must never run against the wedding meeting).")
        return 2
    # The caption URL is a credential: never take it from the command line (shell history).
    if args.url_file:
        raw = Path(args.url_file).read_text(encoding="utf-8").strip()
    elif os.environ.get("SUBTALK_TEST_CAPTION_URL"):
        raw = os.environ["SUBTALK_TEST_CAPTION_URL"].strip()
    else:
        raw = getpass.getpass("Paste the TEST meeting caption URL (input hidden): ").strip()
    url = parse_caption_url(raw, allow_real_zoom=True, allow_mock=True)
    stamp = time.strftime("%H:%M:%S")
    payloads = [
        ("turkish_chars", f"SubTalk TEST {stamp} 1/6 — Türkçe: ç ğ ı İ ö ş ü Ç Ğ Ö Ş Ü"),
        ("english_punct", f"SubTalk TEST {stamp} 2/6 — English: it's “quoted”, Jehovah's Witnesses, Ephesians 5:33."),
        ("jw_terms", f"SubTalk TEST {stamp} 3/6 — Yehova’nın Şahitleri · Efesliler 5:33"),
        ("two_lines", f"SubTalk TEST {stamp} 4/6 — iki satır denemesi\nikinci satır burada"),
        ("bilingual", f"TR: SubTalk TEST {stamp} 5/6 — Hepinize hoş geldiniz!\nEN: Welcome, everyone!"),
        ("long_text", f"SubTalk TEST {stamp} 6/6 — " + "uzun altyazı denemesi " * 6),
    ]

    async def run():
        report = {"host": url.host, "fingerprint": url.fingerprint[:8], "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "evidence_level": "HTTP-accepted only - record what was OBSERVED in Zoom separately"}
        async with httpx.AsyncClient() as http:
            r = await http.get(url.seq_url(), timeout=5)
            report["seq_endpoint"] = {"status": r.status_code, "body": r.text[:40]}
            try:
                report["seq_initial"] = parse_seq_response(r.text)
            except ValueError as exc:
                report["seq_parse_error"] = str(exc)
            s = ZoomCaptionSender(http, SeqStore(None), attempt_timeout=3)
            print("attach:", await s.attach(url), s.seq_source, s.last_confirmed)
            for name, text in payloads:
                t = time.monotonic()
                res = await s.send(text)
                ms = round((time.monotonic() - t) * 1000)
                print(f"{name:14s} seq={res.seq} {res.outcome} HTTP {res.http_status} {ms}ms")
                report.setdefault("posts", []).append({"case": name, "seq": res.seq, "outcome": res.outcome,
                                                       "http": res.http_status, "ms": ms,
                                                       "zoom_ts": res.zoom_timestamp})
                await asyncio.sleep(args.interval)
            empty = await s.send("   ")
            report["empty_text"] = empty.outcome  # must be "blocked" (never posted)
            if args.retry_test and s.last_confirmed:
                same = s.last_confirmed
                # Exactly the bytes the sender posted for that seq (normalised), as a real retry would.
                r = await http.post(url.post_url(same), content=normalize_caption_text(payloads[-1][1]).encode(),
                                    headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=5)
                report["same_seq_retry"] = {"seq": same, "status": r.status_code}
                print(f"same-seq retry of seq={same}: HTTP {r.status_code} -> check in Zoom that it was NOT shown twice")
            if args.gap_test and s.last_confirmed:
                gap = s.last_confirmed + 2
                r = await http.post(url.post_url(gap), content=f"SubTalk TEST {stamp} — seq gap".encode(),
                                    headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=5)
                report["gap_post"] = {"seq": gap, "status": r.status_code}
                print(f"gap post seq={gap}: HTTP {r.status_code} -> check whether Zoom showed it")
            r = await http.get(url.seq_url(), timeout=5)
            report["seq_final"] = r.text[:40]
        out = cfg_root / "test-artifacts"
        out.mkdir(exist_ok=True)
        path = out / f"zoom_probe_{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nReport (no URL inside): {path}")
        print("Now record what was OBSERVED in the Zoom clients in docs/ZOOM_TEST_THURSDAY.md.")

    cfg_root = _cfg(args).root
    asyncio.run(run())
    return 0


# ---------------------------------------------------------------------- replay
def cmd_replay(args) -> int:
    """Stream a WAV file to the engine in real time (like the sender)."""
    import wave

    import numpy as np
    import websockets

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sender"))
    from subtalk_sender import Resampler, pack_frame  # type: ignore

    with wave.open(args.file, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        print("Only 16-bit PCM WAV is supported")
        return 1
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    y = Resampler(rate).process(x)

    async def run():
        from subtalk.config import audio_token
        headers = {"X-SubTalk-Token": args.token or audio_token() or "", "X-SubTalk-Device": f"replay:{Path(args.file).name}"}
        async with websockets.connect(args.engine, additional_headers=headers, max_size=2**20) as ws:
            t0 = time.monotonic()
            for seq, i in enumerate(range(0, len(y) - 319, 320)):
                pcm = (np.clip(y[i:i + 320], -1, 1) * 32767).astype("<i2").tobytes()
                await ws.send(pack_frame(seq, pcm))
                target = t0 + (seq + 1) * 0.02 / args.speed
                await asyncio.sleep(max(0, target - time.monotonic()))
        print(f"Replayed {len(y) / 16000:.1f}s")

    asyncio.run(run())
    return 0


# ---------------------------------------------------------------------- claude eval
def _ledger(cfg):
    from subtalk.budget import TestBudgetLedger
    return TestBudgetLedger(cfg.path(cfg.paths.state_dir) / "api_test_budget.json")


def cmd_budget(args) -> int:
    """Show the persisted live-API test budget ledger."""
    cfg = _cfg(args)
    print(json.dumps(_ledger(cfg).summary(), indent=2))
    return 0


def cmd_eval_claude(args) -> int:
    """Run the evaluation cases through real Claude in Turkish + English (paid, ledgered)."""
    from subtalk.config import anthropic_api_key
    from subtalk.evalrun import all_cases, run_eval, summarize, write_report
    from subtalk.glossary.store import load_glossary
    from subtalk.translate.claude import ClaudeTranslator

    cfg = _cfg(args)
    key = anthropic_api_key()
    if not key:
        print("No ANTHROPIC_API_KEY / CLAUDE_KEY (environment or .env).")
        return 2
    if args.model:
        cfg.translate.model = args.model
    cfg.translate.dual_mode = args.mode
    g = load_glossary(cfg.path(cfg.paths.glossary_dir), cfg.path(cfg.paths.bible_dir))
    ids = [s.strip() for s in args.cases.split(",")] if args.cases else None
    n = len([c for c in all_cases(g) if (not ids or c["id"] in ids)])
    ledger = _ledger(cfg)
    per_case = 2 if args.mode == "parallel" else 1
    print(f"Test budget: spent ${ledger.spent:.4f}, stop at ${ledger.stop:.2f} "
          f"(remaining ${max(0, ledger.stop - ledger.spent):.4f}). Up to {n * per_case} requests to "
          f"{cfg.translate.model} ({args.mode}).")
    if not args.yes:
        print("Add --yes to run.")
        return 2
    t = ClaudeTranslator(cfg.translate, g, key, ledger=ledger, ledger_group=args.group or args.mode)
    results = asyncio.run(run_eval(t, g, ids))
    for r in results:
        tr, en = r.out.get("tr"), r.out.get("en")
        print(f"{r.id:32s} {round(r.latency_ms):5d}ms  TR {tr.auto:8s} {(tr.text or '-')[:44]:46s} "
              f"EN {en.auto:8s} {(en.text or '-')[:44]}")
        for t_, lc in r.out.items():
            for p in lc.problems:
                print(f"{'':40s}- {t_.upper()}: {p}")
    s = summarize(results)
    s["ledger"] = ledger.summary()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    jl, md = write_report(results, cfg.path("bench_out"), tag=args.mode)
    print(f"Report: {md}")
    bad = sum(s[k]["FAIL"] + s[k]["error"] for k in ("turkish", "english"))
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------------- bench
def cmd_soak(args) -> int:
    """Failure-injection soak with mock Claude + mock Zoom (no paid calls)."""
    import asyncio

    from subtalk.soak import run_soak, write_report

    root = Path(args.home or ".").resolve()
    rep = asyncio.run(run_soak(args.seconds, seed=args.seed, tmp=root / "state" / "soak", fault_scale=args.faults))
    path = write_report(rep, root / "test-artifacts")
    print(json.dumps({"ok": rep.ok, "violations": rep.violations, "segments": rep.segments, "faults": rep.faults,
                      "zoom_sent": rep.zoom_sent, "zoom_abandoned": rep.zoom_abandoned,
                      "confirmed_by_seq": rep.zoom_confirmed_by_seq, "queue_max": rep.queue_max,
                      "caption_age_ms": rep.caption_age_ms, "rss_mb": [rep.rss_mb_start, rep.rss_mb_max, rep.rss_mb_end],
                      "cpu_percent_avg": rep.cpu_percent_avg}, indent=2, ensure_ascii=False, default=str))
    print(f"Report: {path}")
    return 0 if rep.ok else 1


def cmd_bench_asr(args) -> int:
    from subtalk.bench import run_benchmark

    cfg = _cfg(args)
    return run_benchmark(cfg, Path(args.dir), [m.strip() for m in args.models.split(",")], Path(args.out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="subtalk")
    ap.add_argument("--home", help="SubTalk folder (default: current directory)")
    ap.add_argument("--config", help="extra YAML config file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start").set_defaults(fn=cmd_start)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("set-passcode")
    p.add_argument("--role", choices=["operator", "speaker"], required=True)
    p.add_argument("--passcode", help="non-interactive (avoid: shell history)")
    p.set_defaults(fn=cmd_set_passcode)
    sub.add_parser("glossary-check").set_defaults(fn=cmd_glossary_check)
    p = sub.add_parser("zoom-probe", help="TEST meeting only; URL read hidden / from a file / env")
    p.add_argument("--url-file", help="file containing the TEST caption URL (delete it afterwards)")
    p.add_argument("--i-confirm-test-meeting", action="store_true")
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--retry-test", action="store_true")
    p.add_argument("--gap-test", action="store_true")
    p.set_defaults(fn=cmd_zoom_probe)
    p = sub.add_parser("replay")
    p.add_argument("file")
    p.add_argument("--engine", default="ws://127.0.0.1:8000/ws/audio")
    p.add_argument("--token")
    p.add_argument("--speed", type=float, default=1.0)
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("eval-claude", help="glossary evaluation cases through real Claude (paid)")
    p.add_argument("--yes", action="store_true", help="confirm the paid requests")
    p.add_argument("--model", help="override translate.model (e.g. claude-sonnet-5-5)")
    p.add_argument("--cases", help="comma-separated case IDs (default: all)")
    p.add_argument("--mode", choices=["single_request", "parallel"], default="single_request",
                   help="one request for TR+EN (default) or one request per language")
    p.add_argument("--group", help="ledger group name for the spend report")
    p.set_defaults(fn=cmd_eval_claude)
    sub.add_parser("budget", help="show the persisted live-API test budget ledger").set_defaults(fn=cmd_budget)
    p = sub.add_parser("soak", help="failure-injection soak, mock Claude + mock Zoom (free)")
    p.add_argument("--seconds", type=float, default=600)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--faults", type=float, default=1.0, help="fault rate multiplier")
    p.set_defaults(fn=cmd_soak)
    p = sub.add_parser("bench-asr")
    p.add_argument("dir", help="folder of 16-bit WAV files (+ optional .txt references)")
    p.add_argument("--models", default="large-v3,large-v3-turbo")
    p.add_argument("--out", default="bench_out")
    p.set_defaults(fn=cmd_bench_asr)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
