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
import geo_policy
import queue_policy
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


def _target_lead(row):
    if not row:
        return False
    try:
        birthday = row["birthday"]
    except (KeyError, TypeError):
        birthday = row.get("birthday", "") if hasattr(row, "get") else ""
    return geo_policy.birthday_is_target(birthday)


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
    agenda_day = _request_agenda_day()
    city_filter = _agenda_city(owner, agenda_day, request.args.get("city") if "city" in request.args else None)
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
            if not _target_lead(r):
                continue
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
            if not _target_lead(r):
                continue
            d = dict(r); d["age"] = _age(r["birthday"]); d["phone"] = phone_of(r["person_id"])
            appointments.append(d)

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
    # 'Call Next' uses the SAME ranking the dialer pulls, so dashboard order == dialer order.
    ranked_all = _build_ranked_queue(owner, limit=5000, agenda_date=agenda_day, city_filter=city_filter)
    ranked = ranked_all[:500]
    agenda_all_cities = _build_ranked_queue(owner, limit=5000, agenda_date=agenda_day, city_filter="")
    city_counts = {}
    for item in agenda_all_cities:
        city = item["City"] or "Unknown"
        city_counts[city] = city_counts.get(city, 0) + 1
    call_next = []
    for r in ranked:
        call_next.append({
            "id": r["person_id"], "full_name": r["Name"], "city": r["City"], "county": r["County"],
            "phone": r["Phone"], "source": r["Source"], "lead_score": r["Lead_Score"],
            "stage": r["Queue_Type"], "age": _age(r["Birthday"]),
            "attempt_count": r.get("Attempt_Count", 0),
            "attempts_remaining": r.get("Attempts_Remaining", 0),
            "last_disposition": r.get("Last_Disposition", ""),
            "last_call_time": r.get("Last_Call_Time", ""),
            "reason": {"scheduled_call": "Scheduled callback due", "callback": "Callback requested",
                       "hot_fresh": "🔥 Fresh lead", "never_called": "Never called",
                       "retry": "Retry"}.get(r["Queue_Type"], r["Queue_Type"]),
        })
    agenda = {
        "date": agenda_day.isoformat(),
        "city_filter": city_filter,
        "total": len(ranked_all),
        "unfiltered_total": len(agenda_all_cities),
        "city_options": [{"city": k, "count": v} for k, v in sorted(city_counts.items(), key=lambda x: (-x[1], x[0]))],
    }
    return jsonify({"hero": hero, "callbacks": callbacks, "appointments": appointments,
                    "call_next": call_next, "agenda": agenda,
                    "synced_at": datetime.now().strftime("%I:%M:%S %p")})


# ----------------------------- API: leads -----------------------------

# Note authors written by the system, not by a human. "People I left notes for"
# means a note from anyone NOT in this set (the dashboard 'agent', the admin
# menu, an agent name, etc.).
SYSTEM_NOTE_AUTHORS = ("ai_score", "post_call_ai", "automation", "oscr-agent",
                       "oscr-ingestion", "call-agent", "system", "lead_scoring",
                       "number_lookup")


