"""
Command Center -> Dialer queue builder (closes the loop).

The Command Center is the brain; the dialer is the phone. This script pulls the
PRIORITIZED call list out of the brain and writes it to the dialer's contacts.xlsx,
so when new OSCR/T65 leads arrive (or callbacks come due) they automatically get
dialed in the right order — no manual list-building.

Call order (highest priority first):
  1) Scheduled calls that are DUE   (callbacks the prospect asked for, redials)
  2) Callbacks-stage leads          (warm)
  3) New leads                      (never called) — by lead_score
  4) Retry leads                    (no-answer/voicemail) — by lead_score, oldest first
Excludes: DNC, suppressed numbers, already-reached people, and anyone with a
scheduled call in the FUTURE (so we don't call before the requested time).

Per-agent: pass --owner chris|will to build that agent's queue into their dialer.

Usage:
    python build_dialer_queue.py --owner chris --out "C:\\dialer\\MyDialer_TODAY_READY\\MyDialer\\contacts.xlsx"
    python build_dialer_queue.py --owner will  --out "...\\Will_Dialer\\contacts.xlsx"

Run it before a dial session (or on a timer). It is read-only on the database and
writes the queue atomically so it's safe to run anytime.
"""
import argparse
import os
from datetime import datetime

import pandas as pd

import db

OUT_COLUMNS = ["Queue_Type", "Name", "Phone", "Address", "City", "County", "Birthday",
               "Last_Disposition", "Last_Call_Time", "Source", "Lead_Score", "person_id"]


def _is_suppressed_phone(conn, e164):
    return conn.execute("SELECT 1 FROM suppressions WHERE scope='phone' AND value=? LIMIT 1", (e164,)).fetchone() is not None


def build_queue(owner="", limit=2000, did_cap=0):
    """Return a list of ordered contact dicts (highest priority first)."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    own_sql = " AND p.owner=?" if owner else ""
    own_p = [owner] if owner else []
    rows = []
    seen = set()

    with db.connect() as conn:
        def phones(pid):
            return [r["e164"] for r in conn.execute(
                "SELECT e164 FROM phone_numbers WHERE person_id=? AND status<>'bad' ORDER BY status='active' DESC", (pid,))]

        def add(person, qtype):
            pid = person["id"]
            if pid in seen:
                return
            ph = phones(pid)
            ph = [x for x in ph if not _is_suppressed_phone(conn, x)]
            if not ph:
                return
            seen.add(pid)
            rows.append({
                "Queue_Type": qtype, "Name": person["full_name"] or "", "Phone": ph[0],
                "Address": person["address"] or "", "City": person["city"] or "",
                "County": person["county"] or "", "Birthday": person["birthday"] or "",
                "Last_Disposition": person["last_outcome"] or "", "Last_Call_Time": person["last_activity_at"] or "",
                "Source": person["source"] or "", "Lead_Score": person["lead_score"], "person_id": pid,
            })

        base = f"""
            SELECT p.id, p.full_name, p.address, p.city, p.county, p.birthday, p.source,
                   p.lead_score, p.stage, p.last_activity_at, p.owner,
                   (SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id
                    ORDER BY dialed_at DESC LIMIT 1) last_outcome
            FROM people p WHERE p.stage<>'dnc'{own_sql} """

        # 1) Scheduled calls that are DUE now (callbacks they asked for, redials)
        due = conn.execute(f"""
            SELECT DISTINCT s.person_id FROM scheduled_tasks s
            WHERE s.kind='call' AND s.status='pending' AND s.due_at<=?""", (now_iso,)).fetchall()
        due_ids = {r["person_id"] for r in due}
        # people who have a FUTURE scheduled call should be held until then
        future = conn.execute(f"""
            SELECT DISTINCT s.person_id FROM scheduled_tasks s
            WHERE s.kind='call' AND s.status='pending' AND s.due_at>?""", (now_iso,)).fetchall()
        hold_ids = {r["person_id"] for r in future} - due_ids

        for r in conn.execute(base + " AND p.id IN ({}) ORDER BY p.lead_score DESC".format(
                ",".join("?" * len(due_ids)) or "NULL"), own_p + list(due_ids)):
            add(dict(r), "scheduled_call")

        # 2) Callback-stage (warm) — not held for a future time
        for r in conn.execute(base + " AND p.stage='callback' ORDER BY p.last_activity_at ASC", own_p):
            if r["id"] not in hold_ids:
                add(dict(r), "callback")

        # 3) New leads (never called) by score
        for r in conn.execute(base + " AND p.stage='new' ORDER BY p.lead_score DESC, p.first_seen_at ASC", own_p):
            if r["id"] not in hold_ids:
                add(dict(r), "never_called")

        # 4) Retry leads (no-answer / voicemail) by score, oldest activity first
        for r in conn.execute(base + " AND p.stage IN ('attempted','voicemail') ORDER BY p.lead_score DESC, p.last_activity_at ASC", own_p):
            if r["id"] not in hold_ids:
                add(dict(r), "retry")

    if did_cap and did_cap > 0:
        rows = rows[:did_cap]
    return rows[:limit]


def write_queue(rows, out_path):
    """Atomically write the queue to the dialer's contacts.xlsx."""
    df = pd.DataFrame(rows, columns=OUT_COLUMNS)
    folder = os.path.dirname(os.path.abspath(out_path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = out_path + ".tmp.xlsx"
    df.to_excel(tmp, index=False, engine="openpyxl")
    os.replace(tmp, out_path)
    return len(df)


def main():
    ap = argparse.ArgumentParser(description="Build the dialer's prioritized call queue from Command Center.")
    ap.add_argument("--owner", default="", help="chris or will (blank = all)")
    ap.add_argument("--out", required=True, help="path to the dialer's contacts.xlsx")
    ap.add_argument("--cap", type=int, default=0, help="optional max rows (e.g. daily DID cap)")
    args = ap.parse_args()
    rows = build_queue(owner=args.owner, did_cap=args.cap)
    n = write_queue(rows, args.out)
    by_type = {}
    for r in rows:
        by_type[r["Queue_Type"]] = by_type.get(r["Queue_Type"], 0) + 1
    print(f"Wrote {n} contacts -> {args.out}")
    print("  order:", by_type)


if __name__ == "__main__":
    main()
