// Shared client helpers: API, authoritative-state WebSocket, hold-to-confirm.
"use strict";

const ST = {
  $(id) { return document.getElementById(id); },

  async post(path, body) {
    const r = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (_) { /* ignore */ }
    return { ok: r.ok, status: r.status, data };
  },

  uuid() {
    // randomUUID needs a secure context (HTTPS/localhost); plain-HTTP LAN access falls back
    // to getRandomValues, which is available everywhere.
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  },

  // Sends a command. The UI never shows success before the server acknowledges.
  async command(type, args, opts) {
    const body = { command_id: ST.uuid(), type, args: args || {} };
    if (opts && opts.version !== undefined) body.expected_version = opts.version;
    try {
      const r = await ST.post("/api/command", body);
      if (r.status === 401) return { accepted: false, error: "Signed out", auth: true };
      return r.data || { accepted: false, error: "No response" };
    } catch (e) {
      return { accepted: false, error: "Network error - command NOT applied" };
    }
  },

  // Live state socket with stale detection and engine-restart detection.
  connect(handlers) {
    let ws = null, lastMsg = 0, bootId = null, backoff = 500, closed = false;
    const open = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/ui`);
      ws.onopen = () => { backoff = 500; };
      ws.onmessage = (ev) => {
        const snap = JSON.parse(ev.data);
        lastMsg = Date.now();
        if (bootId && snap.boot_id !== bootId) { handlers.onRestart(); return; }
        bootId = snap.boot_id;
        handlers.onSnapshot(snap);
      };
      ws.onclose = (ev) => {
        if (closed) return;
        if (ev.code === 4401) { handlers.onAuth(); return; }
        setTimeout(open, backoff);
        backoff = Math.min(5000, backoff * 2);
      };
      ws.onerror = () => { /* onclose follows */ };
    };
    open();
    setInterval(() => {
      const stale = !lastMsg || Date.now() - lastMsg > 3000;
      handlers.onStale(stale, lastMsg);
      if (ws && ws.readyState === 1) { try { ws.send("ping"); } catch (_) { /* ignore */ } }
    }, 1000);
    // After a restart the server has a new boot id: our cookie is invalid.
    setInterval(async () => {
      if (!bootId) return;
      try {
        const r = await fetch("/api/session", { credentials: "same-origin" });
        const d = await r.json();
        if (d.boot_id && d.boot_id !== bootId) handlers.onRestart();
      } catch (_) { /* server down: the stale overlay covers it */ }
    }, 4000);
    return { close() { closed = true; if (ws) ws.close(); } };
  },

  // Press-and-hold confirmation (pointer or keyboard Space/Enter).
  hold(el, ms, onConfirm) {
    let start = 0, raf = 0, fill = el.querySelector(".fill");
    if (!fill) { fill = document.createElement("span"); fill.className = "fill"; el.appendChild(fill); }
    const reset = () => { start = 0; cancelAnimationFrame(raf); fill.style.width = "0"; };
    const tick = () => {
      if (!start) return;
      const p = Math.min(1, (performance.now() - start) / ms);
      fill.style.width = (p * 100) + "%";
      if (p >= 1) { reset(); onConfirm(); return; }
      raf = requestAnimationFrame(tick);
    };
    const begin = (e) => { if (el.disabled || start) return; e.preventDefault(); start = performance.now(); raf = requestAnimationFrame(tick); };
    el.addEventListener("pointerdown", begin);
    ["pointerup", "pointerleave", "pointercancel"].forEach((n) => el.addEventListener(n, reset));
    el.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) begin(e); });
    el.addEventListener("keyup", reset);
    el.addEventListener("click", (e) => e.preventDefault());
  },

  toast(msg, isError) {
    const t = document.createElement("div");
    t.className = "toast" + (isError ? " error" : "");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), isError ? 5200 : 2600);
  },

  ago(ts) {
    if (!ts) return "—";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.round(s) + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    return Math.round(s / 3600) + "h ago";
  },

  clock(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  },

  ms(v) { return v === null || v === undefined ? "—" : (v >= 1000 ? (v / 1000).toFixed(1) + "s" : Math.round(v) + "ms"); },

  el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  },
};
