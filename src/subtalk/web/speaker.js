// Minimal Speaker page: Main Speech start/end + basic status only.
"use strict";

const $ = ST.$;
let snap = null, stale = true, conn = null, wakeLock = null;

const VERDICT_MN = {
  CAPTIONING: "Орчуулга ажиллаж байна", LISTENING: "Сонсож байна", DEGRADED: "Орчуулга тасалдаж магадгүй",
  NOT_SENDING: "Орчуулга илгээгдэхгүй байна", PAUSED: "Орчуулга түр зогссон", READY: "Бэлэн (шууд эфирт биш)",
  OFFLINE: "Zoom холбогдоогүй", ESTOP: "Орчуулга зогссон", FAULT: "Системийн алдаа",
};

async function boot() {
  const r = await fetch("/api/session", { credentials: "same-origin" });
  if (r.status === 401) {
    const d = await r.json().catch(() => ({}));
    $("dev").textContent = d.dev_passcodes ? "Mock mode: dev-speaker" : "";
    $("login").classList.remove("hidden");
    return;
  }
  start();
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await ST.post("/api/login", { role: "speaker", passcode: $("pass").value, name: "Speaker" });
  if (!r.ok) { $("err").textContent = (r.data && r.data.error) || "Алдаа"; return; }
  $("login").classList.add("hidden");
  start();
});

function start() {
  $("app").classList.remove("hidden");
  requestWake();
  conn = ST.connect({
    onSnapshot: render,
    onStale: (s) => { stale = s; $("stale").classList.toggle("hidden", !s); if (snap) render(snap); },
    onRestart: () => { $("staleMsg").textContent = "Систем дахин эхэллээ — дахин нэвтэрнэ үү."; $("stale").classList.remove("hidden"); setTimeout(() => location.reload(), 2500); },
    onAuth: () => location.reload(),
  });
}

async function requestWake() {
  try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch (_) { /* optional */ }
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") requestWake(); });

ST.hold($("msBtn"), 1000, async () => {
  if (stale || !snap) return;
  const on = !snap.context.main_speech;
  $("msBtn").classList.add("pending");
  const res = await ST.command("set_main_speech", { on });
  $("msBtn").classList.remove("pending");
  if (!res.accepted) ST.toast(res.error || "Татгалзсан", true);
});

function render(s) {
  snap = s;
  $("verdict").textContent = VERDICT_MN[s.verdict.code] || s.verdict.label;
  $("verdict").style.color = { green: "var(--green)", amber: "var(--amber)", red: "var(--red)", estop: "var(--red)" }[s.verdict.level] || "";
  $("detail").textContent = s.event_name || "";
  const on = s.context.main_speech;
  $("msState").textContent = on ? "ИДЭВХТЭЙ" : "Идэвхгүй";
  $("msState").style.color = on ? "var(--gold-2)" : "";
  const b = $("msBtn");
  b.textContent = on ? "Үндсэн илтгэл ДУУСГАХ" : "Үндсэн илтгэл ЭХЛЭХ";
  if (!b.querySelector(".fill")) b.appendChild(ST.el("span", "fill"));
  b.classList.toggle("on", on);
  b.disabled = stale || !s.context.speaker_control_enabled;
  $("msNote").textContent = s.context.speaker_control_enabled
    ? "Товчийг 1 секунд дарж байна уу. Энэ горим нь орчуулгыг зогсоохгүй — зөвхөн япон орчуулагчтай ажиллах тохиргоог асаана."
    : "Операторын шийдвэрээр энэ товч түр идэвхгүй байна.";
}

boot();
