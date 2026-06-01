"""
Command Center — Agent API.

A machine-facing API layer designed for AI agents to read from and write to the
shared lead database. Separate from the browser dashboard API on purpose: this is
the contract other agents build against.

Auth: every endpoint requires a bearer token in the Authorization header:
    Authorization: Bearer <CC_AGENT_TOKEN>
(Locally, if no token is set, auth is open so you can develop.)

The three core workflows this enables:
  1) Lead-puller agent   -> POST /agent/leads/import      (drop new leads under 'new')
  2) Call agent          -> GET  /agent/leads/call-queue  (read prioritized leads)
                         -> POST /agent/leads/score       (push best to the top)
                         -> POST /agent/leads/disposition (log a call outcome)
  3) Messaging agent     -> GET  /agent/leads/outreach-queue (who's due for email/text)
                         -> POST /agent/messages/log      (record a sent message)
                         -> POST /agent/leads/opt-out     (honor STOP / unsubscribe)

Discovery: GET /agent/manifest returns this whole contract as JSON so an agent can
self-configure. GET /agent/schema returns the field dictionary.
"""
import hmac
import os
from datetime import datetime

from flask import Blueprint, jsonify, request, Response

import db
import geo_policy

agent_api = Blueprint("agent_api", __name__)

AGENT_TOKEN = os.getenv("CC_AGENT_TOKEN", "")
VALID_STAGES = {"new", "attempted", "voicemail", "contacted", "callback", "appointment", "dnc"}
DNC_VALUES = {"DNC", "DO NOT CALL", "DONOTCALL", "DO-NOT-CALL", "DO NOT CONTACT", "OPT OUT", "OPTOUT", "STOP"}
DNC_NO_VALUES = {"", "NO", "N", "FALSE", "0"}
DNC_FIELD_NAMES = {"dnc", "do not call", "donotcall", "do not contact", "opt out", "optout"}
PHONE_FIELD_NAMES = {
    "phone", "phone number", "phonet", "homephone", "home phone",
    "mobile", "cell", "cell phone", "number", "telephone", "work phone",
}


# ------------------------------------------------------------------ auth

@agent_api.before_request
def _auth():
    if request.method == "OPTIONS":
        return
    if not AGENT_TOKEN:
        return  # local/dev: open
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not hmac.compare_digest(token, AGENT_TOKEN):
        return jsonify({"error": "unauthorized", "hint": "send Authorization: Bearer <CC_AGENT_TOKEN>"}), 401


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _clean(v):
    return "" if v is None else str(v).strip()


def _field_name(key):
    text = str(key or "").strip().lower().replace("_", " ")
    return " ".join(text.split())


def _lead_phones(lead):
    phone_fields = {p.replace(" ", "") for p in PHONE_FIELD_NAMES}
    phones, seen = [], set()
    for key, value in (lead or {}).items():
        name = _field_name(key)
        compact = name.replace(" ", "")
        if (
            name in PHONE_FIELD_NAMES
            or compact in phone_fields
            or "phone" in compact
            or "mobile" in compact
            or "cell" in compact
            or "telephone" in compact
        ):
            phone = db.normalize_phone(value)
            if phone and phone not in seen:
                phones.append(phone)
                seen.add(phone)
    return phones


def _lead_value(lead, *names):
    wanted = {_field_name(name) for name in names}
    for key, value in (lead or {}).items():
        if _field_name(key) in wanted:
            return _clean(value)
    return ""


def _lead_is_dnc(lead):
    for key, value in (lead or {}).items():
        name = _field_name(key)
        text = _clean(value).upper()
        if text in DNC_VALUES:
            return True
        if name in DNC_FIELD_NAMES and text not in DNC_NO_VALUES:
            return True
    return False


# ------------------------------------------------------------------ discovery

