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

app = Flask(__name__)
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
    with db.connect() as conn:
        def phone_of(pid):
            r = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? LIMIT 1", (pid,)).fetchone()
            return r["e164"] if r else ""

        # 1) Callbacks the prospect asked for — top priority.
        callbacks = []
        for r in conn.execute(
            """SELECT id, full_name, city, county, birthday, owner, lead_score, last_activity_at
               FROM people WHERE stage='callback' ORDER BY last_activity_at DESC LIMIT 100"""):
            d = dict(r); d["age"] = _age(r["birthday"]); d["phone"] = phone_of(r["id"])
            callbacks.append(d)

        # 2) Appointments to confirm / upcoming.
        appointments = []
        for r in conn.execute(
            """SELECT a.id, a.scheduled_at, a.status, a.notes, a.agent,
                      p.id person_id, p.full_name, p.city, p.birthday
               FROM appointments a JOIN people p ON p.id=a.person_id
               WHERE a.status IN ('scheduled','kept') ORDER BY a.scheduled_at LIMIT 100"""):
            d = dict(r); d["age"] = _age(r["birthday"]); d["phone"] = phone_of(r["person_id"])
            appointments.append(d)

        # 3) Best leads to call next — not yet reached, not DNC, highest score first.
        call_next = []
        for r in conn.execute(
            """SELECT p.id, p.full_name, p.city, p.county, p.birthday, p.source, p.owner,
                      p.lead_score, p.stage, p.last_activity_at,
                      (SELECT COUNT(*) FROM call_attempts c WHERE c.person_id=p.id) call_count,
                      (SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id
                       ORDER BY dialed_at DESC LIMIT 1) last_outcome
               FROM people p
               WHERE p.stage IN ('new','attempted','voicemail')
               ORDER BY p.lead_score DESC, p.last_activity_at ASC LIMIT 60"""):
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

    where, params = [], []
    if q:
        where.append("(p.full_name LIKE ? OR ph.e164 LIKE ? OR p.city LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if stage:
        where.append("p.stage=?")
        params.append(stage)
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
        conn.execute("UPDATE people SET stage='appointment' WHERE id=?", (pid,))
        db.audit(conn, "person", pid, "appointment_set", data.get("scheduled_at", ""))
    return jsonify({"ok": True})


@app.route("/api/lead/<int:pid>/stage", methods=["POST"])
def api_set_stage(pid):
    stage = (request.json or {}).get("stage", "").strip()
    if stage not in {"new", "attempted", "voicemail", "contacted", "callback", "appointment", "dnc"}:
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


@app.route("/api/sync", methods=["POST"])
def api_sync():
    return jsonify({"results": sync.sync_all()})


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


_started = False


def start_background(initial=True):
    """Initialize the DB and launch the sync loop. Runs once, whether started by
    gunicorn (on import) or by `python app.py` directly."""
    global _started
    if _started:
        return
    _started = True
    db.init_db()
    if initial:
        try:
            sync.sync_all()
        except Exception as exc:
            print("initial sync warning:", exc)
    threading.Thread(target=_background_sync, daemon=True).start()


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
