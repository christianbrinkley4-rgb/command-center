"""Tests for post-call AI analysis -> DB action mapping.

The AI call itself is not exercised here (it's a thin provider switch). What we
verify is the action layer: given a parsed analysis dict, do we book the right
appointment, schedule the right callback, suppress correctly, and append the
note? All against a throwaway DB.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

_tmp = os.path.join(tempfile.mkdtemp(), "test_postcall.db")
import db
db.DB_PATH = _tmp
_orig_connect = db.connect
db.connect = lambda *a, **k: _orig_connect(_tmp)
db.init_db(_tmp)

import agent_api
agent_api.db.connect = db.connect
agent_api._ensure_columns()

import automation
automation.db.connect = db.connect
automation.ensure_schema()

import post_call
post_call.db.connect = db.connect

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def seed_person(name="Alice Tester", city="Greensboro", phone="+13365557777"):
    """Insert a person + phone + one call_attempt + one transcript. Return ids."""
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, first_name, last_name,
               city, owner, stage, lead_score, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"k|{name}|{city}", name, name.split()[0], name.split()[-1],
             city, "chris", "attempted", 60, now, now))
        pid = cur.lastrowid
        conn.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                     (pid, phone, now))
        cur2 = conn.execute(
            """INSERT INTO call_attempts(person_id, phone, agent, dialed_at,
               disposition, disposition_category, call_control_id, dedupe_key)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, phone, "chris", now, "human_transferred", "live_conversation",
             f"v3:test-{pid}", f"seed|{pid}"))
        caid = cur2.lastrowid
        cur3 = conn.execute(
            """INSERT INTO transcripts(person_id, call_attempt_id, call_control_id,
               full_text, segments_json, raw_payload, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (pid, caid, f"v3:test-{pid}", "[Speaker A 00:00] hi", "[]", "{}", now))
        tid = cur3.lastrowid
    return pid, caid, tid


# ------------------------------------------------------------------ unit pieces

print("\n=== date helpers ===")
dt = post_call._parse_iso("2026-06-05T14:00:00")
check("ISO parses cleanly", dt is not None and dt.hour == 14)
dt2 = post_call._parse_iso("2026-06-05T14:00:00Z")
check("ISO with trailing Z accepted", dt2 is not None and dt2.hour == 14)
check("garbage rejected", post_call._parse_iso("absolutely not a date") is None)

now = datetime.now()
check("future date kept", post_call._sane_future_dt(now + timedelta(days=2)) is not None)
check("ancient date rejected", post_call._sane_future_dt(now - timedelta(days=30)) is None)
check("far-future date rejected", post_call._sane_future_dt(now + timedelta(days=400)) is None)


# ------------------------------------------------------------------ appointment_set

print("\n=== outcome=appointment_set with parseable time ===")
pid, caid, tid = seed_person("Alice Tester")
appt_when = (now + timedelta(days=4)).replace(hour=14, minute=0, second=0, microsecond=0)
analysis = {
    "outcome": "appointment_set",
    "appointment_at": appt_when.isoformat(timespec="seconds"),
    "callback_at": None,
    "summary": "Prospect agreed to meet Tuesday at 2pm to review Medicare Advantage options.",
    "key_facts": ["currently on Aetna", "wife also turning 65 in Sept"],
    "objections": [],
    "prospect_quotes": ["Tuesday at two works"],
    "next_step": "Send confirmation text",
    "tpmo_delivered": True,
    "soa_collected": False,
    "do_not_call_requested": False,
    "confidence": 0.92,
}
with db.connect() as conn:
    actions = post_call.apply_actions(conn, pid, caid, tid, analysis, agent="chris")

with db.connect() as conn:
    appts = conn.execute("SELECT * FROM appointments WHERE person_id=?", (pid,)).fetchall()
    person = conn.execute("SELECT stage FROM people WHERE id=?", (pid,)).fetchone()
    tasks = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE person_id=? AND kind='sms' AND reason LIKE 'appt_reminder%'",
        (pid,)).fetchall()
    notes = conn.execute("SELECT body FROM person_notes WHERE person_id=?", (pid,)).fetchall()
    transcript_row = conn.execute("SELECT analysis_json FROM transcripts WHERE id=?", (tid,)).fetchone()

check("appointment row inserted", len(appts) == 1)
check("appointment scheduled at the AI's time", appts[0]["scheduled_at"].startswith(appt_when.isoformat()[:13]))
check("stage advanced to appointment", person["stage"] == "appointment")
check("reminder tasks queued", len(tasks) >= 1)
check("structured note appended", any("KEY FACTS" in (n["body"] or "") for n in notes))
check("analysis_json persisted on transcript", transcript_row["analysis_json"] and "appointment_set" in transcript_row["analysis_json"])
check("actions list reports the booking", any("appointment booked" in a for a in actions))


# ------------------------------------------------------------------ callback_set

print("\n=== outcome=callback_set with parseable time ===")
pid2, caid2, tid2 = seed_person("Bob Caller", phone="+13365557778")
cb_when = (now + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
analysis2 = {
    "outcome": "callback_set",
    "callback_at": cb_when.isoformat(timespec="seconds"),
    "summary": "Prospect asked to be called back Thursday at 10am.",
    "next_step": "Call Thursday 10am",
    "tpmo_delivered": False,
    "confidence": 0.85,
}
with db.connect() as conn:
    acts2 = post_call.apply_actions(conn, pid2, caid2, tid2, analysis2, agent="chris")

with db.connect() as conn:
    call_tasks = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE person_id=? AND kind='call' AND reason='post_call_ai_callback'",
        (pid2,)).fetchall()
    person2 = conn.execute("SELECT stage FROM people WHERE id=?", (pid2,)).fetchone()
check("callback task scheduled", len(call_tasks) == 1)
check("callback at the AI's time", call_tasks[0]["due_at"].startswith(cb_when.isoformat()[:13]))
check("stage advanced to callback", person2["stage"] == "callback")


# ------------------------------------------------------------------ do_not_call

print("\n=== outcome with do_not_call_requested=True ===")
pid3, caid3, tid3 = seed_person("Carol Hates Calls", phone="+13365557779")
analysis3 = {
    "outcome": "do_not_call",
    "summary": "Prospect asked to be removed from the list.",
    "do_not_call_requested": True,
    "tpmo_delivered": False,
    "confidence": 0.97,
}
with db.connect() as conn:
    post_call.apply_actions(conn, pid3, caid3, tid3, analysis3, agent="chris")
with db.connect() as conn:
    p_sup = conn.execute(
        "SELECT 1 FROM suppressions WHERE scope='person' AND value=?", (str(pid3),)).fetchone()
    ph_sup = conn.execute(
        "SELECT 1 FROM suppressions WHERE scope='phone' AND value='+13365557779'").fetchone()
    p3 = conn.execute("SELECT stage FROM people WHERE id=?", (pid3,)).fetchone()
check("DNC adds person suppression", p_sup is not None)
check("DNC adds phone suppression", ph_sup is not None)
check("DNC sets stage=dnc", p3["stage"] == "dnc")


# ------------------------------------------------------------------ not_interested

print("\n=== outcome=not_interested (soft) ===")
pid4, caid4, tid4 = seed_person("Dan Polite", phone="+13365557780")
analysis4 = {
    "outcome": "not_interested",
    "summary": "Prospect happy with current plan, not switching.",
    "next_step": "Drop from active dialing, retry at AEP.",
    "tpmo_delivered": True,
    "confidence": 0.8,
}
with db.connect() as conn:
    post_call.apply_actions(conn, pid4, caid4, tid4, analysis4, agent="chris")
with db.connect() as conn:
    p4 = conn.execute("SELECT stage FROM people WHERE id=?", (pid4,)).fetchone()
    sup = conn.execute(
        "SELECT 1 FROM suppressions WHERE scope='person' AND value=?", (str(pid4),)).fetchone()
check("not_interested -> stage=contacted", p4["stage"] == "contacted")
check("not_interested does NOT suppress (soft)", sup is None)


# ------------------------------------------------------------------ appointment_set with NO parseable time -> graceful

print("\n=== appointment_set but unparseable time -> downgraded ===")
pid5, caid5, tid5 = seed_person("Eve Vague", phone="+13365557781")
analysis5 = {
    "outcome": "appointment_set",
    "appointment_at": "sometime next week-ish maybe",
    "summary": "Said yes but did not pin a time.",
    "tpmo_delivered": True,
    "confidence": 0.4,
}
with db.connect() as conn:
    acts5 = post_call.apply_actions(conn, pid5, caid5, tid5, analysis5, agent="chris")
with db.connect() as conn:
    appts5 = conn.execute("SELECT * FROM appointments WHERE person_id=?", (pid5,)).fetchall()
    p5 = conn.execute("SELECT stage FROM people WHERE id=?", (pid5,)).fetchone()
check("no appointment row created without a real time", len(appts5) == 0)
check("stage NOT advanced to appointment on bad time", p5["stage"] != "appointment")
check("downgrade noted in actions", any("downgraded" in a for a in acts5))


# ------------------------------------------------------------------ note formatting

print("\n=== note formatting ===")
note = post_call._format_note({
    "summary": "Hot lead.",
    "key_facts": ["A", "B"],
    "objections": ["price"],
    "prospect_quotes": ["yes that works"],
    "next_step": "Email plan PDF",
    "tpmo_delivered": True,
    "confidence": 0.91,
})
check("note has summary", "Hot lead." in note)
check("note lists facts", "KEY FACTS" in note and "  - A" in note)
check("note lists objections", "OBJECTIONS" in note and "  - price" in note)
check("note quotes prospect", "PROSPECT SAID" in note)
check("note includes next step", "NEXT STEP: Email plan PDF" in note)
check("note flags compliance", "TPMO delivered" in note)


# ------------------------------------------------------------------ process() respects the toggle

print("\n=== process() respects ENABLE_POST_CALL_AI ===")
prev = os.environ.pop("ENABLE_POST_CALL_AI", None)
res = post_call.process(tid, pid, caid, "some transcript", {"full_name": "Alice"}, agent="chris")
check("disabled -> skipped=ai_disabled", res.get("skipped") == "ai_disabled" and res["ok"] is False)
if prev is not None:
    os.environ["ENABLE_POST_CALL_AI"] = prev


passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys
sys.exit(0 if passed == len(results) else 1)
