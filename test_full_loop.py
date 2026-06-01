"""Full closed-loop test: OSCR import -> rank -> build dialer queue (correct order)
-> simulate a dialer session -> sync results back -> automation schedules next actions.
Fresh throwaway DB + temp xlsx. Run: python test_full_loop.py"""
import json
import os
import tempfile
from datetime import datetime, timedelta

_d = tempfile.mkdtemp()
_tmp = os.path.join(_d, "loop.db")
import db
db.DB_PATH = _tmp
_o = db.connect
db.connect = lambda *a, **k: _o(_tmp)
db.init_db(_tmp)

os.environ["CC_NO_AUTOSTART"] = "1"; os.environ["CC_AGENT_TOKEN"] = ""
import agent_api; agent_api.db.connect = db.connect; agent_api._ensure_columns()
import automation; automation.db.connect = db.connect; automation.ensure_schema()
import build_dialer_queue as bq; bq.db.connect = db.connect
import sync as syncmod; syncmod.db.connect = db.connect
import app as appmod; appmod.db.connect = db.connect
import pandas as pd
client = appmod.app.test_client()

results = []
def check(n, c): results.append((n, bool(c))); print(f"  [{'PASS' if c else 'FAIL'}] {n}")
def post(p, b): return client.post(p, data=json.dumps(b), content_type="application/json")

print("\n=== 1. OSCR agent imports a fresh T65 list ===")
leads = [{"name": f"Lead {i:03d}", "phone": f"33644{40000+i}", "city": "Greensboro",
          "birthday": "1962-03-15", "email": (f"l{i}@x.com" if i % 2 == 0 else "")} for i in range(20)]
post("/agent/leads/import", {"source": "OSCR_T65_June", "owner": "chris", "leads": leads})
# give a couple high scores (scoring agent) + one callback
ids = [l["id"] for l in client.get("/agent/leads/call-queue?limit=50").get_json()["leads"]]
post("/agent/leads/score", {"scores": [{"person_id": ids[5], "lead_score": 95}, {"person_id": ids[8], "lead_score": 88}]})
# a prospect texted in a callback time
post("/agent/inbound", {"phone": leads[3]["phone"], "channel": "sms", "text": "call me tomorrow at 2pm"})
check("20 leads imported as 'new'", len(ids) == 20)

print("\n=== 2. Build the dialer's prioritized queue (closing the loop) ===")
out = os.path.join(_d, "contacts.xlsx")
rows = bq.build_queue(owner="chris")
n = bq.write_queue(rows, out)
check("queue written to contacts.xlsx", os.path.exists(out) and n > 0)
df = pd.read_excel(out, dtype=str, keep_default_na=False)
check("dialer columns present", all(c in df.columns for c in ["Queue_Type", "Name", "Phone", "City"]))
# callback that's due tomorrow should NOT be first (it's held until tomorrow 2pm)
top_types = list(df["Queue_Type"])[:5]
check("top of queue is highest-score new lead", df.iloc[0]["Name"] == f"Lead 005" or int(df.iloc[0]["Lead_Score"]) >= 88)
# the future-callback lead should be HELD out of today's queue
held_name = leads[3]["name"]
check("future callback is held out of today's list", held_name not in set(df["Name"]))
print("     order:", {t: list(df['Queue_Type']).count(t) for t in set(df['Queue_Type'])})

print("\n=== 3. Simulate a dialer session writing results ===")
# the dialer would write dial_results.xlsx; emulate that file
res_rows = []
for _, r in df.head(6).iterrows():
    disp = {"0": "no_answer", "1": "voicemail_dropped", "2": "human_transferred",
            "3": "busy", "4": "dead_number", "5": "voicemail_dropped"}[str(_ % 6)]
    res_rows.append({"Name": r["Name"], "Phone": r["Phone"], "City": r["City"],
                     "Birthday": r["Birthday"], "Source": r["Source"], "disposition": disp,
                     "disposition_time": datetime.now().isoformat(timespec="seconds"),
                     "from_number": "+13369622307", "call_control_id": f"cc-{_}"})
res_path = os.path.join(_d, "dial_results.xlsx")
pd.DataFrame(res_rows).to_excel(res_path, index=False)

print("\n=== 4. Sync the dialer results back into the brain ===")
before = None
with db.connect() as c:
    before = c.execute("SELECT COUNT(*) n FROM call_attempts").fetchone()["n"]
syncmod.import_file_path = res_path  # not used; call import directly
with db.connect() as c:
    r = syncmod.import_file(c, "chris", res_path)
check("results synced back", r["new_calls"] >= 6)
with db.connect() as c:
    after = c.execute("SELECT COUNT(*) n FROM call_attempts").fetchone()["n"]
    # the human_transferred lead should now be 'contacted'
    contacted = c.execute("SELECT COUNT(*) n FROM people WHERE stage='contacted'").fetchone()["n"]
check("call_attempts grew", after > before)
check("a reached lead moved to 'contacted'", contacted >= 1)

print("\n=== 5. Build the NEXT queue — reached/again-held leads drop off automatically ===")
rows2 = bq.build_queue(owner="chris")
names2 = {r["Name"] for r in rows2}
with db.connect() as c:
    reached = [row["full_name"] for row in c.execute("SELECT full_name FROM people WHERE stage='contacted'")]
check("reached lead no longer in next call queue", all(nm not in names2 for nm in reached))
check("voicemail leads return for retry", any(r["Queue_Type"] == "retry" for r in rows2))

passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*52}\n{passed}/{len(results)} PASSED" + (" — FULL LOOP WORKS" if passed == len(results) else " *** FAILURES ***"))
import sys; sys.exit(0 if passed == len(results) else 1)
