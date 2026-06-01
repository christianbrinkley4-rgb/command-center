"""Tests for oscr_ingestion_agent.py. Run: python test_oscr_ingestion_agent.py"""
import os
import tempfile

_d = tempfile.mkdtemp()
_tmp = os.path.join(_d, "oscr.db")

import db
db.DB_PATH = _tmp
_connect = db.connect
db.connect = lambda *a, **k: _connect(_tmp)

import build_dialer_queue as bq
bq.db.connect = db.connect

import oscr_ingestion_agent as oscr
oscr.db.connect = db.connect

results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + name)


os.environ["CC_TARGET_BIRTH_YEARS"] = "1962"
cfg = oscr.Config(
    strategy="api",
    owner="chris",
    default_source="OSCR",
    default_score=100,
    campaign_source_map={"T65 June": "OSCR_T65_June"},
)
oscr.ensure_schema()

print("\n=== OSCR ingest: hot lead, dedupe, DNC ===")
payload = [
    {
        "id": "oscr-1",
        "name": "Fresh Lead",
        "phone": "3365551001",
        "city": "Greensboro",
        "county": "Guilford",
        "birthday": "1/1/1962",
        "campaign": "T65 June",
    },
    {
        "id": "oscr-2",
        "name": "Fresh Lead Again",
        "phone": "3365551001",
        "city": "Greensboro",
        "birthday": "1/1/1962",
    },
    {
        "id": "oscr-3",
        "name": "No Call",
        "phone": "3365551002",
        "city": "Greensboro",
        "birthday": "1/1/1962",
        "do_not_call": True,
    },
]
counts = oscr.ingest_many(payload, cfg)
check("one lead imported", counts.get("imported") == 1)
check("duplicate phone skipped", counts.get("duplicate_phone") == 1)
check("dnc skipped", counts.get("skipped:oscr_dnc") == 1)

with db.connect() as conn:
    people = conn.execute("SELECT full_name, source, lead_score, owner FROM people").fetchall()
    phones = conn.execute("SELECT e164 FROM phone_numbers").fetchall()
    suppressed = conn.execute("SELECT value FROM suppressions WHERE reason='oscr_dnc'").fetchall()

check("only one person row created", len(people) == 1)
check("campaign source mapped", people[0]["source"] == "OSCR_T65_June")
check("hot lead score set", people[0]["lead_score"] == 100)
check("owner assigned", people[0]["owner"] == "chris")
check("one phone row created", len(phones) == 1 and phones[0]["e164"] == "+13365551001")
check("dnc phone suppressed", len(suppressed) == 1 and suppressed[0]["value"] == "+13365551002")

queue = bq.build_queue(owner="chris", limit=10)
check("hot OSCR lead is queue-ready", len(queue) == 1 and queue[0]["Name"] == "Fresh Lead")
check("queue source is preserved", queue[0]["Source"] == "OSCR_T65_June")

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*52}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else " *** FAILURES ***"))
raise SystemExit(0 if passed == len(results) else 1)
