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
check("schema returns field dictionary", "person_fields" in get("/agent/schema").get_json())
r = get("/agent/leads/export?source=T65_June2026")
check("export returns people", r.get_json()["count"] >= 2)

print("\n=== auth gate ===")
agent_api.AGENT_TOKEN = "secret-xyz"
check("no token -> 401", client.get("/agent/leads/call-queue").status_code == 401)
check("bad token -> 401", client.get("/agent/leads/call-queue", headers={"Authorization": "Bearer wrong"}).status_code == 401)
check("good token -> 200", client.get("/agent/leads/call-queue", headers={"Authorization": "Bearer secret-xyz"}).status_code == 200)
agent_api.AGENT_TOKEN = ""

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  ✓ ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys; sys.exit(0 if passed == len(results) else 1)
