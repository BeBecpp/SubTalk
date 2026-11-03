// SubTalk public caption relay: Cloudflare Worker + one Durable Object.
//
//   Computer B (engine) --POST /publish (Bearer PUBLISH_TOKEN)--> Worker --> CaptionRoom (DO)
//   Guests  <--wss /ws/viewer (read-only)--  CaptionRoom fans each message out to every socket
//
// No database: the room keeps the last captions in memory only. If the object
// is evicted while idle, the next engine heartbeat (every 15 s) restores the
// status; reconnecting guests then see only newer captions.
// Guests can never publish: messages from viewer sockets are ignored, apart
// from "ping", which the runtime answers with "pong" without waking the object.
import { DurableObject } from "cloudflare:workers";
import VIEWER_HTML from "../../src/subtalk/web/viewer.html";

const MAX_BODY = 64 * 1024;
const MAX_TEXT = 600;
const RECENT_MAX = 30;
const TYPES = new Set(["caption", "status", "clear", "heartbeat"]);
const STATES = new Set(["live", "paused", "stopped"]);

const PAGE_HEADERS = {
  "Content-Type": "text/html; charset=utf-8",
  "Cache-Control": "no-cache",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "X-Robots-Tag": "noindex, nofollow",
  "Content-Security-Policy":
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; " +
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

async function tokenOk(request, env) {
  const expected = env.PUBLISH_TOKEN || "";
  const auth = request.headers.get("Authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (expected.length < 24 || !given) return false;
  // Constant-time comparison of fixed-length digests.
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(given)),
    crypto.subtle.digest("SHA-256", enc.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(a, b);
}

function str(v) {
  return typeof v === "string" ? v.slice(0, MAX_TEXT) : null;
}

function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// Keep only the fields the viewer understands; anything else is dropped.
export function clean(m) {
  if (!m || typeof m !== "object" || !TYPES.has(m.type)) return null;
  if (m.type === "caption") {
    const tr = str(m.tr), en = str(m.en);
    if (!tr && !en) return null;
    return {
      type: "caption", seq: num(m.seq), event_id: str(m.event_id), t: num(m.t), speech_end: num(m.speech_end),
      kind: m.kind === "manual" ? "manual" : "auto", tr, en,
    };
  }
  if (m.type === "status" || m.type === "heartbeat") {
    if (!STATES.has(m.state)) return null;
    return { type: m.type, state: m.state, t: num(m.t) };
  }
  return { type: "clear", t: num(m.t) };
}

export class CaptionRoom extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.recent = [];
    this.status = { type: "status", state: "stopped", t: null };
    this.sourceAt = null; // seconds; last time the engine published anything
    this.maxViewers = Number(env.MAX_VIEWERS || 3000);
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/ws/viewer") return this.join();
    if (url.pathname === "/publish") return this.publish(request);
    if (url.pathname === "/stats") return json({ viewers: this.ctx.getWebSockets().length, source_t: this.sourceAt });
    return json({ error: "not found" }, 404);
  }

  join() {
    if (this.ctx.getWebSockets().length >= this.maxViewers) return json({ error: "full" }, 503);
    const pair = new WebSocketPair();
    this.ctx.acceptWebSocket(pair[1]);
    pair[1].send(JSON.stringify({
      type: "hello", recent: this.recent.slice(-10), status: this.status,
      t: Date.now() / 1000, source_t: this.sourceAt,
    }));
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  async publish(request) {
    const text = await request.text();
    if (text.length > MAX_BODY) return json({ error: "too large" }, 413);
    let batch;
    try {
      batch = JSON.parse(text);
    } catch {
      return json({ error: "bad json" }, 400);
    }
    if (!Array.isArray(batch)) batch = [batch];
    let accepted = 0;
    for (const raw of batch.slice(0, 50)) {
      const m = clean(raw);
      if (!m) continue;
      accepted++;
      this.sourceAt = Date.now() / 1000;
      if (m.type === "caption") {
        this.recent.push(m);
        if (this.recent.length > RECENT_MAX) this.recent.splice(0, this.recent.length - RECENT_MAX);
      } else if (m.type === "clear") {
        this.recent = [];
      } else {
        this.status = { type: "status", state: m.state, t: m.t };
      }
      this.broadcast(JSON.stringify(m));
    }
    return json({ ok: true, accepted, viewers: this.ctx.getWebSockets().length });
  }

  broadcast(msg) {
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.send(msg);
      } catch {
        // socket already closing; the runtime removes it
      }
    }
  }

  async webSocketMessage() {
    // Viewers are read-only. ("ping" never reaches here: it is auto-answered.)
  }

  async webSocketClose(ws, code) {
    try {
      ws.close(code === 1005 || code === 1006 ? 1000 : code, "bye");
    } catch {
      // already closed
    }
  }

  async webSocketError(ws) {
    try {
      ws.close(1011, "error");
    } catch {
      // already closed
    }
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const room = () => env.ROOM.get(env.ROOM.idFromName("event"));

    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/viewer")) {
      return new Response(VIEWER_HTML, { headers: PAGE_HEADERS });
    }
    if (url.pathname === "/ws/viewer") {
      if (request.headers.get("Upgrade") !== "websocket") return json({ error: "expected websocket" }, 426);
      return room().fetch(request);
    }
    if (url.pathname === "/publish") {
      if (request.method !== "POST") return json({ error: "method" }, 405);
      if (!(await tokenOk(request, env))) return json({ error: "unauthorized" }, 401);
      return room().fetch(request);
    }
    if (url.pathname === "/healthz" && request.method === "GET") {
      const r = await room().fetch(new Request(new URL("/stats", url)));
      const s = await r.json();
      return json({ ok: true, viewers: s.viewers, source_age_s: s.source_t ? Math.round(Date.now() / 1000 - s.source_t) : null });
    }
    return json({ error: "not found" }, 404);
  },
};
