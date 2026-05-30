/* Command Center dashboard */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let charts = {}, leadState = { q: "", stage: "", sort: "last_activity_at" }, currentLead = null;

const CAT_COLOR = {
  "Live Conversations": "#34d399", "Appointments": "#34d399", "Callbacks": "#22d3ee",
  "Voicemail / Machine": "#60a5fa", "No Answer / Busy": "#64748b", "Dropped / Dead Air": "#fbbf24",
  "Bad / Dead / Spam": "#f87171", "Not Interested": "#fbbf24", "Do Not Call": "#f87171", "Other": "#8497ad",
};
const esc = s => (s ?? "").toString().replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmtClock(iso) {
  if (!iso) return "";
  const d = new Date(iso.replace(" ", "T"));
  if (isNaN(d)) return iso;
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
               : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtTalk(s) { s = Math.round(s || 0); return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "—"; }

/* ---------------- navigation ---------------- */
function showView(v) {
  $$(".view").forEach(el => el.classList.add("hidden"));
  $("#view-" + v).classList.remove("hidden");
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === v));
}
$$(".tab").forEach(t => t.onclick = () => { showView(t.dataset.view); if (t.dataset.view === "leads") loadLeads(); });
$("#backBtn").onclick = () => showView("leads");
$("#refreshBtn").onclick = async () => { await fetch("/api/sync", { method: "POST" }); loadOverview(); if (!$("#view-leads").classList.contains("hidden")) loadLeads(); };

/* ---------------- overview ---------------- */
async function loadOverview() {
  const d = await (await fetch("/api/overview")).json();
  renderKPIs(d.kpis);
  renderTrend(d.by_day); renderDonut(d.categories); renderHour(d.by_hour);
  renderFunnel(d.funnel); renderAgents(d.per_agent); renderActivity(d.recent);
  $("#syncText").textContent = "synced " + d.synced_at;
  $("#activityMuted").textContent = d.recent.length + " most recent";
}

function renderKPIs(k) {
  const cards = [
    ["k-blue", k.total_leads, "Total Leads", ""],
    ["k-cyan", k.total_calls, "Total Calls", ""],
    ["k-green", k.live_conversations, "Live Conversations", k.contact_rate + "% contact rate"],
    ["k-green", k.appointments, "Appointments", ""],
    ["k-cyan", k.callbacks_pending, "Callbacks Pending", ""],
    ["k-cyan", fmtTalk(k.avg_talk_seconds), "Avg Talk Time", ""],
    ["k-blue", k.today_calls, "Calls Today", ""],
    ["k-green", k.today_live, "Live Today", ""],
    ["k-blue", k.voicemails, "Voicemail / Machine", ""],
    ["k-amber", k.no_answers, "No Answer / Busy", ""],
    ["k-red", k.bad_numbers, "Bad / Dead / Spam", ""],
    ["k-magenta", k.contact_rate + "%", "Contact Rate", ""],
  ];
  $("#kpiGrid").innerHTML = cards.map(([c, v, l, s]) =>
    `<div class="kpi ${c}"><div class="k-val">${v}</div><div class="k-label">${l}</div>${s ? `<div class="k-sub">${s}</div>` : ""}</div>`).join("");
}

Chart.defaults.color = "#8497ad";
Chart.defaults.font.family = "Inter, sans-serif";
Chart.defaults.borderColor = "#1f2b3d";

