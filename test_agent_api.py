"""End-to-end test of the Agent API — simulates the full three-agent workflow
against a throwaway database. No network, no real data touched. Run: python test_agent_api.py"""
import functools
import json
import os
import tempfile

# Point the DB at a FRESH temp file and make every db.connect() use it, regardless
# of the default-arg captured at function definition time.
_tmp = os.path.join(tempfile.mkdtemp(), "test_cc.db")
import db
db.DB_PATH = _tmp
_orig_connect = db.connect
db.connect = lambda *a, **k: _orig_connect(_tmp)
db.init_db(_tmp)

os.environ["CC_NO_AUTOSTART"] = "1"
os.environ["CC_AGENT_TOKEN"] = ""   # open in test
import agent_api
agent_api.db.connect = db.connect   # ensure the blueprint uses the temp DB too
agent_api._ensure_columns()
import app as appmod
appmod.db.connect = db.connect
client = appmod.app.test_client()

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

def post(path, body):
    return client.post(path, data=json.dumps(body), content_type="application/json")
def get(path):
    return client.get(path)

print("\n=== AGENT 1: lead-puller imports a new T65 list ===")
r = post("/agent/leads/import", {
    "source": "T65_June2026", "owner": "chris",
    "leads": [
        {"name": "Jane Doe", "phone": "3365550101", "city": "Greensboro", "county": "Guilford",
         "birthday": "1962-03-04", "email": "jane@example.com", "lead_type": "t65"},
        {"name": "Bob Smith", "phone": "3365550102", "city": "High Point", "birthday": "1962-07-15"},
        {"name": "Jane Doe", "phone": "3365550101", "city": "Greensboro", "birthday": "1962-03-04"},  # dup
    ]})
j = r.get_json()
check("import returns 200", r.status_code == 200)
check("imported 2 new", j["imported"] == 2)
check("detected 1 duplicate", j["duplicates"] == 1)
check("returned person_ids", len(j["person_ids"]) == 3)
jane_id = j["person_ids"][0]

print("\n=== AGENT 2: call agent reads queue, scores best to top, logs outcome ===")
r = get("/agent/leads/call-queue?owner=chris")
j = r.get_json()
check("call-queue returns leads", j["count"] >= 2)
check("new leads are in queue", any(l["stage"] == "new" for l in j["leads"]))
check("queue exposes phone E.164", j["leads"][0]["phone"].startswith("+1"))

r = post("/agent/leads/score", {"scores": [{"person_id": jane_id, "lead_score": 98},
                                            {"phone": "3365550102", "lead_score": 60}]})
check("score update 2", r.get_json()["updated"] == 2)
r = get("/agent/leads/call-queue?owner=chris")
top = r.get_json()["leads"][0]
check("highest score floats to top", top["id"] == jane_id and top["lead_score"] == 98)

r = post("/agent/leads/disposition", {"person_id": jane_id, "disposition": "callback_requested",
                                       "agent": "chris", "live_talk_seconds": 95, "note": "wants Tue AM"})
j = r.get_json()
check("disposition advances stage to callback", j["stage"] == "callback")
r = get("/agent/leads/call-queue?owner=chris")
check("callback ranks first in queue", r.get_json()["leads"][0]["id"] == jane_id)

print("\n=== AGENT 3: messaging agent pulls email queue, writes+logs, honors opt-out ===")
r = get("/agent/leads/outreach-queue?channel=email&source=T65_June2026")
j = r.get_json()
check("outreach-queue returns only emailable leads", j["count"] == 1 and j["leads"][0]["email"] == "jane@example.com")
check("outreach includes context for personalization", "context" in j["leads"][0] and j["leads"][0]["context"]["first_name"] == "Jane")

r = post("/agent/messages/log", {"person_id": jane_id, "channel": "email", "direction": "out",
                                 "subject": "Your Medicare options", "body": "Hi Jane, ...",
                                 "status": "sent", "template_id": "t65_intro"})
check("message logged", r.get_json()["ok"] and r.get_json()["message_id"] > 0)
r = get("/agent/leads/outreach-queue?channel=email&source=T65_June2026")
check("times_messaged increments", r.get_json()["leads"][0]["context"]["times_messaged"] == 1)

r = post("/agent/leads/opt-out", {"person_id": jane_id, "channel": "all", "reason": "opt_out"})
check("opt-out ok", r.get_json()["ok"])
r = get("/agent/leads/outreach-queue?channel=email&source=T65_June2026")
check("opted-out lead removed from outreach", r.get_json()["count"] == 0)
r = get("/agent/leads/call-queue?owner=chris")
check("opted-out lead removed from call queue (now dnc)", all(l["id"] != jane_id for l in r.get_json()["leads"]))

