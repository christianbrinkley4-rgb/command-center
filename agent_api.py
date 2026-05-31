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

agent_api = Blueprint("agent_api", __name__)

AGENT_TOKEN = os.getenv("CC_AGENT_TOKEN", "")
VALID_STAGES = {"new", "attempted", "voicemail", "contacted", "callback", "appointment", "dnc"}


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

    imported, duplicates, suppressed, person_ids = 0, 0, 0, []
    with db.connect() as conn:
        if source:
            conn.execute("INSERT OR IGNORE INTO lead_sources(name, first_seen_at) VALUES (?,?)", (source, _now()))
        for lead in leads:
            name = _clean(lead.get("name") or f"{_clean(lead.get('first_name'))} {_clean(lead.get('last_name'))}".strip())
            city = _clean(lead.get("city"))
            birthday = _clean(lead.get("birthday"))
            phone = db.normalize_phone(lead.get("phone") or lead.get("mobile") or lead.get("number"))
            key = db.person_key(name, city, birthday) or (f"phone|{phone}" if phone else "")
            if not key:
                continue

            # Never resurrect an opted-out / DNC number from a freshly pulled list.
            if phone and conn.execute(
                    "SELECT 1 FROM suppressions WHERE scope='phone' AND value=? LIMIT 1", (phone,)).fetchone():
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
                    (_clean(lead.get("email")), _clean(lead.get("address")), _clean(lead.get("county")), source, pid))
            else:
                parts = name.split()
                cur = conn.execute("""INSERT INTO people
                    (person_key, full_name, first_name, last_name, birthday, city, county, address,
                     email, source, owner, stage, lead_score, first_seen_at, last_activity_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new', ?, ?, ?)""",
                    (key, name, parts[0] if parts else "", parts[-1] if len(parts) > 1 else "",
                     birthday, city, _clean(lead.get("county")), _clean(lead.get("address")),
                     _clean(lead.get("email")), source, owner,
                     int(lead.get("lead_score") or 50), _now(), _now()))
                pid = cur.lastrowid
                imported += 1
            if phone:
                conn.execute("INSERT OR IGNORE INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                             (pid, phone, _now()))
            person_ids.append(pid)
        db.audit(conn, "import", source or "batch", "leads_imported",
                 f"+{imported} new, {duplicates} dup, {suppressed} suppressed")
        if source:
            conn.execute("UPDATE lead_sources SET lead_count=(SELECT COUNT(*) FROM people WHERE source=?) WHERE name=?",
                         (source, source))
    return jsonify({"imported": imported, "duplicates": duplicates,
                    "suppressed": suppressed, "person_ids": person_ids})


# ------------------------------------------------------------------ ensure schema extras

def _ensure_columns():
    """Add columns the agent API relies on (email on people, subject on messages),
    safely and idempotently — won't disturb existing data."""
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
        if "email" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN email TEXT DEFAULT ''")
        mcols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "subject" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN subject TEXT DEFAULT ''")


# ------------------------------------------------------------------ 2) call agent: queue / score / disposition

@agent_api.route("/agent/leads/call-queue")
def call_queue():
    limit = min(int(request.args.get("limit", 100)), 1000)
    owner = _clean(request.args.get("owner"))
    min_score = request.args.get("min_score")
    where = ["stage IN ('new','attempted','voicemail','callback')"]
    params = []
    if owner:
        where.append("owner=?"); params.append(owner)
    if min_score:
        where.append("lead_score>=?"); params.append(int(min_score))
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT p.id, p.full_name, p.city, p.county, p.birthday, p.source, p.owner,
                   p.lead_score, p.stage, p.last_activity_at,
                   (SELECT e164 FROM phone_numbers x WHERE x.person_id=p.id AND x.status='active' LIMIT 1) phone,
                   (SELECT COUNT(*) FROM call_attempts c WHERE c.person_id=p.id) call_count,
                   (SELECT disposition_category FROM call_attempts c WHERE c.person_id=p.id
                    ORDER BY dialed_at DESC LIMIT 1) last_outcome
            FROM people p
            WHERE {' AND '.join(where)}
            ORDER BY (stage='callback') DESC, lead_score DESC, last_activity_at ASC
            LIMIT ?""", params + [limit]).fetchall()
    return jsonify({"count": len(rows), "leads": [dict(r) for r in rows]})


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
