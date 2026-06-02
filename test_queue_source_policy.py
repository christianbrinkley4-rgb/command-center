"""Queue source policy tests. Run: python test_queue_source_policy.py"""
import tempfile
import os

import db
import queue_source_policy as qsp
import build_dialer_queue as bq

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


print("\n=== queue_source_policy ===")
check("T65_Dec_NC is December 1957 cohort", qsp.is_december_1957_cohort("T65_Dec_NC", "12/01/1957"))
check("birth year 1957 alone is December cohort", qsp.is_december_1957_cohort("", "03/15/1957"))
check("December cohort not dialer-eligible", not qsp.dialer_queue_eligible("T65_Dec_VA", "01/02/1957"))
check("OSCR direct is eligible", qsp.dialer_queue_eligible("OSCR_T65_June", "1962-03-15"))
check("other T65 is eligible", qsp.dialer_queue_eligible("T65_June2026", "1962-03-15"))
check("Dialer_Existing T65 is eligible", qsp.dialer_queue_eligible("Dialer_Existing", "1962-03-15"))
check("empty source + 1962 birthday is eligible", qsp.dialer_queue_eligible("", "1/1/1962"))
check("empty source + 1957 birthday is not eligible", not qsp.dialer_queue_eligible("", "12/1/1957"))

print("\n=== build_dialer_queue excludes Dec 1957 ===")
_d = tempfile.mkdtemp()
_tmp = os.path.join(_d, "policy.db")
db.DB_PATH = _tmp
_orig = db.connect
db.connect = lambda *a, **k: _orig(_tmp)
db.init_db(_tmp)

with db.connect() as conn:
    now = "2026-06-02T12:00:00"
    for name, source, birthday in [
        ("Dec NC", "T65_Dec_NC", "12/01/1957"),
        ("OSCR Fresh", "OSCR_T65_June", "1962-03-15"),
        ("T65 Other", "T65_June2026", "1962-04-01"),
    ]:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, birthday, city, source, owner,
               stage, lead_score, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?, 'new', 80, ?, ?)""",
            (f"k|{name}", name, birthday, "Greensboro", source, "chris", now, now),
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
            (pid, f"+1336555{pid:04d}", now),
        )

rows = bq.build_queue(owner="chris")
names = {r["Name"] for r in rows}
check("Dec NC lead excluded from queue", "Dec NC" not in names)
check("OSCR lead in queue", "OSCR Fresh" in names)
check("other T65 in queue", "T65 Other" in names)

passed = sum(1 for _, ok in results if ok)
print(f"\n{'=' * 52}\n{passed}/{len(results)} PASSED")
import sys
sys.exit(0 if passed == len(results) else 1)
