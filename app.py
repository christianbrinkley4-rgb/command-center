"""
Command Center — dashboard server (Flask).

Serves a single, polished operations dashboard over the database: live KPIs,
charts, a searchable lead book, and a full per-person timeline. Reads from the
dialers via the sync engine on a background timer, so it stays live while you
dial — without touching the dialers themselves.

Run:  python app.py    then open  http://localhost:5055
"""
import hmac
import os
import threading
import time
from datetime import datetime, date, timedelta

from flask import Flask, jsonify, request, render_template, Response

import db
import sync
from agent_api import agent_api, _ensure_columns as _agent_ensure_columns

app = Flask(__name__)
app.register_blueprint(agent_api)
PORT = int(os.getenv("COMMAND_CENTER_PORT", "5055"))
SYNC_INTERVAL = int(os.getenv("COMMAND_CENTER_SYNC_SECONDS", "30"))

# --- Security gate -----------------------------------------------------------
# Login is ENFORCED whenever a password is set (i.e. on the public droplet).
# Locally, with no password set, it stays open so nothing is locked out on your
# own machine. Set CC_PASSWORD on the server and auth turns on automatically.
CC_USER = os.getenv("CC_USERNAME", "chris")
CC_PASS = os.getenv("CC_PASSWORD", "")
CC_INGEST_TOKEN = os.getenv("CC_INGEST_TOKEN", "")   # bearer token for the laptop uploader


@app.before_request
def _require_auth():
    if request.path == "/health":
        return  # health check is open for the load balancer
    if request.path == "/api/ingest":
        return  # protected separately by the ingest token (below)
    if request.path.startswith("/agent/"):
        return  # Agent API enforces its own bearer-token auth (CC_AGENT_TOKEN)
    if not CC_PASS:
        return  # no password configured => local-only mode, no lock
    auth = request.authorization
    if not auth or auth.username != CC_USER or not hmac.compare_digest(auth.password or "", CC_PASS):
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="Command Center"'})


@app.route("/health")
def health():
    return "ok", 200


# ----------------------------- helpers -----------------------------

def _age(birthday):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            d = datetime.strptime(str(birthday).strip(), fmt).date()
            today = date.today()
            return today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        except (ValueError, TypeError):
            continue
    return None


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


# ----------------------------- API: overview -----------------------------

@app.route("/api/overview")
def api_overview():
    today = date.today().isoformat()
    with db.connect() as conn:
        total_people = conn.execute("SELECT COUNT(*) n FROM people").fetchone()["n"]
        total_calls = conn.execute("SELECT COUNT(*) n FROM call_attempts").fetchone()["n"]

        cat = {r["disposition_category"]: r["n"] for r in conn.execute(
            "SELECT disposition_category, COUNT(*) n FROM call_attempts GROUP BY disposition_category")}
        live = cat.get("live_conversation", 0)

        today_calls = conn.execute(
            "SELECT COUNT(*) n FROM call_attempts WHERE substr(dialed_at,1,10)=?", (today,)).fetchone()["n"]
        today_live = conn.execute(
            "SELECT COUNT(*) n FROM call_attempts WHERE substr(dialed_at,1,10)=? AND disposition_category='live_conversation'",
            (today,)).fetchone()["n"]

        appts = conn.execute("SELECT COUNT(*) n FROM appointments").fetchone()["n"]
        callbacks = conn.execute("SELECT COUNT(*) n FROM people WHERE stage='callback'").fetchone()["n"]

        avg_talk = conn.execute(
            "SELECT AVG(live_talk_seconds) a FROM call_attempts WHERE disposition_category='live_conversation' AND live_talk_seconds>0").fetchone()["a"] or 0

        # last 14 days of calls + live
        by_day = []
        for i in range(13, -1, -1):
            d = (date.today() - timedelta(days=i)).isoformat()
            row = conn.execute(
                """SELECT COUNT(*) calls, SUM(CASE WHEN disposition_category='live_conversation' THEN 1 ELSE 0 END) live
                   FROM call_attempts WHERE substr(dialed_at,1,10)=?""", (d,)).fetchone()
            by_day.append({"day": d[5:], "calls": row["calls"] or 0, "live": row["live"] or 0})

        # today's calls by hour
        by_hour = [0] * 24
        for r in conn.execute(
            "SELECT substr(dialed_at,12,2) hh, COUNT(*) n FROM call_attempts WHERE substr(dialed_at,1,10)=? GROUP BY hh", (today,)):
            try:
                by_hour[int(r["hh"])] = r["n"]
            except (ValueError, TypeError):
                pass

        per_agent = [dict(r) for r in conn.execute(
            """SELECT agent, COUNT(*) calls,
                      SUM(CASE WHEN disposition_category='live_conversation' THEN 1 ELSE 0 END) live
               FROM call_attempts GROUP BY agent ORDER BY calls DESC""")]

        funnel = {
            "leads": total_people,
            "attempted": conn.execute("SELECT COUNT(*) n FROM people WHERE stage NOT IN ('new')").fetchone()["n"],
            "contacted": conn.execute("SELECT COUNT(*) n FROM people WHERE stage IN ('contacted','callback','appointment')").fetchone()["n"],
            "callback": callbacks,
            "appointment": appts,
        }

        recent = [dict(r) for r in conn.execute(
            """SELECT c.dialed_at, c.disposition, c.disposition_category, c.agent, c.live_talk_seconds,
                      p.id person_id, p.full_name, p.city
               FROM call_attempts c JOIN people p ON p.id=c.person_id
               ORDER BY c.dialed_at DESC LIMIT 25""")]

    categories = {db.CATEGORY_LABELS.get(k, k): v for k, v in cat.items()}
    return jsonify({
        "kpis": {
            "total_leads": total_people, "total_calls": total_calls,
            "live_conversations": live, "contact_rate": _pct(live, total_calls),
            "voicemails": cat.get("voicemail", 0), "no_answers": cat.get("no_answer", 0),
            "bad_numbers": cat.get("bad_number", 0),
            "today_calls": today_calls, "today_live": today_live,
            "appointments": appts, "callbacks_pending": callbacks,
            "avg_talk_seconds": round(avg_talk),
        },
        "categories": categories, "by_day": by_day, "by_hour": by_hour,
        "per_agent": per_agent, "funnel": funnel, "recent": recent,
        "synced_at": datetime.now().strftime("%I:%M:%S %p"),
    })


