"""
Command Center — Telnyx Number Lookup pipeline.

Pre-dial data quality: for every phone we've never classified, hit Telnyx
`/v2/number_lookup`, store `line_type` + `carrier` + `portable` on
`phone_numbers`. Landlines and confirmed-disconnected numbers are then
excluded from `_build_ranked_queue` (gated by CC_LOOKUP_FILTER_ENABLED).

Cost discipline:
  * Each lookup is ~$0.004 (Telnyx v2 list price). We track a daily-spend
    estimate in `oscr_ingestion_state` and refuse to start a batch if it
    would exceed TELNYX_LOOKUP_DAILY_BUDGET_USD (default $10).
  * Per-lookup retry is bounded; transient errors leave the row pending so
    a future run picks it up.

Safe to invoke as a no-op when TELNYX_API_KEY is missing — bulk runner
exits early with an actionable message.

Surface:
  * `lookup_one(phone)` -> dict
  * `lookup_pending(limit, max_spend_usd)` -> summary dict
  * `bulk_classify_results(phone_lookups)` -> writes back to phone_numbers
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from typing import Optional

import requests

import db

log = logging.getLogger(__name__)


# ----------------------------------------------------- config

TELNYX_BASE = "https://api.telnyx.com/v2"
LOOKUP_COST_USD = float(os.getenv("TELNYX_LOOKUP_COST_USD", "0.004"))
DAILY_BUDGET_USD = float(os.getenv("TELNYX_LOOKUP_DAILY_BUDGET_USD", "10.0"))
DEFAULT_BATCH = int(os.getenv("TELNYX_LOOKUP_BATCH_DEFAULT", "100"))
HTTP_TIMEOUT = float(os.getenv("TELNYX_LOOKUP_TIMEOUT_SECONDS", "10.0"))


# Map Telnyx's terminology to our internal `line_type` values. We treat
# 'fixed_line' as 'landline' for clarity since the rest of the codebase uses
# that vocabulary.
LINE_TYPE_NORMALIZATION = {
    "mobile": "mobile",
    "landline": "landline",
    "fixed_line": "landline",
    "fixed line": "landline",
    "voip": "voip",
    "non_fixed_voip": "voip",
    "toll_free": "toll_free",
    "premium_rate": "premium",
    "personal_number": "personal",
    "pager": "pager",
    "uan": "uan",
    "shared_cost": "shared_cost",
    "satellite": "satellite",
    "unknown": "",
}


def filter_enabled() -> bool:
    raw = os.getenv("CC_LOOKUP_FILTER_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _api_key() -> str:
    return (os.getenv("TELNYX_API_KEY") or "").strip()


# ----------------------------------------------------- HTTP

class TelnyxError(Exception):
    pass


def _http_get(path: str, params=None) -> dict:
    api_key = _api_key()
    if not api_key:
        raise TelnyxError("TELNYX_API_KEY not set")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    resp = requests.get(f"{TELNYX_BASE}{path}", headers=headers,
                        params=params or {}, timeout=HTTP_TIMEOUT)
    if resp.status_code == 429:
        # Telnyx returns 429 with a Retry-After header on rate limit.
        retry = float(resp.headers.get("Retry-After", "1") or "1")
        raise TelnyxError(f"rate_limited (retry after {retry}s)")
    if resp.status_code >= 400:
        raise TelnyxError(f"http {resp.status_code}: {resp.text[:200]}")
    if not resp.text:
        return {}
    return resp.json()


def _parse_lookup_payload(payload: dict) -> dict:
    """Reshape Telnyx's v2 response into our normalized dict."""
    data = (payload.get("data") or {})
    raw_line_type = (
        data.get("portability", {}).get("line_type")
        or data.get("carrier", {}).get("type")
        or data.get("line_type")
        or ""
    )
    line_type = LINE_TYPE_NORMALIZATION.get(
        str(raw_line_type).strip().lower(),
        str(raw_line_type).strip().lower(),
    )
    carrier_name = (
        data.get("portability", {}).get("ocn_name")
        or data.get("carrier", {}).get("name")
        or ""
    )
    portable = bool((data.get("portability") or {}).get("portable"))
    return {
        "phone": data.get("phone_number") or "",
        "line_type": line_type,
        "carrier": carrier_name,
        "portable": portable,
        "raw": payload,
    }


