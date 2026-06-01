"""
Command Center — outbound senders (Telnyx SMS + email).

Fully built, but GATED OFF by default until you're ready (and A2P 10DLC is approved
for SMS). With senders off, the worker still renders + records every message as
'queued' so you can preview exactly what would go out — nothing is sent.

Turn on per channel via env (set on the droplet, never committed):
    SMS:    CC_SMS_ENABLED=1   + TELNYX_API_KEY, TELNYX_SMS_FROM (your DID), [TELNYX_MESSAGING_PROFILE_ID]
    EMAIL:  CC_EMAIL_ENABLED=1 + GMAIL_USER, GMAIL_APP_PASSWORD   (or SMTP_* for a domain sender)

Design: send from the SAME local DID that called the prospect when we can, so the
text feels like one person, not a robocaller.
"""
import os
import smtplib
from email.mime.text import MIMEText

import message_templates as mt


def sms_enabled():
    return os.getenv("CC_SMS_ENABLED", "0") in ("1", "true", "yes")


def email_enabled():
    return os.getenv("CC_EMAIL_ENABLED", "0") in ("1", "true", "yes")


# ---------------------------------------------------------------- SMS (Telnyx)

def send_sms(to_number, body, from_number=""):
    """Send an SMS via Telnyx. Raises on failure so the worker records it as not-sent."""
    if not sms_enabled():
        raise RuntimeError("SMS disabled (set CC_SMS_ENABLED=1 after A2P 10DLC approval)")
    import requests
    api_key = os.getenv("TELNYX_API_KEY", "")
    frm = from_number or os.getenv("TELNYX_SMS_FROM", "")
    if not api_key or not frm:
        raise RuntimeError("TELNYX_API_KEY / TELNYX_SMS_FROM not set")
    payload = {"from": frm, "to": to_number, "text": body}
    mp = os.getenv("TELNYX_MESSAGING_PROFILE_ID", "")
    if mp:
        payload["messaging_profile_id"] = mp
    r = requests.post("https://api.telnyx.com/v2/messages",
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                      json=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Telnyx SMS {r.status_code}: {r.text[:200]}")
    return r.json().get("data", {}).get("id", "")


# ---------------------------------------------------------------- Email (SMTP)

def send_email(to_email, subject, body, from_email=""):
    if not email_enabled():
        raise RuntimeError("Email disabled (set CC_EMAIL_ENABLED=1)")
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", os.getenv("GMAIL_USER", ""))
    pw = os.getenv("SMTP_PASSWORD", os.getenv("GMAIL_APP_PASSWORD", ""))
    sender = from_email or os.getenv("CC_EMAIL_FROM", user)
    if not user or not pw:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD (or GMAIL_*) not set")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(sender, [to_email], msg.as_string())
    return "sent"


# ---------------------------------------------------------------- render helper

def render_message(person, template_key, channel, last_call_at=None, appt_at=None):
    """Render the personalized message for this person/template. Returns dict or None."""
    return mt.render(template_key, person, last_call_at=last_call_at, appt_at=appt_at, channel=channel)
