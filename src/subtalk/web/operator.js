// SubTalk Control - operator dashboard. Renders only server-confirmed state.
"use strict";

const $ = ST.$;
let snap = null;
let stale = true;
let restarted = false;
let conn = null;
const expectations = new Map(); // button element -> {check, until} (segment buttons have no id)

// ------------------------------------------------------------------ auth
async function boot() {
  const r = await fetch("/api/session", { credentials: "same-origin" });
  if (r.status === 401) {
    const d = await r.json().catch(() => ({}));
    showLogin(d.dev_passcodes);
    return;
  }
  const d = await r.json();
  if (d.role !== "operator") { location.href = "/speaker"; return; }
  start();
}

function showLogin(dev) {
  $("app").classList.add("hidden");
  $("stale").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("loginDev").textContent = dev ? "Mock mode: development passcode is “dev-operator”." : "";
  try { $("loginName").value = localStorage.getItem("subtalk.name") || ""; } catch (_) { /* ignore */ }
  setTimeout(() => $("loginPass").focus(), 50);
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("loginName").value.trim();
  const r = await ST.post("/api/login", { role: "operator", passcode: $("loginPass").value, name });
  if (!r.ok) { $("loginErr").textContent = (r.data && r.data.error) || "Sign-in failed"; return; }
  try { localStorage.setItem("subtalk.name", name); } catch (_) { /* ignore */ }
  $("loginPass").value = "";
  $("loginErr").textContent = "";
  $("login").classList.add("hidden");
  start();
});

function start() {
  $("app").classList.remove("hidden");
  if (conn) conn.close();
  conn = ST.connect({
    onSnapshot: render,
    onStale: (isStale, _last, message) => {
      stale = isStale;
      $("stale").classList.toggle("hidden", !isStale || !$("login").classList.contains("hidden"));
      if (!restarted) $("staleMsg").textContent = message || "Connection lost — reconnecting…";
      if (snap) renderControls(snap);
    },
    onRestart: () => {
      restarted = true;
      $("staleMsg").textContent = "Engine restarted — caption output is OFF. Please sign in again.";
      $("stale").classList.remove("hidden");
      if (conn) conn.close();
      setTimeout(() => location.reload(), 2500);
    },
    onAuth: () => showLogin(false),
  });
}

// ------------------------------------------------------------------ commands
async function run(btn, type, args, check, versioned) {
  if (stale) { ST.toast("Screen is out of date - command not sent", true); return; }
  if (btn) { btn.classList.add("pending"); btn.classList.remove("acked"); }
  const res = await ST.command(type, args, versioned && snap ? { version: snap.version } : undefined);
  if (btn) btn.classList.remove("pending");
  if (!res.accepted) {
    ST.toast(res.error || "Rejected", true);
    if (res.auth) showLogin(false);
    return false;
  }
  if (btn && check) {
    btn.classList.add("acked");  // acknowledged by the server, waiting for actual state
    expectations.set(btn, { check, until: Date.now() + 5000 });
  }
  return true;
}

const outState = () => (snap ? snap.output.state : null);

ST.hold($("btnLive"), 1500, () => run($("btnLive"), "start_live", {}, (s) => s.output.state === "LIVE", true));
ST.hold($("btnResetEstop"), 3000, () => run($("btnResetEstop"), "reset_estop", {}, (s) => s.output.state !== "E_STOPPED", true));
$("btnEstop").addEventListener("click", () => run($("btnEstop"), "estop", {}, (s) => s.output.state === "E_STOPPED"));
$("btnPause").addEventListener("click", () => {
  if (outState() === "PAUSED") run($("btnPause"), "resume", {}, (s) => s.output.state === "LIVE", true);
  else run($("btnPause"), "pause", {}, (s) => s.output.state === "PAUSED", true);
});
$("btnStop").addEventListener("click", () => run($("btnStop"), "stop", {}, (s) => s.output.state === "CONNECTED", true));
$("btnConnect").addEventListener("click", () => {
  if (snap && snap.output.state !== "DISCONNECTED" && !confirm("Replace or disconnect the current caption URL?")) return;
  openConnect();
});