# ----------------------------- API: action queue -----------------------------

@app.route("/api/action-queue")
def api_action_queue():
    """The daily driver: what to do next to book appointments.
    Three buckets — callbacks due (warmest), appointments to confirm, and the
    prioritized 'call next' list (best-scoring leads not yet reached)."""
    today = date.today().isoformat()
    owner = (request.args.get("owner") or "").strip().lower()
    own_sql = " AND owner=?" if owner else ""
    own_p = [owner] if owner else []
    with db.connect() as conn:
        def phone_of(pid):
            r = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? LIMIT 1", (pid,)).fetchone()
            return r["e164"] if r else ""

        # 1) Callbacks the prospect asked for — top priority.
        callbacks = []
        for r in conn.execute(
            f"""SELECT id, full_name, city, county, birthday, owner, lead_score, last_activity_at
               FROM people WHERE stage='callback'{own_sql} ORDER BY last_activity_at DESC LIMIT 100""", own_p):
            d = dict(r); d["age"] = _age(r["birthday"]); d["phone"] = phone_of(r["id"])
            callbacks.append(d)

        # 2) Appointments to confirm / upcoming.
        appt_own = " AND p.owner=?" if owner else ""
        appointments = []
        for r in conn.execute(
            f"""SELECT a.id, a.scheduled_at, a.status, a.notes, a.agent,
                      p.id person_id, p.full_name, p.city, p.birthday
               FROM appointments a JOIN people p ON p.id=a.person_id
               WHERE a.status IN ('scheduled','kept'){appt_own} ORDER BY a.scheduled_at LIMIT 100""", own_p):
            d = dict(r); d["age"] = _age(r["birthday"]); d["phone"] = phone_of(r["person_id"])
            appointments.append(d)

        # 3) Best leads to call next — not yet reached, not DNC, highest score first.
        call_next = []
        for r in conn.execute(
            f"""SELECT p.id, p.full_name, p.city, p.county, p.birthday, p.source, p.owner,
                      p.lead_score, p.stage, p.last_activity_at,
                      (SELECT COUNT(*) FROM call_attempts c WHERE c.person_id=p.id) call_count,
                      (SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id
                       ORDER BY dialed_at DESC LIMIT 1) last_outcome
               FROM people p
               WHERE p.stage IN ('new','attempted','voicemail'){own_sql}
               ORDER BY p.lead_score DESC, p.last_activity_at ASC LIMIT 60""", own_p):
            d = dict(r); d["age"] = _age(r["birthday"]); d["phone"] = phone_of(r["id"])
            d["reason"] = ("Never called" if not d["call_count"]
                           else f"{d['call_count']}x · last: " + db.CATEGORY_LABELS.get(d["last_outcome"], d["last_outcome"] or "—"))
            call_next.append(d)

        hero = {
            "appointments_total": conn.execute("SELECT COUNT(*) n FROM appointments").fetchone()["n"],
            "appointments_today": conn.execute(
                "SELECT COUNT(*) n FROM appointments WHERE substr(created_at,1,10)=?", (today,)).fetchone()["n"],
            "live_today": conn.execute(
                "SELECT COUNT(*) n FROM call_attempts WHERE substr(dialed_at,1,10)=? AND disposition_category='live_conversation'",
                (today,)).fetchone()["n"],
            "calls_today": conn.execute(
                "SELECT COUNT(*) n FROM call_attempts WHERE substr(dialed_at,1,10)=?", (today,)).fetchone()["n"],
            "callbacks_due": len(callbacks),
        }
    return jsonify({"hero": hero, "callbacks": callbacks, "appointments": appointments,
                    "call_next": call_next, "synced_at": datetime.now().strftime("%I:%M:%S %p")})


# ----------------------------- API: leads -----------------------------