@app.route("/api/leads")
def api_leads():
    q = (request.args.get("q") or "").strip()
    stage = (request.args.get("stage") or "").strip()
    sort = (request.args.get("sort") or "last_activity_at").strip()
    sort_cols = {"score": "lead_score", "name": "full_name", "city": "city",
                 "last_activity_at": "last_activity_at", "calls": "call_count"}
    order = sort_cols.get(sort, "last_activity_at")

    owner = (request.args.get("owner") or "").strip().lower()
    noted = (request.args.get("noted") or "").strip().lower() in {"1", "true", "yes"}
    include_ineligible = (request.args.get("include_ineligible") or "").strip().lower() in {"1", "true", "yes"}

    author_ph = ",".join("?" * len(SYSTEM_NOTE_AUTHORS))
    human_note_filter = f"author NOT IN ({author_ph})"

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
    if noted:
        where.append(
            f"EXISTS (SELECT 1 FROM person_notes n WHERE n.person_id=p.id AND n.{human_note_filter})")
        params += list(SYSTEM_NOTE_AUTHORS)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    direction = "ASC" if order == "full_name" or order == "city" else "DESC"

    fetch_limit = 500 if include_ineligible else 5000
    # The human_note_count subquery in the SELECT binds its params BEFORE the
    # WHERE clause, so its authors go first in the param list.
    select_params = list(SYSTEM_NOTE_AUTHORS)
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT p.id, p.full_name, p.city, p.county, p.source, p.stage, p.lead_score,
                   p.owner, p.last_activity_at, p.birthday,
                   (SELECT COUNT(*) FROM call_attempts c WHERE c.person_id=p.id) call_count,
                   (SELECT COUNT(*) FROM person_notes n WHERE n.person_id=p.id AND n.{human_note_filter}) human_note_count,
                   (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id LIMIT 1) phone
            FROM people p
            LEFT JOIN phone_numbers ph ON ph.person_id=p.id
            {clause}
            GROUP BY p.id
            ORDER BY {order} {direction}
            LIMIT ?
        """, select_params + params + [fetch_limit]).fetchall()
    out = []
    for r in rows:
        if not include_ineligible and not _target_lead(r):
            continue
        d = dict(r)
        d["age"] = _age(r["birthday"])
        out.append(d)
        if len(out) >= 500:
            break
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
        if data.get("replace"):
            conn.execute("""UPDATE appointments SET status='cancelled',
                            notes=COALESCE(NULLIF(notes,''),'') || ?
                            WHERE person_id=? AND status='scheduled'""",
                         (f"\n[{now}] Replaced by new appointment time", pid))
            try:
                import automation
                automation.cancel_pending(conn, pid, "appointment_replaced")
            except Exception:
                pass
        conn.execute(
            "INSERT INTO appointments(person_id, agent, scheduled_at, notes, status, created_at) VALUES (?,?,?,?, 'scheduled', ?)",
            (pid, data.get("agent", ""), data.get("scheduled_at", ""), data.get("notes", ""), now))
        conn.execute("UPDATE people SET stage='appointment', last_activity_at=? WHERE id=?", (now, pid))
        db.audit(conn, "person", pid, "appointment_replaced" if data.get("replace") else "appointment_set",
                 data.get("scheduled_at", ""))
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


def _stage_after_cancelled_appointment(conn, pid):
    had_contact = conn.execute("""SELECT 1 FROM call_attempts
        WHERE person_id=? AND disposition_category IN ('live_conversation','callback') LIMIT 1""",
        (pid,)).fetchone()
    return "callback" if had_contact else "attempted"


@app.route("/api/lead/<int:pid>/appointment-outcome", methods=["POST"])
def api_appointment_outcome(pid):
    """Mark the most recent appointment kept / no-show / cancelled. A no-show auto-
    reschedules a follow-up call so the lead is never dropped."""
    outcome = (request.json or {}).get("outcome", "").strip()
    if outcome not in {"kept", "no_show", "cancelled"}:
        return jsonify({"error": "bad outcome"}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        row = conn.execute("SELECT id, agent FROM appointments WHERE person_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
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
        elif outcome == "cancelled":
            new_stage = _stage_after_cancelled_appointment(conn, pid)
            conn.execute("UPDATE people SET stage=?, last_activity_at=? WHERE id=?", (new_stage, now, pid))
            conn.execute("""INSERT INTO person_notes(person_id, body, author, created_at)
                            VALUES (?,?,?,?)""",
                         (pid, f"Appointment cancelled -> stage={new_stage}", "agent", now))
            try:
                import automation
                automation.cancel_pending(conn, pid, "appointment_cancelled")
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
    include_ineligible = (request.args.get("include_ineligible") or "").strip().lower() in {"1", "true", "yes"}
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
        if not include_ineligible and not _target_lead(r):
            continue
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


# Approx drive-time (minutes) from Greensboro — used as the conversion tie-breaker:
# closer prospects are likelier to keep an in-person appointment. Lower = call sooner.
CITY_DRIVE_MIN = {
    "greensboro": 0, "mc leansville": 8, "mcleansville": 8, "pleasant gdn": 12,
    "pleasant garden": 12, "jamestown": 14, "sedalia": 14, "browns summit": 14,
    "forest oaks": 14, "high point": 18, "colfax": 18, "summerfield": 18, "whitsett": 18,
    "julian": 20, "gibsonville": 20, "oak ridge": 20, "climax": 22, "archdale": 22,
    "stokesdale": 24, "trinity": 24, "elon": 24, "thomasville": 26, "kernersville": 26,
    "burlington": 28, "randleman": 28, "sophia": 30, "graham": 30, "reidsville": 30,
    "staley": 32, "haw river": 32, "asheboro": 34, "liberty": 34, "mebane": 34,
}
DEFAULT_DRIVE_MIN = 45  # unknown/farther cities go after known-close ones

# 30-minute drive-time whitelist (mirrors loader.py in the dialer). Enabled by
# default; set CC_GEO_FILTER_ENABLED=false only for explicit audit/export work.
ALLOWED_CITY_NAMES = {
    "archdale", "browns summit", "burlington", "climax", "colfax",
    "elon", "forest oaks", "gibsonville", "graham", "greensboro", "haw river",
    "high point", "jamestown", "julian", "kernersville", "liberty", "mcleansville",
    "mebane", "oak ridge", "pleasant garden", "randleman", "reidsville", "sedalia",
    "sophia", "stokesdale", "summerfield", "thomasville", "trinity", "whitsett",
}
CITY_ALIASES_CLOUD = {
    "brown summit": "browns summit", "highpoint": "high point",
    "mc leansville": "mcleansville", "pleasant gdn": "pleasant garden",
}


def _lookup_filter_active():
    try:
        import number_lookup
        return number_lookup.filter_enabled()
    except Exception:
        return False


def _is_callable_line_type(line_type):
    try:
        import number_lookup
        return number_lookup.is_callable_line_type(line_type)
    except Exception:
        return True


def _drive_minutes(city):
    return geo_policy.drive_minutes(city)


def _normalized_city(city):
    return geo_policy.normalize_city(city)


def _geo_filter_enabled():
    return geo_policy.filter_enabled("CC_GEO_FILTER_ENABLED", default=True)


def _city_in_whitelist(city):
    return geo_policy.city_is_allowed(city)


AGENDA_SCHEMA = """
CREATE TABLE IF NOT EXISTS agenda_settings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agenda_date TEXT NOT NULL,
    owner       TEXT NOT NULL DEFAULT '',
    city_filter TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    UNIQUE(agenda_date, owner)
);
"""


def _ensure_agenda_schema():
    with db.connect() as conn:
        conn.executescript(AGENDA_SCHEMA)


def _request_agenda_day():
    return queue_policy.coerce_day(request.args.get("date") or request.args.get("agenda_date"))


def _agenda_settings(owner="", agenda_day=None):
    _ensure_agenda_schema()
    owner = (owner or "").strip().lower()
    agenda_day = queue_policy.coerce_day(agenda_day)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT city_filter, notes FROM agenda_settings WHERE agenda_date=? AND owner=?",
            (agenda_day.isoformat(), owner),
        ).fetchone()
    return {
        "date": agenda_day.isoformat(),
        "owner": owner,
        "city_filter": row["city_filter"] if row else "",
        "notes": row["notes"] if row else "",
    }


def _save_agenda_settings(owner, agenda_day, city_filter="", notes=""):
    _ensure_agenda_schema()
    owner = (owner or "").strip().lower()
    agenda_day = queue_policy.coerce_day(agenda_day)
    city_filter = (city_filter or "").strip()
    notes = (notes or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO agenda_settings(agenda_date, owner, city_filter, notes, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(agenda_date, owner) DO UPDATE SET
                 city_filter=excluded.city_filter,
                 notes=excluded.notes,
                 updated_at=excluded.updated_at""",
            (agenda_day.isoformat(), owner, city_filter, notes, now),
        )
    return _agenda_settings(owner, agenda_day)