function toggle(el, type, key) {
  const fire = () => {
    const on = !(snap && snap.context[key]);
    run(el, type, { on }, (s) => s.context[key] === on);
  };
  el.addEventListener("click", fire);
  el.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); fire(); } });
}
toggle($("tMain"), "set_main_speech", "main_speech");
toggle($("tMusic"), "set_music_mode", "music_mode");
$("jaPolicy").querySelectorAll("button").forEach((b) => b.addEventListener("click", () =>
  run(b, "set_ja_policy", { policy: b.dataset.v }, (s) => s.context.ja_policy === b.dataset.v)));
$("outMode").querySelectorAll("button").forEach((b) => b.addEventListener("click", () =>
  run(b, "set_output_mode", { mode: b.dataset.v }, (s) => s.context.output_mode === b.dataset.v)));
$("btnTest").addEventListener("click", async () => {
  const ok = await run($("btnTest"), "send_test_caption", {});
  if (ok) ST.toast("Test caption queued for the TEST meeting - check it in Zoom");
});

$("btnEngineStart").addEventListener("click", () => run($("btnEngineStart"), "engine_start", {}, (s) => s.engine.processing));
$("btnEngineStop").addEventListener("click", () => {
  if (confirm("Stop processing audio? Zoom and the meeting are not affected.")) run($("btnEngineStop"), "engine_stop", {}, (s) => !s.engine.processing);
});
$("btnResync").addEventListener("click", () => run($("btnResync"), "resync_zoom", {}));
$("btnWarm").addEventListener("click", () => run($("btnWarm"), "warm_cache", {}));
$("btnAutoMatch").addEventListener("click", () => {
  const on = !(snap && snap.context.auto_matching_enabled);
  run($("btnAutoMatch"), "set_auto_matching", { enabled: on }, (s) => s.context.auto_matching_enabled === on);
});
$("btnSpeakerCtl").addEventListener("click", () => {
  const on = !(snap && snap.context.speaker_control_enabled);
  run($("btnSpeakerCtl"), "set_speaker_control", { enabled: on }, (s) => s.context.speaker_control_enabled === on);
});
$("btnTake").addEventListener("click", () => {
  if (confirm("Take control from the current operator?")) run($("btnTake"), "take_control", {});
});
$("btnLogout").addEventListener("click", async () => { await ST.post("/api/logout"); location.reload(); });

// ------------------------------------------------------------------ connect dialog
function openConnect() {
  $("connect").classList.remove("hidden");
  $("connectErr").textContent = "";
  $("zoomUrl").value = "";
  $("eventConfirm").checked = false;
  const mock = snap && snap.mode === "mock";
  $("mockHint").classList.toggle("hidden", !mock);
  const evOk = snap && snap.config && snap.config.allow_event_target;
  document.querySelector('input[value="EVENT"]').disabled = !evOk;
  document.querySelector('input[value="TEST"]').checked = true;
  $("eventWarn").classList.add("hidden");
  setTimeout(() => $("zoomUrl").focus(), 50);
}
document.querySelectorAll('input[name="target"]').forEach((r) => r.addEventListener("change", () => {
  $("eventWarn").classList.toggle("hidden", r.value !== "EVENT" || !r.checked);
}));
$("connectCancel").addEventListener("click", () => $("connect").classList.add("hidden"));
$("connectForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const target = document.querySelector('input[name="target"]:checked').value;
  const confirmEvent = $("eventConfirm").checked;
  if (target === "EVENT" && !confirmEvent) { $("connectErr").textContent = "Tick the confirmation for the wedding meeting."; return; }
  const res = await ST.command("connect_zoom", { url: $("zoomUrl").value.trim(), target, confirm_event: confirmEvent },
    snap ? { version: snap.version } : undefined);
  $("zoomUrl").value = "";
  if (!res.accepted) { $("connectErr").textContent = res.error || "Rejected"; return; }
  $("connect").classList.add("hidden");
  ST.toast("Caption URL connected - output is NOT live yet");
});