@app.route("/api/leads")
def api_leads():
    q = (request.args.get("q") or "").strip()
    stage = (request.args.get("stage") or "").strip()
    sort = (request.args.get("sort") or "last_activity_at").strip()
    sort_cols = {"score": "lead_score", "name": "full_name", "city": "city",
                 "last_activity_at": "last_activity_at", "calls": "call_count"}
    order = sort_cols.get(sort, "last_activity_at")

    owner = (request.args.get("owner") or "").strip().lower()
    where, params = [], []
    if q:
        where.append("(p.full_name LIKE ? OR ph.e164 LIKE ? OR p.city LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if stage:
        where.append("p.stage=?")
        params.append(stage)
    if owner:
        where.append("p.owner=?")
        params.append(owner)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    direction = "ASC" if order == "full_name" or order == "city" else "DESC"

    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT p.id, p.full_name, p.city, p.county, p.source, p.stage, p.lead_score,
                   p.owner, p.last_activity_at, p.birthday,
                   (SELECT COUNT(*) FROM call_attempts c WHERE c.person_id=p.id) call_count,
                   (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id LIMIT 1) phone
            FROM people p
            LEFT JOIN phone_numbers ph ON ph.person_id=p.id
            {clause}
            GROUP BY p.id
            ORDER BY {order} {direction}
            LIMIT 500
        """, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["age"] = _age(r["birthday"])
        out.append(d)
    return jsonify({"leads": out, "count": len(out)})


# ----------------------------- API: lead detail -----------------------------

@app.route("/api/lead/<int:pid>")
def api_lead(pid):
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify({"error": "not found"}), 404
        person = dict(p)
        person["age"] = _age(p["birthday"])
        person["phones"] = [dict(r) for r in conn.execute(
            "SELECT e164, line_type, status FROM phone_numbers WHERE person_id=?", (pid,))]

        timeline = []
        for r in conn.execute(
            "SELECT dialed_at, disposition, disposition_category, agent, from_number, live_talk_seconds, "
            "answer_to_agent_seconds, amd_result FROM call_attempts WHERE person_id=? ORDER BY dialed_at", (pid,)):
            timeline.append({"type": "call", "at": r["dialed_at"], **dict(r)})
        for r in conn.execute("SELECT channel, direction, body, status, created_at FROM messages WHERE person_id=?", (pid,)):
            timeline.append({"type": "message", "at": r["created_at"], **dict(r)})
        for r in conn.execute("SELECT scheduled_at, status, agent, notes, created_at FROM appointments WHERE person_id=?", (pid,)):
            timeline.append({"type": "appointment", "at": r["created_at"], **dict(r)})
        for r in conn.execute("SELECT body, author, created_at FROM person_notes WHERE person_id=?", (pid,)):
            timeline.append({"type": "note", "at": r["created_at"], **dict(r)})
        timeline.sort(key=lambda x: x.get("at") or "")
    return jsonify({"person": person, "timeline": timeline})


# ----------------------------- API: actions (write-back) -----------------------------

@app.route("/api/lead/<int:pid>/note", methods=["POST"])
def api_add_note(pid):
    body = (request.json or {}).get("body", "").strip()
    if not body:
        return jsonify({"error": "empty"}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("INSERT INTO person_notes(person_id, body, author, created_at) VALUES (?,?,?,?)",
                     (pid, body, (request.json or {}).get("author", "agent"), now))
        db.audit(conn, "person", pid, "note_added", body[:80])
    return jsonify({"ok": True})


@app.route("/api/lead/<int:pid>/appointment", methods=["POST"])
def api_add_appointment(pid):
    data = request.json or {}
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO appointments(person_id, agent, scheduled_at, notes, status, created_at) VALUES (?,?,?,?, 'scheduled', ?)",
            (pid, data.get("agent", ""), data.get("scheduled_at", ""), data.get("notes", ""), now))
        conn.execute("UPDATE people SET stage='appointment', last_activity_at=? WHERE id=?", (now, pid))
        db.audit(conn, "person", pid, "appointment_set", data.get("scheduled_at", ""))
        # Auto-schedule reminder texts (24h before + morning-of) when we can parse the time.
        try:
            import automation
            dt = _parse_appt_dt(data.get("scheduled_at", ""))
            if dt:
                automation.set_appointment_reminders(conn, pid, dt, data.get("agent", ""))
        except Exception:
            pass
    return jsonify({"ok": True})


def _parse_appt_dt(text):
    """Parse the appointment datetime from the modal (uses nlp_time as a flexible parser)."""
    try:
        import nlp_time
        return nlp_time.parse_when(text)
    except Exception:
        return None


@app.route("/api/lead/<int:pid>/appointment-outcome", methods=["POST"])
def api_appointment_outcome(pid):
    """Mark the most recent appointment kept / no-show / cancelled. A no-show auto-
    reschedules a follow-up call so the lead is never dropped."""
    outcome = (request.json or {}).get("outcome", "").strip()
    if outcome not in {"kept", "no_show", "cancelled"}:
        return jsonify({"error": "bad outcome"}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        row = conn.execute("SELECT id, agent FROM appointments WHERE person_id=? ORDER BY created_at DESC LIMIT 1",
                           (pid,)).fetchone()
        if row:
            conn.execute("UPDATE appointments SET status=? WHERE id=?", (outcome, row["id"]))
        if outcome == "no_show":
            # don't lose them — auto-schedule a follow-up call next good window
            try:
                import automation
                from datetime import timedelta as _td
                when = automation._clamp_to_window(datetime.now() + _td(days=1), "call")
                automation.schedule_task(conn, pid, "call", when.isoformat(timespec="seconds"),
                                         "appointment_no_show_followup",
                                         automation._phone_of(conn, pid), "",
                                         row["agent"] if row else "")
                conn.execute("UPDATE people SET stage='callback' WHERE id=?", (pid,))
            except Exception:
                pass
        db.audit(conn, "person", pid, "appointment_outcome", outcome)
    return jsonify({"ok": True})


@app.route("/api/lead/<int:pid>/deal", methods=["POST"])
def api_add_deal(pid):
    """Log a deal (the 'into deals' half of the funnel). Tracks product, stage, and value."""
    d = request.json or {}
    now = datetime.now().isoformat(timespec="seconds")
    stage = (d.get("stage") or "quoted").strip()
    closed = now if stage in ("enrolled", "lost") else ""
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM deals WHERE person_id=? ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()
        if existing and d.get("update"):
            conn.execute("""UPDATE deals SET product=?, stage=?, est_value=?, est_commission=?,
                            lost_reason=?, notes=?, updated_at=?, closed_at=? WHERE id=?""",
                         (d.get("product", ""), stage, float(d.get("est_value") or 0),
                          float(d.get("est_commission") or 0), d.get("lost_reason", ""),
                          d.get("notes", ""), now, closed, existing["id"]))
            deal_id = existing["id"]
        else:
            cur = conn.execute("""INSERT INTO deals(person_id, agent, product, stage, est_value,
                                  est_commission, lost_reason, notes, created_at, updated_at, closed_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                               (pid, d.get("agent", ""), d.get("product", ""), stage,
                                float(d.get("est_value") or 0), float(d.get("est_commission") or 0),
                                d.get("lost_reason", ""), d.get("notes", ""), now, now, closed))
            deal_id = cur.lastrowid
        # reflect on the person
        if stage == "enrolled":
            conn.execute("UPDATE people SET stage='closed', last_activity_at=? WHERE id=?", (now, pid))
        db.audit(conn, "person", pid, "deal_" + stage, d.get("product", ""))
    return jsonify({"ok": True, "deal_id": deal_id})


@app.route("/api/lead/<int:pid>/stage", methods=["POST"])
def api_set_stage(pid):
    stage = (request.json or {}).get("stage", "").strip()
    if stage not in {"new", "attempted", "voicemail", "contacted", "callback", "appointment", "closed", "dnc"}:
        return jsonify({"error": "bad stage"}), 400
    with db.connect() as conn:
        conn.execute("UPDATE people SET stage=? WHERE id=?", (stage, pid))
        if stage == "dnc":
            ph = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (pid,)).fetchall()
            for r in ph:
                conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?, 'dnc', ?)",
                             (r["e164"], datetime.now().isoformat(timespec="seconds")))
        db.audit(conn, "person", pid, "stage_changed", stage)
    return jsonify({"ok": True})


