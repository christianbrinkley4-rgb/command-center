"""
OSCR Lead Ingestion Agent.

Runs as a long-lived daemon on the Command Center droplet. It supports two
connection strategies:

  A) API strategy: poll an OSCR JSON endpoint with a bearer token.
  B) Browser strategy: use Playwright with a persistent authenticated session.

Both strategies feed the same ingestion core: normalize -> DNC/geo/age checks ->
dedupe by OSCR id and phone -> insert as hot OSCR leads with high lead_score.

Credentials are read only from environment variables. Do not commit real values.
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

import db
import geo_policy

LOG = logging.getLogger("oscr_ingestion_agent")
STOP = False


OSCR_SCHEMA = """
CREATE TABLE IF NOT EXISTS oscr_ingestion_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oscr_seen_leads (
    oscr_id       TEXT PRIMARY KEY,
    phone         TEXT NOT NULL DEFAULT '',
    person_id     INTEGER,
    source        TEXT NOT NULL DEFAULT 'OSCR',
    first_seen_at TEXT NOT NULL,
    raw_json      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_oscr_seen_phone ON oscr_seen_leads(phone);
"""


@dataclass
class Config:
    strategy: str = "api"
    owner: str = "chris"
    default_source: str = "OSCR"
    default_score: int = 100
    poll_seconds: float = 10.0
    max_poll_seconds: float = 300.0
    request_timeout: float = 20.0
    geo_filter_enabled: bool = True
    target_birth_years: str = "1962"

    # API strategy
    api_url: str = ""
    api_token: str = ""
    api_cursor_param: str = "since"
    api_cursor_field: str = "next_cursor"
    api_leads_path: str = "leads"

    # Browser strategy
    login_url: str = ""
    dashboard_url: str = ""
    username: str = ""
    password: str = ""
    browser_profile_dir: str = "/opt/command_center/oscr_profile"
    username_selector: str = "input[name='username']"
    password_selector: str = "input[name='password']"
    submit_selector: str = "button[type='submit']"
    logged_in_selector: str = "body"
    lead_row_selector: str = "[data-lead-row]"
    browser_refresh_seconds: float = 15.0
    browser_headless: bool = True

    # Selector map for browser rows. Values are CSS selectors scoped to a row.
    browser_selectors: dict[str, str] = field(default_factory=dict)
    campaign_source_map: dict[str, str] = field(default_factory=dict)


@dataclass
class Lead:
    oscr_id: str = ""
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    county: str = ""
    birthday: str = ""
    email: str = ""
    campaign: str = ""
    source: str = ""
    lead_score: int = 100
    raw: dict[str, Any] = field(default_factory=dict)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_env(name: str, default: Any) -> Any:
    text = _env(name)
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        LOG.warning("Ignoring invalid JSON in %s: %s", name, exc)
        return default


def load_config() -> Config:
    default_selectors = {
        "oscr_id": "[data-field='oscr_id']",
        "name": "[data-field='name']",
        "phone": "[data-field='phone']",
        "address": "[data-field='address']",
        "city": "[data-field='city']",
        "county": "[data-field='county']",
        "birthday": "[data-field='birthday']",
        "email": "[data-field='email']",
        "campaign": "[data-field='campaign']",
    }
    return Config(
        strategy=(_env("OSCR_STRATEGY", "api") or "api").lower(),
        owner=_env("OSCR_OWNER", "chris"),
        default_source=_env("OSCR_DEFAULT_SOURCE", "OSCR"),
        default_score=_int(_env("OSCR_DEFAULT_SCORE", "100"), 100),
        poll_seconds=_float(_env("OSCR_POLL_SECONDS", "10"), 10),
        max_poll_seconds=_float(_env("OSCR_MAX_POLL_SECONDS", "300"), 300),
        request_timeout=_float(_env("OSCR_REQUEST_TIMEOUT_SECONDS", "20"), 20),
        geo_filter_enabled=geo_policy.filter_enabled("OSCR_GEO_FILTER_ENABLED", default=True),
        target_birth_years=_env("OSCR_TARGET_BIRTH_YEARS", _env("CC_TARGET_BIRTH_YEARS", "1962")),
        api_url=_env("OSCR_API_URL"),
        api_token=_env("OSCR_API_TOKEN"),
        api_cursor_param=_env("OSCR_API_CURSOR_PARAM", "since"),
        api_cursor_field=_env("OSCR_API_CURSOR_FIELD", "next_cursor"),
        api_leads_path=_env("OSCR_API_LEADS_PATH", "leads"),
        login_url=_env("OSCR_LOGIN_URL"),
        dashboard_url=_env("OSCR_DASHBOARD_URL"),
        username=_env("OSCR_USERNAME"),
        password=_env("OSCR_PASSWORD"),
        browser_profile_dir=_env("OSCR_BROWSER_PROFILE_DIR", "/opt/command_center/oscr_profile"),
        username_selector=_env("OSCR_USERNAME_SELECTOR", "input[name='username']"),
        password_selector=_env("OSCR_PASSWORD_SELECTOR", "input[name='password']"),
        submit_selector=_env("OSCR_SUBMIT_SELECTOR", "button[type='submit']"),
        logged_in_selector=_env("OSCR_LOGGED_IN_SELECTOR", "body"),
        lead_row_selector=_env("OSCR_LEAD_ROW_SELECTOR", "[data-lead-row]"),
        browser_refresh_seconds=_float(_env("OSCR_BROWSER_REFRESH_SECONDS", "15"), 15),
        browser_headless=_bool(_env("OSCR_BROWSER_HEADLESS", "true"), True),
        browser_selectors=_json_env("OSCR_BROWSER_SELECTORS_JSON", default_selectors),
        campaign_source_map=_json_env("OSCR_CAMPAIGN_SOURCE_MAP_JSON", {}),
    )


def setup_logging() -> None:
    level = _env("OSCR_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _stop(_signum, _frame) -> None:
    global STOP
    STOP = True
    LOG.info("Shutdown requested; finishing current poll.")


def ensure_schema() -> None:
    db.init_db()
    with db.connect() as conn:
        conn.executescript(OSCR_SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
        if "email" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN email TEXT DEFAULT ''")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def state_get(key: str, default: str = "") -> str:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM oscr_ingestion_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def state_set(key: str, value: str) -> None:
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO oscr_ingestion_state(key, value, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, str(value or ""), utc_now()),
        )


def pick(obj: dict[str, Any], *names: str) -> str:
    lowered = {str(k).lower().replace(" ", "_"): v for k, v in obj.items()}
    for name in names:
        key = name.lower().replace(" ", "_")
        value = lowered.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def nested_value(payload: Any, path: str) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def source_for(lead: Lead, config: Config) -> str:
    campaign = (lead.campaign or "").strip()
    if campaign and campaign in config.campaign_source_map:
        return config.campaign_source_map[campaign]
    if campaign:
        safe = "_".join(campaign.split())
        return f"{config.default_source}_{safe}"
    return lead.source or config.default_source


def normalize_api_lead(raw: dict[str, Any], config: Config) -> Lead:
    first = pick(raw, "first_name", "firstname", "first name")
    last = pick(raw, "last_name", "lastname", "last name")
    name = pick(raw, "name", "full_name", "fullname", "contact_name") or f"{first} {last}".strip()
    campaign = pick(raw, "campaign", "campaign_name", "tag", "lead_campaign")
    lead = Lead(
        oscr_id=pick(raw, "oscr_id", "id", "lead_id", "contact_id"),
        name=name,
        first_name=first,
        last_name=last,
        phone=db.normalize_phone(pick(raw, "phone", "phone_number", "mobile", "cell", "home_phone")),
        address=pick(raw, "address", "street_address", "home_address"),
        city=pick(raw, "city", "home_city", "town"),
        county=pick(raw, "county", "home_county"),
        birthday=pick(raw, "birthday", "birthdate", "date_of_birth", "dob"),
        email=pick(raw, "email", "email_address"),
        campaign=campaign,
        source=pick(raw, "source", "lead_source"),
        lead_score=_int(pick(raw, "lead_score", "score"), config.default_score),
        raw=raw,
    )
    lead.source = source_for(lead, config)
    if not lead.oscr_id and lead.phone:
        lead.oscr_id = f"phone:{lead.phone}"
    return lead


def lead_is_dnc(lead: Lead) -> bool:
    raw_text = json.dumps(lead.raw, default=str).lower()
    return any(token in raw_text for token in ["do_not_call", "do not call", "donotcall", "opt out", "opted_out"])


def should_skip_for_policy(lead: Lead, config: Config) -> tuple[bool, str]:
    if not lead.phone:
        return True, "missing_phone"
    if lead_is_dnc(lead):
        return True, "oscr_dnc"
    if lead.birthday:
        old = os.environ.get("CC_TARGET_BIRTH_YEARS", "")
        os.environ["CC_TARGET_BIRTH_YEARS"] = config.target_birth_years
        try:
            if not geo_policy.birthday_is_target(lead.birthday):
                return True, "ineligible_age"
        finally:
            if old:
                os.environ["CC_TARGET_BIRTH_YEARS"] = old
            else:
                os.environ.pop("CC_TARGET_BIRTH_YEARS", None)
    if config.geo_filter_enabled and not geo_policy.city_is_allowed(lead.city):
        return True, "outside_geo"
    return False, ""


def ingest_lead(lead: Lead, config: Config) -> tuple[str, int | None]:
    """Insert one lead using a short SQLite transaction.

    Returns (status, person_id). Status is one of:
    imported, duplicate_oscr_id, duplicate_phone, suppressed, skipped.
    """
    skip, reason = should_skip_for_policy(lead, config)
    now = utc_now()
    raw_json = json.dumps(lead.raw, default=str, ensure_ascii=False)

    with db.connect() as conn:
        if lead.oscr_id and conn.execute(
            "SELECT person_id FROM oscr_seen_leads WHERE oscr_id=?", (lead.oscr_id,)
        ).fetchone():
            return "duplicate_oscr_id", None

        if skip:
            if reason == "oscr_dnc" and lead.phone:
                conn.execute(
                    "INSERT OR IGNORE INTO suppressions(scope, value, reason, created_at) VALUES ('phone',?,?,?)",
                    (lead.phone, "oscr_dnc", now),
                )
            if lead.oscr_id:
                conn.execute(
                    """INSERT OR IGNORE INTO oscr_seen_leads(oscr_id, phone, person_id, source, first_seen_at, raw_json)
                       VALUES (?,?,?,?,?,?)""",
                    (lead.oscr_id, lead.phone, None, lead.source or config.default_source, now, raw_json),
                )
            db.audit(conn, "oscr", lead.oscr_id or lead.phone, "lead_skipped", reason)
            return f"skipped:{reason}", None

        if conn.execute(
            "SELECT 1 FROM suppressions WHERE scope='phone' AND value=? LIMIT 1", (lead.phone,)
        ).fetchone():
            return "suppressed", None

        existing_phone = conn.execute(
            "SELECT person_id FROM phone_numbers WHERE e164=? LIMIT 1", (lead.phone,)
        ).fetchone()
        if existing_phone:
            pid = existing_phone["person_id"]
            if lead.oscr_id:
                conn.execute(
                    """INSERT OR IGNORE INTO oscr_seen_leads(oscr_id, phone, person_id, source, first_seen_at, raw_json)
                       VALUES (?,?,?,?,?,?)""",
                    (lead.oscr_id, lead.phone, pid, lead.source, now, raw_json),
                )
            db.audit(conn, "oscr", lead.oscr_id or lead.phone, "lead_duplicate_phone", lead.phone)
            return "duplicate_phone", pid

        key = db.person_key(lead.name, lead.city, lead.birthday) or f"phone|{lead.phone}"
        row = conn.execute("SELECT id FROM people WHERE person_key=?", (key,)).fetchone()
        if row:
            pid = row["id"]
            conn.execute(
                "INSERT OR IGNORE INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                (pid, lead.phone, now),
            )
            conn.execute(
                """UPDATE people SET lead_score=MAX(lead_score, ?),
                   source=COALESCE(NULLIF(source,''), ?), last_activity_at=?
                   WHERE id=?""",
                (lead.lead_score, lead.source, now, pid),
            )
            status = "duplicate_person"
        else:
            parts = lead.name.split()
            cur = conn.execute(
                """INSERT INTO people(person_key, full_name, first_name, last_name, birthday,
                   city, county, address, email, source, lead_score, stage, owner,
                   first_seen_at, last_activity_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?)""",
                (
                    key,
                    lead.name,
                    lead.first_name or (parts[0] if parts else ""),
                    lead.last_name or (parts[-1] if len(parts) > 1 else ""),
                    lead.birthday,
                    lead.city,
                    lead.county,
                    lead.address,
                    lead.email,
                    lead.source,
                    lead.lead_score,
                    config.owner,
                    now,
                    now,
                ),
            )
            pid = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO phone_numbers(person_id, e164, created_at) VALUES (?,?,?)",
                (pid, lead.phone, now),
            )
            status = "imported"

        conn.execute("INSERT OR IGNORE INTO lead_sources(name, first_seen_at) VALUES (?,?)", (lead.source, now))
        conn.execute(
            "UPDATE lead_sources SET lead_count=(SELECT COUNT(*) FROM people WHERE source=?) WHERE name=?",
            (lead.source, lead.source),
        )
        if lead.oscr_id:
            conn.execute(
                """INSERT OR IGNORE INTO oscr_seen_leads(oscr_id, phone, person_id, source, first_seen_at, raw_json)
                   VALUES (?,?,?,?,?,?)""",
                (lead.oscr_id, lead.phone, pid, lead.source, now, raw_json),
            )
        conn.execute(
            """INSERT INTO person_notes(person_id, body, author, created_at)
               VALUES (?,?,?,?)""",
            (pid, f"Hot OSCR lead ingested from {lead.source}", "oscr-agent", now),
        )
        db.audit(conn, "oscr", lead.oscr_id or lead.phone, "lead_imported", f"person_id={pid} source={lead.source}")
    return status, pid


def ingest_many(raw_leads: Iterable[dict[str, Any]], config: Config) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw in raw_leads:
        try:
            lead = normalize_api_lead(raw, config)
            status, pid = ingest_lead(lead, config)
            counts[status] = counts.get(status, 0) + 1
            if status == "imported":
                LOG.info("Imported hot OSCR lead person_id=%s name=%s city=%s", pid, lead.name, lead.city)
        except Exception:
            counts["errors"] = counts.get("errors", 0) + 1
            LOG.exception("Failed to ingest one OSCR lead")
    return counts


class ApiEngine:
    def __init__(self, config: Config) -> None:
        if not config.api_url:
            raise ValueError("OSCR_API_URL is required for OSCR_STRATEGY=api")
        if not config.api_token:
            raise ValueError("OSCR_API_TOKEN is required for OSCR_STRATEGY=api")
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config.api_token}",
            "Accept": "application/json",
            "User-Agent": "CommandCenter-OSCR-Ingestion/1.0",
        })

    def poll_once(self) -> tuple[list[dict[str, Any]], str]:
        cursor = state_get("api_cursor")
        params = {}
        if cursor:
            params[self.config.api_cursor_param] = cursor
        resp = self.session.get(self.config.api_url, params=params, timeout=self.config.request_timeout)
        resp.raise_for_status()
        payload = resp.json()
        leads = nested_value(payload, self.config.api_leads_path)
        if isinstance(leads, dict):
            leads = list(leads.values())
        if not isinstance(leads, list):
            leads = payload if isinstance(payload, list) else []
        next_cursor = ""
        if isinstance(payload, dict):
            next_cursor = str(nested_value(payload, self.config.api_cursor_field) or "")
        return [x for x in leads if isinstance(x, dict)], next_cursor


class BrowserEngine:
    def __init__(self, config: Config) -> None:
        if not config.dashboard_url:
            raise ValueError("OSCR_DASHBOARD_URL is required for OSCR_STRATEGY=browser")
        self.config = config
        self.playwright = None
        self.context = None
        self.page = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Install browser support with: pip install playwright && playwright install chromium") from exc
        os.makedirs(self.config.browser_profile_dir, exist_ok=True)
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            self.config.browser_profile_dir,
            headless=self.config.browser_headless,
            viewport={"width": 1440, "height": 1000},
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            if self.playwright:
                self.playwright.stop()
        self.context = self.page = self.playwright = None

    def ensure_started(self) -> None:
        if self.page is None:
            self.start()

    def login_if_needed(self) -> None:
        assert self.page is not None
        page = self.page
        page.goto(self.config.dashboard_url, wait_until="domcontentloaded", timeout=60_000)
        login_form_visible = False
        try:
            login_form_visible = page.locator(self.config.username_selector).first.is_visible(timeout=1500)
        except Exception:
            login_form_visible = False
        if (
            not login_form_visible
            and page.locator(self.config.logged_in_selector).count()
            and (not self.config.login_url or self.config.login_url not in page.url)
        ):
            return
        if not self.config.username or not self.config.password:
            raise ValueError("OSCR_USERNAME and OSCR_PASSWORD are required for browser login")
        page.goto(self.config.login_url or self.config.dashboard_url, wait_until="domcontentloaded", timeout=60_000)
        page.fill(self.config.username_selector, self.config.username)
        page.fill(self.config.password_selector, self.config.password)
        page.click(self.config.submit_selector)
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.wait_for_selector(self.config.logged_in_selector, timeout=60_000)

    def text_in_row(self, row: Any, field: str) -> str:
        selector = self.config.browser_selectors.get(field, "")
        if not selector:
            return ""
        loc = row.locator(selector)
        if not loc.count():
            return ""
        return (loc.first.text_content() or "").strip()

    def poll_once(self) -> tuple[list[dict[str, Any]], str]:
        self.ensure_started()
        assert self.page is not None
        self.login_if_needed()
        page = self.page
        page.goto(self.config.dashboard_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector(self.config.lead_row_selector, timeout=10_000)
        except Exception:
            return [], ""
        rows = page.locator(self.config.lead_row_selector)
        leads = []
        for i in range(rows.count()):
            row = rows.nth(i)
            item = {field: self.text_in_row(row, field) for field in self.config.browser_selectors}
            if any(item.values()):
                leads.append(item)
        return leads, ""


def run_forever(config: Config) -> None:
    ensure_schema()
    if config.strategy == "api":
        engine: Any = ApiEngine(config)
        interval = config.poll_seconds
    elif config.strategy == "browser":
        engine = BrowserEngine(config)
        interval = config.browser_refresh_seconds
    else:
        raise ValueError("OSCR_STRATEGY must be api or browser")

    backoff = interval
    LOG.info("OSCR ingestion agent started strategy=%s owner=%s", config.strategy, config.owner)
    try:
        while not STOP:
            started = time.monotonic()
            try:
                leads, next_cursor = engine.poll_once()
                counts = ingest_many(leads, config)
                if next_cursor:
                    state_set("api_cursor", next_cursor)
                if leads or counts:
                    LOG.info("Poll complete fetched=%s counts=%s", len(leads), counts)
                backoff = interval
            except requests.Timeout:
                LOG.warning("OSCR poll timed out; backing off")
                backoff = min(config.max_poll_seconds, max(interval, backoff * 1.8))
            except requests.RequestException as exc:
                LOG.warning("OSCR network/API error: %s", exc)
                backoff = min(config.max_poll_seconds, max(interval, backoff * 1.8))
            except Exception:
                LOG.exception("OSCR poll failed")
                if config.strategy == "browser" and hasattr(engine, "close"):
                    try:
                        engine.close()
                    except Exception:
                        LOG.exception("Failed to close browser after error")
                backoff = min(config.max_poll_seconds, max(interval, backoff * 1.8))

            elapsed = time.monotonic() - started
            sleep_for = max(1.0, backoff - elapsed) + random.uniform(0, min(2.0, backoff * 0.1))
            for _ in range(int(sleep_for)):
                if STOP:
                    break
                time.sleep(1)
    finally:
        if hasattr(engine, "close"):
            engine.close()
        LOG.info("OSCR ingestion agent stopped")


def main() -> int:
    setup_logging()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    config = load_config()
    try:
        run_forever(config)
        return 0
    except Exception:
        LOG.exception("Fatal OSCR ingestion agent error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