@agent_api.route("/agent/manifest")
def manifest():
    """Self-describing contract so any agent can learn the API without docs."""
    return jsonify({
        "service": "Command Center Agent API",
        "version": "1.0",
        "auth": "Authorization: Bearer <CC_AGENT_TOKEN>",
        "base_url_examples": ["https://68.183.116.10.sslip.io"],
        "concepts": {
            "person": "a unique human (deduped by name+city+birthday)",
            "stage": "new -> attempted -> voicemail -> contacted -> callback -> appointment / dnc",
            "lead_score": "0-100, higher = call sooner; set by the call/scoring agent",
            "source": "where the lead came from (T65_June, OSCR_Life, ...)",
            "owner": "which human agent owns it (chris / will)",
        },
        "endpoints": [
            {"method": "POST", "path": "/agent/leads/import",
             "purpose": "Lead-puller: add a batch of new leads (auto-dedup, stage=new).",
             "body": {"source": "T65_June2026", "owner": "chris (optional)",
                      "leads": [{"name": "Jane Doe", "phone": "3365550101", "city": "Greensboro",
                                 "county": "Guilford", "birthday": "1962-03-04", "address": "...",
                                 "email": "jane@x.com (optional)", "lead_type": "t65 (optional)"}]},
             "returns": {"imported": 0, "duplicates": 0, "person_ids": []}},
            {"method": "GET", "path": "/agent/leads/call-queue",
             "purpose": "Call agent: prioritized leads to dial (highest score first).",
             "query": {"limit": "default 100", "owner": "optional", "min_score": "optional"},
             "returns": {"leads": [{"id": 0, "full_name": "", "phone": "", "lead_score": 0, "stage": "", "source": ""}]}},
            {"method": "POST", "path": "/agent/leads/score",
             "purpose": "Call/scoring agent: set scores so the best leads rise to the top.",
             "body": {"scores": [{"person_id": 1, "lead_score": 95}, {"phone": "3365550101", "lead_score": 80}]},
             "returns": {"updated": 0}},
            {"method": "POST", "path": "/agent/leads/disposition",
             "purpose": "Call agent: log a call outcome and advance the lead's stage.",
             "body": {"person_id": 1, "disposition": "callback_requested", "agent": "chris",
                      "live_talk_seconds": 95, "note": "wants Tue AM"},
             "returns": {"ok": True}},
            {"method": "GET", "path": "/agent/leads/outreach-queue",
             "purpose": "Messaging agent: leads due for an email or text, with full context.",
             "query": {"channel": "email | sms", "stage": "optional", "source": "optional", "limit": "default 100"},
             "returns": {"leads": [{"id": 0, "full_name": "", "email": "", "phone": "",
                                    "source": "", "stage": "", "last_outcome": "", "context": {}}]}},
            {"method": "POST", "path": "/agent/messages/log",
             "purpose": "Messaging agent: record an email/text that was written & sent.",
             "body": {"person_id": 1, "channel": "email", "direction": "out",
                      "subject": "(email only)", "body": "Hi Jane ...", "status": "sent", "template_id": "t65_intro"},
             "returns": {"ok": True, "message_id": 0}},
            {"method": "POST", "path": "/agent/leads/opt-out",
             "purpose": "Honor STOP/unsubscribe: suppress the person from all outreach + mark DNC.",
             "body": {"person_id": 1, "channel": "sms | email | all", "reason": "opt_out"},
             "returns": {"ok": True}},
            {"method": "GET", "path": "/agent/leads/export",
             "purpose": "Bulk export of all people (optionally filtered) for any agent/analytics.",
             "query": {"stage": "optional", "source": "optional", "updated_since": "ISO ts optional", "limit": "default 1000"},
             "returns": {"count": 0, "people": []}},
            {"method": "POST", "path": "/agent/transcripts/ingest",
             "purpose": "Dialer -> brain: store the diarized text of a finished call. Idempotent on transcription_id.",
             "body": {"call_control_id": "telnyx id (UNIQUE on call_attempts)", "transcription_id": "telnyx id",
                      "recording_id": "telnyx id", "language": "en", "duration_seconds": 0,
                      "full_text": "[Speaker A 00:03] ...", "segments": [{"speaker": "Speaker A",
                      "channel": 0, "start": 0.0, "end": 1.2, "text": "..."}], "raw_payload": {}},
             "returns": {"ok": True, "matched": True, "transcript_id": 0, "person_id": 0, "call_attempt_id": 0}},
            {"method": "GET", "path": "/agent/admin/recent-calls",
             "purpose": "Admin menu: most recent calls with person/disposition context.",
             "query": {"limit": "default 25", "owner": "optional"},
             "returns": {"count": 0, "calls": []}},
            {"method": "GET", "path": "/agent/admin/lead/search",
             "purpose": "Admin menu: find a lead by phone digits or name fragment.",
             "query": {"q": "required", "limit": "default 25"}},
            {"method": "GET", "path": "/agent/admin/lead/{id}",
             "purpose": "Admin menu: full lead detail (phones, calls, appointments, transcripts, notes)."},
            {"method": "POST", "path": "/agent/admin/lead/{id}/update",
             "purpose": "Admin menu: edit lead fields, log a manual disposition, or append a note.",
             "body": {"stage": "callback (optional, must be a valid stage)", "full_name": "...",
                      "city": "...", "email": "...", "owner": "...", "lead_score": 0,
                      "disposition": "not_interested (optional manual log)",
                      "agent": "chris (optional)", "note": "free text (optional)"},
             "returns": {"ok": True, "changed": {}, "before": {}, "person": {}}},
            {"method": "POST", "path": "/agent/admin/appointment/undo",
             "purpose": "Admin menu: strip an appointment flag and put the lead back in the dialing pool.",
             "body": {"person_id": "or appointment_id or call_control_id or phone",
                      "new_stage": "optional override; default callback if ever contacted, else attempted"},
             "returns": {"ok": True, "person_id": 0, "undone_appointments": [], "new_stage": "callback"}},
            {"method": "POST", "path": "/agent/admin/lead/{id}/dnc",
             "purpose": "Admin menu: hard DNC — suppress the person and all their phones, stage=dnc.",
             "body": {"reason": "manual_dnc (default)"}, "returns": {"ok": True}},
            {"method": "GET", "path": "/agent/admin/transcript/{id}",
             "purpose": "Admin menu: fetch one transcript (full text + raw segments)."},
            {"method": "GET", "path": "/agent/schema", "purpose": "Field dictionary for people/messages."},
        ],
    })


@agent_api.route("/agent/schema")
def schema():
    return jsonify({
        "person_fields": {
            "id": "int (stable key)", "full_name": "str", "first_name": "str", "last_name": "str",
            "birthday": "str", "age": "int (derived)", "city": "str", "county": "str", "address": "str",
            "email": "str", "phone": "str (primary, E.164)", "phones": "list of E.164",
            "source": "str", "lead_score": "int 0-100", "stage": "enum", "owner": "str",
            "first_seen_at": "ISO", "last_activity_at": "ISO", "last_outcome": "category",
        },
        "stages": sorted(VALID_STAGES),
        "dispositions": sorted(db.DISPOSITION_CATEGORY.keys()),
        "disposition_categories": db.CATEGORY_LABELS,
    })


# ------------------------------------------------------------------ 1) lead-puller: import