@app.route("/api/leads/bulk", methods=["POST"])
def api_bulk():
    """Bulk action on selected leads from the Lead Book: assign owner, set DNC, or
    re-tag source. Saves a ton of clicks for a 2-person team."""
    d = request.json or {}
    ids = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()]
    action = (d.get("action") or "").strip()
    value = (d.get("value") or "").strip()
    if not ids or not action:
        return jsonify({"error": "ids and action required"}), 400
    qmarks = ",".join("?" * len(ids))
    done = 0
    with db.connect() as conn:
        if action == "owner":
            done = conn.execute(f"UPDATE people SET owner=? WHERE id IN ({qmarks})", [value] + ids).rowcount
        elif action == "source":
            done = conn.execute(f"UPDATE people SET source=? WHERE id IN ({qmarks})", [value] + ids).rowcount
        elif action == "dnc":
            done = conn.execute(f"UPDATE people SET stage='dnc' WHERE id IN ({qmarks})", ids).rowcount
            for pid in ids:
                for r in conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (pid,)):
                    conn.execute("INSERT OR IGNORE INTO suppressions(scope,value,reason,created_at) VALUES ('phone',?,'dnc',?)",
                                 (r["e164"], datetime.now().isoformat(timespec="seconds")))
        else:
            return jsonify({"error": "unknown action"}), 400
        db.audit(conn, "bulk", action, "applied", f"{done} leads -> {value}")
    return jsonify({"ok": True, "updated": done})


