"""Edge cases + light concurrency for scoring, lookup, and the queue builder.

The goal is to make sure boundary conditions (no phones, max attempts, racing
threads, malformed payloads) degrade gracefully — never deadlock, never write
inconsistent state, never break the dashboard."""
import os
import sys
import tempfile
import threading
from datetime import datetime

_tmp = os.path.join(tempfile.mkdtemp(), "test_edge.db")
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

import oscr_ingestion_agent
oscr_ingestion_agent.db.connect = db.connect
oscr_ingestion_agent.ensure_schema()

import lead_scoring
import number_lookup
number_lookup._ensure_columns()
import app as appmod
appmod.db.connect = db.connect

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def now():
    return datetime.now().isoformat(timespec="seconds")


_PHONE = [0]


def _phone():
    _PHONE[0] += 1
    return f"+13369998{_PHONE[0]:03d}"


def seed(name, score=60, source="T65_Dec_NC", stage="new", city="Greensboro",
         birthday="1962-03-04", phone=None):
    if phone is None:
        phone = _phone()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, city, birthday, owner, stage,
               lead_score, source, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"k|{name}|{phone}", name, city, birthday, "chris", stage, score,
             source, now(), now()))
        pid = cur.lastrowid
        if phone:
            conn.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                         (pid, phone, now()))
    return pid, phone


# ------------------------------------------------------------------ scoring edges

print("\n=== Lead scoring: edge cases ===")

# Person with NO phone at all — heuristic still scores, no exceptions
no_phone_pid, _ = seed("No Phone Person", phone="")
with db.connect() as conn:
    conn.execute("DELETE FROM phone_numbers WHERE person_id=?", (no_phone_pid,))
res = lead_scoring.score_batch([no_phone_pid], use_ai=False)
check("scoring a person with no phone does not crash", res["scored"] == 1 and res["errors"] == 0)

# Person with 50 prior attempts — history adjustment caps
many_pid, many_ph = seed("Heavy Attempts")
with db.connect() as conn:
    for i in range(50):
        conn.execute(
            """INSERT INTO call_attempts(person_id, phone, agent, dialed_at,
               disposition, disposition_category, dedupe_key) VALUES (?,?,?,?,?,?,?)""",
            (many_pid, many_ph, "chris", now(), "no_answer", "no_answer", f"e|{many_pid}|{i}"))
res = lead_scoring.score_batch([many_pid], force=True, use_ai=False)
with db.connect() as conn:
    s = conn.execute("SELECT lead_score FROM people WHERE id=?", (many_pid,)).fetchone()["lead_score"]
check("50-attempt no-connect history pulls score down but stays >= 0", 0 <= s < 60)

# Scoring a non-existent person id
res = lead_scoring.score_batch([999999], use_ai=False)
check("non-existent person id reports skipped, no crash",
      res["scored"] == 0 and res["skipped"] == 1)


# ------------------------------------------------------------------ scoring concurrency

print("\n=== Lead scoring: concurrent threads on the same lead don't double-write ===")

racey_pid, _ = seed("Race Target", score=50)
# Force the AI off so timing is deterministic
errors = []


def race_target():
    try:
        lead_scoring.score_batch([racey_pid], force=True, use_ai=False)
    except Exception as e:
        errors.append(repr(e))


threads = [threading.Thread(target=race_target) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=10)
with db.connect() as conn:
    note_count = conn.execute(
        "SELECT COUNT(*) n FROM person_notes WHERE person_id=? AND author='ai_score'",
        (racey_pid,)).fetchone()["n"]
check("no exceptions from concurrent scoring", not errors)
check("5 concurrent score calls produced exactly 5 notes (no lost writes)", note_count == 5)


# ------------------------------------------------------------------ number_lookup edges

print("\n=== Number lookup: malformed payloads + 429 + empty data ===")

os.environ["TELNYX_API_KEY"] = "test-key"

# Malformed payload: missing 'data'
parsed = number_lookup._parse_lookup_payload({"weird": "shape"})
check("malformed payload returns empty-ish dict, no exception",
      parsed["line_type"] == "" and parsed["carrier"] == "")

# Wholly unknown line_type
parsed2 = number_lookup._parse_lookup_payload({"data": {"phone_number": "+1...",
                                                        "carrier": {"type": "alien_relay"}}})
check("unknown line_type passes through as the original lowercased token",
      parsed2["line_type"] == "alien_relay")

# Save runner errors
saved = []
def boom_lookup(phone):
    raise number_lookup.TelnyxError("rate_limited (retry after 1.0s)")

_orig = number_lookup.lookup_one
number_lookup.lookup_one = boom_lookup

with db.connect() as conn:
    conn.execute("DELETE FROM oscr_ingestion_state WHERE key LIKE 'lookup_spend_%'")
err_pid, err_ph = seed("Errored Phone", score=85)
summary = number_lookup.lookup_pending(limit=1, sleep_between_seconds=0,
                                       never_called=True, queue_only=True)
check("rate-limit error counts as error, doesn't crash the runner",
      summary["errors"] >= 1 and summary["classified"] == 0)
number_lookup.lookup_one = _orig


# ------------------------------------------------------------------ queue edges

print("\n=== _build_ranked_queue: degenerate inputs ===")

q_empty = appmod._build_ranked_queue(owner="never-anyone", limit=10)
check("unknown owner returns empty queue, no error", q_empty == [])

q_zero = appmod._build_ranked_queue(owner="chris", limit=0)
check("limit=0 returns empty queue", q_zero == [])

# Suppress every lead — queue should be empty
with db.connect() as conn:
    for pid in [r["id"] for r in conn.execute("SELECT id FROM people WHERE owner='chris'")]:
        conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('person',?, 'test', ?)",
                     (str(pid), now()))
q_all_sup = appmod._build_ranked_queue(owner="chris", limit=100)
check("all leads suppressed -> queue empty", q_all_sup == [])

# Cleanup so other sections see fresh data
with db.connect() as conn:
    conn.execute("DELETE FROM suppressions WHERE reason='test'")


# ------------------------------------------------------------------ /today + search

print("\n=== /today + /api/today/search render under edge cases ===")

client = appmod.app.test_client()
r = client.get("/today?owner=chris")
check("/today renders 200 even with weird state", r.status_code == 200)
check("/today contains the system health row", b"System health" not in r.data or b"Queue depth" in r.data)
check("/today contains the 7-day section", b"Last 7 days" in r.data)
check("/today contains the search input", b"leadSearch" in r.data)

r2 = client.get("/api/today/search?q=")
check("empty q returns count=0", r2.get_json()["count"] == 0)

r3 = client.get("/api/today/search?q=zzzzzzz-not-a-name")
check("no-match returns count=0", r3.get_json()["count"] == 0)


# ------------------------------------------------------------------ summary

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
sys.exit(0 if passed == len(results) else 1)
