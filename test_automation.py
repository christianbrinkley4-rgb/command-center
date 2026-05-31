"""End-to-end test of the automation engine: dispositions -> scheduled next actions,
inbound replies -> parsed callbacks, the worker firing due tasks, and learning stats.
Fresh throwaway DB. Run: python test_automation.py"""
import json
import os
import tempfile
from datetime import datetime, timedelta

_tmp = os.path.join(tempfile.mkdtemp(), "test_auto.db")
import db
db.DB_PATH = _tmp
_orig = db.connect
db.connect = lambda *a, **k: _orig(_tmp)
db.init_db(_tmp)

os.environ["CC_NO_AUTOSTART"] = "1"
os.environ["CC_AGENT_TOKEN"] = ""
import agent_api; agent_api.db.connect = db.connect; agent_api._ensure_columns()
import automation; automation.db.connect = db.connect; automation.ensure_schema()
import learning; learning.db.connect = db.connect
import app as appmod; appmod.db.connect = db.connect
client = appmod.app.test_client()

results = []
def check(name, cond):
    results.append((name, bool(cond))); print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
def post(p, b): return client.post(p, data=json.dumps(b), content_type="application/json")
def get(p): return client.get(p)

# seed a lead with phone + email
post("/agent/leads/import", {"source": "T65_June", "owner": "chris", "leads": [
    {"name": "Mary Jones", "phone": "3365551200", "city": "Greensboro", "birthday": "1962-04-01", "email": "mary@x.com"}]})
pid = get("/agent/leads/call-queue").get_json()["leads"][0]["id"]

print("\n=== voicemail disposition -> auto-schedules follow-up text + redial ===")
r = post("/agent/leads/disposition", {"person_id": pid, "disposition": "voicemail_dropped", "agent": "chris"})
sched = r.get_json().get("scheduled", [])
check("voicemail scheduled something", len(sched) >= 1)
check("scheduled a follow-up text", any("sms" in s for s in sched))
with db.connect() as c:
    tasks = c.execute("SELECT kind, reason FROM scheduled_tasks WHERE person_id=? AND status='pending'", (pid,)).fetchall()
kinds = {t["kind"] for t in tasks}
check("a redial call is queued too", "call" in kinds)
check("a text is queued", "sms" in kinds)

print("\n=== inbound text 'call me 10am next wednesday' -> scheduled call at that time ===")
r = post("/agent/inbound", {"phone": "3365551200", "channel": "sms", "text": "call me 10am next wednesday"})
j = r.get_json()
check("inbound matched person", j.get("person_id") == pid)
check("intent recognized as callback", j.get("intent") == "callback")
check("a call was scheduled", j.get("scheduled") and j["scheduled"]["kind"] == "call")
sched_dt = datetime.fromisoformat(j["scheduled"]["due_at"])
check("scheduled on a Wednesday at 10am", sched_dt.weekday() == 2 and sched_dt.hour == 10)
with db.connect() as c:
    stage = c.execute("SELECT stage FROM people WHERE id=?", (pid,)).fetchone()["stage"]
check("lead moved to callback stage", stage == "callback")

print("\n=== inbound 'STOP' -> opt-out, suppressed, pending tasks cancelled ===")
post("/agent/leads/import", {"source": "T65_June", "leads": [
    {"name": "Stan Stop", "phone": "3365551299", "city": "High Point", "birthday": "1962-01-01"}]})
with db.connect() as c:
    spid = c.execute("SELECT id FROM people WHERE full_name='Stan Stop'").fetchone()["id"]
post("/agent/leads/disposition", {"person_id": spid, "disposition": "no_answer"})  # schedules a retry
r = post("/agent/inbound", {"phone": "3365551299", "channel": "sms", "text": "STOP"})
check("STOP recognized", r.get_json().get("intent") == "stop")
with db.connect() as c:
    sup = c.execute("SELECT 1 FROM suppressions WHERE scope='person' AND value=?", (str(spid),)).fetchone()
    pend = c.execute("SELECT COUNT(*) n FROM scheduled_tasks WHERE person_id=? AND status='pending'", (spid,)).fetchone()["n"]
    st = c.execute("SELECT stage FROM people WHERE id=?", (spid,)).fetchone()["stage"]
check("person suppressed", sup is not None)
check("pending tasks cancelled", pend == 0)
check("stage is dnc", st == "dnc")

print("\n=== worker fires due tasks (fresh lead -> voicemail -> text becomes due -> sends) ===")
post("/agent/leads/import", {"source": "T65_June", "owner": "will", "leads": [
    {"name": "Wendy Worker", "phone": "3365551250", "city": "Elon", "birthday": "1962-05-05"}]})
with db.connect() as c:
    wpid = c.execute("SELECT id FROM people WHERE full_name='Wendy Worker'").fetchone()["id"]
post("/agent/leads/disposition", {"person_id": wpid, "disposition": "voicemail_dropped", "agent": "will"})
with db.connect() as c:
    c.execute("UPDATE scheduled_tasks SET due_at=? WHERE person_id=? AND kind='sms'",
              ((datetime.now() - timedelta(minutes=5)).isoformat(timespec='seconds'), wpid))
fired = automation.fire_due_tasks()
check("worker fired at least one sms", fired["sms"] >= 1)
with db.connect() as c:
    msg = c.execute("SELECT COUNT(*) n FROM messages WHERE person_id=? AND channel='sms' AND direction='out'", (wpid,)).fetchone()["n"]
check("outbound text recorded in history", msg >= 1)

print("\n=== suppressed lead never gets a text fired ===")
with db.connect() as c:
    # schedule a text for Stan (suppressed) in the past, then fire
    automation.schedule_task(c, spid, "sms", (datetime.now() - timedelta(minutes=1)).isoformat(timespec='seconds'),
                             "should_not_send", "+13365551299")
fired2 = automation.fire_due_tasks()
check("suppressed text skipped, not sent", fired2["skipped_suppressed"] >= 1)

print("\n=== learning stats compute without error ===")
learning.refresh_stats()
ins = get("/api/insights").get_json()
check("insights endpoint returns structure", "sources" in ins and "overall_contact_rate" in ins)

print("\n=== automations API surfaces the queue ===")
au = get("/api/automations").get_json()
check("automations API returns counts", "counts" in au and "upcoming" in au)

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + (" ALL GOOD" if passed == len(results) else " *** FAILURES ***"))
import sys; sys.exit(0 if passed == len(results) else 1)