def _agenda_city(owner="", agenda_day=None, override=None):
    if override is not None:
        return (override or "").strip()
    return _agenda_settings(owner, agenda_day).get("city_filter", "")


def _agenda_window(agenda_day):
    agenda_day = queue_policy.coerce_day(agenda_day)
    if agenda_day == date.today():
        return datetime.now().isoformat(timespec="seconds")
    return f"{agenda_day.isoformat()}T23:59:59"


def _city_matches_filter(city, city_filter):
    if not city_filter:
        return True
    return _normalized_city(city) == _normalized_city(city_filter)


def _build_ranked_queue(owner="", limit=5000, agenda_date=None, city_filter=None):
    """THE single ranking used everywhere — the dialer's call list AND the dashboard's
    'Call Next' both call this, so their order is ALWAYS identical. Priority:
    scheduled-calls-due > callbacks > hot fresh > new(by score) > retry, and within
    each tier: score desc -> closest city -> name. Excludes DNC/suppressed/future-held."""
    owner = (owner or "").strip().lower()
    agenda_day = queue_policy.coerce_day(agenda_date)
    due_cutoff = _agenda_window(agenda_day)
    cutoff_24h = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    own = " AND p.owner=?" if owner else ""
    own_p = [owner] if owner else []
    city_filter = (city_filter or "").strip()
    rows, seen = [], set()
    try:
        import automation
        automation.ensure_schema()
    except Exception:
        pass
    with db.connect() as conn:
        def suppressed(e):
            return conn.execute("SELECT 1 FROM suppressions WHERE scope='phone' AND value=? LIMIT 1", (e,)).fetchone() is not None

        def person_suppressed(pid):
            return conn.execute("SELECT 1 FROM suppressions WHERE scope='person' AND value=? LIMIT 1", (str(pid),)).fetchone() is not None

        def phone_of(pid):
            rows = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? AND status<>'bad' "
                                "ORDER BY status='active' DESC", (pid,)).fetchall()
            for r in rows:
                if not suppressed(r["e164"]):
                    return r["e164"]
            return ""

        # Future-held: a scheduled call after this agenda day stays off this day.
        held = {r["person_id"] for r in conn.execute(
            "SELECT DISTINCT person_id FROM scheduled_tasks WHERE kind='call' AND status='pending' AND due_at>?", (due_cutoff,))}
        due_rows = conn.execute(
            "SELECT person_id, reason FROM scheduled_tasks WHERE kind='call' AND status='pending' AND due_at<=?",
            (due_cutoff,)).fetchall()
        due = {r["person_id"] for r in due_rows}
        due_auto_retry = {r["person_id"] for r in due_rows if queue_policy.is_auto_retry_reason(r["reason"])}
        due_warm = due - due_auto_retry
        held -= due

        base = (f"SELECT p.id, p.full_name, p.city, p.county, p.birthday, p.address, p.source, "
                f"p.lead_score, p.stage, p.last_activity_at, p.owner, "
                f"(SELECT disposition FROM call_attempts c WHERE c.person_id=p.id ORDER BY dialed_at DESC LIMIT 1) last_disposition, "
                f"(SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id ORDER BY dialed_at DESC LIMIT 1) last_outcome, "
                f"(SELECT dialed_at FROM call_attempts c WHERE c.person_id=p.id ORDER BY dialed_at DESC LIMIT 1) last_call_time "
                f"FROM people p WHERE p.stage<>'dnc' AND p.stage<>'closed'{own}")

        def add(r, qtype, bucket):
            pid = r["id"]
            if pid in seen or pid in held:
                return
            if person_suppressed(pid):
                return
            if not _target_lead(r):
                return
            if _geo_filter_enabled() and not _city_in_whitelist(r["city"]):
                return
            if not _city_matches_filter(r["city"], city_filter):
                return
            ph = phone_of(pid)
            if not ph or suppressed(ph):
                return
            # Drop confirmed-landline / deactivated phones once Telnyx Number
            # Lookup has classified them. Toggle CC_LOOKUP_FILTER_ENABLED off to
            # short-circuit this (useful while the lookup pass is still running).
            if _lookup_filter_active():
                phone_meta = conn.execute(
                    "SELECT line_type, status FROM phone_numbers WHERE e164=? LIMIT 1", (ph,)
                ).fetchone()
                if phone_meta:
                    if str(phone_meta["status"] or "").lower() == "deactivated":
                        return
                    if not _is_callable_line_type(phone_meta["line_type"] or ""):
                        return
            status = queue_policy.retry_status(conn, pid, ph, r["stage"], qtype, agenda_day)
            if not status["allowed"]:
                return
            latest = status.get("latest") or {}
            seen.add(pid)
            bucket.append({"Queue_Type": qtype, "Name": r["full_name"] or "", "Phone": ph,
                           "Address": r["address"] or "", "City": r["city"] or "", "County": r["county"] or "",
                           "Birthday": r["birthday"] or "", "Last_Disposition": latest.get("disposition") or r["last_disposition"] or "",
                           "Last_Outcome": latest.get("disposition_category") or r["last_outcome"] or "",
                           "Last_Call_Time": latest.get("dialed_at") or r["last_call_time"] or "",
                           "Attempt_Count": status["attempts"],
                           "Attempts_Remaining": status["attempts_remaining"],
                           "Agenda_Date": agenda_day.isoformat(),
                           "Source": r["source"] or "",
                           "Lead_Score": r["lead_score"], "person_id": pid,
                           "_drive": _drive_minutes(r["city"])})

        # Each tier keeps its priority, but WITHIN a tier we rank by score desc then
        # CLOSEST CITY first (shorter drive = likelier to keep an appointment).
        def sort_tier(b):
            b.sort(key=lambda x: (-(x["Lead_Score"] or 0), x["_drive"], x["Name"]))
            return b

        t_sched, t_call, t_hot, t_new, t_retry = [], [], [], [], []
        if due_warm:
            q = base + " AND p.id IN ({})".format(",".join("?" * len(due_warm)))
            for r in conn.execute(q, own_p + list(due_warm)):
                add(r, "scheduled_call", t_sched)
        for r in conn.execute(base + " AND p.stage='callback'", own_p):
            add(r, "callback", t_call)
        for r in conn.execute(base + " AND p.stage='new' AND p.first_seen_at>=?", own_p + [cutoff_24h]):
            add(r, "hot_fresh", t_hot)
        for r in conn.execute(base + " AND p.stage='new'", own_p):
            add(r, "never_called", t_new)
        for r in conn.execute(base + " AND p.stage IN ('attempted','voicemail')", own_p):
            add(r, "retry", t_retry)

        for tier in (t_sched, t_call, t_hot, t_new):
            rows.extend(sort_tier(tier))
        t_retry.sort(key=lambda x: (-(x["Lead_Score"] or 0), x["Last_Call_Time"] or "", x["_drive"], x["Name"]))
        rows.extend(t_retry)
        # strip internal sort key from the response
        for x in rows:
            x.pop("_drive", None)
    return rows[:limit]


