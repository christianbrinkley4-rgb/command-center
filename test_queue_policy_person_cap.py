"""The attempt cap must count per PERSON across all their numbers, not per phone.
A lead with two numbers called twice each (4 total) is capped at max=4."""
import os
import tempfile
from datetime import datetime

os.environ["CC_NO_AUTOSTART"] = "1"
os.environ["CC_MAX_CALL_ATTEMPTS"] = "4"

_tmp = os.path.join(tempfile.mkdtemp(), "test_qp.db")
import db
db.DB_PATH = _tmp
_orig = db.connect
db.connect = lambda *a, **k: _orig(_tmp)
db.init_db(_tmp)
import queue_policy
queue_policy.db.connect = db.connect

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

def now():
    return datetime.now().isoformat(timespec="seconds")

with db.connect() as conn:
    cur = conn.execute(
        """INSERT INTO people(person_key, full_name, city, birthday, owner, stage,
           lead_score, source, first_seen_at, last_activity_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("k|multi", "Multi Phone", "Greensboro", "1/1/1962", "chris", "voicemail",
         50, "Dialer_Existing", now(), now()))
    pid = cur.lastrowid
    for e164 in ("+13365559001", "+13365559002"):
        conn.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                     (pid, e164, now()))
    # 2 calls on each number = 4 total for the person, spread across 2 phones,
    # on a PRIOR day so the same-day guard doesn't interfere.
    for e164 in ("+13365559001", "+13365559002"):
        for i in range(2):
            conn.execute(
                """INSERT INTO call_attempts(person_id, phone, agent, dialed_at,
                   disposition, disposition_category, dedupe_key) VALUES (?,?,?,?,?,?,?)""",
                (pid, e164, "chris", "2026-05-29T10:0%d:00" % i, "no_answer", "no_answer",
                 f"k|{e164}|{i}"))

print("\n=== per-person attempt cap ===")
with db.connect() as conn:
    # Per-phone count is only 2 (would wrongly pass the old per-phone cap)...
    check("per-phone count is 2", queue_policy.attempt_count(conn, pid, "+13365559001") == 2)
    # ...but the person's total is 4.
    check("per-person count is 4", queue_policy.attempt_count(conn, pid) == 4)

    status = queue_policy.retry_status(conn, pid, "+13365559001", stage="voicemail",
                                       qtype="retry", agenda_day="2026-06-02")
    check("lead is BLOCKED at the cap (per-person)", status["allowed"] is False)
    check("reason is max_attempts", status["reason"] == "max_attempts")
    check("attempts reported as 4", status["attempts"] == 4)

print("\n=== under the cap still allowed ===")
# A different person with only 2 total calls (1 per phone) stays dialable.
with db.connect() as conn:
    cur = conn.execute(
        """INSERT INTO people(person_key, full_name, city, birthday, owner, stage,
           lead_score, source, first_seen_at, last_activity_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("k|under", "Under Cap", "Greensboro", "1/1/1962", "chris", "voicemail",
         50, "Dialer_Existing", now(), now()))
    pid2 = cur.lastrowid
    for e164 in ("+13365559003", "+13365559004"):
        conn.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                     (pid2, e164, now()))
        conn.execute(
            """INSERT INTO call_attempts(person_id, phone, agent, dialed_at,
               disposition, disposition_category, dedupe_key) VALUES (?,?,?,?,?,?,?)""",
            (pid2, e164, "chris", "2026-05-29T10:00:00", "no_answer", "no_answer", f"u|{e164}"))
    status2 = queue_policy.retry_status(conn, pid2, "+13365559003", stage="voicemail",
                                        qtype="retry", agenda_day="2026-06-02")
    check("2-total-call lead still allowed", status2["allowed"] is True)

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys
sys.exit(0 if passed == len(results) else 1)