@app.route("/api/briefing")
def api_briefing():
    """Morning briefing: a plain-English summary of what to do today + yesterday's wins."""
    from datetime import timedelta as _td
    today = date.today().isoformat()
    yest = (date.today() - _td(days=1)).isoformat()
    with db.connect() as conn:
        def one(sql, p=()):
            return conn.execute(sql, p).fetchone()["n"]
        callbacks = one("SELECT COUNT(*) n FROM people WHERE stage='callback'")
        appts = one("SELECT COUNT(*) n FROM appointments WHERE substr(scheduled_at,1,10)>=? OR status='scheduled'", (today,))
        calls_due = one("SELECT COUNT(*) n FROM scheduled_tasks WHERE kind='call' AND status='pending' AND substr(due_at,1,10)<=?", (today,))
        texts_q = one("SELECT COUNT(*) n FROM scheduled_tasks WHERE kind='sms' AND status='pending'")
        y_live = one("SELECT COUNT(*) n FROM call_attempts WHERE substr(dialed_at,1,10)=? AND disposition_category='live_conversation'", (yest,))
        y_calls = one("SELECT COUNT(*) n FROM call_attempts WHERE substr(dialed_at,1,10)=?", (yest,))
        fresh = one("SELECT COUNT(*) n FROM people WHERE stage='new'")
    lines = []
    if callbacks: lines.append(f"{callbacks} callback{'s' if callbacks!=1 else ''} waiting — your warmest leads.")
    if calls_due: lines.append(f"{calls_due} scheduled call{'s' if calls_due!=1 else ''} due today.")
    if fresh: lines.append(f"{fresh} fresh lead{'s' if fresh!=1 else ''} ready to dial.")
    if texts_q: lines.append(f"{texts_q} follow-up text{'s' if texts_q!=1 else ''} queued to send automatically.")
    if y_calls: lines.append(f"Yesterday: {y_live} live conversation{'s' if y_live!=1 else ''} from {y_calls} calls.")
    return jsonify({"date": today, "headline": "Here's your day", "lines": lines or ["All caught up — import a list to get rolling."]})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    return jsonify({"results": sync.sync_all()})


