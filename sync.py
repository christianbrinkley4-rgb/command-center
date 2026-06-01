"""
Command Center — sync engine.

Reads each dialer's dial_results.xlsx and folds every call into the database
without touching the dialers. Idempotent: re-running only adds new calls. This is
the bridge that lets the dashboard show a live, unified picture across all agents.
"""
import os
from datetime import datetime

import pandas as pd

import db
import queue_policy

# Each dialer's live results file + which agent it belongs to.
# Add a line here when a new agent comes online — that's the whole integration.
SOURCES = [
    ("chris", r"C:\dialer\MyDialer_TODAY_READY\MyDialer\dial_results.xlsx"),
    ("will",  r"C:\Users\chris\OneDrive\Documents\Dialer\Will_Dialer\dial_results.xlsx"),
    ("will",  r"C:\Users\chris\OneDrive\Documents\Dialer\Will_Dialer_SEND_TO_WILL\dial_results.xlsx"),
]

# Stage ranking: a person's stage is their most-advanced outcome so far.
STAGE_RANK = {"new": 0, "attempted": 1, "voicemail": 2, "contacted": 3, "callback": 4, "appointment": 5, "dnc": 6}
CATEGORY_TO_STAGE = {
    "no_answer": "attempted", "bad_number": "attempted", "dropped": "attempted",
    "voicemail": "voicemail",
    "live_conversation": "contacted", "not_interested": "contacted",
    "callback": "callback", "appointment": "appointment", "dnc": "dnc",
}


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _upsert_person(conn, row):
    name = _clean(row.get("Name"))
    city = _clean(row.get("City"))
    birthday = _clean(row.get("Birthday"))
    phone = db.normalize_phone(row.get("Phone"))
    key = db.person_key(name, city, birthday) or (f"phone|{phone}" if phone else "")
    if not key:
        return None

    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute("SELECT id FROM people WHERE person_key=?", (key,))
    found = cur.fetchone()
    parts = name.split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    source = _clean(row.get("Source"))

    if found:
        pid = found["id"]
        conn.execute(
            """UPDATE people SET full_name=COALESCE(NULLIF(?,''), full_name),
               city=COALESCE(NULLIF(?,''), city), county=COALESCE(NULLIF(?,''), county),
               address=COALESCE(NULLIF(?,''), address), birthday=COALESCE(NULLIF(?,''), birthday),
               source=COALESCE(NULLIF(?,''), source)
               WHERE id=?""",
            (name, city, _clean(row.get("County")), _clean(row.get("Address")), birthday, source, pid),
        )
    else:
        cur = conn.execute(
            """INSERT INTO people(person_key, full_name, first_name, last_name, birthday, city,
               county, address, source, first_seen_at, last_activity_at, stage)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new')""",
            (key, name, first, last, birthday, city, _clean(row.get("County")),
             _clean(row.get("Address")), source, now, now),
        )
        pid = cur.lastrowid
        if source:
            conn.execute(
                "INSERT OR IGNORE INTO lead_sources(name, first_seen_at) VALUES (?,?)",
                (source, now),
            )

    if phone:
        conn.execute(
            "INSERT OR IGNORE INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
            (pid, phone, now),
        )
    return pid


def _insert_call(conn, pid, agent, row):
    phone = db.normalize_phone(row.get("Phone"))
    disposition = _clean(row.get("disposition")).lower()
    dialed_at = _clean(row.get("disposition_time"))
    ccid = _clean(row.get("call_control_id"))
    dedupe_key = ccid or f"{phone}|{dialed_at}|{disposition}"
    category = db.disposition_category(disposition)

    try:
        conn.execute(
            """INSERT INTO call_attempts(person_id, phone, agent, from_number, dialed_at, disposition,
               disposition_category, amd_result, answer_to_agent_seconds, live_talk_seconds,
               total_call_seconds, hangup_initiator, call_control_id, dedupe_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, phone, agent, _clean(row.get("from_number")), dialed_at, disposition, category,
             _clean(row.get("amd_result")), _f(row.get("answer_to_agent_seconds")),
             _f(row.get("live_talk_seconds")), _f(row.get("total_call_seconds")),
             _clean(row.get("hangup_initiator")), ccid or None, dedupe_key),
        )
        return category, dialed_at
    except Exception:
        return None, None  # duplicate (already imported) — idempotent no-op


def _apply_disposition_side_effects(conn, pid, row):
    """Keep cloud queue eligibility aligned with the dialer's local resume rules."""
    phone = db.normalize_phone(row.get("Phone"))
    disposition = _clean(row.get("disposition")).lower()
    category = db.disposition_category(disposition)
    now = _clean(row.get("disposition_time")) or datetime.now().isoformat(timespec="seconds")

    if phone and (category == "bad_number" or disposition in queue_policy.TERMINAL_PHONE_DISPOSITIONS):
        conn.execute("UPDATE phone_numbers SET status='bad' WHERE e164=?", (phone,))
        if disposition == "spam_blocked":
            conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                         (phone, "spam_blocked", now))

    if phone and queue_policy.attempt_count(conn, pid, phone) >= queue_policy.max_attempts():
        conn.execute("UPDATE phone_numbers SET status='bad' WHERE e164=?", (phone,))
        conn.execute("INSERT INTO person_notes(person_id, body, author, created_at) "
                     "SELECT ?, ?, 'automation', ? WHERE NOT EXISTS ("
                     "SELECT 1 FROM person_notes WHERE person_id=? AND body LIKE 'Phone reached max call attempts:%')",
                     (pid, f"Phone reached max call attempts: {phone}", now, pid))

    if category == "dnc":
        conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('person',?,?,?)",
                     (str(pid), disposition or "dnc", now))
        for r in conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (pid,)):
            conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                         (r["e164"], disposition or "dnc", now))