print("\n=== hardening: opt-out by phone + suppressed re-import ===")
# Bob opts out via a STOP text (we only know his phone, not his id)
r = post("/agent/leads/opt-out", {"phone": "3365550102", "reason": "opt_out"})
check("opt-out by phone matches person", r.get_json().get("matched_person") is True)
# Re-pulling a list that includes Bob's number must NOT resurrect him
r = post("/agent/leads/import", {"source": "T65_July2026",
         "leads": [{"name": "Bob Smith", "phone": "3365550102", "city": "High Point", "birthday": "1962-07-15"},
                   {"name": "New Guy", "phone": "3365550199", "city": "Greensboro", "birthday": "1962-01-01"}]})
j = r.get_json()
check("suppressed number skipped on re-import", j.get("suppressed", 0) == 1)
check("fresh lead still imported", j["imported"] == 1)
# STOP from an unknown number is still recorded so we never text it
r = post("/agent/leads/opt-out", {"phone": "9995551234", "reason": "opt_out"})
check("opt-out of unknown number recorded", r.get_json()["ok"] and r.get_json()["matched_person"] is False)

print("\n=== discovery + export ===")
check("manifest lists all endpoints", len(get("/agent/manifest").get_json()["endpoints"]) >= 9)
check("manifest advertises transcript ingest",
      any(e["path"] == "/agent/transcripts/ingest" for e in get("/agent/manifest").get_json()["endpoints"]))
check("manifest advertises admin endpoints",
      any(e["path"].startswith("/agent/admin/") for e in get("/agent/manifest").get_json()["endpoints"]))
check("schema returns field dictionary", "person_fields" in get("/agent/schema").get_json())
r = get("/agent/leads/export?source=T65_June2026")
check("export returns people", r.get_json()["count"] >= 2)

print("\n=== transcripts: ingest + join + idempotency ===")
# Seed a call_attempt with a known call_control_id so the ingest can resolve a person.
_tx_ccid = "v3:fake-call-control-id-1"
_tx_phone = "+13365550199"  # 'New Guy' from earlier import
with db.connect() as _conn:
    _row = _conn.execute("SELECT person_id FROM phone_numbers WHERE e164=?", (_tx_phone,)).fetchone()
    _tx_pid = _row["person_id"] if _row else None
    _conn.execute("""INSERT INTO call_attempts
        (person_id, phone, agent, dialed_at, disposition, disposition_category,
         call_control_id, dedupe_key)
        VALUES (?,?,?,?,?,?,?,?)""",
        (_tx_pid, _tx_phone, "chris", "2026-06-01T10:00:00",
         "human_transferred", "live_conversation", _tx_ccid, "t|seed|1"))

r = post("/agent/transcripts/ingest", {
    "call_control_id": _tx_ccid,
    "recording_id": "rec-1", "transcription_id": "tx-1",
    "language": "en", "duration_seconds": 42.5,
    "full_text": "[Speaker A 00:00] Hello\n[Speaker B 00:02] Hi there",
    "segments": [
        {"speaker": "Speaker A", "channel": 0, "start": 0.0, "end": 1.0, "text": "Hello"},
        {"speaker": "Speaker B", "channel": 1, "start": 2.0, "end": 3.0, "text": "Hi there"},
    ],
    "raw_payload": {"event": "call.recording.transcription.saved"},
})
j = r.get_json()
check("transcript ingest 200", r.status_code == 200 and j["ok"])
check("transcript joined to person via call_control_id", j["matched"] and j["person_id"] == _tx_pid)
check("transcript joined to call_attempt", j["call_attempt_id"] is not None)

# Idempotency: same transcription_id is a no-op replay.
r2 = post("/agent/transcripts/ingest", {
    "call_control_id": _tx_ccid, "transcription_id": "tx-1",
    "full_text": "duplicate ignored",
})
j2 = r2.get_json()
check("idempotent on transcription_id", j2.get("duplicate") is True and j2["transcript_id"] == j["transcript_id"])

# Unknown call_control_id still saves but reports matched=False.
r3 = post("/agent/transcripts/ingest", {
    "call_control_id": "v3:unknown-call",
    "transcription_id": "tx-orphan",
    "full_text": "[Speaker A 00:00] orphan",
    "segments": [],
})
j3 = r3.get_json()
check("orphan transcript still saved", j3["ok"] and j3["transcript_id"] > 0)
check("orphan transcript reports matched=False", j3["matched"] is False and j3["person_id"] is None)