// ------------------------------------------------------------------ render
function setChip(id, h) {
  const el = $(id);
  if (!h) return;
  el.querySelector(".led").className = "led " + (h.level || "grey");
  el.querySelector(".v").textContent = h.label || "";
  el.title = h.label || "";
}

function kv(id, rows) {
  const box = $(id);
  box.replaceChildren();
  rows.forEach(([k, v]) => { box.appendChild(ST.el("div", "k", k)); box.appendChild(ST.el("div", "v", v === null || v === undefined ? "—" : String(v))); });
}

const STATE_TEXT = {
  DISCONNECTED: ["Not connected", "Paste the Zoom caption URL to begin"],
  CONNECTED: ["Ready · not live", "Nothing is sent until you hold “go LIVE”"],
  LIVE: ["LIVE", "Captions are being sent"],
  PAUSED: ["Paused", "New captions are dropped, not queued"],
  E_STOPPED: ["E-STOP", "Output halted. Hold reset, then go live again."],
};

function render(s) {
  snap = s;
  const o = s.output;
  // expectations: acknowledged -> actual
  for (const [el, exp] of expectations) {
    if (exp.check(s) || Date.now() > exp.until) { el.classList.remove("acked"); expectations.delete(el); }
  }
  $("devBanner").classList.toggle("hidden", !s.dev_passcodes);
  $("eventName").textContent = s.event_name || "";
  const v = s.verdict;
  $("verdict").className = "verdict v-" + v.level;
  $("verdictLabel").textContent = v.label;
  const tb = $("targetBadge");
  tb.classList.toggle("hidden", !o.target);
  tb.className = "target-badge " + (o.target || "") + (o.target ? "" : " hidden");
  tb.textContent = o.target === "EVENT" ? "WEDDING" : "TEST";
  $("app").classList.toggle("live-event", o.target === "EVENT" && o.state === "LIVE");
  $("app").classList.toggle("target-test", o.target === "TEST" && o.state === "LIVE");

  const h = s.health || {};
  setChip("hAudio", h.audio); setChip("hAsr", h.asr); setChip("hClaude", h.claude); setChip("hZoom", h.zoom);
  $("hViewer").classList.toggle("hidden", !h.viewer); setChip("hViewer", h.viewer);
  const e2e = o.e2e_ms || {};
  $("latency").textContent = e2e.n ? `${ST.ms(e2e.p50)} / ${ST.ms(e2e.p95)}` : "—";
  $("leaseInfo").textContent = s.you.has_control ? `● ${s.you.name} · in control`
    : (s.lease.holder ? `Read-only · ${s.lease.holder} has control` : `${s.you.name}`);

  const [title, sub] = STATE_TEXT[o.state] || [o.state, ""];
  $("outState").textContent = title;
  $("outState").style.color = o.state === "LIVE" ? "var(--green)" : o.state === "E_STOPPED" ? "var(--red)" : o.state === "PAUSED" ? "var(--amber)" : "";
  const modeLabel = { TR: "Turkish", EN: "English", BILINGUAL: "Turkish + English" }[s.context.output_mode] || "";
  $("outSub").textContent = o.reason === "zoom_failures" ? "Auto-paused: Zoom rejected captions"
    : (o.state === "LIVE" ? `${sub} · ${modeLabel}` : sub);

  renderOnAir(o);
  renderFeed(s.events || [], s.pipeline || {});
  renderHistory(o.history || []);
  renderAlerts(s.alerts || []);
  renderPhrases(s.phrases || [], o.state);
  renderControls(s);
  renderDiag(s);
}

