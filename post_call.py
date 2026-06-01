"""
Command Center — Post-call AI analysis.

After a transcript lands via /agent/transcripts/ingest, this module reads the
text, extracts what HAPPENED (appointment? callback? DNC? key facts?), and
writes that back to the DB as concrete actions:

  * appointment_set + a parseable time -> appointments row + automation reminders
                                          + stage='appointment'
  * callback_set    + a parseable time -> scheduled_tasks 'call' row
                                          + stage='callback'
  * do_not_call                        -> suppressions + stage='dnc'
                                          + cancel pending automation
  * not_interested                     -> stage='contacted'
                                          + cancel pending automation

  Plus, always: a structured note appended to person_notes summarizing the
  conversation, the prospect's own words on key topics, and the agreed next
  step — so the dialer's queue card and the dashboard show what was said
  without anyone re-reading the transcript.

Provider hierarchy mirrors ai_client.py (Anthropic → OpenAI → Groq → Gemini → none).
Gated by ENABLE_POST_CALL_AI=1 so it can be turned on without code changes.
Never raises — failures degrade to "no analysis, original disposition stands."
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import db

try:
    import nlp_time
except Exception:
    nlp_time = None

try:
    import automation
except Exception:
    automation = None

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ config / provider

def _provider_for_post_call():
    """Same fallback order ai_client.py uses, evaluated lazily so env changes on
    the droplet take effect on next request without a process restart."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return ("anthropic", os.getenv("AI_POST_CALL_MODEL", "claude-opus-4-7"))
    if os.getenv("OPENAI_API_KEY"):
        return ("openai", os.getenv("AI_POST_CALL_MODEL", "gpt-4o"))
    if os.getenv("GROQ_API_KEY"):
        return ("groq", os.getenv("AI_POST_CALL_MODEL", "llama-3.3-70b-versatile"))
    if os.getenv("GEMINI_API_KEY"):
        return ("gemini", os.getenv("AI_POST_CALL_MODEL", "gemini-2.0-flash"))
    return (None, None)


def is_enabled() -> bool:
    if os.getenv("ENABLE_POST_CALL_AI", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    return _provider_for_post_call()[0] is not None


# ------------------------------------------------------------------ prompts

SYSTEM_PROMPT = """You are a Medicare sales operations assistant. Read the call \
transcript and return a single JSON object describing what HAPPENED on the call. \
Your output drives database actions (booking an appointment, scheduling a callback, \
suppressing DNC), so be conservative: only report appointment_set or callback_set \
when both parties clearly agreed on a specific time. If the prospect was vague, use \
"no_decision".

Channels are diarized: "Speaker A" is the AGENT, "Speaker B" is the PROSPECT.

Return JSON ONLY. No markdown, no commentary. Schema:

{
  "outcome": "appointment_set|callback_set|not_interested|do_not_call|no_decision|voicemail|hung_up",
  "appointment_at": "ISO 8601 datetime if outcome=appointment_set AND a clear time was agreed, else null",
  "callback_at":    "ISO 8601 datetime if outcome=callback_set AND a clear time was agreed, else null",
  "summary":        "2-3 neutral sentences: what was discussed and where it landed",
  "key_facts":      ["short factual items the prospect said about themselves (current coverage, doctors, family, finances, health concerns)"],
  "objections":     ["any objection the prospect raised, in their own words if possible"],
  "next_step":      "the single concrete next step the agent should take",
  "prospect_quotes":["up to 3 short direct quotes from the prospect (Speaker B) that matter"],
  "tpmo_delivered": true,
  "soa_collected":  false,
  "do_not_call_requested": false,
  "confidence":     0.0
}

Rules:
- All datetimes MUST be ISO 8601 in the prospect's local timezone (assume America/New_York unless stated otherwise). If a relative time was said ("next Tuesday at 10am"), resolve it against the call_started_at timestamp provided in the user message.
- If outcome is appointment_set or callback_set but no specific time was agreed, drop the time field to null and use "no_decision" instead.
- "do_not_call_requested" is true ONLY if the prospect explicitly asked to be removed, not called back, or said "stop calling".
- "tpmo_delivered" is true if the AGENT clearly stated they do not offer every plan available in the area (the CMS TPMO disclaimer). If you're not sure, mark false.
- confidence is your overall confidence in the JSON (0-1)."""


def _build_user_prompt(transcript_text: str, context: dict) -> str:
    call_started = context.get("call_started_at") or context.get("dialed_at") or ""
    parts = [
        f"call_started_at: {call_started}",
        f"prospect_name: {context.get('full_name', '')}",
        f"prospect_city: {context.get('city', '')}",
        f"prior_disposition: {context.get('prior_disposition', '')}",
        f"agent: {context.get('agent', '')}",
        "",
        "TRANSCRIPT:",
        transcript_text or "(empty transcript)",
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------ AI call

def _call_ai(system: str, user: str, max_tokens: int = 1500) -> Optional[str]:
    provider, model = _provider_for_post_call()
    if not provider:
        return None
    try:
        if provider == "anthropic":
            import anthropic  # type: ignore
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text
        else:
            import openai  # type: ignore
            base_urls = {
                "groq":   "https://api.groq.com/openai/v1",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
            }
            key_env = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}[provider]
            kwargs = {"api_key": os.getenv(key_env)}
            if provider in base_urls:
                kwargs["base_url"] = base_urls[provider]
            client = openai.OpenAI(**kwargs)
            resp = client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user}],
                response_format={"type": "json_object"} if provider in {"openai", "groq"} else None,
            )
            return resp.choices[0].message.content
    except Exception as exc:
        log.warning("post_call AI call failed (%s/%s): %s", provider, model, exc)
        return None


