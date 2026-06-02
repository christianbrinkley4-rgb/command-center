"""Tests for the destructive /agent/admin/leads/purge endpoint.

Proves: it deletes ONLY the targeted rows (and their children), leaves
everything else intact, keeps phone suppressions, refuses to run without
criteria or without confirm, and dry_run touches nothing."""
import json
import os
import tempfile
from datetime import datetime

# Prevent app.py's background sync/worker threads from pulling real data into
# our throwaway DB (they would pollute absolute row counts this test asserts on).
os.environ["CC_NO_AUTOSTART"] = "1"

_tmp = os.path.join(tempfile.mkdtemp(), "test_purge.db")
import db
db.DB_PATH = _tmp
_orig = db.connect
db.connect = lambda *a, **k: _orig(_tmp)
db.init_db(_tmp)

import agent_api
agent_api.db.connect = db.connect
agent_api._ensure_columns()
import automation
automation.db.connect = db.connect
automation.ensure_schema()
import oscr_ingestion_agent
oscr_ingestion_agent.db.connect = db.connect
oscr_ingestion_agent.ensure_schema()

import app as appmod
appmod.db.connect = db.connect
client = appmod.app.test_client()

# Start hermetic: clear anything autostart may have added before CC_NO_AUTOSTART
# took effect, so absolute counts are deterministic.
with db.connect() as _c:
    _c.execute("PRAGMA foreign_keys=OFF")
    for _t in ("transcripts", "call_attempts", "messages", "appointments",
               "person_notes", "deals", "scheduled_tasks", "oscr_seen_leads",
               "phone_numbers", "suppressions", "people"):
        try:
            _c.execute(f"DELETE FROM {_t}")
        except Exception:
            pass
    _c.execute("PRAGMA foreign_keys=ON")

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

def now():
    return datetime.now().isoformat(timespec="seconds")

_ph = [0]
def ph():
    _ph[0] += 1
    return f"+1336000{_ph[0]:04d}"

def seed(name, source, birthday):
    p = ph()
    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO people(person_key, full_name, city, birthday, owner, stage,
               lead_score, source, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"k|{name}|{p}", name, "Greensboro", birthday, "chris", "new", 50, source, now(), now()))
        pid = cur.lastrowid
        c.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)", (pid, p, now()))
        c.execute("""INSERT INTO call_attempts(person_id, phone, agent, dialed_at, disposition,
                     disposition_category, dedupe_key) VALUES (?,?,?,?,?,?,?)""",
                  (pid, p, "chris", now(), "no_answer", "no_answer", f"d|{pid}"))
        c.execute("INSERT INTO person_notes(person_id, body, author, created_at) VALUES (?,?,?,?)",
                  (pid, "note", "test", now()))
    return pid, p

# Seed: 3 December-1957 leads (target) + 2 Dialer_Existing-1962 (keep)
t1, t1p = seed("Dec One", "T65_Dec_NC", "12/1/1957")
t2, t2p = seed("Dec Two", "T65_Dec_NC", "1957-12-01")
t3, t3p = seed("Dec Three", "T65_Dec_VA", "12/15/1957")
k1, k1p = seed("Keep One", "Dialer_Existing", "1/1/1962")
k2, k2p = seed("Keep Two", "Dialer_Existing", "1/1/1962")

# A phone suppression on a target lead — must SURVIVE the purge
with db.connect() as c:
    c.execute("INSERT INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
              (t1p, "oscr_dnc", now()))

def post(body):
    return client.post("/agent/admin/leads/purge", data=json.dumps(body), content_type="application/json")

print("\n=== guards ===")
check("no criteria -> 400", post({"confirm": True}).status_code == 400)
check("criteria but no confirm/dry_run -> 400",
      post({"sources": ["T65_Dec_NC"]}).status_code == 400)

print("\n=== dry_run touches nothing ===")
r = post({"sources": ["T65_Dec_NC", "T65_Dec_VA"], "birth_year": 1957, "dry_run": True})
j = r.get_json()
check("dry_run reports 3 matched", j["matched_people"] == 3)
check("dry_run flags children to delete", j["would_delete_children"]["phone_numbers"] == 3)
with db.connect() as c:
    still = c.execute("SELECT COUNT(*) n FROM people").fetchone()["n"]
check("dry_run deleted nothing (still 5 people)", still == 5)

print("\n=== real purge (AND semantics: source AND birth_year) ===")
r = post({"sources": ["T65_Dec_NC", "T65_Dec_VA"], "birth_year": 1957, "confirm": True})
j = r.get_json()
check("purge ok", r.status_code == 200 and j["ok"])
check("deleted exactly 3 people", j["deleted_people"] == 3)
check("deleted their phones", j["deleted_children"]["phone_numbers"] == 3)
check("deleted their call_attempts", j["deleted_children"]["call_attempts"] == 3)
check("deleted their notes", j["deleted_children"]["person_notes"] == 3)

with db.connect() as c:
    remaining = {r["full_name"]: r["id"] for r in c.execute("SELECT id, full_name FROM people")}
    remaining_people = c.execute("SELECT COUNT(*) n FROM people").fetchone()["n"]
    orphan_phones = c.execute(
        "SELECT COUNT(*) n FROM phone_numbers WHERE person_id NOT IN (SELECT id FROM people)").fetchone()["n"]
    orphan_calls = c.execute(
        "SELECT COUNT(*) n FROM call_attempts WHERE person_id NOT IN (SELECT id FROM people)").fetchone()["n"]
    supp_kept = c.execute("SELECT COUNT(*) n FROM suppressions WHERE value=?", (t1p,)).fetchone()["n"]

check("2 people remain", remaining_people == 2)
check("the kept leads are the 1962 ones", "Keep One" in remaining and "Keep Two" in remaining)
check("targets are gone", "Dec One" not in remaining and "Dec Two" not in remaining and "Dec Three" not in remaining)
check("no orphaned phone rows", orphan_phones == 0)
check("no orphaned call_attempts", orphan_calls == 0)
check("phone suppression SURVIVED the purge (DNC kept)", supp_kept == 1)

print("\n=== safety: source-only without birth_year won't touch a mismatched year ===")
# Seed a hypothetical T65_Dec_NC that is somehow 1962 — AND semantics protect it
x1, _ = seed("Weird Year", "T65_Dec_NC", "1/1/1962")
r = post({"sources": ["T65_Dec_NC"], "birth_year": 1957, "confirm": True})
with db.connect() as c:
    weird_still = c.execute("SELECT COUNT(*) n FROM people WHERE full_name='Weird Year'").fetchone()["n"]
check("AND semantics spared the 1962-dated T65 row", weird_still == 1)

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys
sys.exit(0 if passed == len(results) else 1)
