/* Command Center dashboard */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let charts = {}, leadState = { q: "", stage: "", sort: "last_activity_at" }, currentLead = null, activeView = "today";
let ownerFilter = "";  // "", "chris", or "will"

/* ---------------- toast ---------------- */
let toastTimer;
function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
}

const CAT_COLOR = {
  "Live Conversations": "#34d399", "Appointments": "#34d399", "Callbacks": "#22d3ee",
  "Voicemail / Machine": "#60a5fa", "No Answer / Busy": "#64748b", "Dropped / Dead Air": "#fbbf24",
  "Bad / Dead / Spam": "#f87171", "Not Interested": "#fbbf24", "Do Not Call": "#f87171", "Other": "#8497ad",
};
const esc = s => (s ?? "").toString().replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const initials = n => (n || "?").trim().split(/\s+/).map(w => w[0]).slice(0, 2).join("").toUpperCase();

function fmtClock(iso) {
  if (!iso) return "";
  const d = new Date(iso.replace(" ", "T"));
  if (isNaN(d)) return iso;
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
               : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtTalk(s) { s = Math.round(s || 0); return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "—"; }
function telLink(p) { return p ? `<a class="tel" href="tel:${esc(p)}" onclick="event.stopPropagation()">${esc(p)}</a>` : ""; }

/* ---------------- navigation ---------------- */
function showView(v) {
  activeView = v;
  $$(".view").forEach(el => el.classList.add("hidden"));
  $("#view-" + v).classList.remove("hidden");
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === v));
}
$$(".tab").forEach(t => t.onclick = () => {
  showView(t.dataset.view);
  if (t.dataset.view === "today") loadToday();
  if (t.dataset.view === "automations") loadAutomations();
  if (t.dataset.view === "analytics") loadAnalytics();
  if (t.dataset.view === "leads") loadLeads();
});
$("#backBtn").onclick = () => showView("leads");
$("#refreshBtn").onclick = async () => { await fetch("/api/sync", { method: "POST" }); toast("Synced with dialers"); refreshVisibleView(); };

/* ---------------- owner filter (Chris / Will / All) ---------------- */
$$("#ownerToggle .ot").forEach(b => b.onclick = () => {
  $$("#ownerToggle .ot").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  ownerFilter = b.dataset.owner;
  refreshVisibleView();
});

/* ---------------- TODAY / action queue ---------------- */
async function loadToday() {
  const qs = ownerFilter ? "?owner=" + ownerFilter : "";
  fetch("/api/briefing").then(r => r.json()).then(b => {
    const hour = new Date().getHours();
    const greet = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    $("#briefing").innerHTML = `<div class="brief-greet">${greet}, Chris 👋</div>
      <ul class="brief-list">${b.lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>`;
  });
  const d = await (await fetch("/api/action-queue" + qs)).json();
  const h = d.hero;
  $("#heroAppts").textContent = h.appointments_total;
  $("#heroApptsToday").textContent = h.appointments_today ? `+${h.appointments_today} today` : "none today yet";
  $("#heroMinis").innerHTML = [
    ["Live Today", h.live_today, "c-live"],
    ["Calls Today", h.calls_today, "c-call2"],
    ["Callbacks Due", h.callbacks_due, "c-callback"],
  ].map(([l, v, c]) => `<div class="mini ${c}"><div class="mini-val">${v}</div><div class="mini-label">${l}</div></div>`).join("");

  $("#cbCount").textContent = d.callbacks.length;
  $("#apCount").textContent = d.appointments.length;
  $("#cnCount").textContent = d.call_next.length;

  $("#callbackList").innerHTML = d.callbacks.map(c => qRow(c, "callback")).join("")
    || emptyState("No callbacks due", "When someone asks for a callback, they show up here first.");
  $("#apptList").innerHTML = d.appointments.map(apptRow).join("")
    || emptyState("No appointments yet", "Set one from any lead and it appears here.");
  $("#callNextList").innerHTML = d.call_next.map(c => qRow(c, "next")).join("")
    || emptyState("Queue clear", "No un-reached leads right now.");
  $("#syncText").textContent = "synced " + d.synced_at;
}
function qRow(c, kind) {
  (window._lastLeads = window._lastLeads || {})[c.id] = c.full_name || ("Lead #" + c.id);
  const where = [c.city, c.county ? c.county + " Co." : ""].filter(Boolean).join(", ");
  const meta = [c.age ? "Age " + c.age : "", where, c.reason || ""].filter(Boolean).join(" · ");
  return `<div class="q-row" onclick="openLead(${c.id})">
    <div class="q-av">${esc(initials(c.full_name))}</div>
    <div class="q-main">
      <div class="q-name">${esc(c.full_name || "Unknown")} <span class="q-score">${c.lead_score}</span></div>
      <div class="q-meta">${esc(meta)}</div>
    </div>
    <div class="q-right">${telLink(c.phone)}
      <button class="q-btn" onclick="event.stopPropagation();actAppt(${c.id})">Set Appt</button>
    </div></div>`;
}
function apptRow(a) {
  return `<div class="q-row" onclick="openLead(${a.person_id})">
    <div class="q-av appt">${esc(initials(a.full_name))}</div>
    <div class="q-main">
      <div class="q-name">${esc(a.full_name || "Unknown")}</div>
      <div class="q-meta">${esc(a.scheduled_at || "time TBD")}${a.agent ? " · " + esc(a.agent) : ""}${a.notes ? " · " + esc(a.notes) : ""}</div>
    </div>
    <div class="q-right">${telLink(a.phone)}<span class="badge b-appointment">${esc(a.status)}</span></div></div>`;
}
function emptyState(title, sub) {
  return `<div class="empty"><div class="empty-t">${esc(title)}</div><div class="empty-s">${esc(sub)}</div></div>`;
}