function renderTrend(byDay) {
  const ctx = $("#chartTrend");
  charts.trend?.destroy();
  charts.trend = new Chart(ctx, {
    type: "line",
    data: {
      labels: byDay.map(d => d.day),
      datasets: [
        { label: "Calls", data: byDay.map(d => d.calls), borderColor: "#22d3ee", backgroundColor: "rgba(34,211,238,.10)", fill: true, tension: .35, borderWidth: 2, pointRadius: 0 },
        { label: "Live", data: byDay.map(d => d.live), borderColor: "#34d399", backgroundColor: "rgba(52,211,153,.12)", fill: true, tension: .35, borderWidth: 2, pointRadius: 0 },
      ],
    },
    options: { plugins: { legend: { labels: { boxWidth: 10, usePointStyle: true } } }, scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: "#16202f" } } } },
  });
}
function renderDonut(cats) {
  const labels = Object.keys(cats), vals = Object.values(cats);
  charts.donut?.destroy();
  charts.donut = new Chart($("#chartDonut"), {
    type: "doughnut",
    data: { labels, datasets: [{ data: vals, backgroundColor: labels.map(l => CAT_COLOR[l] || "#8497ad"), borderColor: "#121a28", borderWidth: 2 }] },
    options: { cutout: "62%", plugins: { legend: { position: "right", labels: { boxWidth: 9, usePointStyle: true, font: { size: 11 } } } } },
  });
}
function renderHour(byHour) {
  charts.hour?.destroy();
  charts.hour = new Chart($("#chartHour"), {
    type: "bar",
    data: { labels: byHour.map((_, h) => (h % 12 || 12) + (h < 12 ? "a" : "p")), datasets: [{ data: byHour, backgroundColor: "#22d3ee", borderRadius: 4 }] },
    options: { plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { font: { size: 9 } } }, y: { beginAtZero: true, grid: { color: "#16202f" } } } },
  });
}
function renderFunnel(f) {
  const steps = [["Leads", f.leads], ["Attempted", f.attempted], ["Contacted", f.contacted], ["Callback", f.callback], ["Appointment", f.appointment]];
  const max = Math.max(f.leads, 1);
  $("#funnel").innerHTML = steps.map(([n, v]) =>
    `<div class="f-row"><span class="f-name">${n}</span><div class="f-bar" style="width:${Math.max(8, 100 * v / max)}%">${v}</div></div>`).join("");
}
function renderAgents(agents) {
  const colors = { chris: "#22d3ee", will: "#34d399" };
  $("#perAgent").innerHTML = agents.map(a => {
    const rate = a.calls ? Math.round(100 * (a.live || 0) / a.calls) : 0;
    const col = colors[a.agent] || "#8497ad";
    return `<div class="agent-row"><div class="agent-av" style="background:${col}">${(a.agent || "?")[0].toUpperCase()}</div>
      <div class="agent-meta"><div class="agent-name">${esc(a.agent || "unknown")}</div><div class="agent-stat">${a.calls} calls · ${a.live || 0} live · ${rate}%</div></div>
      <div class="agent-big" style="color:${col}">${a.live || 0}</div></div>`;
  }).join("") || `<div class="muted">No calls yet.</div>`;
}
function renderActivity(rows) {
  $("#activity").innerHTML = rows.map(r =>
    `<div class="act-row" onclick="openLead(${r.person_id})">
      <span class="act-time">${fmtClock(r.dialed_at)}</span>
      <span class="act-name">${esc(r.full_name || "Unknown")}</span>
      <span class="act-city">${esc(r.city || "")}</span>
      ${badge(r.disposition_category, r.disposition)}
      <span class="act-agent">${esc(r.agent || "")}</span></div>`).join("");
}
function badge(cat, label) {
  const text = (label || cat || "").toString().replace(/_/g, " ");
  return `<span class="badge b-${cat || "unknown"}">${esc(text)}</span>`;
}

/* ---------------- lead book ---------------- */
const STAGES = ["", "new", "attempted", "voicemail", "contacted", "callback", "appointment", "dnc"];
$("#stageChips").innerHTML = STAGES.map(s =>
  `<div class="chip ${s === "" ? "active" : ""}" data-stage="${s}">${s === "" ? "All" : s}</div>`).join("");
$$("#stageChips .chip").forEach(c => c.onclick = () => {
  $$("#stageChips .chip").forEach(x => x.classList.remove("active")); c.classList.add("active");
  leadState.stage = c.dataset.stage; loadLeads();
});
let searchTimer;
$("#leadSearch").oninput = e => { clearTimeout(searchTimer); leadState.q = e.target.value; searchTimer = setTimeout(loadLeads, 220); };
$$(".leads-table th[data-sort]").forEach(th => th.onclick = () => { leadState.sort = th.dataset.sort; loadLeads(); });

async function loadLeads() {
  const p = new URLSearchParams({ q: leadState.q, stage: leadState.stage, sort: leadState.sort });
  const d = await (await fetch("/api/leads?" + p)).json();
  $("#leadCount").textContent = d.count + " leads";
  $("#leadsBody").innerHTML = d.leads.map(l => {
    const sc = l.lead_score >= 80 ? "#34d399" : l.lead_score >= 50 ? "#22d3ee" : "#64748b";
    return `<tr onclick="openLead(${l.id})">
      <td class="t-name">${esc(l.full_name || "Unknown")}</td>
      <td>${l.age ?? "—"}</td><td>${esc(l.city || "")}</td>
      <td>${esc(l.phone || "")}</td><td>${esc(l.source || "")}</td>
      <td>${l.call_count}</td>
      <td><span class="badge b-${l.stage}">${esc(l.stage)}</span></td>
      <td><span class="score-pill" style="background:${sc}22;color:${sc}">${l.lead_score}</span></td>
      <td>${fmtClock(l.last_activity_at)}</td>
      <td style="text-transform:capitalize">${esc(l.owner || "")}</td></tr>`;
  }).join("") || `<tr><td colspan="10" class="muted" style="padding:30px;text-align:center">No leads match.</td></tr>`;
}