@app.route("/api/dialer-queue")
def api_dialer_queue():
    """The dialer pulls this exact ranked list (single source of truth)."""
    owner = (request.args.get("owner") or "").strip().lower()
    limit = min(int(request.args.get("limit", 1000)), 5000)
    agenda_day = _request_agenda_day()
    city_filter = _agenda_city(owner, agenda_day, request.args.get("city") if "city" in request.args else None)
    rows = _build_ranked_queue(owner, limit, agenda_day, city_filter)
    return jsonify({
        "count": len(rows),
        "queue": rows,
        "agenda": {"date": agenda_day.isoformat(), "owner": owner, "city_filter": city_filter},
    })


@app.route("/api/agenda")
def api_agenda():
    """Show the call agenda for a day, using the same order the dialer exports."""
    owner = (request.args.get("owner") or "").strip().lower()
    agenda_day = _request_agenda_day()
    city_filter = _agenda_city(owner, agenda_day, request.args.get("city") if "city" in request.args else None)
    limit = min(int(request.args.get("limit", 5000)), 5000)
    unfiltered = _build_ranked_queue(owner, 5000, agenda_day, "")
    filtered = _build_ranked_queue(owner, 5000, agenda_day, city_filter)
    rows = filtered[:limit]

    by_type, by_city = {}, {}
    for r in filtered:
        by_type[r["Queue_Type"]] = by_type.get(r["Queue_Type"], 0) + 1
    for r in unfiltered:
        city = r["City"] or "Unknown"
        by_city[city] = by_city.get(city, 0) + 1
    city_options = [{"city": k, "count": v} for k, v in sorted(by_city.items(), key=lambda x: (-x[1], x[0]))]
    settings = _agenda_settings(owner, agenda_day)
    return jsonify({
        "agenda": {
            "date": agenda_day.isoformat(),
            "owner": owner,
            "city_filter": city_filter,
            "saved_city_filter": settings.get("city_filter", ""),
            "notes": settings.get("notes", ""),
            "total": len(filtered),
            "unfiltered_total": len(unfiltered),
            "by_type": by_type,
            "city_options": city_options,
        },
        "queue": rows,
        "synced_at": datetime.now().strftime("%I:%M:%S %p"),
    })


