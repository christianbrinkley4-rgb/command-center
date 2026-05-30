"""
Command Center — relational data spine (SQLite).

Single source of truth for every person, phone, call, message, appointment,
and outcome across all dialers (Chris + Will + any future agent). SQLite is
chosen deliberately: ACID, zero-ops, one portable file, perfect for this scale,
and trivially deployable to the DigitalOcean droplet later.

Design rules:
- Idempotent schema (safe to run repeatedly).
- WAL mode so the dashboard can read while the sync writes.
- Every meaningful change is auditable.
- Person identity is stable (name+city+birthday) so a lead's whole journey —
  across numbers, calls, texts, sessions, and agents — lives on one timeline.
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "command_center.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_key    TEXT UNIQUE,          -- normalized name|city|birthday
    full_name     TEXT,
    first_name    TEXT,
    last_name     TEXT,
    birthday      TEXT,
    city          TEXT,
    county        TEXT,
    address       TEXT,
    source        TEXT,                 -- lead source (T65_June, OSCR_Life, ...)
    lead_score    INTEGER DEFAULT 0,
    stage         TEXT DEFAULT 'new',   -- new, attempted, contacted, callback, appointment, closed, dnc
    owner         TEXT,                 -- chris / will
    first_seen_at TEXT,
    last_activity_at TEXT,
    notes         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS phone_numbers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER REFERENCES people(id),
    e164       TEXT UNIQUE,
    line_type  TEXT DEFAULT '',         -- mobile / landline / voip
    status     TEXT DEFAULT 'active',   -- active / bad / spam / suppressed
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS call_attempts (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id                INTEGER REFERENCES people(id),
    phone                    TEXT,
    agent                    TEXT,
    from_number              TEXT,
    dialed_at                TEXT,
    disposition              TEXT,
    disposition_category     TEXT,
    amd_result               TEXT,
    answer_to_agent_seconds  REAL,
    live_talk_seconds        REAL,
    total_call_seconds       REAL,
    hangup_initiator         TEXT,
    call_control_id          TEXT UNIQUE,
    dedupe_key               TEXT UNIQUE  -- fallback when no call_control_id
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER REFERENCES people(id),
    channel     TEXT,                   -- sms / email
    direction   TEXT,                   -- out / in
    body        TEXT,
    status      TEXT,                   -- queued / sent / delivered / replied / failed / opted_out
    template_id TEXT DEFAULT '',
    created_at  TEXT,
    sent_at     TEXT,
    replied_at  TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER REFERENCES people(id),
    agent        TEXT,
    scheduled_at TEXT,
    type         TEXT DEFAULT 'medicare',
    status       TEXT DEFAULT 'scheduled',  -- scheduled / kept / no_show / cancelled
    notes        TEXT DEFAULT '',
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS suppressions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT,                    -- phone / person
    value      TEXT,
    reason     TEXT,                    -- dnc / opt_out / spam / bad
    created_at TEXT,
    UNIQUE(scope, value)
);

CREATE TABLE IF NOT EXISTS lead_sources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE,
    type       TEXT DEFAULT '',
    first_seen_at TEXT,
    lead_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS person_notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people(id),
    body      TEXT,
    author    TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entity    TEXT,
    entity_id TEXT,
    action    TEXT,
    detail    TEXT,
    actor     TEXT DEFAULT 'system',
    at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_person   ON call_attempts(person_id);
CREATE INDEX IF NOT EXISTS idx_calls_dialed   ON call_attempts(dialed_at);
CREATE INDEX IF NOT EXISTS idx_calls_disp     ON call_attempts(disposition_category);
CREATE INDEX IF NOT EXISTS idx_phones_person  ON phone_numbers(person_id);
CREATE INDEX IF NOT EXISTS idx_people_stage   ON people(stage);
CREATE INDEX IF NOT EXISTS idx_people_activity ON people(last_activity_at);
"""

# How raw dialer dispositions roll up into the categories the dashboard reports on.
DISPOSITION_CATEGORY = {
    "human_transferred": "live_conversation",
    "callback_requested": "callback",
    "appointment_set": "appointment",
    "not_interested": "not_interested",
    "do_not_call": "dnc",
    "voicemail_dropped": "voicemail",
    "answering_machine": "voicemail",
    "machine_skipped": "voicemail",
    "no_answer": "no_answer",
    "busy": "no_answer",
    "false_bridge": "dropped",
    "agent_skipped_dead_air": "dropped",
    "transfer_timeout": "dropped",
    "dead_number": "bad_number",
    "spam_blocked": "bad_number",
    "place_call_failed": "bad_number",
    "telecom_failure": "bad_number",
}

CATEGORY_LABELS = {
    "live_conversation": "Live Conversations",
    "callback": "Callbacks",
    "appointment": "Appointments",
    "not_interested": "Not Interested",
    "dnc": "Do Not Call",
    "voicemail": "Voicemail / Machine",
    "no_answer": "No Answer / Busy",
    "dropped": "Dropped / Dead Air",
    "bad_number": "Bad / Dead / Spam",
    "unknown": "Other",
}


def disposition_category(disposition):
    return DISPOSITION_CATEGORY.get(str(disposition or "").strip().lower(), "unknown")


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return ""


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


def person_key(name, city, birthday):
    name_n = _norm(name)
    if not name_n:
        return ""
    return f"{name_n}|{_norm(city)}|{_norm(birthday)}"


@contextmanager
def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path=DB_PATH):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
    return db_path


def audit(conn, entity, entity_id, action, detail="", actor="system"):
    conn.execute(
        "INSERT INTO audit_log(entity, entity_id, action, detail, actor, at) VALUES (?,?,?,?,?,?)",
        (entity, str(entity_id), action, detail, actor, datetime.now().isoformat(timespec="seconds")),
    )


if __name__ == "__main__":
    path = init_db()
    print(f"Command Center database ready at: {path}")