@agent_api.route("/agent/leads/import", methods=["POST"])
def leads_import():
    data = request.get_json(silent=True) or {}
    source = _clean(data.get("source"))
    owner = _clean(data.get("owner"))
    leads = data.get("leads") or []
    if not isinstance(leads, list) or not leads:
        return jsonify({"error": "send a non-empty 'leads' array"}), 400

    imported, duplicates, suppressed, ineligible_age, person_ids = 0, 0, 0, 0, []
    with db.connect() as conn:
        if source:
            conn.execute("INSERT OR IGNORE INTO lead_sources(name, first_seen_at) VALUES (?,?)", (source, _now()))
        for lead in leads:
            first_name = _lead_value(lead, "first_name", "first name", "firstname")
            last_name = _lead_value(lead, "last_name", "last name", "lastname")
            name = _lead_value(lead, "name", "full name", "fullname") or f"{first_name} {last_name}".strip()
            city = _lead_value(lead, "city", "homecity", "home city", "town")
            birthday = _lead_value(lead, "birthday", "birthdate", "date of birth", "dob")
            county = _lead_value(lead, "county", "homecounty", "home county")
            address = _lead_value(lead, "address", "street address", "home address", "homestreet", "home street")
            email = _lead_value(lead, "email", "email address")
            phones = _lead_phones(lead)
            phone = phones[0] if phones else ""

            if _lead_is_dnc(lead):
                for p in phones:
                    conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                                 (p, "oscr_dnc", _now()))
                suppressed += 1
                continue
            if birthday and not geo_policy.birthday_is_target(birthday):
                ineligible_age += 1
                continue

            key = db.person_key(name, city, birthday) or (f"phone|{phone}" if phone else "")
            if not key:
                continue

            # Never resurrect an opted-out / DNC person from a freshly pulled list.
            if phones and any(conn.execute(
                    "SELECT 1 FROM suppressions WHERE scope='phone' AND value=? LIMIT 1", (p,)).fetchone()
                    for p in phones):
                suppressed += 1
                continue

            row = conn.execute("SELECT id FROM people WHERE person_key=?", (key,)).fetchone()
            if row:
                duplicates += 1
                pid = row["id"]
                # enrich missing fields without overwriting existing data
                conn.execute("""UPDATE people SET
                    email=COALESCE(NULLIF(email,''), ?), address=COALESCE(NULLIF(address,''), ?),
                    county=COALESCE(NULLIF(county,''), ?), source=COALESCE(NULLIF(source,''), ?)
                    WHERE id=?""",
                    (email, address, county, source, pid))
            else:
                parts = name.split()
                cur = conn.execute("""INSERT INTO people
                    (person_key, full_name, first_name, last_name, birthday, city, county, address,
                     email, source, owner, stage, lead_score, first_seen_at, last_activity_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new', ?, ?, ?)""",
                    (key, name, parts[0] if parts else "", parts[-1] if len(parts) > 1 else "",
                     birthday, city, county, address, email, source, owner,
                     int(_lead_value(lead, "lead_score", "lead score") or 50), _now(), _now()))
                pid = cur.lastrowid
                imported += 1
            for phone in phones:
                conn.execute("INSERT OR IGNORE INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                             (pid, phone, _now()))
            person_ids.append(pid)
        db.audit(conn, "import", source or "batch", "leads_imported",
                 f"+{imported} new, {duplicates} dup, {suppressed} suppressed, {ineligible_age} ineligible_age")
        if source:
            conn.execute("UPDATE lead_sources SET lead_count=(SELECT COUNT(*) FROM people WHERE source=?) WHERE name=?",
                         (source, source))
    return jsonify({"imported": imported, "duplicates": duplicates,
                    "suppressed": suppressed, "ineligible_age": ineligible_age,
                    "person_ids": person_ids})


# ------------------------------------------------------------------ ensure schema extras

def _ensure_columns():
    """Add columns the agent API relies on (email on people, subject on messages,
    analysis_json on transcripts), safely and idempotently — won't disturb existing data."""
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
        if "email" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN email TEXT DEFAULT ''")
        mcols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "subject" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN subject TEXT DEFAULT ''")
        # transcripts may not exist yet on a freshly initialized DB — init_db creates
        # it with analysis_json already in the schema below; this guard handles the
        # upgrade-an-old-DB case.
        try:
            tcols = {r["name"] for r in conn.execute("PRAGMA table_info(transcripts)")}
        except Exception:
            tcols = set()
        if tcols and "analysis_json" not in tcols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN analysis_json TEXT DEFAULT ''")


# ------------------------------------------------------------------ 2) call agent: queue / score / disposition

@agent_api.route("/agent/leads/call-queue")
def call_queue():
    limit = min(int(request.args.get("limit", 100)), 1000)
    fetch_limit = min(limit * 5, 5000) if geo_policy.filter_enabled("CC_GEO_FILTER_ENABLED", default=True) else limit
    owner = _clean(request.args.get("owner"))
    min_score = request.args.get("min_score")
    where = ["stage IN ('new','attempted','voicemail','callback')"]
    params = []
    if owner:
        where.append("owner=?"); params.append(owner)
    if min_score:
        where.append("lead_score>=?"); params.append(int(min_score))
    where.append("NOT EXISTS (SELECT 1 FROM suppressions sp WHERE sp.scope='person' AND sp.value=CAST(p.id AS TEXT))")
    where.append("""EXISTS (
        SELECT 1 FROM phone_numbers ph
        WHERE ph.person_id=p.id AND ph.status='active'
          AND NOT EXISTS (SELECT 1 FROM suppressions ss WHERE ss.scope='phone' AND ss.value=ph.e164)
    )""")
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT p.id, p.full_name, p.city, p.county, p.birthday, p.source, p.owner,
                   p.lead_score, p.stage, p.last_activity_at,
                   (SELECT e164 FROM phone_numbers x
                    WHERE x.person_id=p.id AND x.status='active'
                      AND NOT EXISTS (SELECT 1 FROM suppressions ss WHERE ss.scope='phone' AND ss.value=x.e164)
                    LIMIT 1) phone,
                   (SELECT COUNT(*) FROM call_attempts c WHERE c.person_id=p.id) call_count,
                   (SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id
                    ORDER BY dialed_at DESC LIMIT 1) last_outcome
            FROM people p
            WHERE {' AND '.join(where)}
            ORDER BY (stage='callback') DESC, lead_score DESC, last_activity_at ASC
            LIMIT ?""", params + [fetch_limit]).fetchall()
    leads = [dict(r) for r in rows]
    if geo_policy.filter_enabled("CC_GEO_FILTER_ENABLED", default=True):
        leads = [r for r in leads if geo_policy.city_is_allowed(r.get("city", ""))]
    leads = [r for r in leads if geo_policy.birthday_is_target(r.get("birthday", ""))]
    leads = leads[:limit]
    return jsonify({"count": len(leads), "leads": leads})


@agent_api.route("/agent/leads/score", methods=["POST"])
def leads_score():
    scores = (request.get_json(silent=True) or {}).get("scores") or []
    updated = 0
    with db.connect() as conn:
        for s in scores:
            val = int(s.get("lead_score", 0))
            if "person_id" in s:
                cur = conn.execute("UPDATE people SET lead_score=? WHERE id=?", (val, s["person_id"]))
            elif "phone" in s:
                e164 = db.normalize_phone(s["phone"])
                cur = conn.execute(
                    "UPDATE people SET lead_score=? WHERE id=(SELECT person_id FROM phone_numbers WHERE e164=?)",
                    (val, e164))
            else:
                continue
            updated += cur.rowcount
        db.audit(conn, "scoring", "batch", "scores_updated", f"{updated} leads")
    return jsonify({"updated": updated})


@agent_api.route("/agent/leads/disposition", methods=["POST"])
def leads_disposition():
    d = request.get_json(silent=True) or {}
    pid = d.get("person_id")
    disposition = _clean(d.get("disposition")).lower()
    if not pid or not disposition:
        return jsonify({"error": "person_id and disposition required"}), 400
    category = db.disposition_category(disposition)
    stage_map = {"live_conversation": "contacted", "callback": "callback", "appointment": "appointment",
                 "dnc": "dnc", "not_interested": "contacted", "voicemail": "voicemail",
                 "no_answer": "attempted", "bad_number": "attempted", "dropped": "attempted"}
    new_stage = stage_map.get(category, "attempted")
    with db.connect() as conn:
        phone = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? LIMIT 1", (pid,)).fetchone()
        conn.execute("""INSERT INTO call_attempts
            (person_id, phone, agent, dialed_at, disposition, disposition_category, live_talk_seconds, dedupe_key)
            VALUES (?,?,?,?,?,?,?,?)""",
            (pid, phone["e164"] if phone else "", _clean(d.get("agent")), _now(), disposition, category,
             d.get("live_talk_seconds"), f"agentapi|{pid}|{_now()}|{disposition}"))
        conn.execute("UPDATE people SET stage=?, last_activity_at=? WHERE id=?", (new_stage, _now(), pid))
        if _clean(d.get("note")):
            conn.execute("INSERT INTO person_notes(person_id, body, author, created_at) VALUES (?,?,?,?)",
                         (pid, _clean(d.get("note")), _clean(d.get("agent")) or "call-agent", _now()))
        if category == "dnc":
            for r in conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (pid,)):
                conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                             (r["e164"], "dnc", _now()))
        db.audit(conn, "person", pid, "disposition", disposition)

        # AUTOMATION: schedule the next best action for this outcome (the well-oiled machine).
        scheduled = []
        try:
            import automation
            owner_row = conn.execute("SELECT owner FROM people WHERE id=?", (pid,)).fetchone()
            owner = owner_row["owner"] if owner_row else ""
            if category in ("dnc",):
                automation.cancel_pending(conn, pid, "dnc")
            elif category == "contacted":
                automation.cancel_pending(conn, pid, "reached")  # human reached; stop auto-chase
            else:
                scheduled = automation.plan_next_action(conn, pid, disposition, category, owner)
        except Exception:
            pass
    return jsonify({"ok": True, "stage": new_stage, "category": category, "scheduled": scheduled})


@agent_api.route("/agent/inbound", methods=["POST"])
def inbound_reply():
    """A prospect replied (text / email / voicemail transcript). The machine reads the
    intent + any requested time and schedules the next action automatically — e.g.
    "call me 10am next Wednesday" -> a call task lands on that day/time; "STOP" -> opt-out.
    Body: {phone or email or person_id, channel: sms|email, text: "...", agent?}"""
    import automation
    import nlp_time
    d = request.get_json(silent=True) or {}
    text = _clean(d.get("text"))
    channel = _clean(d.get("channel")) or "sms"
    phone = db.normalize_phone(d.get("phone"))
    email = _clean(d.get("email")).lower()
    pid = d.get("person_id")

    with db.connect() as conn:
        if not pid and phone:
            r = conn.execute("SELECT person_id FROM phone_numbers WHERE e164=?", (phone,)).fetchone()
            pid = r["person_id"] if r else None
        if not pid and email:
            r = conn.execute("SELECT id FROM people WHERE lower(email)=?", (email,)).fetchone()
            pid = r["id"] if r else None
        if not pid:
            return jsonify({"error": "could not match a person (send person_id, phone, or email)"}), 404

        # record the inbound message
        conn.execute("""INSERT INTO messages(person_id, channel, direction, body, status, created_at, replied_at)
                        VALUES (?,?, 'in', ?, 'received', ?, ?)""",
                     (pid, channel, text, _now(), _now()))

        parsed = nlp_time.parse_reply(text)
        intent, when = parsed["intent"], parsed["when"]
        result = {"intent": intent, "person_id": pid, "scheduled": None}

        if intent == "stop" or intent == "not_interested":
            reason = "opt_out" if intent == "stop" else "not_interested"
            conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('person',?,?,?)",
                         (str(pid), reason, _now()))
            for r in conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (pid,)):
                conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                             (r["e164"], reason, _now()))
            conn.execute("UPDATE people SET stage='dnc', last_activity_at=? WHERE id=?", (_now(), pid))
            automation.cancel_pending(conn, pid, reason)
            db.audit(conn, "person", pid, "inbound_optout", text[:80])
        elif when is not None or intent in ("callback", "yes"):
            # schedule the requested (or default) callback time as a real call task
            from datetime import datetime, timedelta
            call_dt = when or (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
            owner_row = conn.execute("SELECT owner FROM people WHERE id=?", (pid,)).fetchone()
            owner = owner_row["owner"] if owner_row else ""
            automation.cancel_pending(conn, pid, "replaced_by_callback")
            tid = automation.schedule_task(conn, pid, "call", call_dt.isoformat(timespec="seconds"),
                                           "inbound_callback_request", _phone_for(conn, pid), "", owner,
                                           dedupe_key=f"{pid}|call|inbound|{call_dt:%Y%m%d%H%M}")
            conn.execute("UPDATE people SET stage='callback', last_activity_at=? WHERE id=?", (_now(), pid))
            conn.execute("INSERT INTO person_notes(person_id, body, author, created_at) VALUES (?,?,?,?)",
                         (pid, f"Inbound reply: \"{text}\" → call scheduled {call_dt:%a %m/%d %I:%M%p}",
                          "automation", _now()))
            result["scheduled"] = {"kind": "call", "due_at": call_dt.isoformat(timespec="seconds")}
            db.audit(conn, "person", pid, "inbound_callback", f"{call_dt:%Y-%m-%d %H:%M}")
    return jsonify({"ok": True, **result})


def _phone_for(conn, pid):
    r = conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=? LIMIT 1", (pid,)).fetchone()
    return r["e164"] if r else ""


# ------------------------------------------------------------------ 3) messaging agent

@agent_api.route("/agent/leads/outreach-queue")
def outreach_queue():
    """Leads due for outreach, with the context a messaging agent needs to write a
    personalized message. Excludes anyone suppressed/opted-out on that channel."""
    channel = _clean(request.args.get("channel")) or "email"
    stage = _clean(request.args.get("stage"))
    source = _clean(request.args.get("source"))
    limit = min(int(request.args.get("limit", 100)), 1000)

    where = ["p.stage NOT IN ('dnc')"]
    params = []
    if channel == "email":
        where.append("p.email != ''")
    if stage:
        where.append("p.stage=?"); params.append(stage)
    if source:
        where.append("p.source=?"); params.append(source)

    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT p.id, p.full_name, p.first_name, p.city, p.county, p.birthday, p.email,
                   p.source, p.stage, p.lead_score, p.owner,
                   (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id AND x.status='active' LIMIT 1) phone,
                   (SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id
                    ORDER BY dialed_at DESC LIMIT 1) last_outcome,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id=p.id AND m.channel=? AND m.direction='out') sent_count,
                   (SELECT MAX(created_at) FROM messages m WHERE m.person_id=p.id AND m.channel=?) last_message_at
            FROM people p
            WHERE {' AND '.join(where)}
            ORDER BY p.lead_score DESC, p.last_activity_at DESC
            LIMIT ?""", [channel, channel] + params + [limit]).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            # suppression check for this channel
            sup = conn.execute(
                "SELECT 1 FROM suppressions WHERE scope='person' AND value=? AND reason IN ('opt_out','dnc') LIMIT 1",
                (str(r["id"]),)).fetchone()
            if sup:
                continue
            d["context"] = {
                "first_name": r["first_name"], "city": r["city"], "county": r["county"],
                "source": r["source"], "stage": r["stage"], "last_call_outcome": r["last_outcome"],
                "times_messaged": r["sent_count"], "last_message_at": r["last_message_at"],
            }
            out.append(d)
    return jsonify({"channel": channel, "count": len(out), "leads": out})


