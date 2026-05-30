# Command Center — Database + Tracking Dashboard

The single source of truth and live operations dashboard for the dialer operation.
Tracks **every person, phone, call, outcome, note, and appointment** across all
agents (Chris + Will) on one screen — without touching the dialers.

## Run it
Double-click **`START_COMMAND_CENTER.cmd`** (or `python app.py`), then open:
```
http://localhost:5055
```
It opens your browser automatically.

## What it does
- **Overview** — live KPIs (leads, calls, live conversations, contact rate, appointments,
  callbacks, avg talk time, today's numbers), trend chart (14 days), outcome donut,
  calls-by-hour, pipeline funnel, per-agent scoreboard, and a live activity feed.
- **Lead Book** — every lead, searchable by name/phone/city, filterable by stage,
  sortable by score / activity / calls. One click opens the person.
- **Lead Timeline** — a single person's entire history: every call (with disposition,
  talk time, connect speed, AMD result, agent), plus notes, appointments, and messages.
- **Act on a lead** — set an appointment, add a note, mark a callback, or flag Do-Not-Call
  (which suppresses their numbers). All written to the database with an audit trail.

## How it stays live
A background sync reads each dialer's `dial_results.xlsx` every 30 seconds and folds
new calls into the database. **It only reads the dialer files — never writes them**, so
your calling is never affected. The dashboard auto-refreshes.

## Architecture
```
dialers' dial_results.xlsx ──(read-only sync, 30s)──► command_center.db (SQLite)
                                                              │
                                                       Flask API + dashboard
                                                       http://localhost:5055
```
- **db.py** — schema (people, phone_numbers, call_attempts, messages, appointments,
  suppressions, lead_sources, person_notes, audit_log) + identity/dedup helpers.
- **sync.py** — idempotent importer. Add a new agent by adding one line to `SOURCES`.
- **app.py** — Flask API + dashboard server + background sync.
- **templates/ + static/** — the single-page dashboard (vanilla JS + Chart.js, no build step).

## Adding a new agent
Edit `SOURCES` in `sync.py`:
```python
SOURCES = [
    ("chris", r"C:\dialer\MyDialer_TODAY_READY\MyDialer\dial_results.xlsx"),
    ("will",  r"...\Will_Dialer\dial_results.xlsx"),
    ("newagent", r"...\dial_results.xlsx"),   # <- one line
]
```

## Deploy to DigitalOcean (when ready — makes it reachable from anywhere, 24/7)
On the droplet (Ubuntu):
```bash
sudo apt update && sudo apt install -y python3-pip
pip install -r requirements.txt gunicorn
# serve it:
gunicorn -w 2 -b 0.0.0.0:5055 app:app
# (then point a subdomain like cc.ncwealthprotection.me at the droplet,
#  and add a login layer before exposing it publicly — see "Next" below)
```
The sync expects the dialer result files to be reachable on the droplet — easiest path
is to have the dialers (or a small uploader) drop `dial_results.xlsx` to the droplet, or
run the dialers' webhook + this dashboard on the same droplet.

## This is the foundation for what's next
- **Texting/Emailing** plugs into the `messages` table + outcome triggers (after A2P 10DLC).
- **Lead scoring / routing** reads/writes here.
- **OSCR auto-pull** lands leads that flow straight into this timeline.

## Roadmap (not built yet)
- Login/auth before public hosting
- SMS/email composer + automation
- Per-lead enrichment (line type, relatives) display
- Appointment calendar view + reminders
- Export to xlsx button