/* ---------------- AUTOMATIONS ---------------- */
const KIND_ICON = { call: "📞", sms: "💬", email: "✉️" };
const REASON_LABEL = {
  post_voicemail_text: "Follow-up text after voicemail", redial_after_voicemail: "Redial after voicemail",
  retry_no_answer_0: "Retry — no answer", retry_no_answer_1: "Retry — no answer (2nd)",
  retry_no_answer_2: "Retry — no answer (3rd)", retry_no_answer_3: "Retry — no answer (4th)",
  callback_default: "Scheduled callback", inbound_callback_request: "Callback they requested",
  nurture_email_1: "Intro nurture email", appt_reminder_24h: "Appointment reminder (24h)",
  appt_reminder_morning: "Appointment reminder (morning of)",
};
const rlabel = r => REASON_LABEL[r] || (r || "").replace(/_/g, " ");

async function loadAutomations() {
  const qs = ownerFilter ? "?owner=" + ownerFilter : "";
  const [a, ins] = await Promise.all([
    (await fetch("/api/automations" + qs)).json(),
    (await fetch("/api/insights")).json(),
  ]);
  const c = a.counts;
  $("#autoCounts").innerHTML = [
    ["📞", c.calls_due_today, "Calls Due Today", "c-call"],
    ["💬", c.texts_queued, "Texts Queued", "c-sms"],
    ["✉️", c.emails_queued, "Emails Queued", "c-email"],
    ["⚡", c.fired_today, "Fired Today", "c-fired"],
  ].map(([ic, v, l, cl]) => `<div class="ac ${cl}"><div class="ac-ic">${ic}</div><div><div class="ac-val">${v}</div><div class="ac-lab">${l}</div></div></div>`).join("");

  $("#upCount").textContent = a.upcoming.length + " scheduled";
  $("#upcomingList").innerHTML = a.upcoming.map(t => autoRow(t, true)).join("")
    || emptyState("Nothing scheduled yet", "As calls get dispositioned, the next actions appear here automatically.");
  $("#firedList").innerHTML = a.recent.map(t => autoRow(t, false)).join("")
    || emptyState("Nothing fired yet", "When a scheduled text or call comes due, it shows here.");

  $("#insights").innerHTML = renderInsights(ins);
  $("#syncText").textContent = "synced " + a.synced_at;
}
function autoRow(t, upcoming) {
  const when = upcoming ? fmtClock(t.due_at) : fmtClock(t.fired_at);
  const tag = upcoming ? "" : `<span class="badge b-${t.status === 'sent' ? 'voicemail' : t.status === 'done' ? 'contacted' : 'attempted'}">${esc(t.status)}</span>`;
  return `<div class="auto-r" onclick="openLead(${t.person_id})">
    <span class="auto-ic">${KIND_ICON[t.kind] || "•"}</span>
    <div class="auto-main"><div class="auto-name">${esc(t.full_name || "Unknown")}</div>
      <div class="auto-reason">${esc(rlabel(t.reason))}${t.city ? " · " + esc(t.city) : ""}</div></div>
    <div class="auto-when">${when} ${tag}</div></div>`;
}
function renderInsights(ins) {
  const rows = [];
  if (ins.overall_contact_rate) rows.push(["Overall contact rate", ins.overall_contact_rate.detail, "var(--green)"]);
  if (ins.best_call_hour) rows.push(["Best hour to call", ins.best_call_hour.label + " — " + ins.best_call_hour.detail, "var(--cyan)"]);
  if (ins.best_call_dow) rows.push(["Best day to call", ins.best_call_dow.label + " — " + ins.best_call_dow.detail, "var(--cyan)"]);
  const src = Object.entries(ins.sources || {}).sort((a, b) => b[1].rate - a[1].rate).slice(0, 5);
  let html = rows.map(([l, v, col]) => `<div class="ins-row"><div class="ins-l">${esc(l)}</div><div class="ins-v" style="color:${col}">${esc(v)}</div></div>`).join("");
  if (src.length) {
    html += `<div class="ins-sub">Source quality (live-conversation rate)</div>`;
    html += src.map(([s, d]) => `<div class="ins-row"><div class="ins-l">${esc(s)}</div><div class="ins-v">${(d.rate * 100).toFixed(0)}%</div></div>`).join("");
  }
  return html || `<div class="muted">Learning from your calls… insights appear as volume grows.</div>`;
}
$("#runNowBtn") && ($("#runNowBtn").onclick = async () => {
  const r = await (await fetch("/api/automations/run", { method: "POST" })).json();
  toast(`Fired: ${r.sms || 0} texts · ${r.call_ready || 0} calls surfaced · ${r.email || 0} emails`);
  loadAutomations();
});

