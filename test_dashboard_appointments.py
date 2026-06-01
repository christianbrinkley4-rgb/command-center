"""Dashboard appointment edit/cancel test. Run: python test_dashboard_appointments.py"""
import os
import tempfile
from datetime import datetime

os.environ["CC_NO_AUTOSTART"] = "1"
os.environ["CC_PASSWORD"] = ""

_d = tempfile.mkdtemp()
_tmp = os.path.join(_d, "appt.db")

import db
db.DB_PATH = _tmp
_connect = db.connect
db.connect = lambda *a, **k: _connect(_tmp)
db.init_db(_tmp)

import agent_api
agent_api.db.connect = db.connect
agent_api._ensure_columns()

import automation
automation.db.connect = db.connect
automation.ensure_schema()

import app as cc_app
cc_app.db.connect = db.connect

results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + name)


with db.connect() as conn:
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute("""INSERT INTO people(person_key, full_name, first_name, last_name,
                       city, owner, stage, lead_score, first_seen_at, last_activity_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       ("appt tester|greensboro|", "Appt Tester", "Appt", "Tester",
                        "Greensboro", "chris", "callback", 90, now, now))
    pid = cur.lastrowid
    conn.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                 (pid, "+13365552000", now))
    conn.execute("""INSERT INTO call_attempts(person_id, phone, agent, dialed_at,
                 disposition, disposition_category, dedupe_key)
                 VALUES (?,?,?,?,?,?,?)""",
                 (pid, "+13365552000", "chris", now, "human_transferred",
                  "live_conversation", "appt-test-call"))

client = cc_app.app.test_client()

print("\n=== dashboard appointment switch/cancel ===")
r = client.post(f"/api/lead/{pid}/appointment", json={
    "scheduled_at": "Tue, Jun 2, 10:00 AM",
    "agent": "chris",
    "notes": "first time",
})
check("set appointment ok", r.status_code == 200 and r.get_json()["ok"])
with db.connect() as conn:
    stage = conn.execute("SELECT stage FROM people WHERE id=?", (pid,)).fetchone()["stage"]
    appts = conn.execute("SELECT status, scheduled_at FROM appointments WHERE person_id=?", (pid,)).fetchall()
check("lead marked appointment", stage == "appointment")
check("one scheduled appointment", len(appts) == 1 and appts[0]["status"] == "scheduled")

r = client.post(f"/api/lead/{pid}/appointment", json={
    "scheduled_at": "Wed, Jun 3, 2:00 PM",
    "agent": "chris",
    "notes": "switched",
    "replace": True,
})
check("switch appointment ok", r.status_code == 200 and r.get_json()["ok"])
with db.connect() as conn:
    appts = conn.execute("SELECT status, scheduled_at FROM appointments WHERE person_id=? ORDER BY id", (pid,)).fetchall()
check("old appointment cancelled", appts[0]["status"] == "cancelled")
check("new appointment scheduled", appts[1]["status"] == "scheduled" and "Jun 3" in appts[1]["scheduled_at"])

r = client.post(f"/api/lead/{pid}/appointment-outcome", json={"outcome": "cancelled"})
check("cancel appointment ok", r.status_code == 200 and r.get_json()["ok"])
with db.connect() as conn:
    stage = conn.execute("SELECT stage FROM people WHERE id=?", (pid,)).fetchone()["stage"]
    active = conn.execute("SELECT COUNT(*) n FROM appointments WHERE person_id=? AND status='scheduled'", (pid,)).fetchone()["n"]
check("cancel moves lead back to callback", stage == "callback")
check("no active scheduled appointments remain", active == 0)

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*52}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else " *** FAILURES ***"))
raise SystemExit(0 if passed == len(results) else 1)
