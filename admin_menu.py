"""
Command Center — Admin Menu.

A small interactive terminal that talks to the live Command Center on the
DigitalOcean droplet so Chris can do quick manual data overrides without
opening the SQLite database directly:

    * View recent calls
    * Undo / modify an appointment (return a lead to the active dialing pool)
    * Edit a lead's status, name, city, owner, score, disposition, notes
    * Manually mark a lead Do-Not-Call
    * Search for a lead by phone or name
    * Read a call transcript

Auth: reuses CC_AGENT_TOKEN (same secret the rest of /agent/* uses). The token
and base URL are read from environment first, then DEPLOY_SECRETS.txt as a
fallback so the menu just works after `python admin_menu.py` with no setup.

Run:  python admin_menu.py
"""
import json
import os
import sys
from datetime import datetime

import requests


# ------------------------------------------------------------------ config

DEFAULT_BASE_URL = "https://68.183.116.10.sslip.io"
SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DEPLOY_SECRETS.txt")


def _load_secrets_file(path=SECRETS_FILE):
    """Read KEY=VALUE pairs from DEPLOY_SECRETS.txt so the menu can run without
    extra setup. Environment variables always win over the file."""
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


_SECRETS = _load_secrets_file()


def _cfg(name, default=""):
    return os.getenv(name) or _SECRETS.get(name) or default


