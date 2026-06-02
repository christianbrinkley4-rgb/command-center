"""Tests for command_center.number_lookup.

We stub HTTP calls so no Telnyx traffic happens. Verifies:
  * lookup_one parses the Telnyx response shape correctly
  * lookup_pending updates phone_numbers.line_type
  * 404 from Telnyx marks the phone deactivated
  * Daily spend cap stops the runner
  * Queue exclusion filter respects CC_LOOKUP_FILTER_ENABLED + line_type
"""
import os
import tempfile
from datetime import datetime

_tmp = os.path.join(tempfile.mkdtemp(), "test_lookup.db")
import db
db.DB_PATH = _tmp
_orig_connect = db.connect
db.connect = lambda *a, **k: _orig_connect(_tmp)
db.init_db(_tmp)

import agent_api
agent_api.db.connect = db.connect
agent_api._ensure_columns()

# oscr_ingestion_agent.ensure_schema also adds tables that number_lookup
# relies on (oscr_ingestion_state). Run it once.
import oscr_ingestion_agent
oscr_ingestion_agent.db.connect = db.connect
oscr_ingestion_agent.ensure_schema()

os.environ["TELNYX_API_KEY"] = "test-key"

import number_lookup
number_lookup.db.connect = db.connect
number_lookup._ensure_columns()


results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def now():
    return datetime.now().isoformat(timespec="seconds")


_PHONE_COUNTER = [0]


def _next_phone():
    _PHONE_COUNTER[0] += 1
    return f"+13367778{_PHONE_COUNTER[0]:03d}"


def seed_person_with_phone(name="Test Lookup", city="Greensboro", birthday="1962-03-04",
                           lead_score=85, source="T65_Dec_NC"):
    """Score defaults above the new filter floor (70) so the existing tests don't
    accidentally exercise the score gate. The dedicated filter section below
    seeds explicit mixes of scores."""
    phone = _next_phone()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, city, birthday, owner, stage, lead_score,
               source, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"k|{name}|{phone}", name, city, birthday, "chris", "new", lead_score, source, now(), now()))
        pid = cur.lastrowid
        conn.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                     (pid, phone, now()))
    return pid, phone


# ------------------------------------------------------------------ payload parsing

print("\n=== _parse_lookup_payload normalizes Telnyx shapes ===")
parsed_mobile = number_lookup._parse_lookup_payload({
    "data": {"phone_number": "+13365551234",
             "carrier": {"name": "AT&T Mobility", "type": "mobile"},
             "portability": {"line_type": "mobile", "portable": True, "ocn_name": "AT&T"}}
})
check("mobile parsed", parsed_mobile["line_type"] == "mobile")
check("carrier name captured", parsed_mobile["carrier"] in ("AT&T", "AT&T Mobility"))

parsed_landline = number_lookup._parse_lookup_payload({
    "data": {"phone_number": "+13365559999",
             "carrier": {"name": "Frontier", "type": "fixed_line"}}
})
check("fixed_line normalized to landline", parsed_landline["line_type"] == "landline")

parsed_voip = number_lookup._parse_lookup_payload({
    "data": {"phone_number": "+13365557777",
             "carrier": {"name": "Twilio", "type": "non_fixed_voip"}}
})
check("non_fixed_voip normalized to voip", parsed_voip["line_type"] == "voip")


# ------------------------------------------------------------------ lookup_pending happy path

print("\n=== lookup_pending updates phone_numbers + respects spend ===")

calls = []


def stub_lookup(phone):
    calls.append(phone)
    # Two mobiles, one landline, then 404 for the next call (disconnected).
    n = len(calls)
    if n == 1:
        return {"phone": phone, "line_type": "mobile", "carrier": "Verizon", "portable": True, "raw": {}}
    if n == 2:
        return {"phone": phone, "line_type": "landline", "carrier": "Frontier", "portable": False, "raw": {}}
    if n == 3:
        return {"phone": phone, "line_type": "voip", "carrier": "Twilio", "portable": False, "raw": {}}
    raise number_lookup.TelnyxError("http 404: not_found")


_orig_one = number_lookup.lookup_one
number_lookup.lookup_one = stub_lookup

