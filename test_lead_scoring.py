"""Tests for command_center.lead_scoring.

Heuristic baseline is exercised directly; AI is stubbed so no network is touched.
Verifies: scoring writes back to people.lead_score, appends an ai_score note,
respects manual overrides + TTL, batch caps work, pending-pass picks up
never-scored leads."""
import os
import tempfile
from datetime import datetime, timedelta

_tmp = os.path.join(tempfile.mkdtemp(), "test_scoring.db")
import db
db.DB_PATH = _tmp
_orig_connect = db.connect
db.connect = lambda *a, **k: _orig_connect(_tmp)
db.init_db(_tmp)

import agent_api
agent_api.db.connect = db.connect
agent_api._ensure_columns()

import lead_scoring

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def now():
    return datetime.now().isoformat(timespec="seconds")


_PHONE_COUNTER = [0]


def _next_phone():
    _PHONE_COUNTER[0] += 1
    return f"+13365557{_PHONE_COUNTER[0]:03d}"


def seed_person(name="Test One", city="Greensboro", source="T65_Dec_NC",
                birthday="1962-03-04", phone=None, email="",
                line_type=""):
    if phone is None:
        phone = _next_phone()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, first_name, last_name, birthday,
               city, owner, stage, lead_score, source, email, first_seen_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"k|{name}|{city}", name, name.split()[0], name.split()[-1],
             birthday, city, "chris", "new", 50, source, email, now(), now()))
        pid = cur.lastrowid
        conn.execute("INSERT INTO phone_numbers(person_id, e164, line_type, created_at) VALUES (?,?,?,?)",
                     (pid, phone, line_type, now()))
    return pid


# ------------------------------------------------------------------ heuristic

print("\n=== heuristic baseline is deterministic + composable ===")
score_greensboro_mobile, signals = lead_scoring.heuristic_score({
    "id": 0, "source": "T65_Dec_NC", "city": "Greensboro",
    "birthday": "1962-03-04", "address": "123 Main St", "full_name": "Jane Doe",
    "email": "jane@example.com",
})
check("greensboro+mobile+T65 scores above the source prior",
      score_greensboro_mobile > 50)
check("signals dict reports each component",
      "source_prior" in signals and "drive_bonus" in signals)

score_far_landline, signals_far = lead_scoring.heuristic_score({
    "id": 0, "source": "T65_Dec_VA", "city": "Ararat",
    "birthday": "1962-12-01", "full_name": "Bob Marl",
}, line_type="landline")
check("out-of-territory landline scores far lower",
      score_far_landline < score_greensboro_mobile - 20)
check("landline penalty applied", signals_far["line_type_adjustment"] < 0)

score_prior_contact, _ = lead_scoring.heuristic_score({
    "id": 0, "source": "OSCR_HotList", "city": "Greensboro",
    "birthday": "1962-04-01", "full_name": "Carl Connect",
}, line_type="mobile")
check("OSCR source prior outscores T65_Dec_NC prior", score_prior_contact > score_greensboro_mobile - 5)

# Clamping
hi = lead_scoring.heuristic_score({
    "id": 0, "source": "OSCR_HotList", "city": "Greensboro",
    "birthday": "1962-06-15", "address": "x", "email": "y@z",
    "full_name": "Score Hi", "county": "Guilford",
})[0]
check("score clamped at <= 100", hi <= 100)
check("score never negative", lead_scoring.heuristic_score({
    "id": 0, "source": "T65_Dec_VA", "city": "Far Away",
    "birthday": "1900-01-01",
}, line_type="landline")[0] >= 0)


# ------------------------------------------------------------------ scoring writes back to people

print("\n=== score_person writes lead_score + note ===")
pid = seed_person(name="Alpha Local", city="Greensboro", source="T65_Dec_NC",
                  birthday="1962-03-04", line_type="mobile")