BASE_URL = _cfg("CC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
AGENT_TOKEN = _cfg("CC_AGENT_TOKEN", "")
ACTOR = _cfg("CC_ADMIN_ACTOR", os.getenv("USERNAME") or os.getenv("USER") or "admin_menu")


# ------------------------------------------------------------------ minimal terminal helpers

_COLORS = {"red": 31, "green": 32, "yellow": 33, "blue": 34, "magenta": 35, "cyan": 36, "gray": 90}


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def c(text, color="", bold=False):
    if os.getenv("NO_COLOR"):
        return str(text)
    prefix = ""
    if bold:
        prefix += "\033[1m"
    if color in _COLORS:
        prefix += f"\033[{_COLORS[color]}m"
    if not prefix:
        return str(text)
    return f"{prefix}{text}\033[0m"


def hr(width=64, color="gray"):
    print(c("-" * width, color))


def header(title):
    print()
    print(c("=" * 64, "cyan", True))
    print(c(f"  {title}", "cyan", True))
    print(c(f"  Target: {BASE_URL}", "gray"))
    print(c("=" * 64, "cyan", True))


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(c(f"{prompt}{suffix}: ", "yellow")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return raw or default


def confirm(prompt, danger=False):
    color = "red" if danger else "yellow"
    try:
        raw = input(c(f"{prompt} (type 'yes' to confirm): ", color)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return raw == "yes"


def pause():
    try:
        input(c("\nPress Enter to return to the menu...", "gray"))
    except (EOFError, KeyboardInterrupt):
        print()


# ------------------------------------------------------------------ HTTP

class CommandCenterError(Exception):
    pass


def _request(method, path, params=None, body=None):
    headers = {"Accept": "application/json", "X-Admin-Actor": ACTOR}
    if AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.request(method, url, params=params,
                                data=json.dumps(body) if body is not None else None,
                                headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise CommandCenterError(f"network error: {exc}") from exc
    if resp.status_code == 401:
        raise CommandCenterError("unauthorized — check CC_AGENT_TOKEN")
    if resp.status_code >= 400:
        msg = resp.text[:300] or f"HTTP {resp.status_code}"
        raise CommandCenterError(f"server error: {msg}")
    if not resp.text:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


# ------------------------------------------------------------------ formatting

def _fmt_when(ts):
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "")).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def _stage_color(stage):
    return {"appointment": "green", "callback": "cyan", "dnc": "red",
            "contacted": "blue", "new": "yellow", "voicemail": "magenta"}.get(stage or "", "gray")


def _print_lead_line(idx, lead):
    stage = lead.get("stage") or "?"
    print(f"  {idx:>2}. "
          f"{c(str(lead.get('id') or '?').rjust(5), 'gray')}  "
          f"{(lead.get('full_name') or '—'):<26.26} "
          f"{(lead.get('phone') or ''):<15} "
          f"{(lead.get('city') or ''):<14.14} "
          f"{c(stage, _stage_color(stage))}")


def _print_call_line(idx, call):
    name = (call.get("full_name") or "—")[:24]
    disp = call.get("disposition") or "?"
    disp_color = {"human_transferred": "green", "voicemail_dropped": "cyan",
                  "no_answer": "gray", "spam_blocked": "red",
                  "dead_number": "red", "false_bridge": "magenta"}.get(disp, "white")
    has_tx = "Y" if call.get("has_transcript") else " "
    print(f"  {idx:>2}. "
          f"{_fmt_when(call.get('dialed_at')):<12} "
          f"{name:<24} "
          f"{(call.get('phone') or ''):<15} "
          f"{c(disp.ljust(20), disp_color)} "
          f"{c(has_tx, 'cyan', True)}")


def _print_person_card(person):
    if not person:
        print(c("  (no person)", "gray"))
        return
    stage = person.get("stage")
    print()
    print(f"  ID:        {c(person.get('id'), 'cyan', True)}")
    print(f"  Name:      {person.get('full_name') or '—'}")
    print(f"  Phone:     {person.get('phone') or '—'}")
    print(f"  City:      {person.get('city') or '—'} / {person.get('county') or '—'}")
    print(f"  Birthday:  {person.get('birthday') or '—'}")
    print(f"  Email:     {person.get('email') or '—'}")
    print(f"  Owner:     {person.get('owner') or '—'}")
    print(f"  Source:    {person.get('source') or '—'}")
    print(f"  Score:     {person.get('lead_score')}")
    print(f"  Stage:     {c(stage, _stage_color(stage), True)}")
    print(f"  Last act.: {_fmt_when(person.get('last_activity_at'))}")
    if person.get("last_disposition"):
        print(f"  Last disp: {person['last_disposition']}")


# ------------------------------------------------------------------ menu actions

def action_recent_calls():
    header("Recent calls")
    try:
        n = ask("How many", "25")
        data = _request("GET", "/agent/admin/recent-calls", params={"limit": int(n or 25)})
    except (ValueError, CommandCenterError) as exc:
        print(c(f"  {exc}", "red"))
        return pause()

    calls = data.get("calls") or []
    if not calls:
        print(c("  (no calls yet)", "gray"))
        return pause()

    hr()
    print(f"  {'#':>2}  {'When':<12} {'Name':<24} {'Phone':<15} {'Disposition':<20} Tx")
    hr()
    for i, call in enumerate(calls, 1):
        _print_call_line(i, call)
    hr()
    pick = ask("Pick a row to open the lead (or 0 to go back)", "0")
    try:
        idx = int(pick)
    except ValueError:
        return
    if 1 <= idx <= len(calls):
        pid = calls[idx - 1].get("person_id")
        if pid:
            return show_lead_detail(pid)


def action_search():
    header("Search a lead")
    q = ask("Phone digits or name fragment")
    if not q:
        return
    try:
        data = _request("GET", "/agent/admin/lead/search", params={"q": q})
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red"))
        return pause()

    leads = data.get("leads") or []
    if not leads:
        print(c("  (no matches)", "gray"))
        return pause()
    hr()
    print(f"   #   {'ID':>5}  {'Name':<26} {'Phone':<15} {'City':<14} Stage")
    hr()
    for i, lead in enumerate(leads, 1):
        _print_lead_line(i, lead)
    hr()
    pick = ask("Pick a row to open (or 0 to go back)", "0")
    try:
        idx = int(pick)
    except ValueError:
        return
    if 1 <= idx <= len(leads):
        return show_lead_detail(leads[idx - 1]["id"])


def _lookup_lead_id(prompt="Lead ID (or 'p PHONE' to look up by phone)"):
    raw = ask(prompt)
    if not raw:
        return None
    if raw.lower().startswith("p ") or any(ch.isdigit() for ch in raw) and "+" in raw:
        q = raw[2:].strip() if raw.lower().startswith("p ") else raw
        try:
            data = _request("GET", "/agent/admin/lead/search", params={"q": q})
        except CommandCenterError as exc:
            print(c(f"  {exc}", "red"))
            return None
        leads = data.get("leads") or []
        if not leads:
            print(c("  (no matches)", "gray"))
            return None
        if len(leads) == 1:
            return leads[0]["id"]
        hr()
        for i, lead in enumerate(leads, 1):
            _print_lead_line(i, lead)
        hr()
        pick = ask("Pick a row", "1")
        try:
            return leads[int(pick) - 1]["id"]
        except (ValueError, IndexError):
            return None
    try:
        return int(raw)
    except ValueError:
        return None


def show_lead_detail(pid):
    header(f"Lead #{pid}")
    try:
        data = _request("GET", f"/agent/admin/lead/{pid}")
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red"))
        return pause()

    _print_person_card(data.get("person"))
    if data.get("is_suppressed"):
        print(c("  [SUPPRESSED — currently on DNC/opt-out]", "red", True))

    appts = data.get("appointments") or []
    if appts:
        print(c("\n  Appointments:", "yellow", True))
        for appt in appts:
            print(f"    #{appt['id']}  {_fmt_when(appt.get('scheduled_at'))}  "
                  f"{appt.get('status')}  ({appt.get('agent') or '—'})")

    calls = data.get("calls") or []
    if calls:
        print(c("\n  Recent calls:", "yellow", True))
        for call in calls[:10]:
            tx = c(" Tx", "cyan", True) if any(
                t.get("call_control_id") == call.get("call_control_id")
                for t in (data.get("transcripts") or [])) else ""
            print(f"    {_fmt_when(call.get('dialed_at'))}  "
                  f"{(call.get('disposition') or '—'):<22}  "
                  f"{call.get('agent') or '—'}{tx}")

    notes = data.get("notes") or []
    if notes:
        print(c("\n  Notes:", "yellow", True))
        for note in notes[:5]:
            print(f"    [{_fmt_when(note.get('created_at'))} {note.get('author')}] "
                  f"{(note.get('body') or '')[:100]}")

    transcripts = data.get("transcripts") or []
    if transcripts:
        print(c("\n  Transcripts:", "yellow", True))
        for t in transcripts:
            print(f"    #{t['id']}  {_fmt_when(t.get('created_at'))}  "
                  f"{int(t.get('duration_seconds') or 0)}s  "
                  f"\"{(t.get('preview') or '').strip()[:80]}...\"")

    print()
    print("  Actions: [e]dit  [u]ndo-appt  [d]nc  [t]ranscript-view  [enter] back")
    pick = ask("Choose").lower()
    if pick == "e":
        action_edit_lead(pid)
    elif pick == "u":
        do_undo_appointment(person_id=pid)
    elif pick == "d":
        do_mark_dnc(pid)
    elif pick == "t" and transcripts:
        tid_raw = ask("Transcript ID", str(transcripts[0]["id"]))
        try:
            show_transcript(int(tid_raw))
        except ValueError:
            print(c("  Invalid ID", "red"))


def action_undo_appointment():
    header("Undo an appointment")
    print("  Lookup by: Lead ID, or phone number (start with 'p ' e.g. p +13365551234),")
    print("  or paste a Telnyx call_control_id (start with 'c ').")
    raw = ask("Lookup")
    if not raw:
        return

    if raw.lower().startswith("c "):
        return do_undo_appointment(call_control_id=raw[2:].strip())
    pid = None
    if raw.lower().startswith("p "):
        q = raw[2:].strip()
        try:
            data = _request("GET", "/agent/admin/lead/search", params={"q": q})
            leads = data.get("leads") or []
            if not leads:
                print(c("  (no matches)", "gray")); return pause()
            pid = leads[0]["id"] if len(leads) == 1 else None
            if pid is None:
                hr()
                for i, lead in enumerate(leads, 1):
                    _print_lead_line(i, lead)
                hr()
                try:
                    pid = leads[int(ask("Pick a row", "1")) - 1]["id"]
                except (ValueError, IndexError):
                    return
        except CommandCenterError as exc:
            print(c(f"  {exc}", "red")); return pause()
    else:
        try:
            pid = int(raw)
        except ValueError:
            print(c("  Invalid input", "red"))
            return pause()

    do_undo_appointment(person_id=pid)


def do_undo_appointment(person_id=None, call_control_id=None):
    body = {}
    if person_id:
        body["person_id"] = person_id
    if call_control_id:
        body["call_control_id"] = call_control_id

    # Preview the lead first so the user sees what they're undoing.
    if person_id:
        try:
            data = _request("GET", f"/agent/admin/lead/{person_id}")
        except CommandCenterError as exc:
            print(c(f"  {exc}", "red")); return pause()
        _print_person_card(data.get("person"))
        active = [a for a in (data.get("appointments") or [])
                  if a.get("status") not in ("cancelled", "no_show")]
        if not active:
            print(c("  No active appointment on this lead.", "yellow"))
            return pause()
        print(c("\n  Will cancel these appointments:", "yellow", True))
        for appt in active[:1]:
            print(f"    #{appt['id']}  {_fmt_when(appt.get('scheduled_at'))}  ({appt.get('agent') or '—'})")

    if not confirm("Undo appointment and return lead to dialing pool?", danger=True):
        print(c("  Cancelled.", "gray"))
        return

    try:
        result = _request("POST", "/agent/admin/appointment/undo", body=body)
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red")); return pause()

    print(c("  ✓ Done.", "green", True))
    print(f"    Lead stage -> {c(result.get('new_stage'), 'cyan', True)}")
    print(f"    Cancelled appointment ids: {result.get('undone_appointments')}")
    pause()


def action_edit_lead(pid=None):
    if pid is None:
        header("Edit a lead")
        pid = _lookup_lead_id()
        if not pid:
            return

    try:
        data = _request("GET", f"/agent/admin/lead/{pid}")
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red")); return pause()
    person = data.get("person")
    if not person:
        print(c("  Lead not found.", "red")); return pause()

    _print_person_card(person)
    print()
    print(c("  Editable fields:", "yellow", True))
    print("    1) stage          (new/attempted/voicemail/contacted/callback/appointment/dnc)")
    print("    2) full_name")
    print("    3) city           4) county         5) email")
    print("    6) owner          7) lead_score (0-100)")
    print("    8) Log manual disposition (e.g. not_interested, callback_requested, no_answer)")
    print("    9) Add a note")
    print("    0) Cancel")
    pick = ask("Choose")
    body = {}
    if pick == "1":
        body["stage"] = ask("New stage")
    elif pick == "2":
        body["full_name"] = ask("Full name", person.get("full_name") or "")
    elif pick == "3":
        body["city"] = ask("City", person.get("city") or "")
    elif pick == "4":
        body["county"] = ask("County", person.get("county") or "")
    elif pick == "5":
        body["email"] = ask("Email", person.get("email") or "")
    elif pick == "6":
        body["owner"] = ask("Owner (chris/will)", person.get("owner") or "")
    elif pick == "7":
        body["lead_score"] = ask("Score 0-100", str(person.get("lead_score") or 50))
    elif pick == "8":
        body["disposition"] = ask("Disposition")
        body["note"] = ask("Optional note", "")
    elif pick == "9":
        body["note"] = ask("Note text")
    else:
        return

    if not any(body.values()):
        print(c("  Nothing to change.", "gray")); return pause()

    print(c("\n  Will apply:", "yellow"))
    for k, v in body.items():
        if v:
            print(f"    {k} -> {v}")
    if not confirm("Apply changes?"):
        print(c("  Cancelled.", "gray")); return

    try:
        # numeric coercion done server-side; we just send strings
        result = _request("POST", f"/agent/admin/lead/{pid}/update", body=body)
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red")); return pause()

    print(c("  ✓ Saved.", "green", True))
    print(c("  Changed:", "gray"), result.get("changed"))
    _print_person_card(result.get("person"))
    pause()


def do_mark_dnc(pid=None):
    if pid is None:
        header("Mark a lead Do-Not-Call")
        pid = _lookup_lead_id()
        if not pid:
            return

    try:
        data = _request("GET", f"/agent/admin/lead/{pid}")
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red")); return pause()
    person = data.get("person")
    if not person:
        print(c("  Lead not found.", "red")); return pause()
    _print_person_card(person)
    if data.get("is_suppressed"):
        print(c("  Already suppressed.", "yellow"))
        return pause()
    reason = ask("Reason", "manual_dnc")
    if not confirm(f"Suppress {person.get('full_name') or 'this lead'} and all their phones?", danger=True):
        print(c("  Cancelled.", "gray")); return
    try:
        _request("POST", f"/agent/admin/lead/{pid}/dnc", body={"reason": reason})
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red")); return pause()
    print(c("  ✓ Lead marked DNC and suppressed.", "green", True))
    pause()


def show_transcript(tid):
    header(f"Transcript #{tid}")
    try:
        data = _request("GET", f"/agent/admin/transcript/{tid}")
    except CommandCenterError as exc:
        print(c(f"  {exc}", "red")); return pause()
    t = data.get("transcript") or {}
    name = t.get("full_name") or "Unknown"
    print(f"  Lead:      {name}  ({t.get('city') or ''})")
    print(f"  Recorded:  {_fmt_when(t.get('created_at'))}")
    print(f"  Duration:  {int(t.get('duration_seconds') or 0)}s")
    print(f"  Language:  {t.get('language')}")
    print(f"  Call ID:   {t.get('call_control_id')}")
    hr()
    text = t.get("full_text") or "(no text)"
    for line in text.splitlines() or [text]:
        color = "cyan" if line.startswith("[Speaker A") else ("green" if line.startswith("[Speaker B") else "white")
        print(c(line, color))
    hr()
    pause()


def action_view_transcript():
    header("View transcript")
    tid = ask("Transcript ID (see /admin/recent-calls or a lead's detail page)")
    if not tid:
        return
    try:
        show_transcript(int(tid))
    except ValueError:
        print(c("  Invalid ID", "red"))
        pause()


# ------------------------------------------------------------------ main

def main_menu():
    while True:
        header("COMMAND CENTER — ADMIN MENU")
        print(f"  Actor: {c(ACTOR, 'cyan')}    Token: {c('set' if AGENT_TOKEN else 'NOT SET', 'green' if AGENT_TOKEN else 'red', True)}")
        print()
        print("    1) View recent calls")
        print("    2) Undo / modify an appointment")
        print("    3) Edit a lead (status, name, city, owner, score, disposition, note)")
        print("    4) Mark a lead Do-Not-Call")
        print("    5) Search a lead (by phone or name)")
        print("    6) View a call transcript")
        print("    0) Exit")
        hr()
        choice = ask("Choose")
        if choice == "1":
            action_recent_calls()
        elif choice == "2":
            action_undo_appointment()
        elif choice == "3":
            action_edit_lead()
        elif choice == "4":
            do_mark_dnc()
        elif choice == "5":
            action_search()
        elif choice == "6":
            action_view_transcript()
        elif choice == "0":
            print(c("\nGoodbye.\n", "gray"))
            return
        else:
            continue


def main():
    _enable_windows_ansi()
    if not AGENT_TOKEN:
        print(c("Warning: CC_AGENT_TOKEN is not set. Set it in the environment or DEPLOY_SECRETS.txt.",
                "yellow", True))
    if not BASE_URL:
        print(c("CC_BASE_URL is not set and no default available.", "red", True))
        sys.exit(1)
    try:
        main_menu()
    except KeyboardInterrupt:
        print(c("\nInterrupted.\n", "gray"))


if __name__ == "__main__":
    main()
