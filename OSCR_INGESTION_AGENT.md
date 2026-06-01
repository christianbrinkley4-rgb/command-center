# OSCR Lead Ingestion Agent

`oscr_ingestion_agent.py` is a 24/7 daemon for pulling fresh OSCR leads into the
Command Center database. Imported leads are tagged, de-duped, scored hot, and
immediately become eligible for the ranked dialer agenda.

## Strategy A: API Engine

Use this if OSCR exposes a JSON endpoint.

```bash
OSCR_STRATEGY=api
OSCR_API_URL=https://example.bankers/oscr/api/leads
OSCR_API_TOKEN=...
OSCR_API_LEADS_PATH=leads
OSCR_API_CURSOR_PARAM=since
OSCR_API_CURSOR_FIELD=next_cursor
OSCR_OWNER=chris
OSCR_DEFAULT_SOURCE=OSCR
OSCR_DEFAULT_SCORE=100
OSCR_POLL_SECONDS=10
```

The agent sends `Authorization: Bearer $OSCR_API_TOKEN`, stores the returned
cursor in `oscr_ingestion_state`, and skips duplicate OSCR IDs or phones.

## Strategy B: Browser Engine

Use this if there is no API. This is normal authenticated browser automation
with a persistent Playwright profile; it does not bypass MFA, CAPTCHA, or access
controls.

```bash
OSCR_STRATEGY=browser
OSCR_LOGIN_URL=https://bspn.bankers.com/oscr
OSCR_DASHBOARD_URL=https://bspn.bankers.com/oscr
OSCR_USERNAME=...
OSCR_PASSWORD=...
OSCR_BROWSER_PROFILE_DIR=/opt/command_center/oscr_profile
OSCR_BROWSER_HEADLESS=true
OSCR_LEAD_ROW_SELECTOR=[data-lead-row]
OSCR_BROWSER_SELECTORS_JSON={"oscr_id":"[data-field='oscr_id']","name":"[data-field='name']","phone":"[data-field='phone']","city":"[data-field='city']","birthday":"[data-field='birthday']","campaign":"[data-field='campaign']"}
```

After installing dependencies, run:

```bash
cd /opt/command_center
/opt/command_center/venv/bin/pip install -r requirements.txt
/opt/command_center/venv/bin/playwright install chromium
```

## Safe Secret Setup

Put real OSCR credentials in `/opt/command_center/.env.oscr` on the droplet.
Do not commit that file.

```bash
sudo install -m 600 -o root -g root /dev/null /opt/command_center/.env.oscr
sudo nano /opt/command_center/.env.oscr
```

Then install the service:

```bash
sudo cp /opt/command_center/oscr-ingestion-agent.service.example /etc/systemd/system/oscr-ingestion-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now oscr-ingestion-agent
sudo journalctl -u oscr-ingestion-agent -f
```

## What Gets Written

- `people`: new lead, `stage='new'`, `lead_score=100`, `source='OSCR...'`
- `phone_numbers`: normalized E.164 phone
- `lead_sources`: source rollup count
- `oscr_seen_leads`: OSCR ID/phone idempotency record
- `audit_log` and `person_notes`: traceability

The ranked queue already promotes `stage='new'` leads with `first_seen_at` in the
last 24 hours, so fresh OSCR leads move into the hot-fresh tier immediately.
