"""
Command Center — Learning loop (gets smarter over time).

Continuously measures what actually works and feeds it back into the system:
  - Which lead SOURCES convert to live conversations / appointments (so good sources
    get scored higher automatically).
  - Which HOURS and WEEKDAYS produce the most live answers (so the call queue and the
    automation engine schedule calls when people actually pick up).
  - Which outreach CADENCE steps earn replies.

Results are cached in a small `learning_stats` table and exposed via /api/insights.
The scoring + automation modules read these to self-tune. Pure SQL aggregation over the
data you already collect — no external services, improves as call volume grows.
"""
from datetime import datetime

import db

STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_stats (
    key        TEXT PRIMARY KEY,
    value      REAL,
    detail     TEXT,
    updated_at TEXT
);
"""


def ensure_schema():
    with db.connect() as conn:
        conn.executescript(STATS_SCHEMA)


def _set(conn, key, value, detail=""):
    conn.execute(
        "INSERT INTO learning_stats(key, value, detail, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, detail=excluded.detail, updated_at=excluded.updated_at",
        (key, float(value or 0), detail, datetime.now().isoformat(timespec="seconds")))


def refresh_stats():
    """Recompute all learning metrics. Cheap; safe to run every minute."""
    ensure_schema()
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM call_attempts").fetchone()["c"]
        if not total:
            return

        # 1) Best hour to call (by live-conversation rate, min volume guard)
        best_hour = conn.execute("""
            SELECT CAST(substr(dialed_at,12,2) AS INT) hh,
                   SUM(CASE WHEN disposition_category='live_conversation' THEN 1 ELSE 0 END)*1.0/COUNT(*) rate,
                   COUNT(*) n
            FROM call_attempts WHERE length(dialed_at)>=13
            GROUP BY hh HAVING n>=10 ORDER BY rate DESC LIMIT 1""").fetchone()
        if best_hour:
            _set(conn, "best_call_hour", best_hour["hh"], f"{best_hour['rate']*100:.0f}% live over {best_hour['n']} calls")

        # 2) Best weekday (0=Mon..6=Sun) — sqlite strftime %w is 0=Sun..6=Sat
        best_dow = conn.execute("""
            SELECT CAST(strftime('%w', dialed_at) AS INT) w,
                   SUM(CASE WHEN disposition_category='live_conversation' THEN 1 ELSE 0 END)*1.0/COUNT(*) rate,
                   COUNT(*) n
            FROM call_attempts WHERE length(dialed_at)>=10
            GROUP BY w HAVING n>=10 ORDER BY rate DESC LIMIT 1""").fetchone()
        if best_dow:
            _set(conn, "best_call_dow", best_dow["w"], f"{best_dow['rate']*100:.0f}% live over {best_dow['n']} calls")

        # 3) Source quality: live-conversation rate per source -> drives auto-scoring
        for r in conn.execute("""
            SELECT p.source,
                   SUM(CASE WHEN c.disposition_category='live_conversation' THEN 1 ELSE 0 END)*1.0/COUNT(*) rate,
                   COUNT(*) n
            FROM call_attempts c JOIN people p ON p.id=c.person_id
            WHERE p.source<>'' GROUP BY p.source HAVING n>=5"""):
            _set(conn, f"source_rate::{r['source']}", r["rate"], f"{r['rate']*100:.0f}% live over {r['n']} calls")

        # 4) Overall contact rate (headline learning metric)
        overall = conn.execute("""
            SELECT SUM(CASE WHEN disposition_category='live_conversation' THEN 1 ELSE 0 END)*1.0/COUNT(*) rate
            FROM call_attempts""").fetchone()["rate"] or 0
        _set(conn, "overall_contact_rate", overall, f"{overall*100:.1f}%")


def get_stats():
    ensure_schema()
    with db.connect() as conn:
        rows = conn.execute("SELECT key, value, detail, updated_at FROM learning_stats").fetchall()
    out = {"sources": {}, "best_call_hour": None, "best_call_dow": None,
           "overall_contact_rate": None, "updated_at": None}
    dow_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    for r in rows:
        out["updated_at"] = r["updated_at"]
        if r["key"].startswith("source_rate::"):
            out["sources"][r["key"].split("::", 1)[1]] = {"rate": r["value"], "detail": r["detail"]}
        elif r["key"] == "best_call_hour":
            h = int(r["value"]); out["best_call_hour"] = {"hour": h,
                "label": f"{(h % 12 or 12)}{'am' if h < 12 else 'pm'}", "detail": r["detail"]}
        elif r["key"] == "best_call_dow":
            w = int(r["value"]); out["best_call_dow"] = {"dow": w, "label": dow_names[w], "detail": r["detail"]}
        elif r["key"] == "overall_contact_rate":
            out["overall_contact_rate"] = {"rate": r["value"], "detail": r["detail"]}
    return out


def source_score_bonus(conn, source):
    """A scoring nudge (-10..+15) based on how well a source historically converts."""
    if not source:
        return 0
    r = conn.execute("SELECT value FROM learning_stats WHERE key=?", (f"source_rate::{source}",)).fetchone()
    if not r:
        return 0
    rate = r["value"]            # 0..1 live-conversation rate
    return int(min(15, max(-10, (rate - 0.05) * 200)))   # 5% baseline -> 0; 12.5% -> +15