print("\n=== admin: recent calls, search, detail ===")
r = get("/agent/admin/recent-calls?limit=10")
j = r.get_json()
check("recent-calls returns list", r.status_code == 200 and isinstance(j["calls"], list))
check("recent-calls includes the seeded transcript flag",
      any(c.get("call_control_id") == _tx_ccid and c.get("has_transcript") for c in j["calls"]))

r = get("/agent/admin/lead/search?q=5550199")
check("search by phone digits matches", r.get_json()["count"] >= 1)
r = get("/agent/admin/lead/search?q=Bob")
check("search by name returns Bob", any("Bob" in (l["full_name"] or "") for l in r.get_json()["leads"]))

# Use New Guy (he's not on DNC, has a phone) for editing.
_new_guy_id = _tx_pid
r = get(f"/agent/admin/lead/{_new_guy_id}")
j = r.get_json()
check("lead detail returns person", j["person"]["id"] == _new_guy_id)
check("lead detail includes transcripts", isinstance(j["transcripts"], list))

print("\n=== admin: edit lead (stage + note + manual disposition) ===")
r = post(f"/agent/admin/lead/{_new_guy_id}/update",
         {"stage": "callback", "lead_score": 88, "note": "called manually — hot",
          "disposition": "callback_requested", "agent": "chris"})
j = r.get_json()
check("update returns ok with diff", j["ok"] and j["changed"]["stage"] == "callback")
check("update bumps score", j["person"]["lead_score"] == 88)
check("manual disposition logged", j["changed"].get("manual_disposition") == "callback_requested")

# Reject obviously bad stage
r = post(f"/agent/admin/lead/{_new_guy_id}/update", {"stage": "not_a_stage"})
check("invalid stage rejected", r.status_code == 400)

print("\n=== admin: undo appointment ===")
# Manually book an appointment on New Guy, then undo it via the admin endpoint.
with db.connect() as _conn:
    _conn.execute("UPDATE people SET stage='appointment' WHERE id=?", (_new_guy_id,))
    _conn.execute("""INSERT INTO appointments(person_id, agent, scheduled_at, status, notes, created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (_new_guy_id, "chris", "2026-06-05T14:00:00", "scheduled", "", "2026-06-01T10:00:00"))
r = post("/agent/admin/appointment/undo", {"person_id": _new_guy_id})
j = r.get_json()
check("undo-appointment ok", r.status_code == 200 and j["ok"])
check("undo-appointment returns lead to callback (had prior contact)", j["new_stage"] in ("callback", "attempted"))
check("undo-appointment cancelled at least one appt", len(j["undone_appointments"]) >= 1)

# A lead in 'callback' state is dialable, so the call-queue should include them again
r = get("/agent/leads/call-queue")
check("undone lead re-enters dialing pool", any(l["id"] == _new_guy_id for l in r.get_json()["leads"]))

# No active appointment on a different lead -> 404
with db.connect() as _conn:
    _bob_id = _conn.execute("SELECT id FROM people WHERE full_name LIKE 'Bob%'").fetchone()["id"]
r = post("/agent/admin/appointment/undo", {"person_id": _bob_id})
check("undo with no active appt -> 404", r.status_code == 404)

print("\n=== admin: manual DNC ===")
r = post(f"/agent/admin/lead/{_new_guy_id}/dnc", {"reason": "manual_test"})
check("manual DNC ok", r.status_code == 200 and r.get_json()["ok"])
with db.connect() as _conn:
    _sup = _conn.execute("SELECT COUNT(*) AS n FROM suppressions WHERE scope='person' AND value=?",
                         (str(_new_guy_id),)).fetchone()["n"]
    _phone_sup = _conn.execute("""SELECT COUNT(*) AS n FROM suppressions s
        WHERE s.scope='phone' AND s.value IN (SELECT e164 FROM phone_numbers WHERE person_id=?)""",
        (_new_guy_id,)).fetchone()["n"]
check("DNC creates person suppression", _sup >= 1)
check("DNC creates phone suppression for every phone", _phone_sup >= 1)
r = get("/agent/leads/call-queue")
check("DNC lead removed from call queue", all(l["id"] != _new_guy_id for l in r.get_json()["leads"]))

print("\n=== auth gate ===")
agent_api.AGENT_TOKEN = "secret-xyz"
check("no token -> 401", client.get("/agent/leads/call-queue").status_code == 401)
check("bad token -> 401", client.get("/agent/leads/call-queue", headers={"Authorization": "Bearer wrong"}).status_code == 401)
check("good token -> 200", client.get("/agent/leads/call-queue", headers={"Authorization": "Bearer secret-xyz"}).status_code == 200)
agent_api.AGENT_TOKEN = ""

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys; sys.exit(0 if passed == len(results) else 1)