function renderOnAir(o) {
  const a = o.on_air;
  const box = $("onair");
  box.classList.toggle("live", o.state === "LIVE");
  $("onairTag").textContent = o.state === "LIVE" ? (o.target === "EVENT" ? "● ON AIR · WEDDING" : "● ON AIR · TEST") : "LAST SENT";
  const t = $("onairText");
  if (a) {
    t.textContent = a.text; t.classList.remove("empty");
    $("onairSeq").textContent = "seq " + a.seq;
    $("onairAgo").textContent = ST.ago(a.at);
    $("onairLat").textContent = a.e2e_ms ? "speech→Zoom " + ST.ms(a.e2e_ms) : (a.kind === "manual" ? "manual" : "");
  } else {
    t.textContent = "Nothing sent yet"; t.classList.add("empty");
    $("onairSeq").textContent = $("onairAgo").textContent = $("onairLat").textContent = "";
  }
}

const seen = new Set();
const KIND_LABEL = { MN: "MN", JA: "JA", UNCERTAIN: "?", OVERLAP: "MIX", NONSPEECH: "—", MUSIC: "♪" };
function renderFeed(events, p) {
  const feed = $("feed");
  const frag = document.createDocumentFragment();
  [...events].reverse().forEach((e) => {
    const row = ST.el("div", "ev" + (seen.has(e.id) ? "" : " fresh"));
    seen.add(e.id);
    row.appendChild(ST.el("div", "lang " + e.kind, KIND_LABEL[e.kind] || e.kind));
    const body = ST.el("div");
    body.appendChild(ST.el("div", "src", e.text || (e.kind === "MUSIC" ? "music mode" : "(no speech)")));
    const out = e.out || {};
    [["tr", "TR", e.turkish], ["en", "EN", e.english]].forEach(([k, code, text]) => {
      const o = out[k];
      if (!o && !text) return;
      const line = ST.el("div", "tl");
      line.appendChild(ST.el("span", "code", code));
      const st = o ? o.status : "PASS";
      const chip = ST.el("span", "ls " + st, st.replace("_", " "));
      if (o && o.reasons && o.reasons.length) chip.title = o.reasons.join(", ");
      line.appendChild(chip);
      const shown = text || (o && o.text) || (o && o.reasons && o.reasons.length ? o.reasons.join(", ") : "");
      line.appendChild(ST.el("span", "tx" + (text ? "" : " muted"), shown));
      body.appendChild(line);
    });
    const why = [];
    if (e.relation) why.push(e.relation.replace("_", " "));
    if (e.citations && e.citations.length) why.push("📖 " + e.citations.join(", "));
    if (e.status === "blocked" || e.status === "dropped" || e.status === "failed" || e.status === "held") why.push(...(e.reasons || []).slice(-2));
    if (e.flags && e.flags.length) why.push(e.flags.filter((f) => !f.startsWith("source_number")).slice(-2).join(" · "));
    if (e.translate_ms) why.push("translate " + ST.ms(e.translate_ms));
    if (e.asr_ms) why.push("ASR " + ST.ms(e.asr_ms));
    const w = why.filter(Boolean).join(" · ");
    if (w) body.appendChild(ST.el("div", "why", w));
    row.appendChild(body);
    row.appendChild(ST.el("div", "st " + e.status, e.status.replace("_", " ")));
    frag.appendChild(row);
  });
  feed.replaceChildren(frag);
  const c = p.counts || {};
  $("feedCounts").textContent = `blocked ${c.blocked || 0} · held ${c.held || 0} · JA covered ${c.ja_covered || 0}`;
}

function renderHistory(hist) {
  const box = $("history");
  const frag = document.createDocumentFragment();
  [...hist].reverse().slice(0, 14).forEach((r) => {
    const row = ST.el("div", "h");
    row.appendChild(ST.el("span", "t", ST.clock(r.at)));
    row.appendChild(ST.el("span", "", r.text));
    row.appendChild(ST.el("span", "s " + r.status, r.status.replace("dropped_", "drop:") + (r.seq ? " #" + r.seq : "")));
    frag.appendChild(row);
  });
  box.replaceChildren(frag);
}