def _coerce_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner)
    # Some models prepend a stray "json" line.
    text = re.sub(r"^json\s*\n", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except Exception:
        # Last resort: try to grab the first {...} block.
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def analyze(transcript_text: str, context: dict) -> Optional[dict]:
    """Run AI analysis. Returns dict or None — never raises."""
    if not is_enabled() or not transcript_text:
        return None
    raw = _call_ai(SYSTEM_PROMPT, _build_user_prompt(transcript_text, context))
    parsed = _coerce_json(raw or "")
    if parsed is None:
        log.warning("post_call: could not parse AI JSON")
        return None
    return parsed


# ------------------------------------------------------------------ date parsing helpers

_VALID_OUTCOMES = {
    "appointment_set", "callback_set", "not_interested", "do_not_call",
    "no_decision", "voicemail", "hung_up",
}


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s or s.lower() in {"null", "none"}:
        return None
    # Strip trailing Z (UTC marker) — we treat times as local.
    if s.endswith("Z"):
        s = s[:-1]
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    # nlp_time fallback for natural language ("next Tuesday at 10am").
    if nlp_time is not None:
        try:
            parsed = nlp_time.parse_reply(s)
            if parsed and parsed.get("when"):
                return parsed["when"]
        except Exception:
            pass
    return None


def _sane_future_dt(dt: datetime, now: Optional[datetime] = None) -> Optional[datetime]:
    """Reject obviously stale / nonsensical dates so the AI doesn't book yesterday."""
    if not dt:
        return None
    now = now or datetime.now()
    # Allow up to 4 hours in the past (clock-skew tolerance), and at most 1 year out.
    if dt < now - timedelta(hours=4):
        return None
    if dt > now + timedelta(days=365):
        return None
    return dt


# ------------------------------------------------------------------ DB actions

def _append_note(conn, person_id, body, author="post_call_ai"):
    conn.execute(
        """INSERT INTO person_notes(person_id, body, author, created_at)
           VALUES (?,?,?,?)""",
        (person_id, body, author, datetime.now().isoformat(timespec="seconds")))


def _format_note(analysis: dict) -> str:
    """Build the human-readable note body for the lead card."""
    parts = []
    summary = (analysis.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    facts = [f for f in (analysis.get("key_facts") or []) if f]
    if facts:
        parts.append("KEY FACTS:")
        parts.extend(f"  - {f}" for f in facts[:10])
    objections = [o for o in (analysis.get("objections") or []) if o]
    if objections:
        parts.append("OBJECTIONS:")
        parts.extend(f"  - {o}" for o in objections[:10])
    quotes = [q for q in (analysis.get("prospect_quotes") or []) if q]
    if quotes:
        parts.append("PROSPECT SAID:")
        parts.extend(f"  “{q}”" for q in quotes[:3])
    nxt = (analysis.get("next_step") or "").strip()
    if nxt:
        parts.append(f"NEXT STEP: {nxt}")
    compliance_bits = []
    if analysis.get("tpmo_delivered") is True:
        compliance_bits.append("TPMO delivered")
    elif analysis.get("tpmo_delivered") is False:
        compliance_bits.append("TPMO NOT delivered")
    if analysis.get("soa_collected") is True:
        compliance_bits.append("SOA collected")
    if compliance_bits:
        parts.append("COMPLIANCE: " + " / ".join(compliance_bits))
    conf = analysis.get("confidence")
    if isinstance(conf, (int, float)):
        parts.append(f"(AI confidence: {conf:.2f})")
    return "\n".join(parts) if parts else "Post-call analysis ran but produced no notes."


def apply_actions(conn, person_id, call_attempt_id, transcript_id, analysis: dict,
                  agent: str = "", actor: str = "post_call_ai") -> list:
    """Apply the parsed analysis to the DB. Returns a list of action strings for
    logging/response. Tolerant of partial / missing fields."""
    actions = []
    if not analysis or not person_id:
        return actions

    outcome = str(analysis.get("outcome") or "").strip().lower()
    if outcome not in _VALID_OUTCOMES:
        outcome = "no_decision"

    now = datetime.now().isoformat(timespec="seconds")

    # 1) Appointment booking.
    if outcome == "appointment_set":
        when = _sane_future_dt(_parse_iso(analysis.get("appointment_at")))
        if when:
            cur = conn.execute(
                """INSERT INTO appointments(person_id, agent, scheduled_at, type, status, notes, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (person_id, agent or "", when.isoformat(timespec="seconds"), "medicare",
                 "scheduled", (analysis.get("summary") or "")[:500], now))
            actions.append(f"appointment booked for {when:%a %m/%d %I:%M%p} (id={cur.lastrowid})")
            conn.execute("UPDATE people SET stage='appointment', last_activity_at=? WHERE id=?",
                         (now, person_id))
            if automation is not None:
                try:
                    automation.cancel_pending(conn, person_id, "appointment_booked")
                    automation.set_appointment_reminders(conn, person_id, when, agent or "")
                    actions.append("reminders scheduled")
                except Exception as exc:
                    log.warning("post_call reminders failed: %s", exc)
        else:
            # AI thought there was an appt but no time landed cleanly. Downgrade.
            outcome = "callback_set" if analysis.get("callback_at") else "no_decision"
            actions.append("appointment downgraded: no parseable time")

    # 2) Callback scheduling.
    if outcome == "callback_set":
        when = _sane_future_dt(_parse_iso(analysis.get("callback_at")))
        if when and automation is not None:
            try:
                phone_row = conn.execute(
                    "SELECT e164 FROM phone_numbers WHERE person_id=? LIMIT 1", (person_id,)).fetchone()
                phone = phone_row["e164"] if phone_row else ""
                tid = automation.schedule_task(
                    conn, person_id, "call", when.isoformat(timespec="seconds"),
                    "post_call_ai_callback", phone, "", agent or "",
                    dedupe_key=f"{person_id}|call|ai|{when:%Y%m%d%H%M}")
                if tid:
                    actions.append(f"callback scheduled for {when:%a %m/%d %I:%M%p}")
                    conn.execute("UPDATE people SET stage='callback', last_activity_at=? WHERE id=?",
                                 (now, person_id))
            except Exception as exc:
                log.warning("post_call callback schedule failed: %s", exc)
        elif when:
            # No automation module — best-effort: just advance stage.
            conn.execute("UPDATE people SET stage='callback', last_activity_at=? WHERE id=?",
                         (now, person_id))
            actions.append(f"callback noted for {when:%a %m/%d %I:%M%p} (no automation)")

    # 3) DNC.
    if analysis.get("do_not_call_requested") is True or outcome == "do_not_call":
        reason = "ai_extracted_dnc"
        conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('person',?,?,?)",
                     (str(person_id), reason, now))
        for r in conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (person_id,)):
            conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                         (r["e164"], reason, now))
        conn.execute("UPDATE people SET stage='dnc', last_activity_at=? WHERE id=?", (now, person_id))
        if automation is not None:
            try:
                automation.cancel_pending(conn, person_id, reason)
            except Exception:
                pass
        actions.append("DNC suppression applied (AI detected explicit opt-out)")

    # 4) Not interested -> contacted, but don't suppress.
    if outcome == "not_interested" and analysis.get("do_not_call_requested") is not True:
        conn.execute("UPDATE people SET stage='contacted', last_activity_at=? WHERE id=?", (now, person_id))
        if automation is not None:
            try:
                automation.cancel_pending(conn, person_id, "not_interested")
            except Exception:
                pass
        actions.append("stage advanced to contacted")

    # 5) ALWAYS append the structured note + audit + persist analysis.
    note_body = _format_note(analysis)
    if note_body:
        _append_note(conn, person_id, note_body, actor)

    if transcript_id:
        try:
            conn.execute("UPDATE transcripts SET analysis_json=? WHERE id=?",
                         (json.dumps(analysis), transcript_id))
        except Exception:
            pass

    try:
        db.audit(conn, "person", person_id, "post_call_ai",
                 f"outcome={outcome} actions={len(actions)}", actor)
    except Exception:
        pass

    return actions


# ------------------------------------------------------------------ orchestrator

def process(transcript_id: int, person_id: int, call_attempt_id: Optional[int],
            transcript_text: str, context: dict, agent: str = "",
            db_connect=None) -> dict:
    """End-to-end: AI -> DB actions. Safe to call from a background thread.
    db_connect is an optional override so tests can inject a different connection
    factory; defaults to db.connect."""
    result = {"ok": False, "actions": [], "skipped": "", "outcome": ""}
    if not is_enabled():
        result["skipped"] = "ai_disabled"
        return result
    if not transcript_text:
        result["skipped"] = "empty_transcript"
        return result

    analysis = analyze(transcript_text, context)
    if analysis is None:
        result["skipped"] = "no_analysis"
        return result

    result["outcome"] = str(analysis.get("outcome") or "")
    result["analysis"] = analysis

    connect = db_connect or db.connect
    try:
        with connect() as conn:
            actions = apply_actions(conn, person_id, call_attempt_id, transcript_id,
                                    analysis, agent=agent)
            result["actions"] = actions
            result["ok"] = True
    except Exception as exc:
        log.exception("post_call DB write failed: %s", exc)
        result["skipped"] = f"db_error: {exc}"
    return result