@app.route("/api/export.csv")
def api_export_csv():
    """One-click export of the full lead book (respects the same filters as the
    Lead Book) as a CSV the user can open in Excel."""
    import csv
    import io
    stage = (request.args.get("stage") or "").strip()
    q = (request.args.get("q") or "").strip()
    where, params = [], []
    if stage:
        where.append("p.stage=?"); params.append(stage)
    if q:
        where.append("(p.full_name LIKE ? OR p.city LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    cols = ["id", "full_name", "phone", "email", "age", "city", "county", "address",
            "birthday", "source", "stage", "lead_score", "owner", "call_count",
            "last_outcome", "last_activity_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c.replace("_", " ").title() for c in cols])
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT p.id, p.full_name, p.email, p.city, p.county, p.address, p.birthday,
                   p.source, p.stage, p.lead_score, p.owner, p.last_activity_at,
                   (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id LIMIT 1) phone,
                   (SELECT COUNT(*) FROM call_attempts c WHERE c.person_id=p.id) call_count,
                   (SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id
                    ORDER BY dialed_at DESC LIMIT 1) last_outcome
            FROM people p {clause} ORDER BY p.lead_score DESC LIMIT 20000""", params).fetchall()
    for r in rows:
        d = dict(r); d["age"] = _age(r["birthday"])
        w.writerow([d.get(c, "") for c in cols])
    fname = f"command_center_leads_{date.today().isoformat()}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/api/import-csv", methods=["POST"])
def api_import_csv():
    """Drag-and-drop lead import from the dashboard. Accepts a CSV/XLSX upload,
    routes it through the same agent-API import logic (dedup + suppression-safe)."""
    import io
    import pandas as pd
    from agent_api import leads_import  # reuse the audited import path
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    source = (request.form.get("source") or os.path.splitext(f.filename or "import")[0]).strip()
    owner = (request.form.get("owner") or "").strip()
    raw = f.read()
    try:
        if (f.filename or "").lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw), dtype=str, keep_default_na=False)
        else:
            df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    except Exception as exc:
        return jsonify({"error": f"could not read file: {exc}"}), 400
    alias = {"full name": "name", "first name": "first_name", "last name": "last_name",
             "phone number": "phone", "mobile": "phone", "cell": "phone", "number": "phone",
             "telephone": "phone", "dob": "birthday", "date of birth": "birthday",
             "lead type": "lead_type", "email address": "email", "lead source": "source"}
    df = df.rename(columns={c: alias.get(str(c).strip().lower(), str(c).strip().lower()) for c in df.columns})
    leads = df.to_dict(orient="records")
    # Call the agent_api import function directly with a synthetic request body.
    with app.test_request_context(json={"source": source, "owner": owner, "leads": leads}):
        resp = leads_import()
    payload = resp.get_json() if hasattr(resp, "get_json") else resp
    return jsonify(payload)


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive a dialer's dial_results.xlsx from the laptop uploader and stash it
    where the server's sync can read it. Protected by a bearer token, not the
    dashboard login (so the uploader runs unattended)."""
    if not CC_INGEST_TOKEN:
        return jsonify({"error": "ingest disabled"}), 403
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not hmac.compare_digest(token, CC_INGEST_TOKEN):
        return jsonify({"error": "unauthorized"}), 401
    agent = (request.args.get("agent") or "agent").strip().replace("/", "_").replace("\\", "_")
    data_dir = os.getenv("CC_DATA_DIR", "")
    if not data_dir:
        return jsonify({"error": "CC_DATA_DIR not set"}), 500
    os.makedirs(data_dir, exist_ok=True)
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    f.save(os.path.join(data_dir, f"{agent}.xlsx"))
    sync.sync_all()
    return jsonify({"ok": True, "agent": agent})


# ----------------------------- automations + insights -----------------------------

@app.route("/api/automations")
def api_automations():
    """The automation control panel: what's scheduled, what fired, what's due today."""
    import automation
    automation.ensure_schema()
    today = date.today().isoformat()
    owner = (request.args.get("owner") or "").strip().lower()
    own = " AND s.owner=?" if owner else ""
    own_p = [owner] if owner else []
    with db.connect() as conn:
        def rows(sql, p=()):
            return [dict(r) for r in conn.execute(sql, p)]
        upcoming = rows(f"""
            SELECT s.id, s.kind, s.reason, s.due_at, s.status, s.channel_to, p.id person_id,
                   p.full_name, p.city, p.owner
            FROM scheduled_tasks s JOIN people p ON p.id=s.person_id
            WHERE s.status='pending'{own} ORDER BY s.due_at LIMIT 200""", own_p)
        recent = rows(f"""
            SELECT s.kind, s.reason, s.fired_at, s.status, p.full_name, p.id person_id
            FROM scheduled_tasks s JOIN people p ON p.id=s.person_id
            WHERE s.fired_at IS NOT NULL AND s.fired_at<>''{own}
            ORDER BY s.fired_at DESC LIMIT 40""", own_p)
        counts = {
            "calls_due_today": conn.execute(
                "SELECT COUNT(*) n FROM scheduled_tasks WHERE kind='call' AND status='pending' AND substr(due_at,1,10)<=?",
                (today,)).fetchone()["n"],
            "texts_queued": conn.execute(
                "SELECT COUNT(*) n FROM scheduled_tasks WHERE kind='sms' AND status='pending'").fetchone()["n"],
            "emails_queued": conn.execute(
                "SELECT COUNT(*) n FROM scheduled_tasks WHERE kind='email' AND status='pending'").fetchone()["n"],
            "fired_today": conn.execute(
                "SELECT COUNT(*) n FROM scheduled_tasks WHERE substr(fired_at,1,10)=?", (today,)).fetchone()["n"],
        }
    return jsonify({"counts": counts, "upcoming": upcoming, "recent": recent,
                    "synced_at": datetime.now().strftime("%I:%M:%S %p")})


@app.route("/api/insights")
def api_insights():
    import learning
    return jsonify(learning.get_stats())


@app.route("/api/dialer-queue")
def api_dialer_queue():
    """The single source of truth for call order. The dialer pulls this exact ranked
    list so the dashboard's order and the dialer's order ALWAYS match. Same priority
    as the Today view: scheduled-calls-due > callbacks > hot fresh > new(by score) >
    retry. Excludes DNC/suppressed/reached/future-held."""
    owner = (request.args.get("owner") or "").strip().lower()
    limit = min(int(request.args.get("limit", 1000)), 5000)
    now_iso = datetime.now().isoformat(timespec="seconds")
    cutoff_24h = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    own = " AND p.owner=?" if owner else ""
    own_p = [owner] if owner else []
    rows, seen = [], set()
    with db.connect() as conn:
        def phone_of(pid):
            r = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? AND status<>'bad' "
                             "ORDER BY status='active' DESC LIMIT 1", (pid,)).fetchone()
            return r["e164"] if r else ""
        def suppressed(e):
            return conn.execute("SELECT 1 FROM suppressions WHERE scope='phone' AND value=? LIMIT 1", (e,)).fetchone() is not None

        # future-held: a scheduled call later than now -> keep out of today's list
        held = {r["person_id"] for r in conn.execute(
            "SELECT DISTINCT person_id FROM scheduled_tasks WHERE kind='call' AND status='pending' AND due_at>?", (now_iso,))}
        due = {r["person_id"] for r in conn.execute(
            "SELECT DISTINCT person_id FROM scheduled_tasks WHERE kind='call' AND status='pending' AND due_at<=?", (now_iso,))}
        held -= due

        base = (f"SELECT p.id, p.full_name, p.city, p.county, p.birthday, p.address, p.source, "
                f"p.lead_score, p.stage, p.last_activity_at, p.owner, "
                f"(SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id ORDER BY dialed_at DESC LIMIT 1) last_outcome "
                f"FROM people p WHERE p.stage<>'dnc' AND p.stage<>'closed'{own}")

        def add(r, qtype):
            pid = r["id"]
            if pid in seen or pid in held:
                return
            ph = phone_of(pid)
            if not ph or suppressed(ph):
                return
            seen.add(pid)
            rows.append({"Queue_Type": qtype, "Name": r["full_name"] or "", "Phone": ph,
                         "Address": r["address"] or "", "City": r["city"] or "", "County": r["county"] or "",
                         "Birthday": r["birthday"] or "", "Last_Disposition": r["last_outcome"] or "",
                         "Last_Call_Time": r["last_activity_at"] or "", "Source": r["source"] or "",
                         "Lead_Score": r["lead_score"], "person_id": pid})

        # 1) scheduled calls due
        if due:
            q = base + " AND p.id IN ({}) ORDER BY p.lead_score DESC".format(",".join("?" * len(due)))
            for r in conn.execute(q, own_p + list(due)):
                add(r, "scheduled_call")
        # 2) callbacks
        for r in conn.execute(base + " AND p.stage='callback' ORDER BY p.last_activity_at ASC", own_p):
            add(r, "callback")
        # 3) hot fresh (<24h)
        for r in conn.execute(base + " AND p.stage='new' AND p.first_seen_at>=? ORDER BY p.lead_score DESC, p.first_seen_at DESC",
                              own_p + [cutoff_24h]):
            add(r, "hot_fresh")
        # 4) other new by score
        for r in conn.execute(base + " AND p.stage='new' ORDER BY p.lead_score DESC, p.first_seen_at ASC", own_p):
            add(r, "never_called")
        # 5) retry by score
        for r in conn.execute(base + " AND p.stage IN ('attempted','voicemail') ORDER BY p.lead_score DESC, p.last_activity_at ASC", own_p):
            add(r, "retry")
    return jsonify({"count": len(rows), "queue": rows[:limit]})


