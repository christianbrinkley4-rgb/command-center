"""
Command Center — Automation Engine (the "well-oiled machine").

Decides and schedules the NEXT BEST ACTION for every lead, automatically, based on
what just happened to them. Examples:
  - Voicemail on Monday        -> auto-queue a follow-up text for Wednesday
  - Prospect texts "call me 10am next Wed" -> auto-create a scheduled call at that time
  - No answer                  -> retry in the next good calling window (max N tries)
  - Callback requested         -> a scheduled call task at the requested time
  - Appointment set            -> reminder text 24h before + morning-of
  - Texted, no reply in 3 days -> one warmer follow-up, then rest

Everything lands in `scheduled_tasks`. A background worker fires due tasks (queues the
text/email, surfaces the call on the right day) with no manual work. Every decision is
audited, idempotent, and respects suppressions/opt-outs and quiet hours.

The engine also LEARNS: it records which cadence steps and call-times actually produce
contact, and nudges future timing toward what works (see learning.py).
"""
import os
from datetime import datetime, timedelta, time as dtime

import db

# ---------------------------------------------------------------- schema

SCHEDULED_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER REFERENCES people(id),
    kind        TEXT,            -- call / sms / email
    reason      TEXT,            -- why it was scheduled (cadence step / callback / reminder)
    due_at      TEXT,           -- ISO datetime it becomes actionable
    status      TEXT DEFAULT 'pending',   -- pending / done / cancelled / sent
    channel_to  TEXT,           -- phone or email it targets
    payload     TEXT DEFAULT '',-- optional message body/template id
    owner       TEXT,
    created_at  TEXT,
    fired_at    TEXT,
    dedupe_key  TEXT UNIQUE     -- prevents duplicate scheduling of the same step
);
CREATE INDEX IF NOT EXISTS idx_sched_due    ON scheduled_tasks(due_at, status);
CREATE INDEX IF NOT EXISTS idx_sched_person ON scheduled_tasks(person_id);

CREATE TABLE IF NOT EXISTS automation_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER,
    action     TEXT,
    detail     TEXT,
    at         TEXT
);
"""

# Quiet hours / good windows (Eastern, senior-friendly). Texts/calls won't fire outside.
QUIET_START = 8     # 8am
QUIET_END = 20      # 8pm
# Best-call weekday windows default; learning.py can override per source over time.
GOOD_CALL_DAYS = {1, 2, 3, 5}   # Tue, Wed, Thu, Sat (Mon=0)


def ensure_schema():
    with db.connect() as conn:
        conn.executescript(SCHEDULED_SCHEMA)


def _now():
    return datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _alog(conn, person_id, action, detail=""):
    conn.execute("INSERT INTO automation_log(person_id, action, detail, at) VALUES (?,?,?,?)",
                 (person_id, action, detail, _iso(_now())))


def _is_suppressed(conn, person_id, phone=""):
    if conn.execute("SELECT 1 FROM suppressions WHERE scope='person' AND value=? LIMIT 1",
                    (str(person_id),)).fetchone():
        return True
    if phone and conn.execute("SELECT 1 FROM suppressions WHERE scope='phone' AND value=? LIMIT 1",
                              (phone,)).fetchone():
        return True
    return False


def _clamp_to_window(dt, kind):
    """Move a datetime into the next allowed window (quiet hours; call-days for calls)."""
    # respect quiet hours
    if dt.hour < QUIET_START:
        dt = dt.replace(hour=QUIET_START, minute=0, second=0, microsecond=0)
    elif dt.hour >= QUIET_END:
        dt = (dt + timedelta(days=1)).replace(hour=QUIET_START, minute=0, second=0, microsecond=0)
    # calls prefer good weekdays; nudge forward to the next good day if needed
    if kind == "call":
        guard = 0
        while dt.weekday() not in GOOD_CALL_DAYS and guard < 7:
            dt = (dt + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
            guard += 1
    return dt


def _phone_of(conn, person_id):
    r = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? AND status='active' LIMIT 1",
                     (person_id,)).fetchone()
    if not r:
        r = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? LIMIT 1", (person_id,)).fetchone()
    return r["e164"] if r else ""


# Per-agent caller IDs so texts come from the same number that called the prospect.
AGENT_FROM = {
    "chris": os.getenv("CC_FROM_CHRIS", "+13369622307"),
    "will": os.getenv("CC_FROM_WILL", "+13366451062"),
}
AGENT_PHONE_DISPLAY = {
    "chris": "(336) 962-2307",
    "will": "(336) 740-2604",
}


def _person_for_message(conn, pid):
    """Assemble the person dict the templates need (name parts, city, owner, agent phone)."""
    r = conn.execute("SELECT id, full_name, first_name, city, county, owner FROM people WHERE id=?",
                     (pid,)).fetchone()
    if not r:
        return {}
    d = dict(r)
    d["agent_phone"] = AGENT_PHONE_DISPLAY.get((d.get("owner") or "").lower(), "")
    return d


def _best_from(conn, pid):
    """The DID to text from: the number that last called this person if it's one of
    ours, else the owner's default DID."""
    last = conn.execute(
        "SELECT from_number FROM call_attempts WHERE person_id=? AND from_number<>'' ORDER BY dialed_at DESC LIMIT 1",
        (pid,)).fetchone()
    owner = (conn.execute("SELECT owner FROM people WHERE id=?", (pid,)).fetchone() or {"owner": ""})["owner"]
    if last and last["from_number"]:
        return last["from_number"].split(",")[0].strip()
    return AGENT_FROM.get((owner or "").lower(), "")