/* ---------------- lead detail ---------------- */
async function openLead(id) {
  currentLead = id;
  const d = await (await fetch("/api/lead/" + id)).json();
  const p = d.person;
  $("#contactCard").innerHTML = `
    <div class="cc-name">${esc(p.full_name || "Unknown")}</div>
    <div class="cc-meta">${esc(p.city || "")}${p.county ? ", " + esc(p.county) + " Co." : ""} ${p.age ? "· Age " + p.age : ""}</div>
    <div style="margin:14px 0"><span class="badge b-${p.stage}">${esc(p.stage)}</span> <span class="score-pill" style="background:#22d3ee22;color:#22d3ee">Score ${p.lead_score}</span></div>
    ${p.phones.map(ph => `<div class="cc-row"><span class="l">Phone</span><span class="v">${esc(ph.e164)}</span></div>`).join("")}
    ${p.address ? `<div class="cc-row"><span class="l">Address</span><span class="v">${esc(p.address)}</span></div>` : ""}
    <div class="cc-row"><span class="l">Birthday</span><span class="v">${esc(p.birthday || "—")}</span></div>
    <div class="cc-row"><span class="l">Source</span><span class="v">${esc(p.source || "—")}</span></div>
    <div class="cc-row"><span class="l">Owner</span><span class="v" style="text-transform:capitalize">${esc(p.owner || "—")}</span></div>
    <div class="cc-actions">
      <button class="btn btn-primary" onclick="actAppt(${id})">＋ Set Appointment</button>
      <button class="btn btn-soft" onclick="actNote(${id})">＋ Add Note</button>
      <button class="btn btn-soft" onclick="actStage(${id},'callback')">Mark Callback</button>
      <button class="btn btn-danger" onclick="actStage(${id},'dnc')">Do Not Call</button>
    </div>`;
  $("#timeline").innerHTML = d.timeline.slice().reverse().map(renderTL).join("") || `<div class="muted">No activity yet.</div>`;
  showView("lead");
}
function renderTL(t) {
  if (t.type === "call") {
    const cat = t.disposition_category;
    const dotc = cat === "live_conversation" ? "live" : cat === "voicemail" ? "vm" : cat === "bad_number" || cat === "dnc" ? "bad" : cat === "no_answer" ? "no" : "";
    const det = [t.live_talk_seconds > 0 ? "Talked " + fmtTalk(t.live_talk_seconds) : null,
                 t.answer_to_agent_seconds ? "Connected in " + Math.round(t.answer_to_agent_seconds) + "s" : null,
                 t.amd_result ? "AMD: " + t.amd_result : null, t.from_number ? "from " + t.from_number : null]
                 .filter(Boolean).join(" · ");
    return `<div class="tl-item"><div class="tl-dot ${dotc}"></div>
      <div class="tl-head">${badge(cat, t.disposition)} <span class="tl-when">${fmtClock(t.dialed_at)}</span> <span class="tl-when" style="text-transform:capitalize">${esc(t.agent || "")}</span></div>
      ${det ? `<div class="tl-detail">${esc(det)}</div>` : ""}</div>`;
  }
  if (t.type === "note") return `<div class="tl-item"><div class="tl-dot note"></div><div class="tl-head"><b>Note</b> <span class="tl-when">${fmtClock(t.at)}</span></div><div class="tl-note">${esc(t.body)}</div></div>`;
  if (t.type === "appointment") return `<div class="tl-item"><div class="tl-dot appt"></div><div class="tl-head"><span class="badge b-appointment">Appointment</span> <span class="tl-when">${fmtClock(t.at)}</span></div><div class="tl-detail">${esc(t.scheduled_at || "")} ${esc(t.notes || "")}</div></div>`;
  if (t.type === "message") return `<div class="tl-item"><div class="tl-dot"></div><div class="tl-head"><b>${esc(t.channel)} ${esc(t.direction)}</b> <span class="tl-when">${fmtClock(t.at)}</span></div><div class="tl-note">${esc(t.body)}</div></div>`;
  return "";
}
async function actNote(id) {
  const body = prompt("Add a note for this lead:"); if (!body) return;
  await fetch(`/api/lead/${id}/note`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ body }) });
  openLead(id);
}
async function actAppt(id) {
  const when = prompt("Appointment date/time (e.g. 2026-06-03 2:00 PM):"); if (!when) return;
  await fetch(`/api/lead/${id}/appointment`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scheduled_at: when }) });
  openLead(id);
}
async function actStage(id, stage) {
  if (stage === "dnc" && !confirm("Mark this lead Do Not Call and suppress their numbers?")) return;
  await fetch(`/api/lead/${id}/stage`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stage }) });
  openLead(id);
}

/* ---------------- boot ---------------- */
loadOverview();
setInterval(() => { if (!$("#view-overview").classList.contains("hidden")) loadOverview(); }, 15000);