@app.route("/api/pipeline")
def api_pipeline():
    """The money view: deals in flight, revenue won, close rate, and the full
    lead->deal funnel with conversion rates per source (where revenue comes from)."""
    owner = (request.args.get("owner") or "").strip().lower()
    own = " AND owner=?" if owner else ""
    own_p = [owner] if owner else []
    with db.connect() as conn:
        def n(sql, p=()):
            return conn.execute(sql, p).fetchone()["n"]

        # deal stage counts + value
        deal_stages = {r["stage"]: {"count": r["c"], "value": r["v"] or 0, "comm": r["cm"] or 0}
                       for r in conn.execute(
            f"""SELECT stage, COUNT(*) c, SUM(est_value) v, SUM(est_commission) cm
                FROM deals d {('WHERE ' + own[5:]) if owner else ''} GROUP BY stage""", own_p)}

        won = deal_stages.get("enrolled", {"count": 0, "value": 0, "comm": 0})
        in_flight = sum(v["count"] for k, v in deal_stages.items() if k not in ("enrolled", "lost"))
        in_flight_comm = sum(v["comm"] for k, v in deal_stages.items() if k not in ("enrolled", "lost"))

        # the funnel
        leads = n(f"SELECT COUNT(*) n FROM people WHERE 1=1{own}", own_p)
        contacted = n(f"SELECT COUNT(*) n FROM people WHERE stage IN ('contacted','callback','appointment','closed'){own}", own_p)
        appts = n(f"SELECT COUNT(DISTINCT person_id) n FROM appointments a JOIN people p ON p.id=a.person_id WHERE 1=1{(' AND p.owner=?' if owner else '')}", own_p)
        kept = n(f"SELECT COUNT(*) n FROM appointments a JOIN people p ON p.id=a.person_id WHERE a.status='kept'{(' AND p.owner=?' if owner else '')}", own_p)
        deals_won = won["count"]

        funnel = [
            {"stage": "Leads", "count": leads},
            {"stage": "Contacted", "count": contacted},
            {"stage": "Appointments", "count": appts},
            {"stage": "Kept", "count": kept},
            {"stage": "Deals Won", "count": deals_won},
        ]

        # conversion by source: lead -> appointment -> deal
        by_source = []
        for r in conn.execute(f"""
            SELECT p.source,
                   COUNT(*) leads,
                   SUM(CASE WHEN p.stage IN ('contacted','callback','appointment','closed') THEN 1 ELSE 0 END) contacted,
                   (SELECT COUNT(DISTINCT a.person_id) FROM appointments a JOIN people pp ON pp.id=a.person_id WHERE pp.source=p.source) appts,
                   (SELECT COUNT(*) FROM deals d JOIN people pp ON pp.id=d.person_id WHERE pp.source=p.source AND d.stage='enrolled') deals
            FROM people p WHERE p.source<>''{own} GROUP BY p.source ORDER BY leads DESC LIMIT 12""", own_p):
            d = dict(r)
            d["appt_rate"] = round(100.0 * d["appts"] / d["leads"], 1) if d["leads"] else 0
            d["deal_rate"] = round(100.0 * d["deals"] / d["leads"], 2) if d["leads"] else 0
            by_source.append(d)

        recent_deals = [dict(r) for r in conn.execute(f"""
            SELECT d.id, d.product, d.stage, d.est_value, d.est_commission, d.updated_at,
                   p.id person_id, p.full_name, p.city, p.owner
            FROM deals d JOIN people p ON p.id=d.person_id
            WHERE 1=1{(' AND p.owner=?' if owner else '')} ORDER BY d.updated_at DESC LIMIT 20""", own_p)]

        appt_kept_rate = round(100.0 * kept / appts, 1) if appts else 0
        close_rate = round(100.0 * deals_won / kept, 1) if kept else 0
    return jsonify({
        "kpis": {
            "deals_won": won["count"], "revenue_won": won["value"], "commission_won": won["comm"],
            "deals_in_flight": in_flight, "commission_in_flight": in_flight_comm,
            "appt_kept_rate": appt_kept_rate, "close_rate": close_rate,
        },
        "deal_stages": deal_stages, "funnel": funnel, "by_source": by_source,
        "recent_deals": recent_deals, "synced_at": datetime.now().strftime("%I:%M:%S %p"),
    })