def _render(person, template_key, channel, last_call_at):
    try:
        import message_templates as mt
        return mt.render(template_key, person, last_call_at=last_call_at, channel=channel)
    except Exception:
        return None


def schedule_task(conn, person_id, kind, due_at, reason, channel_to="", payload="", owner="", dedupe_key=""):
    """Insert a scheduled task (idempotent via dedupe_key). Returns task id or None."""
    if not dedupe_key:
        dedupe_key = f"{person_id}|{kind}|{reason}|{due_at[:10]}"
    try:
        cur = conn.execute(
            """INSERT INTO scheduled_tasks(person_id, kind, reason, due_at, status, channel_to,
               payload, owner, created_at, dedupe_key)
               VALUES (?,?,?,?, 'pending', ?,?,?,?,?)""",
            (person_id, kind, reason, due_at, channel_to, payload, owner, _iso(_now()), dedupe_key))
        return cur.lastrowid
    except Exception:
        return None  # duplicate -> already scheduled


# ---------------------------------------------------------------- cadence rules

def _next_good_call(dt):
    return _clamp_to_window(dt, "call")


def plan_next_action(conn, person_id, disposition, category, owner=""):
    """The heart of the machine: given what just happened, schedule what should happen next.
    Returns a list of human-readable actions taken (for logging/response)."""
    actions = []
    phone = _phone_of(conn, person_id)
    has_email = bool((conn.execute("SELECT email FROM people WHERE id=?", (person_id,)).fetchone() or {})["email"]
                     if conn.execute("SELECT email FROM people WHERE id=?", (person_id,)).fetchone() else "")
    suppressed = _is_suppressed(conn, person_id, phone)
    now = _now()

    def add(kind, when, reason, payload="", channel=None):
        if suppressed and kind in ("sms", "email"):
            return
        when = _clamp_to_window(when, kind)
        ch = channel if channel is not None else (phone if kind in ("call", "sms") else "")
        if schedule_task(conn, person_id, kind, _iso(when), reason, ch, payload, owner):
            actions.append(f"{kind} on {when:%a %m/%d %I:%M%p} ({reason})")

    if category == "voicemail":
        # Voicemail -> warm text 5-15 min later (mobile), then a redial 2 days out.
        if phone:
            add("sms", now + timedelta(minutes=10), "post_voicemail_text",
                payload="vm_followup")
        add("call", now + timedelta(days=2), "redial_after_voicemail")

    elif category == "no_answer":
        # No answer -> retry in the next good window, escalating spacing.
        n = conn.execute(
            "SELECT COUNT(*) c FROM call_attempts WHERE person_id=? AND disposition_category='no_answer'",
            (person_id,)).fetchone()["c"]
        if n <= 4:
            spacing = [1, 2, 3, 5][min(n - 1, 3)] if n else 1
            add("call", _next_good_call(now + timedelta(days=spacing)), f"retry_no_answer_{n}")

    elif category == "callback":
        # A specific callback time is set via inbound parsing; if not, default next good day 10am.
        add("call", _next_good_call(now + timedelta(days=1)).replace(hour=10, minute=0),
            "callback_default")

    elif category == "appointment":
        # Reminders are scheduled by set_appointment_reminders() once we know the time.
        pass

    elif category in ("contacted", "not_interested"):
        pass  # human handled it; no auto-chase

    # Email nurture for anyone with an email who isn't progressing (gentle, capped).
    if has_email and category in ("voicemail", "no_answer") and not suppressed:
        sent = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE person_id=? AND channel='email' AND direction='out'",
            (person_id,)).fetchone()["c"]
        if sent == 0:
            add("email", now + timedelta(days=4), "nurture_email_1", payload="t65_intro", channel="email")

    for a in actions:
        _alog(conn, person_id, "scheduled", a)
    return actions


def set_appointment_reminders(conn, person_id, scheduled_dt, owner=""):
    """When an appointment is booked, queue a reminder text 24h before and morning-of."""
    actions = []
    phone = _phone_of(conn, person_id)
    if not phone or _is_suppressed(conn, person_id, phone):
        return actions
    day_before = scheduled_dt - timedelta(days=1)
    morning = scheduled_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    for when, reason in [(day_before, "appt_reminder_24h"), (morning, "appt_reminder_morning")]:
        if when > _now():
            if schedule_task(conn, person_id, "sms", _iso(_clamp_to_window(when, "sms")),
                             reason, phone, "appt_reminder", owner):
                actions.append(f"reminder {when:%a %m/%d %I:%M%p}")
    if actions:
        _alog(conn, person_id, "appt_reminders", "; ".join(actions))
    return actions