def lookup_one(phone: str) -> dict:
    """Single-number lookup. Raises TelnyxError on failure."""
    phone = (phone or "").strip()
    if not phone:
        return {"phone": "", "line_type": "", "carrier": "", "portable": False, "raw": {}}
    # /v2/number_lookup/{phone}?type=carrier returns carrier + line info.
    path = f"/number_lookup/{phone}"
    return _parse_lookup_payload(_http_get(path, params={"type": "carrier"}))


# ----------------------------------------------------- daily-spend tracker

def _today_key() -> str:
    return f"lookup_spend_{date.today().isoformat()}"


def _today_spend(conn) -> float:
    key = _today_key()
    row = conn.execute(
        "SELECT value FROM oscr_ingestion_state WHERE key=?", (key,)).fetchone() if conn else None
    try:
        return float(row["value"]) if row else 0.0
    except Exception:
        return 0.0


def _add_spend(conn, dollars: float) -> float:
    key = _today_key()
    now = datetime.now().isoformat(timespec="seconds")
    current = _today_spend(conn)
    new_total = round(current + dollars, 4)
    conn.execute(
        """INSERT INTO oscr_ingestion_state(key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, str(new_total), now),
    )
    return new_total


def today_spend() -> float:
    """Public accessor for ops + the dashboard."""
    try:
        with db.connect() as conn:
            return _today_spend(conn)
    except Exception:
        return 0.0


# ----------------------------------------------------- write-back to phone_numbers

def _ensure_columns():
    """Add line_type if missing on legacy DBs. Idempotent."""
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(phone_numbers)")}
        if "line_type" not in cols:
            conn.execute("ALTER TABLE phone_numbers ADD COLUMN line_type TEXT DEFAULT ''")
        if "carrier" not in cols:
            conn.execute("ALTER TABLE phone_numbers ADD COLUMN carrier TEXT DEFAULT ''")
        # oscr_ingestion_state holds our daily-spend counter; created by the
        # OSCR agent's ensure_schema. Make sure it exists for stand-alone use.
        conn.execute("""CREATE TABLE IF NOT EXISTS oscr_ingestion_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""")


def _record_lookup(conn, phone: str, result: dict) -> None:
    line_type = (result.get("line_type") or "").strip()
    carrier = (result.get("carrier") or "").strip()
    if not line_type and not carrier:
        return
    # Mark deactivated/disconnected by status. Telnyx never returns 'disconnected'
    # as a line_type — disconnects show up via 404 on the number_lookup endpoint,
    # which the caller handles separately.
    status_update = ""
    if line_type:
        conn.execute(
            "UPDATE phone_numbers SET line_type=?, carrier=COALESCE(NULLIF(?,''), carrier) WHERE e164=?",
            (line_type, carrier, phone),
        )
        status_update = line_type
    else:
        conn.execute(
            "UPDATE phone_numbers SET carrier=? WHERE e164=?", (carrier, phone),
        )
    try:
        db.audit(conn, "phone", phone, "lookup", f"line_type={status_update or '-'} carrier={carrier}", "number_lookup")
    except Exception:
        pass


def _mark_disconnected(conn, phone: str) -> None:
    conn.execute("UPDATE phone_numbers SET status='deactivated' WHERE e164=?", (phone,))
    try:
        db.audit(conn, "phone", phone, "deactivated", "telnyx_lookup_404", "number_lookup")
    except Exception:
        pass


# ----------------------------------------------------- bulk runner

DEFAULT_SOURCE_PATTERNS = [p.strip() for p in os.getenv(
    "TELNYX_LOOKUP_SOURCE_PATTERNS", "T65_").split(",") if p.strip()]
DEFAULT_MIN_SCORE = int(os.getenv("TELNYX_LOOKUP_MIN_SCORE", "70"))
DEFAULT_DIALABLE_STAGES = ("new", "attempted", "voicemail", "callback")


def _pending_phones(conn, limit: int, min_score: int = DEFAULT_MIN_SCORE,
                    source_patterns=None, owner: str = "",
                    dialable_stages=DEFAULT_DIALABLE_STAGES,
                    never_called: bool = True,
                    queue_only: bool = True) -> list:
    """Return phones worth classifying. Defaults are intentionally narrow:

    * T65-source leads
    * lead_score >= 70 (priority)
    * stage in (new, attempted, voicemail, callback)
    * never called before (zero call_attempts)
    * passes the same geo whitelist + target-lead filter the dial queue uses

    so the Telnyx \$ is spent only on the leads we're about to dial — not on
    every random number that's ever entered the system.

    Override never_called=False / queue_only=False to widen the net.
    """
    source_patterns = source_patterns if source_patterns is not None else DEFAULT_SOURCE_PATTERNS

    where = [
        "(ph.line_type IS NULL OR ph.line_type='')",
        "COALESCE(ph.status,'active') NOT IN ('bad','deactivated')",
        "ph.e164 LIKE '+%'",
    ]
    params = []

    if min_score and min_score > 0:
        where.append("COALESCE(p.lead_score, 0) >= ?")
        params.append(int(min_score))

    if source_patterns:
        clauses = []
        for pattern in source_patterns:
            like = pattern if "%" in pattern else f"{pattern}%"
            clauses.append("p.source LIKE ?")
            params.append(like)
        where.append("(" + " OR ".join(clauses) + ")")

    if owner:
        where.append("p.owner=?")
        params.append(owner)

    if dialable_stages:
        placeholders = ",".join("?" * len(dialable_stages))
        where.append(f"p.stage IN ({placeholders})")
        params.extend(dialable_stages)

    # Suppressions: hard exclude
    where.append("NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.scope='person' AND s.value=CAST(p.id AS TEXT))")
    where.append("NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.scope='phone' AND s.value=ph.e164)")

    # Never-called: zero rows in call_attempts for this person AND this phone.
    if never_called:
        where.append("NOT EXISTS (SELECT 1 FROM call_attempts ca WHERE ca.person_id=p.id)")
        where.append("NOT EXISTS (SELECT 1 FROM call_attempts ca WHERE ca.phone=ph.e164)")

    sql = (
        "SELECT DISTINCT ph.e164, p.city, p.birthday, p.source FROM phone_numbers ph "
        "JOIN people p ON p.id=ph.person_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY p.lead_score DESC, ph.id ASC LIMIT ?"
    )
    params.append(int(limit) * 2 if queue_only else int(limit))  # over-fetch then filter
    rows = conn.execute(sql, params).fetchall()

    if not queue_only:
        return [r["e164"] for r in rows[:int(limit)]]

    # Queue-only: enforce geo whitelist + target-lead (T65 birth year) the same
    # way _build_ranked_queue does. These checks aren't pure SQL because
    # geo_policy uses city normalization + aliases.
    try:
        import geo_policy
        geo_on = geo_policy.filter_enabled("CC_GEO_FILTER_ENABLED", default=True)
    except Exception:
        geo_policy = None  # type: ignore
        geo_on = False

    filtered = []
    for row in rows:
        if geo_on and geo_policy is not None and not geo_policy.city_is_allowed(row["city"]):
            continue
        if geo_policy is not None and not geo_policy.birthday_is_target(row["birthday"]):
            continue
        try:
            import queue_source_policy
            if not queue_source_policy.dialer_queue_eligible(
                    row["source"] if "source" in row.keys() else "", row["birthday"]):
                continue
        except Exception:
            pass
        filtered.append(row["e164"])
        if len(filtered) >= int(limit):
            break
    return filtered


def lookup_pending(limit: Optional[int] = None,
                   max_spend_usd: Optional[float] = None,
                   sleep_between_seconds: float = 0.05,
                   min_score: Optional[int] = None,
                   source_patterns: Optional[list] = None,
                   owner: str = "",
                   dialable_stages: Optional[tuple] = None,
                   never_called: bool = True,
                   queue_only: bool = True) -> dict:
    """Find phones worth classifying and look them up.

    DEFAULTS ARE COST-CONSERVATIVE: only T65-source leads with lead_score >= 70
    that are still in the active dialing pool. This stops a bulk pass from
    spending $0.004 per row on leads we'd never call anyway.

    Override min_score=0 / source_patterns=[] to widen the net (e.g. when wiring
    OSCR up tomorrow, include 'OSCR' in source_patterns).

    Returns a summary {requested, processed, classified, deactivated, errors,
    spend_today_usd, stopped_for_budget, filters}.
    """
    _ensure_columns()
    limit = int(limit or DEFAULT_BATCH)
    budget = float(max_spend_usd if max_spend_usd is not None else DAILY_BUDGET_USD)
    if min_score is None:
        min_score = DEFAULT_MIN_SCORE
    if source_patterns is None:
        source_patterns = list(DEFAULT_SOURCE_PATTERNS)
    if dialable_stages is None:
        dialable_stages = DEFAULT_DIALABLE_STAGES

    summary = {
        "requested": 0, "processed": 0, "classified": 0, "deactivated": 0,
        "errors": 0, "spend_today_usd": 0.0, "stopped_for_budget": False,
        "messages": [],
        "filters": {
            "min_score": min_score,
            "source_patterns": list(source_patterns),
            "owner": owner,
            "dialable_stages": list(dialable_stages),
            "never_called": bool(never_called),
            "queue_only": bool(queue_only),
            "budget_usd": budget,
            "limit": limit,
        },
    }

    if not _api_key():
        summary["messages"].append("TELNYX_API_KEY not set; no lookups attempted")
        return summary

    with db.connect() as conn:
        phones = _pending_phones(conn, limit, min_score=min_score,
                                 source_patterns=source_patterns, owner=owner,
                                 dialable_stages=dialable_stages,
                                 never_called=never_called,
                                 queue_only=queue_only)
        summary["requested"] = len(phones)
        spend = _today_spend(conn)
        if spend >= budget:
            summary["spend_today_usd"] = spend
            summary["stopped_for_budget"] = True
            summary["messages"].append(f"already spent ${spend:.4f}; budget ${budget:.2f}")
            return summary

    for phone in phones:
        # Re-check budget every call so concurrent runs share the cap.
        with db.connect() as conn:
            spend = _today_spend(conn)
        if spend + LOOKUP_COST_USD > budget:
            summary["stopped_for_budget"] = True
            summary["messages"].append(f"would exceed ${budget:.2f} budget")
            break

        try:
            result = lookup_one(phone)
            with db.connect() as conn:
                _record_lookup(conn, phone, result)
                spend = _add_spend(conn, LOOKUP_COST_USD)
            if result.get("line_type"):
                summary["classified"] += 1
        except TelnyxError as exc:
            text = str(exc)
            # Telnyx returns 404 for disconnected / unallocated numbers — treat
            # that as a confident 'deactivated' verdict.
            if "404" in text or "not_found" in text.lower():
                with db.connect() as conn:
                    _mark_disconnected(conn, phone)
                    spend = _add_spend(conn, LOOKUP_COST_USD)
                summary["deactivated"] += 1
            else:
                summary["errors"] += 1
                log.warning("lookup error for %s: %s", phone, text)
        except Exception as exc:
            summary["errors"] += 1
            log.warning("lookup unexpected error for %s: %s", phone, exc)
        finally:
            summary["processed"] += 1
            if sleep_between_seconds > 0:
                time.sleep(sleep_between_seconds)

    with db.connect() as conn:
        summary["spend_today_usd"] = _today_spend(conn)
    return summary


# ----------------------------------------------------- queue exclusion helpers

def is_callable_line_type(line_type: str) -> bool:
    """Used by the queue builder when CC_LOOKUP_FILTER_ENABLED is on."""
    t = (line_type or "").strip().lower()
    if t in {"landline", "fixed_line"}:
        return False
    return True