@agent_api.route("/agent/messages/log", methods=["POST"])
def messages_log():
    d = request.get_json(silent=True) or {}
    pid = d.get("person_id")
    if not pid:
        return jsonify({"error": "person_id required"}), 400
    with db.connect() as conn:
        cur = conn.execute("""INSERT INTO messages
            (person_id, channel, direction, subject, body, status, template_id, created_at, sent_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (pid, _clean(d.get("channel")) or "email", _clean(d.get("direction")) or "out",
             _clean(d.get("subject")), _clean(d.get("body")), _clean(d.get("status")) or "sent",
             _clean(d.get("template_id")), _now(), _now()))
        db.audit(conn, "person", pid, "message_logged", f"{d.get('channel')} {d.get('template_id','')}")
    return jsonify({"ok": True, "message_id": cur.lastrowid})


@agent_api.route("/agent/leads/opt-out", methods=["POST"])
def leads_opt_out():
    """Honor STOP/unsubscribe. Accepts person_id OR phone OR email — because a STOP
    text arrives from a phone number and an unsubscribe from an email, not an id."""
    d = request.get_json(silent=True) or {}
    pid = d.get("person_id")
    phone = db.normalize_phone(d.get("phone"))
    email = _clean(d.get("email")).lower()
    reason = _clean(d.get("reason")) or "opt_out"
    with db.connect() as conn:
        if not pid and phone:
            row = conn.execute("SELECT person_id FROM phone_numbers WHERE e164=?", (phone,)).fetchone()
            pid = row["person_id"] if row else None
        if not pid and email:
            row = conn.execute("SELECT id FROM people WHERE lower(email)=?", (email,)).fetchone()
            pid = row["id"] if row else None
        if not pid:
            # Still record the phone-level suppression so we never text it, even if
            # we can't match a person (e.g. STOP from a number not in our DB yet).
            if phone:
                conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                             (phone, reason, _now()))
                db.audit(conn, "phone", phone, "opted_out_unmatched", reason)
                return jsonify({"ok": True, "matched_person": False})
            return jsonify({"error": "person_id, phone, or email required"}), 400
        conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('person',?,?,?)",
                     (str(pid), reason, _now()))
        for r in conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (pid,)):
            conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                         (r["e164"], reason, _now()))
        conn.execute("UPDATE people SET stage='dnc', last_activity_at=? WHERE id=?", (_now(), pid))
        db.audit(conn, "person", pid, "opted_out", reason)
    return jsonify({"ok": True, "matched_person": True, "person_id": pid})


# ------------------------------------------------------------------ transcripts

@agent_api.route("/agent/transcripts/ingest", methods=["POST"])
def transcripts_ingest():
    """Receive a completed Telnyx transcription from the dialer.

    The dialer fires this when `call.recording.transcription.saved` arrives.
    We join on call_control_id (UNIQUE on call_attempts) to find the lead, but
    we still store the transcript even if the join misses (so it can be
    back-filled later by re-syncing dial_results.xlsx).
    """
    import json as _json
    d = request.get_json(silent=True) or {}
    call_control_id = _clean(d.get("call_control_id"))
    transcription_id = _clean(d.get("transcription_id"))
    recording_id = _clean(d.get("recording_id"))
    language = _clean(d.get("language")) or "en"
    full_text = _clean(d.get("full_text"))
    segments = d.get("segments") or []
    raw_payload = d.get("raw_payload") or {}
    try:
        duration_seconds = float(d.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        duration_seconds = 0.0

    if not call_control_id and not transcription_id:
        return jsonify({"error": "call_control_id or transcription_id required"}), 400

    with db.connect() as conn:
        # Idempotency: same transcription_id arriving twice should be a no-op.
        if transcription_id:
            existing = conn.execute(
                "SELECT id, person_id, call_attempt_id FROM transcripts WHERE transcription_id=?",
                (transcription_id,),
            ).fetchone()
            if existing:
                return jsonify({
                    "ok": True,
                    "matched": existing["person_id"] is not None,
                    "transcript_id": existing["id"],
                    "person_id": existing["person_id"],
                    "call_attempt_id": existing["call_attempt_id"],
                    "duplicate": True,
                })

        person_id, call_attempt_id = None, None
        if call_control_id:
            row = conn.execute(
                "SELECT id, person_id FROM call_attempts WHERE call_control_id=?",
                (call_control_id,),
            ).fetchone()
            if row:
                call_attempt_id = row["id"]
                person_id = row["person_id"]

        cur = conn.execute("""INSERT INTO transcripts
            (person_id, call_attempt_id, call_control_id, recording_id,
             transcription_id, source, language, duration_seconds,
             full_text, segments_json, raw_payload, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (person_id, call_attempt_id, call_control_id, recording_id,
             transcription_id or None, "telnyx", language, duration_seconds,
             full_text, _json.dumps(segments), _json.dumps(raw_payload), _now()))
        transcript_id = cur.lastrowid

        context = {}
        if person_id:
            conn.execute("UPDATE people SET last_activity_at=? WHERE id=?", (_now(), person_id))
            db.audit(conn, "person", person_id, "transcript_ingested",
                     f"transcript_id={transcript_id} call={call_control_id[-12:]}")
            # Snapshot the bits post-call analysis needs while we still hold the lock.
            row = conn.execute(
                "SELECT full_name, city FROM people WHERE id=?", (person_id,)).fetchone()
            ca = conn.execute(
                "SELECT agent, disposition, dialed_at FROM call_attempts WHERE id=?",
                (call_attempt_id,)).fetchone() if call_attempt_id else None
            if row:
                context = {
                    "full_name": row["full_name"],
                    "city": row["city"],
                    "agent": ca["agent"] if ca else "",
                    "prior_disposition": ca["disposition"] if ca else "",
                    "dialed_at": ca["dialed_at"] if ca else "",
                }
        else:
            db.audit(conn, "transcript", transcript_id, "unmatched_call",
                     f"call_control_id={call_control_id}")

    post_call_queued = False
    if person_id and full_text:
        try:
            import post_call
            if post_call.is_enabled():
                from threading import Thread
                Thread(
                    target=post_call.process,
                    args=(transcript_id, person_id, call_attempt_id, full_text, context),
                    kwargs={"agent": context.get("agent", "")},
                    daemon=True,
                ).start()
                post_call_queued = True
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "matched": person_id is not None,
        "transcript_id": transcript_id,
        "person_id": person_id,
        "call_attempt_id": call_attempt_id,
        "post_call_queued": post_call_queued,
    })


