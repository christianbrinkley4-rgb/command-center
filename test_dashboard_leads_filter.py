"""Dashboard lead-list visibility tests. Run: python test_dashboard_leads_filter.py"""
import os
import tempfile

os.environ["CC_NO_AUTOSTART"] = "1"
os.environ["CC_PASSWORD"] = ""
os.environ["CC_TARGET_BIRTH_YEARS"] = "1957,1962"

_d = tempfile.mkdtemp()
_tmp = os.path.join(_d, "dashboard_leads.db")

import db
db.DB_PATH = _tmp
_connect = db.connect
db.connect = lambda *a, **k: _connect(_tmp)
db.init_db(_tmp)

import agent_api
agent_api.db.connect = db.connect
agent_api._ensure_columns()

import app as cc_app
cc_app.db.connect = db.connect

results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + name)


with db.connect() as conn:
    now = "2026-06-02T12:00:00"
    for name, stage, phone in [
        ("Visible T65", "new", "+13365553001"),
        ("Closed T65", "closed", "+13365553002"),
        ("DNC T65", "dnc", "+13365553003"),
    ]:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, birthday, city, source, owner,
               stage, lead_score, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,90,?,?)""",
            (f"k|{name}", name, "1/1/1962", "Greensboro", "T65_June2026",
             "chris", stage, now, now),
        )
        conn.execute(
            "INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
            (cur.lastrowid, phone, now),
        )

client = cc_app.app.test_client()

print("\n=== dashboard lead list filters closed/dnc by default ===")
r = client.get("/api/leads?owner=chris")
data = r.get_json()
names = {lead["full_name"] for lead in data["leads"]}
check("api/leads ok", r.status_code == 200)
check("visible lead remains", "Visible T65" in names)
check("closed lead hidden", "Closed T65" not in names)
check("dnc lead hidden", "DNC T65" not in names)

r = client.get("/api/today/search?q=T65")
search_names = {lead["full_name"] for lead in r.get_json()["leads"]}
check("today search hides closed", "Closed T65" not in search_names)
check("today search hides dnc", "DNC T65" not in search_names)

passed = sum(1 for _, ok in results if ok)
print(f"\n{'=' * 52}\n{passed}/{len(results)} PASSED")
raise SystemExit(0 if passed == len(results) else 1)
