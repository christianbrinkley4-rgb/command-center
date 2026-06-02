"""Shared call-queue rules for the Command Center.

These helpers keep the dashboard, the dialer export, and sync side effects using
the same retry policy:
  * new leads are worked before retries by the caller's tier order
  * retryable calls come back on a later day, not the same day
  * a phone is removed after the configured max attempts
"""
import os
from datetime import date, datetime

import db

DEFAULT_RETRYABLE_DISPOSITIONS = {
    "no_answer",
    "busy",
    "voicemail_dropped",
    "answering_machine",
    "machine_skipped",
    "false_bridge",
}

TERMINAL_PHONE_DISPOSITIONS = {
    "dead_number",
    "spam_blocked",
    "place_call_failed",
    "telecom_failure",
    "agent_skipped_dead_air",
    "transfer_timeout",
}


def is_auto_retry_reason(reason):
    text = str(reason or "").strip().lower()
    return text == "redial_after_voicemail" or text.startswith("retry_no_answer_")


def _csv_set(value):
    return {x.strip().lower() for x in str(value or "").split(",") if x.strip()}


def retryable_dispositions():
    configured = _csv_set(os.getenv("CC_RETRYABLE_DISPOSITIONS", ""))
    return configured or set(DEFAULT_RETRYABLE_DISPOSITIONS)


def max_attempts():
    try:
        return max(1, int(os.getenv("CC_MAX_CALL_ATTEMPTS", "4")))
    except (TypeError, ValueError):
        return 4


def coerce_day(value=None):
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return date.today()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return date.today()


def attempt_count(conn, person_id=None, phone=""):
    phone = db.normalize_phone(phone)
    if phone:
        return conn.execute(
            "SELECT COUNT(*) n FROM call_attempts WHERE phone=?",
            (phone,),
        ).fetchone()["n"]
    if person_id:
        return conn.execute(
            "SELECT COUNT(*) n FROM call_attempts WHERE person_id=?",
            (person_id,),
        ).fetchone()["n"]
    return 0


def latest_attempt(conn, person_id=None, phone=""):
    phone = db.normalize_phone(phone)
    if phone:
        row = conn.execute(
            """SELECT disposition, disposition_category, dialed_at
               FROM call_attempts WHERE phone=?
               ORDER BY dialed_at DESC LIMIT 1""",
            (phone,),
        ).fetchone()
    elif person_id:
        row = conn.execute(
            """SELECT disposition, disposition_category, dialed_at
               FROM call_attempts WHERE person_id=?
               ORDER BY dialed_at DESC LIMIT 1""",
            (person_id,),
        ).fetchone()
    else:
        row = None
    return dict(row) if row else None


def retry_status(conn, person_id, phone, stage="", qtype="", agenda_day=None):
    """Return queue eligibility details for a person/phone.

    ``allowed`` answers whether this row may be dialed in the requested agenda.
    ``reason`` is intentionally short so it can be shown/debugged in the UI.
    """
    agenda_day = coerce_day(agenda_day)
    # Cap by the PERSON's total attempts across all their numbers, not just this
    # one phone — a lead with two numbers must not get called 2x the cap.
    phone_attempts = attempt_count(conn, person_id, phone)
    person_attempts = attempt_count(conn, person_id) if person_id else 0
    attempts = max(phone_attempts, person_attempts)
    limit = max_attempts()
    latest = latest_attempt(conn, person_id, phone)
    person_latest = latest_attempt(conn, person_id)
    remaining = max(0, limit - attempts)
    needs_retry_check = qtype == "retry" or stage in {"attempted", "voicemail"}

    if needs_retry_check and person_latest:
        person_call_day = str(person_latest.get("dialed_at") or "")[:10]
        if person_call_day and person_call_day >= agenda_day.isoformat():
            return {
                "allowed": False,
                "reason": "same_day_lead",
                "attempts": attempts,
                "attempts_remaining": remaining,
                "latest": person_latest,
            }

    if attempts >= limit:
        return {
            "allowed": False,
            "reason": "max_attempts",
            "attempts": attempts,
            "attempts_remaining": remaining,
            "latest": latest,
        }

    if not latest:
        return {
            "allowed": True,
            "reason": "new",
            "attempts": attempts,
            "attempts_remaining": remaining,
            "latest": person_latest,
        }

    disposition = str(latest.get("disposition") or "").strip().lower()
    category = str(latest.get("disposition_category") or "").strip().lower()
    call_day = str(latest.get("dialed_at") or "")[:10]

    if disposition in TERMINAL_PHONE_DISPOSITIONS or category in {"bad_number", "dnc"}:
        return {
            "allowed": False,
            "reason": "terminal",
            "attempts": attempts,
            "attempts_remaining": remaining,
            "latest": latest,
        }

    if needs_retry_check:
        if disposition not in retryable_dispositions():
            return {
                "allowed": False,
                "reason": "not_retryable",
                "attempts": attempts,
                "attempts_remaining": remaining,
                "latest": latest,
            }
        if call_day and call_day >= agenda_day.isoformat():
            return {
                "allowed": False,
                "reason": "same_day_retry",
                "attempts": attempts,
                "attempts_remaining": remaining,
                "latest": latest,
            }

    return {
        "allowed": True,
        "reason": "eligible",
        "attempts": attempts,
        "attempts_remaining": remaining,
        "latest": latest,
    }
