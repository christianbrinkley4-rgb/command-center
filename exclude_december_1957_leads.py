"""
Close December 1957 cohort leads in the Command Center database.

These are the OSCR December birthday bulk lists (birth year 1957, T65_Dec_NC /
T65_Dec_VA). Closing them keeps them out of queue rebuilds even if contacts.xlsx
was exported earlier.

Run from command_center:
    python exclude_december_1957_leads.py
    python exclude_december_1957_leads.py --dry-run
"""
import argparse
from datetime import datetime

import db
import queue_source_policy


def main():
    ap = argparse.ArgumentParser(description="Close December 1957 leads in the database.")
    ap.add_argument("--dry-run", action="store_true", help="Count only; do not update.")
    args = ap.parse_args()

    closed = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, full_name, source, birthday, stage FROM people "
            "WHERE stage NOT IN ('dnc', 'closed')"
        ).fetchall()
        for row in rows:
            if not queue_source_policy.is_december_1957_cohort(row["source"], row["birthday"]):
                continue
            closed += 1
            if args.dry_run:
                continue
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE people SET stage='closed', last_activity_at=? WHERE id=?",
                (now, row["id"]),
            )
            conn.execute(
                "INSERT INTO person_notes(person_id, body, author, created_at) "
                "SELECT ?, ?, 'automation', ? WHERE NOT EXISTS ("
                "SELECT 1 FROM person_notes WHERE person_id=? AND body LIKE ?)",
                (
                    row["id"],
                    "Excluded from dialer queue: December 1957 cohort removed per policy.",
                    now,
                    row["id"],
                    "Excluded from dialer queue: December 1957%",
                ),
            )
        if not args.dry_run:
            conn.commit()

    label = "Would close" if args.dry_run else "Closed"
    print(f"{label} {closed} December 1957 lead(s).")


if __name__ == "__main__":
    main()