@app.route("/api/agenda/settings", methods=["POST"])
def api_agenda_settings():
    d = request.json or {}
    owner = (d.get("owner") or "").strip().lower()
    agenda_day = queue_policy.coerce_day(d.get("date") or d.get("agenda_date"))
    city_filter = (d.get("city_filter") or d.get("city") or "").strip()
    if city_filter and _geo_filter_enabled() and not _city_in_whitelist(city_filter):
        return jsonify({"error": f"{city_filter} is outside the 30-minute Greensboro whitelist"}), 400
    settings = _save_agenda_settings(owner, agenda_day, city_filter, d.get("notes", ""))
    return jsonify({"ok": True, "agenda": settings})


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


# Reference numbers shown on the /today page so the operator has them at hand
# during live calls. Anything sensitive (license #, BL back office) is read from
# env so it's never in the public git repo.
def _reference_numbers():
    return [
        {"label": "Medicare (verify info, eligibility)", "number": "1-800-MEDICARE", "alt": "1-800-633-4227",
         "context": "Recommend to prospect for unbiased info on all plans available."},
        {"label": "NC SHIIP (state senior insurance program)", "number": "1-855-408-1212",
         "context": "Free unbiased Medicare counseling. Reference if a prospect wants a second opinion."},
        {"label": "National Do-Not-Call Registry", "number": "1-888-382-1222",
         "context": "If a prospect asks to be removed from EVERY list. Add them to internal DNC first."},
        {"label": "Bankers Life back office", "number": os.getenv("BL_BACK_OFFICE_PHONE", "(set BL_BACK_OFFICE_PHONE)"),
         "context": "Underwriting / app status questions."},
        {"label": "Bankers Life agent support", "number": os.getenv("BL_AGENT_SUPPORT_PHONE", "(set BL_AGENT_SUPPORT_PHONE)"),
         "context": "Compliance, TPMO wording, license/appointment questions."},
        {"label": "Your NC resident license #", "number": os.getenv("OPERATOR_LICENSE_NUM", "(set OPERATOR_LICENSE_NUM)"),
         "context": "State if a prospect asks to verify your credentials."},
        {"label": "CMS Medicare.gov", "number": "Medicare.gov",
         "context": "Send prospects here to research plans on their own."},
    ]