# seed four people with phones
pid1, ph1 = seed_person_with_phone(name="Mobile Mary")
pid2, ph2 = seed_person_with_phone(name="Landline Larry")
pid3, ph3 = seed_person_with_phone(name="Voip Veronica")
pid4, ph4 = seed_person_with_phone(name="Dead Dan")

summary = number_lookup.lookup_pending(limit=10, sleep_between_seconds=0)
check("processed 4 phones", summary["processed"] == 4)
check("classified 3 (mobile + landline + voip)", summary["classified"] == 3)
check("deactivated 1 (the 404)", summary["deactivated"] == 1)
check("errors=0", summary["errors"] == 0)

with db.connect() as conn:
    rows = conn.execute(
        "SELECT e164, line_type, status FROM phone_numbers ORDER BY id"
    ).fetchall()
got = {r["e164"]: (r["line_type"], r["status"]) for r in rows}
check("mary tagged mobile", got[ph1][0] == "mobile")
check("larry tagged landline", got[ph2][0] == "landline")
check("veronica tagged voip", got[ph3][0] == "voip")
check("dan tagged deactivated", got[ph4][1] == "deactivated")

# Idempotency: a second pass with no pending rows is a no-op
summary2 = number_lookup.lookup_pending(limit=10, sleep_between_seconds=0)
check("second pass classifies nothing", summary2["classified"] == 0 and summary2["requested"] == 0)


# ------------------------------------------------------------------ budget cap

print("\n=== Daily spend cap stops the runner ===")

# Reset today's spend so the budget math is predictable; the earlier section
# already racked up spend that would otherwise immediately exceed our test cap.
with db.connect() as conn:
    conn.execute("DELETE FROM oscr_ingestion_state WHERE key LIKE 'lookup_spend_%'")

# Fresh phones so they're pending again
pid5, ph5 = seed_person_with_phone(name="Bigger Spend")
pid6, ph6 = seed_person_with_phone(name="Cap Hit")
pid7, ph7 = seed_person_with_phone(name="Cap Past")

# Set our stub to return mobile for each
def cheap_lookup(phone):
    return {"phone": phone, "line_type": "mobile", "carrier": "x", "portable": False, "raw": {}}
number_lookup.lookup_one = cheap_lookup

# Override budget so two lookups fit and the third would push over.
budget = number_lookup.LOOKUP_COST_USD * 2 + 0.0001
summary_budget = number_lookup.lookup_pending(limit=10, max_spend_usd=budget,
                                              sleep_between_seconds=0)
check("budget stopped runner at 2 calls", summary_budget["processed"] == 2)
check("stopped_for_budget reported", summary_budget["stopped_for_budget"] is True)


# ------------------------------------------------------------------ queue exclusion

print("\n=== queue exclusion respects line_type ===")
# Stage these people so they show up in the ranked queue:
with db.connect() as conn:
    conn.execute("UPDATE people SET stage='new' WHERE id IN (?, ?, ?)", (pid1, pid2, pid4))

import app as appmod
appmod.db.connect = db.connect

# Without the filter flag, landline + deactivated should still appear...
os.environ["CC_LOOKUP_FILTER_ENABLED"] = "0"
queue_off = appmod._build_ranked_queue(owner="chris", limit=100)
pids_off = {r.get("person_id") for r in queue_off}
check("flag OFF: landline still in queue", pid2 in pids_off)
# (deactivated phones are excluded by phone_of() regardless of the flag, because
#  the status<>'bad' filter is on the phone select; deactivated is not 'bad'.
#  We don't assert on pid4 here either way.)

# With flag ON, landline and deactivated drop out.
os.environ["CC_LOOKUP_FILTER_ENABLED"] = "1"
queue_on = appmod._build_ranked_queue(owner="chris", limit=100)
pids_on = {r.get("person_id") for r in queue_on}
check("flag ON: mobile still in queue", pid1 in pids_on)
check("flag ON: landline removed", pid2 not in pids_on)
check("flag ON: deactivated removed", pid4 not in pids_on)

# Restore
number_lookup.lookup_one = _orig_one
os.environ.pop("CC_LOOKUP_FILTER_ENABLED", None)