function renderAlerts(alerts) {
  const box = $("alerts");
  const frag = document.createDocumentFragment();
  [...alerts].reverse().forEach((a) => {
    const row = ST.el("div", "alert " + a.level);
    const body = ST.el("div");
    body.appendChild(ST.el("div", "", a.message + (a.count > 1 ? `  (×${a.count})` : "")));
    body.appendChild(ST.el("div", "when", ST.clock(a.at) + " · " + a.code));
    row.appendChild(body);
    const x = ST.el("button", "x", "✕");
    x.title = "Dismiss";
    x.addEventListener("click", () => run(null, "ack_alert", { id: a.id }));
    row.appendChild(x);
    frag.appendChild(row);
  });
  box.replaceChildren(frag);
  $("alertCount").textContent = alerts.length ? String(alerts.length) : "";
}

let phraseKey = "";
function renderPhrases(phrases, state) {
  const key = JSON.stringify(phrases);
  const box = $("phrases");
  if (key !== phraseKey) {
    phraseKey = key;
    box.replaceChildren();
    phrases.forEach((p) => {
      const b = ST.el("button", "phrase", p.label || p.text);
      b.title = p.text;
      b.addEventListener("click", () => {
        if (confirm(`Send to Zoom now?\n\n${p.text}`)) run(b, "send_phrase", { text: p.text });
      });
      box.appendChild(b);
    });
  }
  const ok = (state === "LIVE" || state === "PAUSED") && !stale;
  box.querySelectorAll("button").forEach((b) => { b.disabled = !ok; });
}

function renderControls(s) {
  const st = s.output.state;
  const canControl = !stale && (s.you.has_control || !s.lease.holder);
  const set = (id, enabled) => { $(id).disabled = !enabled; };
  set("btnConnect", canControl && ["DISCONNECTED", "CONNECTED", "PAUSED"].includes(st));
  const viewerOn = !!(s.output.viewer && s.output.viewer.enabled);
  const zoomReady = st === "CONNECTED" && s.health && s.health.zoom.seq_state === "known";
  const viewerOnly = st === "DISCONNECTED" && viewerOn;  // guests' viewer without Zoom captions
  set("btnLive", canControl && (zoomReady || viewerOnly) && ["RUNNING", "DEGRADED"].includes(s.engine.state));
  set("btnPause", canControl && (st === "LIVE" || st === "PAUSED"));
  $("btnPause").textContent = st === "PAUSED" ? "Resume" : "Pause";
  set("btnStop", canControl && (st === "LIVE" || st === "PAUSED"));
  set("btnEstop", !stale && st !== "E_STOPPED");  // any operator, no lease needed
  $("btnResetEstop").classList.toggle("hidden", st !== "E_STOPPED");
  set("btnResetEstop", canControl && st === "E_STOPPED");
  $("btnLive").textContent = s.output.target === "EVENT" ? "Hold to go LIVE · WEDDING"
    : viewerOnly ? "Hold to go LIVE · VIEWER ONLY" : "Hold to go LIVE";
  if (!$("btnLive").querySelector(".fill")) { const f = ST.el("span", "fill"); $("btnLive").appendChild(f); }
  const c = s.context;
  $("tMain").classList.toggle("on", c.main_speech);
  $("tMain").title = c.main_speech_by ? "Last changed by " + c.main_speech_by : "";
  $("tMusic").classList.toggle("on", c.music_mode);
  $("jaPolicy").querySelectorAll("button").forEach((b) => { b.classList.toggle("on", b.dataset.v === c.ja_policy); b.disabled = !canControl; });
  $("outMode").querySelectorAll("button").forEach((b) => { b.classList.toggle("on", b.dataset.v === c.output_mode); b.disabled = !canControl; });
  set("btnTest", canControl && s.output.target === "TEST" && ["CONNECTED", "LIVE", "PAUSED"].includes(st));
  $("btnTest").classList.toggle("hidden", s.output.target === "EVENT");
  ["btnEngineStart", "btnEngineStop", "btnResync", "btnWarm", "btnAutoMatch", "btnSpeakerCtl"].forEach((id) => set(id, canControl));
  set("btnEngineStart", canControl && !s.engine.processing && s.engine.state !== "FAULT");
  set("btnEngineStop", canControl && s.engine.processing);
  $("btnAutoMatch").textContent = "Auto matching: " + (c.auto_matching_enabled ? "on" : "off");
  $("btnSpeakerCtl").textContent = "Speaker page: " + (c.speaker_control_enabled ? "on" : "off");
  $("btnTake").classList.toggle("hidden", s.you.has_control || !s.lease.holder);
  let hint = "";
  if (!canControl && !stale) hint = `Read-only: ${s.lease.holder} has control.`;
  else if (st === "CONNECTED" && s.health && s.health.zoom.seq_state !== "known") hint = "Caption number unknown - use “Re-read seq” in diagnostics.";
  else if (st === "LIVE" && s.output.zoom_suspended) hint = "Zoom captions SUSPENDED after failures - guest viewer continues. Fix the meeting, then Re-read seq.";
  else if (st === "LIVE") hint = "E-STOP halts output instantly (and clears guest screens). Zoom itself is never affected.";
  $("controlHint").textContent = hint;
}

