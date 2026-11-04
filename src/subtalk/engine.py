"""SubTalk engine: wires components, owns authoritative state, executes commands.

The engine process keeps running when browsers disconnect. Stopping the
engine (processing) never touches Zoom, the meeting, or the audio routing on
Computer A - SubTalk only ever holds the caption URL.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

import httpx
import numpy as np
import yaml

from subtalk.asr.langid import Decision, decide
from subtalk.asr.worker import AsrUnavailable, AsrWorkerClient
from subtalk.audio.ingest import AudioIngest
from subtalk.audio.segmenter import AudioSegment, Segmenter
from subtalk.audio.vad import make_detector
from subtalk.config import Config, anthropic_api_key, audio_token, relay_token
from subtalk.glossary.guards import Guards
from subtalk.glossary.service import GlossaryService
from subtalk.glossary.store import load_glossary
from subtalk.output.controller import CommandError, OutputConfig, OutputController
from subtalk.output.viewer import ViewerFeed
from subtalk.pipeline.core import ContextState, Pipeline
from subtalk.state import EngineState, JaPolicy, OutputState, Role, Target
from subtalk.translate.base import TranslatorError
from subtalk.translate.mock import MockTranslator
from subtalk.zoom.mock import MockZoomCaptionService
from subtalk.zoom.sender import ZoomCaptionSender
from subtalk.zoom.seqstore import SeqStore

log = logging.getLogger("subtalk.engine")


@dataclass
class Alert:
    id: int
    level: str  # info | warn | critical
    code: str
    message: str
    at: float
    count: int = 1
    acked: bool = False


@dataclass
class CommandResult:
    command_id: str
    accepted: bool
    error: str | None = None
    version: int = 0
    data: dict = field(default_factory=dict)


class DisabledTranslator:
    """Used when Claude is configured but unusable: every request fails loudly,
    so nothing is ever captioned from a placeholder."""

    name = "disabled"

    def __init__(self, reason: str):
        self.reason = reason

    async def translate(self, req):
        raise TranslatorError(self.reason)

    interpret = translate

    def status(self) -> dict:
        return {"engine": self.name, "model": None, "error": self.reason, "consecutive_failures": 99, "spent_usd": 0}


# command -> (roles allowed, requires control lease)
COMMANDS: dict[str, tuple[set[Role], bool]] = {
    "engine_start": ({Role.OPERATOR}, True),
    "engine_stop": ({Role.OPERATOR}, True),
    "connect_zoom": ({Role.OPERATOR}, True),
    "disconnect_zoom": ({Role.OPERATOR}, True),
    "resync_zoom": ({Role.OPERATOR}, True),
    "force_seq": ({Role.OPERATOR}, True),
    "start_live": ({Role.OPERATOR}, True),
    "pause": ({Role.OPERATOR}, True),
    "resume": ({Role.OPERATOR}, True),
    "stop": ({Role.OPERATOR}, True),
    "estop": ({Role.OPERATOR}, False),  # always available to any operator
    "reset_estop": ({Role.OPERATOR}, True),
    "set_main_speech": ({Role.OPERATOR, Role.SPEAKER}, False),  # lease checked separately
    "set_ja_policy": ({Role.OPERATOR}, True),
    "set_auto_matching": ({Role.OPERATOR}, True),
    "set_music_mode": ({Role.OPERATOR}, True),
    "set_speaker_control": ({Role.OPERATOR}, True),
    "send_phrase": ({Role.OPERATOR}, True),
    "send_test_caption": ({Role.OPERATOR}, True),
    "set_output_mode": ({Role.OPERATOR}, True),
    "set_caption_layout": ({Role.OPERATOR}, True),
    "take_control": ({Role.OPERATOR}, False),
    "ack_alert": ({Role.OPERATOR}, False),
    "warm_cache": ({Role.OPERATOR}, True),
}
VERSIONED = {"start_live", "resume", "reset_estop", "connect_zoom", "disconnect_zoom", "stop", "pause"}


class Engine:
    def __init__(self, cfg: Config, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.state = EngineState.OFF
        self.state_detail = ""
        self.started_at = time.time()
        self._dirty = asyncio.Event()
        self._engine_version = 0
        self._ctx_version = 0
        self.alerts: deque[Alert] = deque(maxlen=60)
        self._alert_seq = 0
        self._recent_cmds: OrderedDict[str, CommandResult] = OrderedDict()
        self._tasks: list[asyncio.Task] = []
        self.processing = False
        self.mock_zoom = MockZoomCaptionService() if self._mock_zoom_enabled else None
        self.http = http or httpx.AsyncClient(follow_redirects=False)
        self.glossary = None
        self.translator = None
        self.asr: AsrWorkerClient | None = None
        self.asr_dropped = 0
        self.mock_runner_task: asyncio.Task | None = None
        self.phrases = self._load_phrases()

        z = cfg.zoom
        self.sender = ZoomCaptionSender(
            self.http, SeqStore(cfg.path(cfg.paths.state_dir) / "caption_seq.json"),
            attempt_timeout=z.attempt_timeout_s, retry_budget=z.retry_budget_s, backoff_base=z.backoff_base_s,
        )
        v = cfg.viewer
        self.viewer = ViewerFeed(enabled=v.enabled, relay_url=v.relay_url or None, relay_token=relay_token(),
                                 http=self.http, recent_max=v.recent_max, max_age_s=v.relay_max_age_s,
                                 timeout_s=v.relay_timeout_s)
        self.output = OutputController(
            self.sender,
            OutputConfig(
                caption_ttl_s=z.caption_ttl_s, manual_ttl_s=z.manual_ttl_s, max_chars=z.max_chars,
                line_breaks=z.line_breaks, line_chars=z.line_chars, min_interval_s=z.min_interval_s,
                part_read_cps=z.part_read_cps,
                queue_max=z.queue_max, auto_pause_after_failures=z.auto_pause_after_failures,
                allow_real_zoom=z.allow_real_zoom, allow_mock=self._mock_zoom_enabled,
                allow_event_target=z.allow_event_target,
            ),
            on_change=self.mark_dirty, on_alert=self.alert, viewer=self.viewer,
        )
        self.context = ContextState(ja_policy=cfg.interp.ja_policy, auto_matching_enabled=cfg.interp.auto_matching_enabled,
                                    output_mode=cfg.zoom.output_mode, caption_layout=cfg.zoom.caption_layout)
        self.asr_queue: asyncio.Queue[AudioSegment] = asyncio.Queue(maxsize=cfg.asr.queue_max)
        self.segmenter = Segmenter(make_detector(cfg.vad.engine, cfg.vad.silero_model_path, cfg.vad.energy_margin_db),
                                   cfg.vad, self._on_segment)
        self.ingest = AudioIngest(cfg.audio, self.segmenter)
        self.pipeline: Pipeline | None = None

    @property
    def _mock_zoom_enabled(self) -> bool:
        return self.cfg.mode == "mock"

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._set_state(EngineState.INITIALIZING, "loading glossary")
        c = self.cfg
        try:
            ev_dir = c.path(c.paths.event_dir)
            self.glossary = load_glossary(c.path(c.paths.glossary_dir), c.path(c.paths.bible_dir),
                                          event_names_path=ev_dir / "event_names.json",
                                          approvals_path=ev_dir / "glossary_approvals.json")
        except Exception as exc:  # noqa: BLE001
            self._set_state(EngineState.FAULT, f"glossary: {exc}")
            self.alert("critical", "glossary_invalid", str(exc))
            raise
        self.translator = self._make_translator()
        self.pipeline = Pipeline(c, GlossaryService(self.glossary), Guards(self.glossary), self.translator,
                                 self.output, self.context, on_change=self.mark_dirty, on_alert=self.alert)
        self._tasks.append(asyncio.create_task(self.output.run(), name="output"))
        self._tasks.append(asyncio.create_task(self._ticker(), name="ticker"))
        if self.viewer.enabled and self.viewer.relay.configured:
            self._tasks.append(asyncio.create_task(self.viewer.run_relay(), name="viewer_relay"))
        elif self.viewer.enabled and self.cfg.viewer.relay_url:
            self.alert("warn", "relay_no_token", "Viewer relay URL is set but SUBTALK_RELAY_TOKEN is missing: "
                                                  "guests outside the venue network get no captions.")
        if c.asr.engine in ("scripted", "whisper"):
            self._set_state(EngineState.INITIALIZING, f"loading ASR ({c.asr.engine} {c.asr.model})")
            script = self._load_mock_script() if c.asr.engine == "scripted" else None
            self.asr = AsrWorkerClient(c.asr.engine, c.asr, c.langid, script=script)
            await self.asr.start()
            if self.asr.state == "fatal":
                self._set_state(EngineState.FAULT, f"ASR: {self.asr.last_error}")
                self.alert("critical", "asr_fatal", f"ASR could not start: {self.asr.last_error}")
                return
            self._tasks.append(asyncio.create_task(self._asr_consumer(), name="asr"))
        self._set_state(EngineState.READY, "")
        if self.glossary.names and not any(n.approved for n in self.glossary.names):
            self.alert("warn", "names_unapproved", "Couple's Latin name spellings are not approved yet (event/event_names.json).")
        self.start_processing()

    async def stop(self) -> None:
        self.stop_processing()
        for t in self._tasks:
            t.cancel()
        if self.asr:
            await self.asr.stop()
        self._set_state(EngineState.OFF, "")

    def start_processing(self) -> None:
        if self.state in (EngineState.FAULT, EngineState.OFF):
            raise CommandError(f"Engine is {self.state.value}")
        self.processing = True
        if self.cfg.asr.engine == "mock" and (self.mock_runner_task is None or self.mock_runner_task.done()):
            self.mock_runner_task = asyncio.create_task(self._mock_session(), name="mock-session")
        self._set_state(EngineState.RUNNING, "")

    def stop_processing(self) -> None:
        self.processing = False
        if self.mock_runner_task:
            self.mock_runner_task.cancel()
            self.mock_runner_task = None
        self.segmenter.flush()
        if self.state in (EngineState.RUNNING, EngineState.DEGRADED):
            self._set_state(EngineState.READY, "processing stopped")

    def _make_translator(self):
        t = self.cfg.translate
        if t.engine == "claude":
            key = anthropic_api_key()
            if not key:
                self.alert("critical", "claude_no_key", "ANTHROPIC_API_KEY / CLAUDE_KEY is not set - translation disabled.")
                return DisabledTranslator("Claude API key missing")
            from subtalk.translate.claude import ClaudeTranslator
            return ClaudeTranslator(t, self.glossary, key)
        return MockTranslator(self._mock_translations())

    # ------------------------------------------------------------------ audio / ASR
    def _on_segment(self, seg: AudioSegment) -> None:
        if not self.processing or self.asr is None:
            return
        if self.context.music_mode:
            self.pipeline.submit_decision(Decision("NONSPEECH", reasons=["music_mode"]), seg.t_start, seg.t_end)
            return
        if self.asr_queue.full():
            try:
                self.asr_queue.get_nowait()  # ASR is behind: drop the oldest segment, never queue stale speech
                self.asr_dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.asr_queue.put_nowait(seg)

    async def _asr_consumer(self) -> None:
        while True:
            seg = await self.asr_queue.get()
            started = time.monotonic()
            if started - seg.t_end > self.cfg.zoom.caption_ttl_s:
                self.asr_dropped += 1
                continue
            try:
                evidence, ms = await self.asr.transcribe(seg.audio)
            except (AsrUnavailable, asyncio.TimeoutError) as exc:
                self.alert("warn", "asr_error", f"Speech recognition failed for one segment: {exc}")
                continue
            d = decide(evidence, self.cfg.langid)
            stages = {"vad_wait": (seg.t_closed - seg.t_end) * 1000 if seg.t_closed else None,
                      "asr_queue": (started - seg.t_closed) * 1000 if seg.t_closed else None}
            self.pipeline.submit_decision(d, seg.t_start, seg.t_end, asr_ms=ms,
                                          stages={k: v for k, v in stages.items() if v is not None})

    def _load_phrases(self) -> list[dict]:
        for p in (self.cfg.path(self.cfg.paths.event_dir) / "phrases.yaml", self.cfg.path("resources/phrases.yaml")):
            if p.exists():
                doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                return [{"label": str(x.get("label", "")), "text": str(x["text"]), "en": str(x.get("en") or "")}
                        for x in doc.get("phrases", []) if x.get("text")]
        return []

    # ------------------------------------------------------------------ mock session
    def _load_mock_doc(self) -> dict:
        p = self.cfg.path(self.cfg.mock.session_file)
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {"segments": []}

    def _load_mock_script(self) -> list[dict]:
        return [s for s in self._load_mock_doc().get("segments", []) if "lang" in s]

    def _mock_translations(self) -> dict:
        out = {}
        for s in self._load_mock_doc().get("segments", []):
            if s.get("text"):
                out[s["text"]] = {k: s[k] for k in ("tr", "en", "fail_en", "relation", "quality", "note", "link_status")
                                  if k in s}
        return out

    async def _mock_session(self) -> None:
        """Replays the mock ceremony timeline (no audio, no paid calls)."""
        from subtalk.asr.engines import ScriptedAsrEngine

        doc = self._load_mock_doc()
        segs = [s for s in doc.get("segments", []) if "lang" in s]
        if not segs:
            return
        while self.processing:
            for s in segs:
                if not self.processing:
                    return
                await asyncio.sleep(float(s.get("gap", 2.0)))
                dur = float(s.get("dur", 3.0))
                ev = ScriptedAsrEngine([s], loop=False).transcribe(np.zeros(int(dur * 16000), dtype=np.float32))
                end = time.monotonic()
                if self.context.music_mode:
                    d = Decision("NONSPEECH", reasons=["music_mode"])
                else:
                    d = decide(ev, self.cfg.langid)
                self.pipeline.submit_decision(d, end - dur, end, asr_ms=0.0)
            if not self.cfg.mock.loop:
                return

    # ------------------------------------------------------------------ state, alerts
    def _set_state(self, state: EngineState, detail: str) -> None:
        self.state, self.state_detail = state, detail
        self._engine_version += 1
        log.info("engine.state", extra={"state": state.value, "detail": detail})
        self.mark_dirty()

    def mark_dirty(self) -> None:
        self._dirty.set()

    async def wait_dirty(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._dirty.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._dirty.clear()

    def alert(self, level: str, code: str, message: str) -> None:
        now = time.time()
        for a in reversed(self.alerts):
            if a.code == code and not a.acked and now - a.at < 30:
                a.count += 1
                a.at, a.message = now, message
                self.mark_dirty()
                return
        self._alert_seq += 1
        self.alerts.append(Alert(self._alert_seq, level, code, message, now))
        log.warning("alert", extra={"level": level, "code": code, "alert_message": message})
        self.mark_dirty()

    @property
    def control_version(self) -> int:
        return self.output.epoch + self._ctx_version + self._engine_version

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self.pipeline:
                self.pipeline.tick()
            self._update_degraded()
            self.mark_dirty()

    def _update_degraded(self) -> None:
        if self.state not in (EngineState.RUNNING, EngineState.DEGRADED):
            return
        problems = [k for k, v in self.health().items() if v["level"] == "red"]
        new = EngineState.DEGRADED if problems else EngineState.RUNNING
        if new is not self.state:
            self._set_state(new, ", ".join(problems))

    # ------------------------------------------------------------------ health
    def health(self) -> dict:
        h = {}
        a = self.ingest.status()
        if self.cfg.asr.engine == "mock":
            h["audio"] = {"level": "grey", "label": "Mock session", **a}
        else:
            lvl = {"ok": "green", "flatline": "amber", "lost": "red"}[a["state"]]
            h["audio"] = {"level": lvl, "label": {"ok": "Receiving", "flatline": "Silent input", "lost": "No audio"}[a["state"]], **a}
        if self.asr is None:
            h["asr"] = {"level": "grey", "label": "Mock ASR"}
        else:
            s = self.asr.status()
            lvl = {"ready": "green", "starting": "amber", "dead": "red", "fatal": "red", "stopped": "red"}[s["state"]]
            h["asr"] = {"level": lvl, "label": f"{s['model']} · {s['state']}", **s, "dropped": self.asr_dropped}
        ts = self.translator.status() if self.translator else {}
        if self.cfg.translate.engine == "mock":
            h["claude"] = {"level": "grey", "label": "Mock translator", **ts}
        elif not anthropic_api_key():
            h["claude"] = {"level": "red", "label": "No API key", **ts}
        else:
            spent = ts.get("spent_usd", 0)
            lvl = "red" if ts.get("consecutive_failures", 0) >= 2 or spent >= self.cfg.translate.budget_hard_usd else \
                "amber" if ts.get("consecutive_failures", 0) or spent >= self.cfg.translate.budget_soft_usd else "green"
            h["claude"] = {"level": lvl, "label": ts.get("model", ""), **ts}
        z = self.sender.status()
        o = self.output.state
        if not z.attached:
            zl, zlabel = "grey", "Not connected"
        elif self.output.zoom_suspended:
            zl, zlabel = "red", "Suspended after failures (viewer continues)"
        elif z.seq_state != "known":
            zl, zlabel = "red", f"Caption number {z.seq_state}"
        elif z.consecutive_failures:
            zl, zlabel = ("red" if z.consecutive_failures >= 2 else "amber"), f"Failing ({z.last_error})"
        else:
            zl, zlabel = "green", f"{z.host} · seq {z.last_confirmed}"
        h["zoom"] = {"level": zl, "label": zlabel, **z.__dict__, "target": self.output.target.value if self.output.target else None}
        if self.viewer.enabled:
            r = self.viewer.relay
            if not r.configured:
                vl = "amber" if self.cfg.viewer.relay_url else "green"
                vlabel = f"Local only · {self.viewer.local_viewers} viewer(s)"
            elif r.consecutive_failures >= 2:
                vl, vlabel = "red", f"Relay failing ({r.last_error})"
            elif r.consecutive_failures:
                vl, vlabel = "amber", f"Relay retry ({r.last_error})"
            else:
                vl, vlabel = "green", f"Relay OK · {r.viewers if r.viewers is not None else '?'} guest(s)"
            h["viewer"] = {"level": vl, "label": vlabel}
        return h

    def verdict(self, health: dict) -> dict:
        o = self.output
        if o.state is OutputState.E_STOPPED:
            return {"code": "ESTOP", "label": "E-STOP", "level": "estop"}
        if self.state is EngineState.FAULT:
            return {"code": "FAULT", "label": "ENGINE FAULT", "level": "red"}
        if o.state is OutputState.LIVE:
            zoom_bad = health["zoom"]["level"] == "red"
            if o.viewer_on:  # either path reaching guests is enough to keep going
                out_bad = health["viewer"]["level"] == "red" and (zoom_bad or not o.zoom_ready)
            else:
                out_bad = zoom_bad
            if out_bad or (self.cfg.translate.engine == "claude" and health["claude"]["level"] == "red"):
                return {"code": "NOT_SENDING", "label": "NOT SENDING", "level": "red"}
            if health["audio"]["level"] == "red" or health.get("asr", {}).get("level") == "red":
                return {"code": "NOT_SENDING", "label": "NO INPUT", "level": "red"}
            if any(v["level"] == "amber" for v in health.values()) or zoom_bad:
                return {"code": "DEGRADED", "label": "DEGRADED", "level": "amber"}
            last = max(o.on_air.at if o.on_air else 0, self.viewer.last_published_at or 0)
            if time.time() - last < 15:
                return {"code": "CAPTIONING", "label": "CAPTIONING", "level": "green"}
            return {"code": "LISTENING", "label": "LISTENING", "level": "green"}
        if o.state is OutputState.PAUSED:
            return {"code": "PAUSED", "label": "PAUSED", "level": "amber"}
        if o.state is OutputState.CONNECTED:
            return {"code": "READY", "label": "READY · NOT LIVE", "level": "grey"}
        if o.viewer_on:
            return {"code": "READY", "label": "READY · VIEWER ONLY", "level": "grey"}
        return {"code": "OFFLINE", "label": "ZOOM NOT CONNECTED", "level": "grey"}

    # ------------------------------------------------------------------ snapshot
    def snapshot(self, role: Role = Role.OPERATOR) -> dict:
        health = self.health()
        base = {
            "boot_id": None,  # filled by the server
            "server_time": time.time(),
            "version": self.control_version,
            "event_name": self.cfg.event_name,
            "mode": self.cfg.mode,
            "engine": {"state": self.state.value, "detail": self.state_detail, "processing": self.processing},
            "verdict": self.verdict(health),
            "context": self.context.to_dict(),
            "output": {"state": self.output.state.value, "target": self.output.target.value if self.output.target else None},
        }
        if role is Role.SPEAKER:
            return base
        out = self.output.snapshot()
        events = [e.to_dict() for e in (self.pipeline.events.all() if self.pipeline else [])][-18:]
        base.update({
            "output": out,
            "health": health,
            "events": events,
            "alerts": [a.__dict__ for a in list(self.alerts)[-12:] if not a.acked],
            "pipeline": {
                "counts": self.pipeline.stats.counts if self.pipeline else {},
                "pending": self.pipeline.emitter.pending if self.pipeline else 0,
                "head_timeouts": self.pipeline.emitter.head_timeouts if self.pipeline else 0,
            },
            "glossary": self.glossary.summary() if self.glossary else None,
            "phrases": self.phrases,
            "config": {
                "allow_real_zoom": self.cfg.zoom.allow_real_zoom,
                "allow_event_target": self.cfg.zoom.allow_event_target,
                "mock_zoom_url": self.mock_zoom.mock_url(f"http://127.0.0.1:{self.cfg.server.port}") if self.mock_zoom else None,
                "translate_engine": self.cfg.translate.engine,
                "asr_engine": self.cfg.asr.engine,
                "line_breaks": self.cfg.zoom.line_breaks,
            },
        })
        return base

    # ------------------------------------------------------------------ commands
    async def execute(self, session, lease, cmd: dict) -> CommandResult:
        cid = str(cmd.get("command_id") or "")[:64]
        ctype = cmd.get("type")
        args = cmd.get("args") or {}
        key = f"{session.sid}:{cid}"  # replays are scoped to the session that sent the command
        if cid and key in self._recent_cmds:
            prev_type, prev = self._recent_cmds[key]
            if prev_type != ctype:
                return CommandResult(cid, False, "command_id reused for a different command", self.control_version)
            return prev  # idempotent replay (e.g. a retried request)
        result = await self._execute(session, lease, cid, ctype, args, cmd.get("expected_version"))
        if cid:
            self._recent_cmds[key] = (ctype, result)
            while len(self._recent_cmds) > 500:
                self._recent_cmds.popitem(last=False)
        log.info("command", extra={"type": ctype, "role": session.role.value, "who": session.name,
                                   "accepted": result.accepted, "error": result.error})
        return result

    async def _execute(self, session, lease, cid, ctype, args, expected_version) -> CommandResult:
        def reject(msg):
            return CommandResult(cid, False, msg, self.control_version)

        spec = COMMANDS.get(ctype)
        if spec is None:
            return reject(f"Unknown command {ctype!r}")
        roles, needs_lease = spec
        if session.role not in roles:
            return reject("Not permitted for your role")
        if ctype == "set_main_speech" and session.role is Role.SPEAKER and not self.context.speaker_control_enabled:
            return reject("Speaker control is disabled by the operator")
        if needs_lease or (ctype == "set_main_speech" and session.role is Role.OPERATOR):
            if not lease.ensure(session):
                return reject(f"Another operator ({lease.info()['holder']}) has control. Use 'Take control' first.")
        if ctype in VERSIONED and expected_version is not None and int(expected_version) != self.control_version:
            return reject("State changed since your screen was updated - review and retry")
        try:
            data = await self._apply(session, lease, ctype, args)
        except CommandError as exc:
            return reject(str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            return reject(f"Invalid arguments: {exc}")
        self.mark_dirty()
        return CommandResult(cid, True, None, self.control_version, data or {})

    async def _apply(self, session, lease, ctype: str, a: dict) -> dict | None:
        o = self.output
        if ctype == "engine_start":
            self.start_processing()
        elif ctype == "engine_stop":
            self.stop_processing()
        elif ctype == "connect_zoom":
            target = Target(a.get("target", "TEST"))
            url = a.get("url") or ""
            if url == "mock" and self.mock_zoom:
                url = self.mock_zoom.mock_url(f"http://127.0.0.1:{self.cfg.server.port}")
            await o.connect(url, target, confirm_event=bool(a.get("confirm_event")))
        elif ctype == "disconnect_zoom":
            await o.disconnect()
        elif ctype == "resync_zoom":
            if not self.sender.url:
                raise CommandError("No caption URL")
            if not await self.sender.resync():
                raise CommandError(f"Zoom did not return the caption number ({self.sender.last_error})")
            o.zoom_suspended = False
        elif ctype == "force_seq":
            if o.state is OutputState.LIVE:
                raise CommandError("Pause output before overriding the caption number")
            self.sender.force_last_confirmed(int(a["last_confirmed"]))
        elif ctype == "start_live":
            if self.state not in (EngineState.RUNNING, EngineState.DEGRADED):
                raise CommandError(f"Engine is {self.state.value} - start processing first")
            o.start_live()
        elif ctype == "pause":
            o.pause()
        elif ctype == "resume":
            o.resume()
        elif ctype == "stop":
            o.stop()
        elif ctype == "estop":
            o.estop()
        elif ctype == "reset_estop":
            o.reset_estop()
        elif ctype == "set_main_speech":
            on = bool(a["on"])
            if self.context.main_speech != on:
                self.context.main_speech = on
                self.context.main_speech_by = f"{session.role.value}:{session.name}"
                self._ctx_version += 1
                self.alert("info", "main_speech", f"Main Speech {'ON' if on else 'OFF'} ({session.name})")
        elif ctype == "set_ja_policy":
            self.context.ja_policy = JaPolicy(a["policy"])
            self._ctx_version += 1
        elif ctype == "set_auto_matching":
            self.context.auto_matching_enabled = bool(a["enabled"])
            self._ctx_version += 1
        elif ctype == "set_music_mode":
            self.context.music_mode = bool(a["on"])
            self._ctx_version += 1
            if self.context.music_mode:
                self.segmenter.flush()
        elif ctype == "set_speaker_control":
            self.context.speaker_control_enabled = bool(a["enabled"])
            self._ctx_version += 1
        elif ctype == "send_phrase":
            text = str(a["text"])[:200]
            en = next((p["en"] for p in self.phrases if p["text"] == text), "")
            o.send_manual(text, en=en)
        elif ctype == "send_test_caption":
            stamp = time.strftime("%H:%M:%S")
            mode = self.context.output_mode
            tr = f"SubTalk TEST {stamp} — Türkçe: ç ğ ı İ ö ş ü"
            en = f"SubTalk TEST {stamp} — English: it's a test, “quotes”"
            sep = "\n" if self.context.caption_layout == "two_line" else " | "
            text = {"TR": tr, "EN": en}.get(mode, f"TR: {tr}{sep}EN: {en}")
            o.send_test(text)
            return {"text": text}
        elif ctype == "set_output_mode":
            mode = str(a["mode"]).upper()
            if mode not in ("TR", "EN", "BILINGUAL"):
                raise CommandError("Output mode must be TR, EN or BILINGUAL")
            self.context.output_mode = mode
            self._ctx_version += 1
            self.alert("info", "output_mode", f"Caption language: {mode} ({session.name})")
        elif ctype == "set_caption_layout":
            layout = str(a["layout"])
            if layout not in ("two_line", "single_line"):
                raise CommandError("Layout must be two_line or single_line")
            self.context.caption_layout = layout
            self._ctx_version += 1
            self.alert("info", "caption_layout", f"Bilingual layout: {layout.replace('_', ' ')} ({session.name})")
        elif ctype == "take_control":
            lease.take(session)
        elif ctype == "ack_alert":
            for al in self.alerts:
                if al.id == int(a["id"]):
                    al.acked = True
        elif ctype == "warm_cache":
            warm = getattr(self.translator, "warm_cache", None)
            if warm is None:
                raise CommandError("Mock translator - nothing to warm")
            try:
                await warm()
            except TranslatorError as exc:
                raise CommandError(f"Claude request failed: {exc}") from None
        return None