@app.route("/api/message-preview")
def api_message_preview():
    """Show exactly what a personalized message would say for a given lead+template.
    Lets you SEE the automation's output before anything is ever sent."""
    import message_templates as mt
    pid = request.args.get("person_id")
    template = (request.args.get("template") or "post_voicemail_text").strip()
    channel = (request.args.get("channel") or "sms").strip()
    with db.connect() as conn:
        if pid:
            r = conn.execute("SELECT id, full_name, first_name, city, county, owner, email FROM people WHERE id=?",
                             (pid,)).fetchone()
            person = dict(r) if r else {}
            last = conn.execute("SELECT dialed_at FROM call_attempts WHERE person_id=? ORDER BY dialed_at DESC LIMIT 1",
                                (pid,)).fetchone()
            last_call = last["dialed_at"] if last else None
        else:
            person = {"full_name": "Sharon Miller", "first_name": "Sharon", "city": "Greensboro",
                      "county": "Guilford", "owner": "chris"}
            last_call = (datetime.now() - timedelta(days=1)).isoformat()
        from automation import AGENT_PHONE_DISPLAY
        person["agent_phone"] = AGENT_PHONE_DISPLAY.get((person.get("owner") or "").lower(), "")
        rendered = mt.render(template, person, last_call_at=last_call, channel=channel)
    return jsonify({"template": template, "channel": channel, "rendered": rendered,
                    "available_templates": mt.list_templates()})


@app.route("/api/sms-status")
def api_sms_status():
    """Tell the UI whether live sending is on (so it can show 'preview only' vs 'live')."""
    import senders
    return jsonify({"sms_live": senders.sms_enabled(), "email_live": senders.email_enabled()})


@app.route("/api/templates", methods=["GET"])
def api_templates():
    import message_templates as mt
    return jsonify({"templates": mt.all_templates(),
                    "fields": ["first", "name", "agent", "city", "county", "when_called", "agent_phone"]})


@app.route("/api/templates/<key>", methods=["POST"])
def api_save_template(key):
    import message_templates as mt
    d = request.json or {}
    if d.get("reset"):
        ok = mt.reset_template(key)
    else:
        ok = mt.save_template(key, sms=d.get("sms"), email_subject=d.get("email_subject"),
                              email_body=d.get("email_body"))
    if not ok:
        return jsonify({"error": "unknown template"}), 404
    with db.connect() as conn:
        db.audit(conn, "template", key, "edited", "")
    return jsonify({"ok": True})


@app.route("/api/templates/preview", methods=["POST"])
def api_template_preview():
    """Render an UNSAVED template body against a sample lead — for the live editor."""
    import message_templates as mt
    from datetime import timedelta as _td
    d = request.json or {}
    person = {"full_name": "Sharon Miller", "first_name": "Sharon", "city": "Greensboro",
              "county": "Guilford", "owner": "chris", "agent_phone": "(336) 962-2307"}
    data = mt.build_merge_data(person, last_call_at=(datetime.now() - _td(days=1)).isoformat())
    raw = d.get("sms") if d.get("channel", "sms") == "sms" else d.get("email_body", "")
    body = mt._cleanup(mt._fill_fields(mt._fill_opts(raw or "", data), data))
    return jsonify({"body": body})


@app.route("/api/automations/run", methods=["POST"])
def api_run_automations():
    """Manually fire due tasks now (the worker also does this every minute)."""
    import automation
    return jsonify(automation.fire_due_tasks())


# ----------------------------- pages -----------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ----------------------------- background sync -----------------------------

def _background_sync():
    while True:
        try:
            sync.sync_all()
        except Exception:
            pass
        time.sleep(SYNC_INTERVAL)


WORKER_INTERVAL = int(os.getenv("CC_WORKER_SECONDS", "60"))


def _background_worker():
    """The machine that keeps running: every minute it fires due scheduled tasks —
    surfaces calls on the right day, sends/queues follow-up texts & emails — and
    recomputes learning stats. No manual action required."""
    import automation
    import learning
    while True:
        try:
            automation.fire_due_tasks()
        except Exception:
            pass
        try:
            learning.refresh_stats()
        except Exception:
            pass
        time.sleep(WORKER_INTERVAL)


_started = False


def start_background(initial=True):
    """Initialize the DB and launch the sync + automation worker loops. Runs once,
    whether started by gunicorn (on import) or by `python app.py` directly."""
    global _started
    if _started:
        return
    _started = True
    db.init_db()
    try:
        _agent_ensure_columns()  # add people.email + messages.subject if missing
    except Exception as exc:
        print("column ensure warning:", exc)
    try:
        import automation
        automation.ensure_schema()  # scheduled_tasks + automation_log
    except Exception as exc:
        print("automation schema warning:", exc)
    try:
        import message_templates as mt
        mt.ensure_schema_and_seed()  # editable templates table
    except Exception as exc:
        print("templates schema warning:", exc)
    if initial:
        try:
            sync.sync_all()
        except Exception as exc:
            print("initial sync warning:", exc)
    threading.Thread(target=_background_sync, daemon=True).start()
    if os.getenv("CC_WORKER_ENABLED", "1") not in ("0", "false", "no"):
        threading.Thread(target=_background_worker, daemon=True).start()


# Start on import so production servers (gunicorn app:app) initialize correctly.
# Opt out with CC_NO_AUTOSTART=1 (used by the test suite).
if os.getenv("CC_NO_AUTOSTART", "") not in ("1", "true", "yes"):
    start_background()


def main():
    start_background()
    print(f"\n  Command Center  ->  http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


if __name__ == "__main__":
    main()