CMS_TPMO_DISCLAIMER = (
    "We do not offer every plan available in your area. Any information we "
    "provide is limited to those plans we do offer in your area. Please contact "
    "Medicare.gov or 1-800-MEDICARE to get information on all of your options."
)


@app.route("/today")
def today_view():
    """A single-page operator view: live queue top, today's calls, today's
    appointments, pending callbacks, system warnings. Refreshes every 30s."""
    owner = (request.args.get("owner") or "chris").strip()
    today_iso = date.today().isoformat()

    queue = _build_ranked_queue(owner=owner, limit=10)

    with db.connect() as conn:
        # 7-day trend: calls, connects, voicemails, appointments per day
        week_rows = conn.execute(
            """SELECT substr(dialed_at,1,10) d,
                      COUNT(*) total,
                      SUM(CASE WHEN disposition_category='live_conversation' THEN 1 ELSE 0 END) connects,
                      SUM(CASE WHEN disposition_category='voicemail' THEN 1 ELSE 0 END) voicemails,
                      SUM(CASE WHEN disposition_category='bad_number' THEN 1 ELSE 0 END) bad
               FROM call_attempts
               WHERE date(dialed_at) >= date(?, '-7 days')
               GROUP BY substr(dialed_at,1,10)
               ORDER BY d""",
            (today_iso,),
        ).fetchall()
        week_appts = {
            r["d"]: r["n"] for r in conn.execute(
                """SELECT substr(created_at,1,10) d, COUNT(*) n FROM appointments
                   WHERE date(created_at) >= date(?, '-7 days')
                   GROUP BY substr(created_at,1,10)""",
                (today_iso,),
            )
        }
        seven_day_trend = []
        for r in week_rows:
            d = r["d"]
            seven_day_trend.append({
                "date": d,
                "total": r["total"], "connects": r["connects"],
                "voicemails": r["voicemails"], "bad": r["bad"],
                "appointments": week_appts.get(d, 0),
                "connect_pct": round(100.0 * r["connects"] / r["total"], 1) if r["total"] else 0.0,
            })

        # System health: queue depth, pending tasks, last call
        sys_health = {
            "queue_depth": len(queue),
            "pending_tasks_total": conn.execute(
                "SELECT COUNT(*) n FROM scheduled_tasks WHERE status='pending'").fetchone()["n"],
            "pending_tasks_due_today": conn.execute(
                "SELECT COUNT(*) n FROM scheduled_tasks WHERE status='pending' AND date(due_at) <= date(?)",
                (today_iso,)).fetchone()["n"],
            "last_call_at": (conn.execute(
                "SELECT MAX(dialed_at) m FROM call_attempts").fetchone()["m"] or "—"),
            "last_import_at": (conn.execute(
                "SELECT MAX(first_seen_at) m FROM people").fetchone()["m"] or "—"),
            "leads_never_called": conn.execute(
                """SELECT COUNT(*) n FROM people p
                   WHERE p.stage='new'
                     AND NOT EXISTS (SELECT 1 FROM call_attempts c WHERE c.person_id=p.id)"""
            ).fetchone()["n"],
        }

        today_calls = conn.execute(
            """SELECT c.id, c.dialed_at, c.disposition, c.disposition_category,
                      c.agent, c.phone, c.live_talk_seconds,
                      p.full_name, p.city, p.source, p.lead_score
               FROM call_attempts c LEFT JOIN people p ON p.id=c.person_id
               WHERE substr(c.dialed_at,1,10)=?
               ORDER BY c.dialed_at DESC""",
            (today_iso,),
        ).fetchall()

        disp_counts = {}
        for row in today_calls:
            cat = row["disposition_category"] or "unknown"
            disp_counts[cat] = disp_counts.get(cat, 0) + 1

        today_appts = conn.execute(
            """SELECT a.id, a.scheduled_at, a.status, a.agent, a.notes,
                      p.full_name, p.city, p.id person_id
               FROM appointments a LEFT JOIN people p ON p.id=a.person_id
               WHERE substr(a.scheduled_at,1,10)=?
               ORDER BY a.scheduled_at""",
            (today_iso,),
        ).fetchall()

        pending_callbacks = conn.execute(
            """SELECT s.id, s.due_at, s.reason, s.person_id,
                      p.full_name, p.city, p.lead_score
               FROM scheduled_tasks s LEFT JOIN people p ON p.id=s.person_id
               WHERE s.kind='call' AND s.status='pending'
                 AND date(s.due_at) BETWEEN date(?) AND date(?, '+3 days')
               ORDER BY s.due_at LIMIT 10""",
            (today_iso, today_iso),
        ).fetchall()

        recent_appts = conn.execute(
            """SELECT COUNT(*) n FROM appointments WHERE substr(created_at,1,10) >= date(?, '-7 days')""",
            (today_iso,),
        ).fetchone()["n"]

        new_leads_today = conn.execute(
            """SELECT COUNT(*) n FROM people WHERE substr(first_seen_at,1,10)=?""",
            (today_iso,),
        ).fetchone()["n"]

    warnings = []
    try:
        import number_lookup
        spend = number_lookup.today_spend()
        if spend >= number_lookup.DAILY_BUDGET_USD * 0.8:
            warnings.append({
                "level": "warn",
                "text": f"Telnyx lookup spend today ${spend:.2f} of ${number_lookup.DAILY_BUDGET_USD:.2f}.",
            })
    except Exception:
        pass

    if not queue:
        warnings.append({"level": "warn", "text": f"Queue empty for owner={owner}. New imports or filter relaxation needed."})

    return render_template(
        "today.html",
        owner=owner,
        today=today_iso,
        queue=queue,
        today_calls=[dict(r) for r in today_calls][:50],
        disposition_counts=disp_counts,
        appointments_today=[dict(r) for r in today_appts],
        pending_callbacks=[dict(r) for r in pending_callbacks],
        appts_last_7=recent_appts,
        new_leads_today=new_leads_today,
        warnings=warnings,
        reference_numbers=_reference_numbers(),
        tpmo_disclaimer=CMS_TPMO_DISCLAIMER,
        seven_day_trend=seven_day_trend,
        system_health=sys_health,
    )