# ------------------------------------------------------------------ admin (manual overrides used by the terminal menu)
#
# Same blueprint -> same bearer-token auth. Every write logs to audit_log with
# the actor from the X-Admin-Actor header (default 'admin_menu') so manual
# overrides are traceable.

ADMIN_VALID_STAGES = VALID_STAGES | {"voicemail"}


def _actor():
    return _clean(request.headers.get("X-Admin-Actor")) or "admin_menu"


def _resolve_person_id(conn, payload):
    """Look up a person by any of person_id / phone / call_control_id."""
    pid = payload.get("person_id")
    if pid:
        return int(pid)
    phone = db.normalize_phone(payload.get("phone"))
    if phone:
        row = conn.execute("SELECT person_id FROM phone_numbers WHERE e164=?", (phone,)).fetchone()
        if row:
            return row["person_id"]
    call_control_id = _clean(payload.get("call_control_id"))
    if call_control_id:
        row = conn.execute("SELECT person_id FROM call_attempts WHERE call_control_id=?",
                           (call_control_id,)).fetchone()
        if row:
            return row["person_id"]
    return None


def _person_summary_row(conn, pid):
    """Single-row person view used by every admin response."""
    row = conn.execute("""
        SELECT p.id, p.full_name, p.first_name, p.last_name, p.city, p.county,
               p.birthday, p.email, p.source, p.owner, p.stage, p.lead_score,
               p.last_activity_at,
               (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id AND x.status='active' LIMIT 1) phone,
               (SELECT disposition FROM call_attempts c WHERE c.person_id=p.id
                ORDER BY dialed_at DESC LIMIT 1) last_disposition
        FROM people p WHERE p.id=?""", (pid,)).fetchone()
    return dict(row) if row else None


