"""
Natural-language time parser for inbound replies.

Turns a prospect's text like:
    "call me 10am next wednesday"
    "tomorrow afternoon works"
    "how about Friday at 2:30pm"
    "call back in an hour"
    "monday morning"
into a concrete datetime, so the automation engine can drop a scheduled call on the
right day and time with no human involvement.

Deliberately dependency-free (no dateparser) so it runs anywhere. Conservative: if it
can't confidently find a time, it returns None and the caller falls back to a default.
Also classifies intent (callback / stop / not interested / yes) so a single inbound
reply can drive the whole next-action decision.
"""
import re
from datetime import datetime, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
PART_OF_DAY = {"morning": 10, "noon": 12, "afternoon": 14, "evening": 18, "night": 18, "tonight": 18}

STOP_WORDS = {"stop", "unsubscribe", "quit", "cancel", "remove", "optout", "opt-out", "end"}
NEG_WORDS = {"not interested", "no thanks", "no thank you", "leave me alone", "don't call", "do not call",
             "wrong number", "go away"}
YES_WORDS = {"yes", "sure", "ok", "okay", "sounds good", "please do", "go ahead", "yeah", "yep"}


def classify_intent(text):
    """Returns one of: stop, not_interested, callback, yes, unknown."""
    t = (text or "").strip().lower()
    if not t:
        return "unknown"
    if any(w == t or w in t.split() for w in STOP_WORDS) or t in STOP_WORDS:
        return "stop"
    if any(p in t for p in NEG_WORDS):
        return "not_interested"
    # a time/callback request implies callback intent
    if parse_when(text) is not None or any(k in t for k in ("call", "callback", "call back", "reach me", "talk")):
        return "callback"
    if any(t == y or t.startswith(y) for y in YES_WORDS):
        return "yes"
    return "unknown"


def _set_time(dt, hour, minute=0):
    return dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_when(text, now=None):
    """Best-effort parse of a datetime from free text. Returns a datetime or None."""
    if not text:
        return None
    now = now or datetime.now()
    t = text.lower()

    # explicit clock time, e.g. "10am", "2:30 pm", "at 9"
    hour = minute = None
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", t)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "p":
            hour += 12
        minute = int(m.group(2) or 0)
    else:
        m2 = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b", t)
        if m2:
            hour = int(m2.group(1))
            minute = int(m2.group(2) or 0)
            # assume daytime business hours if ambiguous (8 -> 8am, but 1-6 likely pm)
            if 1 <= hour <= 6:
                hour += 12

    # part of day, e.g. "afternoon" (check longer words first so "afternoon" doesn't match "noon")
    if hour is None:
        for word, h in sorted(PART_OF_DAY.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{word}\b", t):
                hour = h
                break

    base = None

    # relative: "in 2 hours", "in 30 minutes", "in an hour"
    rel = re.search(r"\bin\s+(an?|\d+)\s+(hour|hours|hr|hrs|minute|minutes|min|mins|day|days|week|weeks)\b", t)
    if rel:
        qty = 1 if rel.group(1) in ("a", "an") else int(rel.group(1))
        unit = rel.group(2)
        if unit.startswith(("hour", "hr")):
            return now + timedelta(hours=qty)
        if unit.startswith(("min",)):
            return now + timedelta(minutes=qty)
        if unit.startswith("day"):
            base = now + timedelta(days=qty)
        if unit.startswith("week"):
            base = now + timedelta(weeks=qty)

    # "today" / "tomorrow"
    if base is None:
        if "tomorrow" in t:
            base = now + timedelta(days=1)
        elif "today" in t or "tonight" in t:
            base = now

    # weekday, with optional "next"
    if base is None:
        for name, wd in WEEKDAYS.items():
            if re.search(rf"\b{name}\b", t):
                days_ahead = (wd - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7  # "monday" when today is monday -> next monday
                if "next" in t:
                    # "next wednesday" -> the wednesday of next week
                    if days_ahead <= 7 and days_ahead < 7 and ("next " + name) in t:
                        days_ahead += 7 if days_ahead < 7 else 0
                base = now + timedelta(days=days_ahead)
                break

    if base is None and hour is not None:
        # only a time given ("call me at 3") -> today if still in future, else tomorrow
        candidate = _set_time(now, hour, minute or 0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if base is None:
        return None

    if hour is None:
        hour, minute = 10, 0  # default to 10am when only a day is given
    return _set_time(base, hour, minute or 0)


def parse_reply(text, now=None):
    """One call for the inbound handler: returns {intent, when}."""
    return {"intent": classify_intent(text), "when": parse_when(text, now)}
