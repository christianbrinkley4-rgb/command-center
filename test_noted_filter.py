"""The Lead Book 'Noted' filter returns only leads with a HUMAN-authored note,
and never trips on system notes (ai_score, automation, post_call_ai, ...)."""
import json
import os
import tempfile
from datetime import datetime

os.environ["CC_NO_AUTOSTART"] = "1"
_tmp = os.path.join(tempfile.mkdtemp(), "test_noted.db")
import db
db.DB_PATH = _tmp
_orig = db.connect
db.connect = lambda *a, **k: _orig(_tmp)
db.init_db(_tmp)
import agent_api
agent_api.db.connect = db.connect
agent_api._ensure_columns()
import app as appmod
appmod.db.connect = db.connect
client = appmod.app.test_client()

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

def now():
    return datetime.now().isoformat(timespec="seconds")

def seed(name, birthday="1/1/1962"):
    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO people(person_key, full_name, city, birthday, owner, stage,
               lead_score, source, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"k|{name}", name, "Greensboro", birthday, "chris", "contacted", 60,
             "Dialer_Existing", now(), now()))
        return cur.lastrowid

def note(pid, body, author):
    with db.connect() as c:
        c.execute("INSERT INTO person_notes(person_id, body, author, created_at) VALUES (?,?,?,?)",
                  (pid, body, author, now()))

# Lead A: a note I left from the dashboard (author 'agent')
a = seed("Alice Noted"); note(a, "Wants a callback Tuesday", "agent")
# Lead B: a note I left from the admin menu
b = seed("Bob Admin");   note(b, "Corrected spelling", "admin_menu")
# Lead C: ONLY system notes -> should NOT count as 'noted'
c_ = seed("Carl System"); note(c_, "lead_score -> 70", "ai_score"); note(c_, "redial scheduled", "automation")
# Lead D: no notes at all
d_ = seed("Dora None")

def leads(noted=False):
    p = "/api/leads" + ("?noted=1" if noted else "")
    return client.get(p).get_json()["leads"]

print("\n=== unfiltered shows everyone ===")
allnames = {l["full_name"] for l in leads()}
check("all four leads present unfiltered", {"Alice Noted","Bob Admin","Carl System","Dora None"} <= allnames)

print("\n=== noted=1 returns only human-noted leads ===")
noted_names = {l["full_name"] for l in leads(noted=True)}
check("Alice (agent note) included", "Alice Noted" in noted_names)
check("Bob (admin_menu note) included", "Bob Admin" in noted_names)
check("Carl (only system notes) EXCLUDED", "Carl System" not in noted_names)
check("Dora (no notes) EXCLUDED", "Dora None" not in noted_names)
check("exactly 2 noted leads", len(noted_names) == 2)

print("\n=== human_note_count is reported + ignores system notes ===")
by_name = {l["full_name"]: l for l in leads()}
check("Alice human_note_count == 1", by_name["Alice Noted"]["human_note_count"] == 1)
check("Carl human_note_count == 0 (system notes don't count)", by_name["Carl System"]["human_note_count"] == 0)

# Adding a second human note bumps the count
note(a, "Left a voicemail too", "chris")
by_name = {l["full_name"]: l for l in leads()}
check("Alice count now 2 after another human note", by_name["Alice Noted"]["human_note_count"] == 2)

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys
sys.exit(0 if passed == len(results) else 1)