@agent_api.route("/agent/admin/recent-calls")
def admin_recent_calls():
    """Last N calls across all agents — fuel for the menu's 'pick a call' view."""
    limit = min(int(request.args.get("limit", 25)), 200)
    owner = _clean(request.args.get("owner"))
    where, params = [], []
    if owner:
        where.append("c.agent=?")
        params.append(owner)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT c.id call_attempt_id, c.call_control_id, c.dialed_at, c.disposition,
                   c.disposition_category, c.agent, c.phone, c.live_talk_seconds,
                   c.person_id, p.full_name, p.city, p.stage,
                   (SELECT 1 FROM transcripts t WHERE t.call_control_id=c.call_control_id LIMIT 1) has_transcript
            FROM call_attempts c
            LEFT JOIN people p ON p.id=c.person_id
            {clause}
            ORDER BY c.dialed_at DESC
            LIMIT ?""", params + [limit]).fetchall()
    return jsonify({"count": len(rows), "calls": [dict(r) for r in rows]})


@agent_api.route("/agent/admin/lead/search")
def admin_lead_search():
    """Find a lead by phone (digits anywhere) or name fragment."""
    q = _clean(request.args.get("q"))
    if not q:
        return jsonify({"error": "q is required"}), 400
    limit = min(int(request.args.get("limit", 25)), 200)
    digits = "".join(ch for ch in q if ch.isdigit())
    with db.connect() as conn:
        results = []
        if digits and len(digits) >= 4:
            tail = digits[-10:]
            rows = conn.execute("""
                SELECT DISTINCT p.id, p.full_name, p.city, p.stage, p.owner, p.lead_score,
                                ph.e164 phone
                FROM phone_numbers ph
                JOIN people p ON p.id=ph.person_id
                WHERE REPLACE(REPLACE(REPLACE(ph.e164,'+',''),'-',''),' ','') LIKE ?
                ORDER BY p.last_activity_at DESC
                LIMIT ?""", (f"%{tail}%", limit)).fetchall()
            results.extend(dict(r) for r in rows)
        if len(results) < limit:
            rows = conn.execute("""
                SELECT p.id, p.full_name, p.city, p.stage, p.owner, p.lead_score,
                       (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id LIMIT 1) phone
                FROM people p
                WHERE lower(p.full_name) LIKE ?
                ORDER BY p.last_activity_at DESC
                LIMIT ?""", (f"%{q.lower()}%", limit - len(results))).fetchall()
            seen = {r["id"] for r in results}
            for r in rows:
                d = dict(r)
                if d["id"] not in seen:
                    results.append(d)
    return jsonify({"count": len(results), "leads": results})


@agent_api.route("/agent/admin/lead/<int:pid>")
def admin_lead_detail(pid):
    with db.connect() as conn:
        person = _person_summary_row(conn, pid)
        if not person:
            return jsonify({"error": "lead not found", "person_id": pid}), 404
        phones = [dict(r) for r in conn.execute(
            "SELECT e164, line_type, status FROM phone_numbers WHERE person_id=?", (pid,))]
        calls = [dict(r) for r in conn.execute("""
            SELECT id, call_control_id, dialed_at, disposition, disposition_category,
                   live_talk_seconds, agent, phone
            FROM call_attempts WHERE person_id=? ORDER BY dialed_at DESC LIMIT 25""", (pid,))]
        appts = [dict(r) for r in conn.execute("""
            SELECT id, scheduled_at, status, agent, notes, created_at
            FROM appointments WHERE person_id=? ORDER BY scheduled_at DESC""", (pid,))]
        notes = [dict(r) for r in conn.execute("""
            SELECT id, body, author, created_at FROM person_notes
            WHERE person_id=? ORDER BY created_at DESC LIMIT 20""", (pid,))]
        transcripts = [dict(r) for r in conn.execute("""
            SELECT id, call_control_id, language, duration_seconds, created_at,
                   substr(full_text, 1, 300) preview
            FROM transcripts WHERE person_id=? ORDER BY created_at DESC LIMIT 10""", (pid,))]
        suppressed = conn.execute("""
            SELECT 1 FROM suppressions WHERE scope='person' AND value=? LIMIT 1""", (str(pid),)).fetchone()
    return jsonify({
        "person": person,
        "phones": phones,
        "calls": calls,
        "appointments": appts,
        "notes": notes,
        "transcripts": transcripts,
        "is_suppressed": bool(suppressed),
    })


@agent_api.route("/agent/admin/transcript/<int:tid>")
def admin_transcript_detail(tid):
    with db.connect() as conn:
        row = conn.execute("""
            SELECT t.*, p.full_name, p.city
            FROM transcripts t LEFT JOIN people p ON p.id=t.person_id
            WHERE t.id=?""", (tid,)).fetchone()
    if not row:
        return jsonify({"error": "transcript not found"}), 404
    return jsonify({"transcript": dict(row)})


_EDITABLE_FIELDS = {
    "stage", "full_name", "first_name", "last_name", "city", "county",
    "address", "email", "owner", "notes", "lead_score", "birthday", "source",
}


@agent_api.route("/agent/admin/lead/<int:pid>/update", methods=["POST"])
def admin_lead_update(pid):
    d = request.get_json(silent=True) or {}
    with db.connect() as conn:
        before = _person_summary_row(conn, pid)
        if not before:
            return jsonify({"error": "lead not found", "person_id": pid}), 404

        sets, params, changed = [], [], {}
        for field, value in d.items():
            if field not in _EDITABLE_FIELDS:
                continue
            value = _clean(value) if not isinstance(value, (int, float)) else value
            if field == "stage" and value and value not in ADMIN_VALID_STAGES:
                return jsonify({"error": f"invalid stage '{value}'",
                                "valid_stages": sorted(ADMIN_VALID_STAGES)}), 400
            if field == "lead_score":
                try:
                    value = max(0, min(100, int(value)))
                except (TypeError, ValueError):
                    return jsonify({"error": "lead_score must be 0-100"}), 400
            sets.append(f"{field}=?")
            params.append(value)
            changed[field] = value

        # Optional manual disposition: lets the menu log a corrected outcome
        # (e.g. mark a call 'not_interested' that was misclassified).
        disposition = _clean(d.get("disposition")).lower()
        if disposition:
            category = db.disposition_category(disposition)
            conn.execute("""INSERT INTO call_attempts
                (person_id, agent, dialed_at, disposition, disposition_category, dedupe_key)
                VALUES (?,?,?,?,?,?)""",
                (pid, _clean(d.get("agent")) or _actor(), _now(), disposition, category,
                 f"admin|{pid}|{_now()}|{disposition}"))
            changed["manual_disposition"] = disposition

        if not sets and not disposition:
            return jsonify({"ok": True, "changed": {}, "person": before}), 200

        if sets:
            sets.append("last_activity_at=?"); params.append(_now())
            params.append(pid)
            conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE id=?", params)

        if _clean(d.get("note")):
            conn.execute("""INSERT INTO person_notes(person_id, body, author, created_at)
                            VALUES (?,?,?,?)""",
                         (pid, _clean(d["note"]), _actor(), _now()))
            changed["note"] = _clean(d["note"])

        db.audit(conn, "person", pid, "admin_update",
                 ", ".join(f"{k}={v!r}" for k, v in changed.items()), _actor())
        after = _person_summary_row(conn, pid)

    return jsonify({"ok": True, "changed": changed, "before": before, "person": after})


@agent_api.route("/agent/admin/appointment/undo", methods=["POST"])
def admin_appointment_undo():
    """Undo an appointment flag and return the lead to the active dialing pool.

    Accepts {person_id} (undoes latest scheduled appt for that lead) OR
    {appointment_id} (specific appointment) OR {call_control_id} (resolves to
    the call's owning lead). Sets stage based on the lead's prior history so
    they fall back to the right tier of the ranked queue."""
    d = request.get_json(silent=True) or {}
    appointment_id = d.get("appointment_id")
    new_stage_override = _clean(d.get("new_stage")).lower()
    undone = []

    with db.connect() as conn:
        if appointment_id:
            row = conn.execute("SELECT id, person_id FROM appointments WHERE id=?",
                               (appointment_id,)).fetchone()
            if not row:
                return jsonify({"error": "appointment not found"}), 404
            pid = row["person_id"]
            target_ids = [row["id"]]
        else:
            pid = _resolve_person_id(conn, d)
            if not pid:
                return jsonify({"error": "send person_id, phone, call_control_id, or appointment_id"}), 400
            rows = conn.execute("""SELECT id FROM appointments
                                   WHERE person_id=? AND status NOT IN ('cancelled','no_show')
                                   ORDER BY scheduled_at DESC LIMIT 1""", (pid,)).fetchall()
            target_ids = [r["id"] for r in rows]
            if not target_ids:
                return jsonify({"error": "no active appointment found for that lead",
                                "person_id": pid}), 404

        # Decide where the lead should land. If they've ever had a live conversation,
        # 'callback' (top tier of the active queue). Otherwise fall back to 'attempted'.
        if new_stage_override and new_stage_override in ADMIN_VALID_STAGES:
            new_stage = new_stage_override
        else:
            had_contact = conn.execute("""SELECT 1 FROM call_attempts
                WHERE person_id=? AND disposition_category IN ('live_conversation','callback') LIMIT 1""",
                (pid,)).fetchone()
            new_stage = "callback" if had_contact else "attempted"

        for aid in target_ids:
            conn.execute("""UPDATE appointments
                            SET status='cancelled',
                                notes=COALESCE(NULLIF(notes,''),'') || ?
                            WHERE id=?""",
                         (f"\n[{_now()}] {_actor()}: undone via admin menu", aid))
            undone.append(aid)

        conn.execute("UPDATE people SET stage=?, last_activity_at=? WHERE id=?",
                     (new_stage, _now(), pid))
        conn.execute("""INSERT INTO person_notes(person_id, body, author, created_at)
                        VALUES (?,?,?,?)""",
                     (pid, f"Appointment undone via admin menu -> stage={new_stage}",
                      _actor(), _now()))

        # Cancel any pending appointment-reminder tasks the automation queued.
        try:
            import automation
            automation.cancel_pending(conn, pid, "appointment_undone")
        except Exception:
            pass

        db.audit(conn, "person", pid, "appointment_undone",
                 f"appointments={undone} new_stage={new_stage}", _actor())
        after = _person_summary_row(conn, pid)

    return jsonify({
        "ok": True,
        "person_id": pid,
        "undone_appointments": undone,
        "new_stage": new_stage,
        "person": after,
    })


@agent_api.route("/agent/admin/lead/<int:pid>/dnc", methods=["POST"])
def admin_lead_dnc(pid):
    """Manual DNC: suppresses the person and all their phones, sets stage='dnc',
    and cancels any pending automation tasks."""
    d = request.get_json(silent=True) or {}
    reason = _clean(d.get("reason")) or "manual_dnc"
    with db.connect() as conn:
        person = _person_summary_row(conn, pid)
        if not person:
            return jsonify({"error": "lead not found"}), 404

        conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('person',?,?,?)",
                     (str(pid), reason, _now()))
        for r in conn.execute("SELECT e164 FROM phone_numbers WHERE person_id=?", (pid,)):
            conn.execute("INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                         (r["e164"], reason, _now()))
        conn.execute("UPDATE people SET stage='dnc', last_activity_at=? WHERE id=?", (_now(), pid))
        conn.execute("""INSERT INTO person_notes(person_id, body, author, created_at)
                        VALUES (?,?,?,?)""",
                     (pid, f"Manually marked DNC ({reason})", _actor(), _now()))
        try:
            import automation
            automation.cancel_pending(conn, pid, reason)
        except Exception:
            pass
        db.audit(conn, "person", pid, "manual_dnc", reason, _actor())
    return jsonify({"ok": True, "person_id": pid, "reason": reason})


# ------------------------------------------------------------------ bulk export

@agent_api.route("/agent/leads/export")
def leads_export():
    stage = _clean(request.args.get("stage"))
    source = _clean(request.args.get("source"))
    updated_since = _clean(request.args.get("updated_since"))
    limit = min(int(request.args.get("limit", 1000)), 10000)
    where, params = [], []
    if stage:
        where.append("stage=?"); params.append(stage)
    if source:
        where.append("source=?"); params.append(source)
    if updated_since:
        where.append("last_activity_at>=?"); params.append(updated_since)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT p.*,
                   (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id LIMIT 1) phone
            FROM people p {clause}
            ORDER BY p.lead_score DESC LIMIT ?""", params + [limit]).fetchall()
    return jsonify({"count": len(rows), "people": [dict(r) for r in rows]})