def cancel_pending(conn, person_id, reason="superseded"):
    """Cancel a person's pending tasks (e.g., they opted out or were reached)."""
    n = conn.execute(
        "UPDATE scheduled_tasks SET status='cancelled' WHERE person_id=? AND status='pending'",
        (person_id,)).rowcount
    if n:
        _alog(conn, person_id, "cancelled_pending", f"{n} tasks: {reason}")
    return n


# ---------------------------------------------------------------- worker

def due_tasks(conn, now=None, limit=200):
    now = now or _now()
    return conn.execute(
        """SELECT s.*, p.full_name, p.email FROM scheduled_tasks s
           JOIN people p ON p.id=s.person_id
           WHERE s.status='pending' AND s.due_at<=? ORDER BY s.due_at LIMIT ?""",
        (_iso(now), limit)).fetchall()


def fire_due_tasks(now=None, sms_sender=None, email_sender=None):
    """Process all due tasks. Calls are surfaced (left pending->'ready' for the agent's
    day view); texts/emails are sent via the provided sender callables (or queued if none).
    Returns a summary dict. Safe to call every minute."""
    ensure_schema()
    fired = {"call_ready": 0, "sms": 0, "email": 0, "skipped_suppressed": 0}
    now = now or _now()
    with db.connect() as conn:
        for t in due_tasks(conn, now):
            pid, kind = t["person_id"], t["kind"]
            phone = t["channel_to"]
            if kind in ("sms", "email") and _is_suppressed(conn, pid, phone):
                conn.execute("UPDATE scheduled_tasks SET status='cancelled', fired_at=? WHERE id=?",
                             (_iso(now), t["id"]))
                fired["skipped_suppressed"] += 1
                continue

            if kind == "call":
                # Surface it: mark the person so the action queue shows them today, keep task done.
                conn.execute("UPDATE people SET stage=CASE WHEN stage='dnc' THEN 'dnc' ELSE 'callback' END, "
                             "last_activity_at=? WHERE id=?", (_iso(now), pid))
                conn.execute("UPDATE scheduled_tasks SET status='done', fired_at=? WHERE id=?",
                             (_iso(now), t["id"]))
                fired["call_ready"] += 1
                _alog(conn, pid, "call_due", t["reason"])

            elif kind in ("sms", "email"):
                # Build the personalized message from the template + this person's live data.
                person = _person_for_message(conn, pid)
                last_call = conn.execute(
                    "SELECT dialed_at FROM call_attempts WHERE person_id=? ORDER BY dialed_at DESC LIMIT 1",
                    (pid,)).fetchone()
                last_call_at = last_call["dialed_at"] if last_call else None
                rendered = _render(person, t["payload"] or t["reason"], kind, last_call_at)
                body = rendered["body"] if rendered else (t["payload"] or "")
                subject = rendered.get("subject", "") if rendered else ""

                # Frequency cap: never more than 1 outbound of a channel per person per day.
                already = conn.execute(
                    "SELECT COUNT(*) n FROM messages WHERE person_id=? AND channel=? AND direction='out' "
                    "AND substr(created_at,1,10)=?", (pid, kind, _iso(now)[:10])).fetchone()["n"]
                if already:
                    conn.execute("UPDATE scheduled_tasks SET status='cancelled', fired_at=? WHERE id=?",
                                 (_iso(now), t["id"]))
                    continue

                status = "queued"   # default: built + recorded, not physically sent (senders gated off)
                try:
                    import senders
                    if kind == "sms" and senders.sms_enabled() and phone and body:
                        senders.send_sms(phone, body, from_number=_best_from(conn, pid))
                        status = "sent"
                    elif kind == "email" and senders.email_enabled() and t["email"] and body:
                        senders.send_email(t["email"], subject or "A quick follow-up", body)
                        status = "sent"
                except Exception as exc:
                    status = "failed"
                    _alog(conn, pid, f"{kind}_send_error", str(exc)[:120])

                conn.execute(
                    """INSERT INTO messages(person_id, channel, direction, subject, body, status, template_id, created_at, sent_at)
                       VALUES (?,?, 'out', ?, ?, ?, ?, ?, ?)""",
                    (pid, kind, subject, body, status, t["payload"], _iso(now), _iso(now) if status == "sent" else ""))
                # keep failed tasks pending for retry; sent/queued are done
                conn.execute("UPDATE scheduled_tasks SET status=?, fired_at=? WHERE id=?",
                             ("pending" if status == "failed" else "sent", _iso(now), t["id"]))
                fired[kind] += 1
                _alog(conn, pid, f"{kind}_fired", f"{t['reason']} ({status})")
    return fired
