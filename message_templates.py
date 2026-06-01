"""
Command Center — Personalized message templates.

Approved, compliance-safe templates with smart merge fields. The automation engine
picks the right template for the situation and fills in live data from the person's
record + call history — so every text/email is personal without anyone writing it.

Why templates (not AI freestyle): for Medicare/insurance, compliance teams require
APPROVED wording. The system's job is to pick the right template and fill the right
data ("I called you yesterday"), not to improvise regulated content. You can edit any
template here and it instantly applies everywhere.

Merge fields available: {first}, {name}, {agent}, {city}, {county}, {when_called},
{day_of_week}, {agent_phone}. Missing fields degrade gracefully (e.g. no city -> the
clause is dropped).
"""
import re
from datetime import datetime

AGENT_NAMES = {"chris": "Christian", "will": "Will", "": "your agent"}
AGENT_SIGNOFF = {"chris": "Christian", "will": "Will", "": ""}


def relative_day(then_iso, now=None):
    """Turn a past timestamp into natural language relative to send time:
    'earlier today' / 'yesterday' / 'on Monday' / 'last week' / ''."""
    now = now or datetime.now()
    try:
        then = datetime.fromisoformat(str(then_iso))
    except (ValueError, TypeError):
        return ""
    days = (now.date() - then.date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "on " + then.strftime("%A")
    if days < 14:
        return "last week"
    return then.strftime("on %B %-d") if hasattr(then, "strftime") else ""


# Each template: a short SMS and an email (subject + body). {opt:...} clauses are
# dropped entirely if any field inside them is empty (graceful personalization).
TEMPLATES = {
    "post_voicemail_text": {
        "sms": "Hey {first}, {agent} here{opt: with Bankers Life}. I gave you a call {when_called} "
               "about your Medicare options{opt: there in {city}}. Happy to answer any questions whenever "
               "works for you. - {agent}",
    },
    "vm_followup": {  # alias used by the engine
        "sms": "Hey {first}, {agent} here. I tried you {when_called} about your upcoming Medicare options"
               "{opt: in {city}}. No rush at all - just reply here or give me a call back when it's convenient. - {agent}",
    },
    "day7_followup": {
        "sms": "Hi {first}, {agent} again - just following up from earlier this week on your Medicare "
               "questions. I'm local{opt: to {city}} and happy to help whenever you're ready. - {agent}",
    },
    "appt_reminder": {
        "sms": "Hi {first}, friendly reminder of our appointment {when_appt}. Looking forward to it! - {agent}",
    },
    "callback_confirm": {
        "sms": "Thanks {first}! I've got you down and will call you {when_appt}. Talk soon. - {agent}",
    },
    "nurture_email_1": {
        "email_subject": "Following up on your Medicare options, {first}",
        "email_body": (
            "Hi {first},\n\n"
            "This is {agent}{opt} with Bankers Life{/opt} - I reached out {when_called} about the Medicare "
            "options coming up for you{opt} in {city}{/opt}. I know there's a lot of information out there, "
            "so I wanted to make it easy: if you have any questions or want to go over what's available, "
            "just reply to this email or give me a call.\n\n"
            "No pressure at all - I'm here to help whenever the timing is right.\n\n"
            "Best,\n{agent}\n{agent_phone}"
        ),
    },
}


def _fill_opts(text, data):
    """Resolve {opt: ...} / {opt}...{/opt} clauses: keep them only if every {field}
    inside resolves to a non-empty value; otherwise drop the whole clause."""
    # brace form: {opt: with Bankers Life} or {opt: in {city}}
    def repl_brace(m):
        inner = m.group(1)
        fields = re.findall(r"\{(\w+)\}", inner)
        if all(data.get(f) for f in fields):
            return _fill_fields(inner, data)
        return ""
    text = re.sub(r"\{opt:([^{}]*(?:\{\w+\}[^{}]*)*)\}", repl_brace, text)

    # paired form: {opt} ... {/opt}
    def repl_pair(m):
        inner = m.group(1)
        fields = re.findall(r"\{(\w+)\}", inner)
        if all(data.get(f) for f in fields):
            return _fill_fields(inner, data)
        return ""
    text = re.sub(r"\{opt\}(.*?)\{/opt\}", repl_pair, text, flags=re.DOTALL)
    return text


def _fill_fields(text, data):
    def repl(m):
        return str(data.get(m.group(1), "") or "")
    return re.sub(r"\{(\w+)\}", repl, text)


def _cleanup(text):
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,!?])", r"\1", text)
    return text.strip()


def build_merge_data(person, last_call_at=None, appt_at=None, now=None):
    """Assemble the merge dictionary from a person record + context."""
    owner = (person.get("owner") or "").lower()
    first = (person.get("first_name") or (person.get("full_name") or "").split(" ")[0] or "there").strip()
    data = {
        "first": first,
        "name": person.get("full_name") or first,
        "agent": AGENT_NAMES.get(owner, AGENT_NAMES[""]),
        "city": person.get("city") or "",
        "county": person.get("county") or "",
        "agent_phone": person.get("agent_phone") or "",
        "when_called": relative_day(last_call_at, now) if last_call_at else "",
        "day_of_week": (now or datetime.now()).strftime("%A"),
        "when_appt": appt_at or "",
    }
    return data


def render(template_key, person, last_call_at=None, appt_at=None, channel="sms", now=None):
    """Return {channel, subject?, body} fully personalized, or None if no template."""
    tpl = TEMPLATES.get(template_key)
    if not tpl:
        return None
    data = build_merge_data(person, last_call_at, appt_at, now)
    if channel == "email":
        subject = _cleanup(_fill_fields(_fill_opts(tpl.get("email_subject", ""), data), data))
        body = _cleanup(_fill_fields(_fill_opts(tpl.get("email_body", ""), data), data))
        if not body:
            return None
        return {"channel": "email", "subject": subject, "body": body}
    raw = tpl.get("sms")
    if not raw:
        return None
    body = _cleanup(_fill_fields(_fill_opts(raw, data), data))
    return {"channel": "sms", "body": body}


def list_templates():
    return sorted(TEMPLATES.keys())