print("\n=== Filters target the right leads (save money) ===")

# Reset so we can seed cleanly. FK pragma is on for this DB, so drop child
# rows first OR just disable the constraint for this maintenance section.
with db.connect() as conn:
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM scheduled_tasks")
    conn.execute("DELETE FROM call_attempts")
    conn.execute("DELETE FROM suppressions")
    conn.execute("DELETE FROM person_notes")
    conn.execute("DELETE FROM phone_numbers")
    conn.execute("DELETE FROM people")
    conn.execute("DELETE FROM oscr_ingestion_state WHERE key LIKE 'lookup_spend_%'")
    conn.execute("PRAGMA foreign_keys=ON")

def seed_for_filter(name, score, source, stage, suppress=False):
    phone = _next_phone()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, city, birthday, owner, stage,
               lead_score, source, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"k|{name}|{phone}", name, "Greensboro", "1962-03-04", "chris", stage,
             score, source, now(), now()))
        pid = cur.lastrowid
        conn.execute("INSERT INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                     (pid, phone, now()))
        if suppress:
            conn.execute("INSERT INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?, 'manual', ?)",
                         (phone, now()))
    return pid, phone

p_t65_high, ph_t65_high = seed_for_filter("T65 High", 85, "T65_Dec_NC", "new")
p_t65_low, ph_t65_low   = seed_for_filter("T65 Low",  45, "T65_Dec_NC", "new")
p_oscr_hi, ph_oscr_hi   = seed_for_filter("Oscar Hi", 90, "OSCR",       "new")
p_dialer,  ph_dialer    = seed_for_filter("Dialer Old", 88, "Dialer_Existing", "new")
p_t65_dnc, ph_t65_dnc   = seed_for_filter("T65 DNC", 90, "T65_Dec_NC", "dnc")
p_t65_closed, ph_t65_cl = seed_for_filter("T65 Closed", 90, "T65_Dec_NC", "closed")
p_t65_sup, ph_t65_sup   = seed_for_filter("T65 Suppressed Phone", 90, "T65_Dec_NC", "new", suppress=True)

# Defaults: T65 + min_score=70 + dialable stages + not suppressed
with db.connect() as conn:
    default_phones = set(number_lookup._pending_phones(conn, limit=100))
check("default picks the high-score T65 lead", ph_t65_high in default_phones)
check("default drops the low-score T65 lead", ph_t65_low not in default_phones)
check("default drops OSCR (different source pattern)", ph_oscr_hi not in default_phones)
check("default drops Dialer_Existing (not a T65 source)", ph_dialer not in default_phones)
check("default drops the DNC-staged lead", ph_t65_dnc not in default_phones)
check("default drops the closed-staged lead", ph_t65_cl not in default_phones)
check("default drops the suppressed phone", ph_t65_sup not in default_phones)

# Caller can opt-in to OSCR
with db.connect() as conn:
    with_oscr = set(number_lookup._pending_phones(
        conn, limit=100, source_patterns=["T65_", "OSCR"]))
check("opt-in OSCR pattern picks the OSCR lead", ph_oscr_hi in with_oscr)
check("opt-in OSCR still excludes Dialer_Existing", ph_dialer not in with_oscr)

# Caller can lower the score floor
with db.connect() as conn:
    no_floor = set(number_lookup._pending_phones(conn, limit=100, min_score=0))
check("min_score=0 picks up the low-score T65 lead", ph_t65_low in no_floor)

# Endpoint accepts the same filters via JSON body and reports them back
import json as _json
import app as appmod
appmod.db.connect = db.connect
client = appmod.app.test_client()

# Re-stub lookup_one so a live call attempt would no-op
number_lookup.lookup_one = lambda phone: {"phone": phone, "line_type": "mobile", "carrier": "Verizon", "portable": False, "raw": {}}
# Reset spend so the runner doesn't refuse to start due to earlier sections
with db.connect() as conn:
    conn.execute("DELETE FROM oscr_ingestion_state WHERE key LIKE 'lookup_spend_%'")

r = client.post("/agent/admin/number-lookup",
                data=_json.dumps({"limit": 5, "min_score": 80, "source_patterns": ["T65_"]}),
                content_type="application/json")
check("admin/number-lookup returns 200", r.status_code == 200)
j = r.get_json()
check("response echoes the filters",
      j["filters"]["min_score"] == 80 and j["filters"]["source_patterns"] == ["T65_"])
check("endpoint targets at least the high T65 lead", j["processed"] >= 1)


print("\n=== never_called + queue_only filters target priority dial pool only ===")

# Fresh seed: one never-called + score-high + in-territory + T65, vs others
# that fail various filters.
with db.connect() as conn:
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM scheduled_tasks")
    conn.execute("DELETE FROM call_attempts")
    conn.execute("DELETE FROM suppressions")
    conn.execute("DELETE FROM person_notes")
    conn.execute("DELETE FROM phone_numbers")
    conn.execute("DELETE FROM people")
    conn.execute("DELETE FROM oscr_ingestion_state WHERE key LIKE 'lookup_spend_%'")
    conn.execute("PRAGMA foreign_keys=ON")

# (a) The ideal target
ideal_pid, ideal_ph = seed_person_with_phone(name="Ideal Priority", city="Greensboro",
                                              birthday="1962-03-04", lead_score=85,
                                              source="T65_Dec_NC")
# (b) Already-called T65 (should be EXCLUDED by never_called=True)
called_pid, called_ph = seed_person_with_phone(name="Already Called", city="Greensboro",
                                                birthday="1962-03-04", lead_score=85,
                                                source="T65_Dec_NC")
with db.connect() as conn:
    conn.execute(
        """INSERT INTO call_attempts(person_id, phone, agent, dialed_at, disposition,
           disposition_category, dedupe_key) VALUES (?,?,?,?,?,?,?)""",
        (called_pid, called_ph, "chris", now(), "no_answer", "no_answer", f"seed|{called_pid}"))
# (c) Out-of-territory T65 (should be EXCLUDED by queue_only=True / geo)
oot_pid, oot_ph = seed_person_with_phone(name="Out of Territory", city="Ararat",
                                          birthday="1962-03-04", lead_score=85,
                                          source="T65_Dec_VA")
# (d) Wrong birth year (target_lead check)
wrong_bday_pid, wrong_bday_ph = seed_person_with_phone(name="Wrong Year",
                                                       city="Greensboro",
                                                       birthday="1950-03-04",
                                                       lead_score=85,
                                                       source="T65_Dec_NC")
# (e) Low score T65 (should be EXCLUDED by min_score)
lowscore_pid, lowscore_ph = seed_person_with_phone(name="Low Score", city="Greensboro",
                                                    birthday="1962-03-04", lead_score=40,
                                                    source="T65_Dec_NC")

with db.connect() as conn:
    defaults = set(number_lookup._pending_phones(conn, limit=100))
check("ideal target included", ideal_ph in defaults)
check("already-called excluded", called_ph not in defaults)
check("out-of-territory excluded by queue_only", oot_ph not in defaults)
check("wrong birth-year excluded by queue_only", wrong_bday_ph not in defaults)
check("low score excluded by min_score", lowscore_ph not in defaults)

# Override never_called=False — the already-called lead reappears
with db.connect() as conn:
    no_nc = set(number_lookup._pending_phones(conn, limit=100, never_called=False))
check("never_called=False brings the already-called lead back", called_ph in no_nc)

# Override queue_only=False — the OOT lead reappears
with db.connect() as conn:
    no_qo = set(number_lookup._pending_phones(conn, limit=100, queue_only=False))
check("queue_only=False brings the OOT lead back", oot_ph in no_qo)

# Endpoint echoes the new filters
import json as _json
r = client.post("/agent/admin/number-lookup",
                data=_json.dumps({"limit": 5, "never_called": True, "queue_only": True}),
                content_type="application/json")
j = r.get_json()
check("endpoint echoes never_called", j["filters"].get("never_called") is True)
check("endpoint echoes queue_only", j["filters"].get("queue_only") is True)


passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys
sys.exit(0 if passed == len(results) else 1)