function renderDiag(s) {
  const h = s.health || {};
  const a = h.audio || {};
  kv("dAudio", [["state", a.state], ["level", a.level_dbfs + " dBFS"], ["peak", a.peak + " dBFS"], ["VAD", a.vad_prob],
    ["segments", a.segments], ["gaps", a.gaps], ["sender", a.sender && a.sender.device]]);
  const pct = Math.max(0, Math.min(100, ((a.level_dbfs || -90) + 60) / 60 * 100));
  $("meter").style.width = pct + "%";
  const asr = h.asr || {};
  kv("dAsr", [["engine", asr.engine || s.config.asr_engine], ["model", asr.model], ["state", asr.state],
    ["latency p50", ST.ms(asr.latency_ms && asr.latency_ms.p50)], ["p95", ST.ms(asr.latency_ms && asr.latency_ms.p95)],
    ["restarts", asr.restarts], ["dropped", asr.dropped]]);
  const c = h.claude || {};
  kv("dClaude", [["model", c.model], ["requests", c.requests], ["p50", ST.ms(c.latency_ms && c.latency_ms.p50)],
    ["p95", ST.ms(c.latency_ms && c.latency_ms.p95)], ["fallbacks", c.fallback_used], ["spent", "$" + (c.spent_usd || 0)],
    ["error", c.last_error || c.error]]);
  const z = h.zoom || {};
  kv("dZoom", [["target", z.target], ["seq", z.last_confirmed], ["seq state", z.seq_state], ["source", z.seq_source],
    ["sent", z.sent_total], ["abandoned", z.abandoned_total], ["last error", z.last_error],
    ["zoom p50", ST.ms(s.output.zoom_ms && s.output.zoom_ms.p50)]]);
  const v = s.output.viewer;
  $("dViewer").classList.toggle("hidden", !(v && v.enabled));
  if (v && v.enabled) {
    kv("dViewer", [["guest viewer", v.status], ["published", v.published], ["local viewers", v.local_viewers],
      ["relay", v.relay.configured ? "on" : "off"], ["relay guests", v.relay.viewers], ["relay sent", v.relay.sent],
      ["relay failed", v.relay.failed], ["dropped stale", v.relay.dropped_stale], ["queue", v.relay.queue],
      ["relay p50", ST.ms(v.relay.latency_p50_ms)], ["last error", v.relay.last_error]]);
  }
  kv("dEngine", [["engine", s.engine.state], ["detail", s.engine.detail], ["queue", s.output.queue],
    ["pending", s.pipeline && s.pipeline.pending], ["glossary", s.glossary ? `${s.glossary.terms} terms` : "—"],
    ["names", s.glossary && s.glossary.names ? s.glossary.names.map((n) => n.tr + (n.approved ? " ✓" : " ?")).join(", ") : "—"]]);
}

boot();