/* ---------------- ANALYTICS ---------------- */
async function loadAnalytics() {
  const d = await (await fetch("/api/overview")).json();
  renderKPIs(d.kpis);
  renderTrend(d.by_day); renderDonut(d.categories); renderHour(d.by_hour);
  renderFunnel(d.funnel); renderAgents(d.per_agent); renderActivity(d.recent);
  $("#syncText").textContent = "synced " + d.synced_at;
  $("#activityMuted").textContent = d.recent.length + " most recent";
}
function renderKPIs(k) {
  const cards = [
    ["k-green", k.appointments, "Appointments", ""],
    ["k-green", k.live_conversations, "Live Conversations", k.contact_rate + "% contact rate"],
    ["k-cyan", k.callbacks_pending, "Callbacks Pending", ""],
    ["k-cyan", fmtTalk(k.avg_talk_seconds), "Avg Talk Time", ""],
    ["k-blue", k.total_leads, "Total Leads", ""],
    ["k-cyan", k.total_calls, "Total Calls", ""],
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
  charts.trend?.destroy();
  charts.trend = new Chart($("#chartTrend"), {
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

/* export current view to CSV */
$("#exportBtn").onclick = () => {
  const p = new URLSearchParams({ q: leadState.q, stage: leadState.stage });
  if (ownerFilter) p.set("owner", ownerFilter);
  window.location = "/api/export.csv?" + p;
  toast("Exporting leads to CSV…");
};
/* drag-and-drop / click import */
$("#importBtn").onclick = () => $("#importFile").click();
$("#importFile").onchange = async e => {
  const file = e.target.files[0]; if (!file) return;
  const source = prompt("Name this lead source (e.g. T65_June2026):", file.name.replace(/\.[^.]+$/, ""));
  if (source === null) return;
  const fd = new FormData(); fd.append("file", file); fd.append("source", source);
  if (ownerFilter) fd.append("owner", ownerFilter);
  toast("Importing " + file.name + "…");
  const r = await fetch("/api/import-csv", { method: "POST", body: fd });
  const j = await r.json();
  if (j.error) toast("Import failed: " + j.error, "err");
  else toast(`Imported ${j.imported} new · ${j.duplicates} dup · ${j.suppressed || 0} suppressed`, "ok");
  e.target.value = "";
  loadLeads();
};

async function loadLeads() {
  const p = new URLSearchParams({ q: leadState.q, stage: leadState.stage, sort: leadState.sort });
  if (ownerFilter) p.set("owner", ownerFilter);
  const d = await (await fetch("/api/leads?" + p)).json();
  $("#leadCount").textContent = d.count + " leads";
  $("#leadsBody").innerHTML = d.leads.map(l => {
    (window._lastLeads = window._lastLeads || {})[l.id] = l.full_name || ("Lead #" + l.id);
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
    ${(p.phones || []).map(ph => `<div class="cc-row"><span class="l">Phone</span><span class="v">${telLink(ph.e164)}</span></div>`).join("")}
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
  toast("Note added");
  if (currentLead === id) openLead(id);
}
async function actStage(id, stage) {
  if (stage === "dnc" && !confirm("Mark this lead Do Not Call and suppress their numbers?")) return;
  await fetch(`/api/lead/${id}/stage`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stage }) });
  toast(stage === "dnc" ? "Marked Do Not Call" : "Marked " + stage);
  if (currentLead === id) openLead(id); else refreshVisibleView();
}

/* ---------------- appointment modal ---------------- */
let apptLeadId = null;
function actAppt(id) {
  apptLeadId = id;
  const lead = (window._lastLeads || {})[id];
  $("#apptFor").textContent = lead ? lead : "Lead #" + id;
  // default to next business day 10am
  const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(10, 0, 0, 0);
  $("#apptWhen").value = new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  if (ownerFilter) $("#apptAgent").value = ownerFilter;
  $("#apptNotes").value = "";
  $("#apptModal").classList.remove("hidden");
}
$("#apptCancel").onclick = () => $("#apptModal").classList.add("hidden");
$("#apptModal").onclick = e => { if (e.target.id === "apptModal") $("#apptModal").classList.add("hidden"); };
$("#apptSave").onclick = async () => {
  const when = $("#apptWhen").value;
  if (!when) { toast("Pick a date & time", "err"); return; }
  const pretty = new Date(when).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  await fetch(`/api/lead/${apptLeadId}/appointment`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scheduled_at: pretty, agent: $("#apptAgent").value, notes: $("#apptNotes").value })
  });
  $("#apptModal").classList.add("hidden");
  toast("📅 Appointment set for " + pretty);
  if (currentLead === apptLeadId) openLead(apptLeadId); else refreshVisibleView();
};

/* ---------------- keyboard shortcuts ---------------- */
const VIEW_KEYS = { "1": "today", "2": "automations", "3": "analytics", "4": "leads" };
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { $("#apptModal").classList.add("hidden"); return; }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (typing) return;
  if (e.key === "/") { e.preventDefault(); showView("leads"); loadLeads(); setTimeout(() => $("#leadSearch").focus(), 50); return; }
  if (VIEW_KEYS[e.key]) {
    const v = VIEW_KEYS[e.key]; showView(v);
    ({ today: loadToday, automations: loadAutomations, analytics: loadAnalytics, leads: loadLeads }[v])();
  }
  if (e.key.toLowerCase() === "r") { fetch("/api/sync", { method: "POST" }).then(refreshVisibleView); toast("Refreshed"); }
});

/* ---------------- boot ---------------- */
function refreshVisibleView() {
  if (activeView === "today") loadToday();
  else if (activeView === "automations") loadAutomations();
  else if (activeView === "analytics") loadAnalytics();
  else if (activeView === "leads") loadLeads();
  else if (activeView === "lead" && currentLead) openLead(currentLead);
}
loadToday();
setInterval(refreshVisibleView, 15000);