# ----------------------------- /today lead search (JSON) -----------------------------

@app.route("/api/today/search")
def today_search():
    """Lightweight search the /today page uses for the drill-in widget.
    Accepts q=<phone digits or name fragment>, returns minimal lead cards."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"count": 0, "leads": []})
    digits = "".join(ch for ch in q if ch.isdigit())
    with db.connect() as conn:
        results = []
        if digits and len(digits) >= 4:
            tail = digits[-10:]
            rows = conn.execute(
                """SELECT DISTINCT p.id, p.full_name, p.city, p.stage, p.lead_score,
                                   p.source, ph.e164 phone
                   FROM phone_numbers ph JOIN people p ON p.id=ph.person_id
                   WHERE REPLACE(REPLACE(REPLACE(ph.e164,'+',''),'-',''),' ','') LIKE ?
                   ORDER BY p.lead_score DESC, p.last_activity_at DESC LIMIT 15""",
                (f"%{tail}%",),
            ).fetchall()
            results.extend(dict(r) for r in rows)
        if len(results) < 15:
            rows = conn.execute(
                """SELECT p.id, p.full_name, p.city, p.stage, p.lead_score, p.source,
                          (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id LIMIT 1) phone
                   FROM people p WHERE lower(p.full_name) LIKE ?
                   ORDER BY p.lead_score DESC, p.last_activity_at DESC LIMIT ?""",
                (f"%{q.lower()}%", 15 - len(results)),
            ).fetchall()
            seen = {r["id"] for r in results}
            for r in rows:
                d = dict(r)
                if d["id"] not in seen:
                    results.append(d)
    return jsonify({"count": len(results), "leads": results})


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