def _refresh_person_rollup(conn, pid):
    """Recompute a person's stage, owner, last activity, and a simple lead score
    from all their calls."""
    rows = conn.execute(
        "SELECT disposition_category, dialed_at, agent FROM call_attempts WHERE person_id=? ORDER BY dialed_at",
        (pid,),
    ).fetchall()
    if not rows:
        return

    best_stage, best_rank = "attempted", 1
    last_activity, owner = "", ""
    attempts = len(rows)
    for r in rows:
        stage = CATEGORY_TO_STAGE.get(r["disposition_category"], "attempted")
        if STAGE_RANK.get(stage, 0) >= best_rank:
            best_rank, best_stage = STAGE_RANK.get(stage, 0), stage
        if r["dialed_at"] >= last_activity:
            last_activity, owner = r["dialed_at"], r["agent"]

    appt = conn.execute("SELECT 1 FROM appointments WHERE person_id=? LIMIT 1", (pid,)).fetchone()
    if appt:
        best_stage = "appointment"

    # Simple priority score: stage weight minus attrition for repeated tries.
    score = {"appointment": 100, "callback": 90, "contacted": 70, "voicemail": 45,
             "attempted": 50, "dnc": 0}.get(best_stage, 40) - min(attempts, 6) * 4
    conn.execute(
        "UPDATE people SET stage=?, owner=?, last_activity_at=?, lead_score=? WHERE id=?",
        (best_stage, owner, last_activity, max(score, 0), pid),
    )


def import_file(conn, agent, path):
    if not path or not os.path.exists(path):
        return {"file": path, "exists": False, "new_calls": 0}
    df = pd.read_excel(path, dtype=str, keep_default_na=False)
    new_calls = 0
    touched = set()
    for _, row in df.iterrows():
        pid = _upsert_person(conn, row)
        if not pid:
            continue
        category, _ = _insert_call(conn, pid, agent, row)
        _apply_disposition_side_effects(conn, pid, row)
        if category is not None:
            new_calls += 1
        touched.add(pid)
    for pid in touched:
        _refresh_person_rollup(conn, pid)
    return {"file": path, "exists": True, "new_calls": new_calls, "people_touched": len(touched)}


def _resolve_sources():
    """On the droplet, CC_DATA_DIR holds files uploaded by the laptop
    (chris.xlsx, will.xlsx, ...). Locally, fall back to the dialers' real paths."""
    data_dir = os.getenv("CC_DATA_DIR", "")
    if data_dir and os.path.isdir(data_dir):
        found = []
        for fname in sorted(os.listdir(data_dir)):
            if fname.lower().endswith(".xlsx"):
                found.append((os.path.splitext(fname)[0], os.path.join(data_dir, fname)))
        if found:
            return found
    return SOURCES


def sync_all(sources=None):
    db.init_db()
    results = []
    with db.connect() as conn:
        for agent, path in (sources or _resolve_sources()):
            results.append(import_file(conn, agent, path))
        # keep lead_sources counts fresh
        conn.execute(
            """UPDATE lead_sources SET lead_count =
               (SELECT COUNT(*) FROM people WHERE people.source = lead_sources.name)"""
        )
    return results


if __name__ == "__main__":
    for r in sync_all():
        if r["exists"]:
            print(f"  {r['file']}: +{r['new_calls']} new calls, {r.get('people_touched',0)} people")
        else:
            print(f"  (skipped, not found) {r['file']}")
    print("Sync complete.")