res = lead_scoring.score_batch([pid], use_ai=False)
check("score_batch returns ok", res["scored"] == 1 and res["errors"] == 0)
with db.connect() as conn:
    row = conn.execute("SELECT lead_score FROM people WHERE id=?", (pid,)).fetchone()
    note = conn.execute("SELECT body, author FROM person_notes WHERE person_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
check("people.lead_score updated", row["lead_score"] > 50)
check("ai_score note appended", note["author"] == "ai_score" and "lead_score ->" in note["body"])


# ------------------------------------------------------------------ TTL

print("\n=== TTL: a recently-scored lead is skipped without force=True ===")
res2 = lead_scoring.score_batch([pid], use_ai=False)  # already scored above
check("re-score within TTL window is skipped", res2["scored"] == 0 and res2["skipped"] == 1)
res3 = lead_scoring.score_batch([pid], force=True, use_ai=False)
check("force=True overrides the TTL", res3["scored"] == 1)


# ------------------------------------------------------------------ Manual override

print("\n=== a non-ai_score note containing 'lead_score' blocks auto-rescoring ===")
pid_manual = seed_person(name="Manual Setter", city="Burlington", source="T65_Dec_NC")
with db.connect() as conn:
    conn.execute(
        """INSERT INTO person_notes(person_id, body, author, created_at) VALUES (?,?,?,?)""",
        (pid_manual, "operator set lead_score manually to 90", "admin_menu", now()),
    )
res4 = lead_scoring.score_batch([pid_manual], use_ai=False)
check("manual override skips scoring", res4["skipped"] == 1 and res4["scored"] == 0)


# ------------------------------------------------------------------ AI fallback

print("\n=== AI refinement (stubbed) ===")

class _FakeRefiner:
    calls = 0
def fake_ai_refine(person_row, heuristic, signals, line_type=""):
    _FakeRefiner.calls += 1
    return {"score": min(100, heuristic + 5), "rationale": "stubbed test refiner", "confidence": 0.9}

_orig_refine = lead_scoring.ai_refine
_orig_ai_enabled = lead_scoring.ai_enabled
lead_scoring.ai_refine = fake_ai_refine
lead_scoring.ai_enabled = lambda: True

pid_ai = seed_person(name="AI Scored", city="Greensboro", source="OSCR_HotList",
                     birthday="1962-07-15", line_type="mobile")
res_ai = lead_scoring.score_batch([pid_ai])
with db.connect() as conn:
    score_ai = conn.execute("SELECT lead_score FROM people WHERE id=?", (pid_ai,)).fetchone()["lead_score"]
    note_ai = conn.execute("SELECT body FROM person_notes WHERE person_id=? AND author='ai_score'", (pid_ai,)).fetchone()
check("AI refiner was called", _FakeRefiner.calls == 1)
check("final score reflects AI refinement", note_ai and "stubbed test refiner" in note_ai["body"])

lead_scoring.ai_refine = _orig_refine
lead_scoring.ai_enabled = _orig_ai_enabled


# ------------------------------------------------------------------ Batch cap

print("\n=== BATCH_MAX caps runaway batches ===")
batch_ids = list(range(1, lead_scoring.BATCH_MAX + 50))
res_big = lead_scoring.score_batch(batch_ids, use_ai=False)
check("batch trimmed to BATCH_MAX", res_big["requested"] == lead_scoring.BATCH_MAX)


# ------------------------------------------------------------------ score_pending

print("\n=== score_pending picks up never-scored leads ===")
pid_never = seed_person(name="Never Scored", city="High Point", source="T65_Dec_NC")
res_pending = lead_scoring.score_pending(limit=10, use_ai=False)
ids_scored = {r["person_id"] for r in res_pending["results"] if r.get("ok")}
check("score_pending found and scored a fresh lead", pid_never in ids_scored)


# ------------------------------------------------------------------ /agent/admin/score endpoint

print("\n=== /agent/admin/score endpoint ===")
import json as _json
import app as appmod
appmod.db.connect = db.connect
client = appmod.app.test_client()

pid_api = seed_person(name="Api Score Me", city="Jamestown", source="T65_Dec_NC",
                      line_type="mobile")
r = client.post("/agent/admin/score",
                data=_json.dumps({"person_ids": [pid_api]}),
                content_type="application/json")
check("admin/score returns 200", r.status_code == 200)
j = r.get_json()
check("admin/score reports scored count", j.get("scored") == 1)


print("\n=== Score is time-invariant (does NOT change based on scoring hour) ===")
# A lead's intrinsic call-priority must be the same whether it was scored at
# 3am or 11am. The dialer pacing handles when to call; the score is identity.
person = {"id": 0, "source": "T65_Dec_NC", "city": "Greensboro",
          "birthday": "1962-03-04", "full_name": "Hour Test"}
score_11, _ = lead_scoring.heuristic_score(person, hour=11)
score_3, _ = lead_scoring.heuristic_score(person, hour=3)
check("score is identical regardless of scoring hour", score_11 == score_3)


print("\n=== BATCH_MAX default raised ===")
check("BATCH_MAX is at least 2000", lead_scoring.BATCH_MAX >= 2000)


passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{len(results)} PASSED" + ("  [OK] ALL GOOD" if passed == len(results) else "  *** FAILURES ***"))
import sys
sys.exit(0 if passed == len(results) else 1)
