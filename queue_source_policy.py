"""Dialer queue eligibility by lead source and cohort.

Only T65 leads and leads imported directly from OSCR belong in the dialer queue.
The December 1957 bulk lists (birth year 1957) are excluded.
"""
import os
import re
from datetime import datetime

# December 1957 OSCR birthday exports (T65_Dec_NC / T65_Dec_VA).
DECEMBER_1957_BIRTH_YEAR = 1957
DECEMBER_1957_SOURCE_PREFIXES = ("T65_Dec_",)

# Original dialer import tag for true T65 (1962) contacts.
LEGACY_T65_SOURCES = frozenset({"Dialer_Existing"})


def _normalize_source(source):
    return re.sub(r"\s+", " ", str(source or "").strip())


def birth_year(birthday):
    text = str(birthday or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().year
        except (TypeError, ValueError):
            continue
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return int(match.group(0)) if match else None


def is_december_1957_cohort(source="", birthday=""):
    """True for the December-1957 OSCR birthday bulk lists."""
    src = _normalize_source(source)
    year = birth_year(birthday)
    if year == DECEMBER_1957_BIRTH_YEAR:
        return True
    # A T65_Dec_* source without a parseable birthday is treated conservatively,
    # but a real non-1957 birthday can still pass through as a valid T65 row.
    if year is None and any(src.startswith(prefix) for prefix in DECEMBER_1957_SOURCE_PREFIXES):
        return True
    return False


def is_oscr_direct(source=""):
    src = _normalize_source(source)
    return src.upper().startswith("OSCR")


def is_t65_source(source=""):
    src = _normalize_source(source)
    return src.upper().startswith("T65_")


def is_allowed_in_dialer_queue(source="", birthday=""):
    """Return True when a lead may appear in the dialer call queue."""
    if is_december_1957_cohort(source, birthday):
        return False
    src = _normalize_source(source)
    if is_oscr_direct(src):
        return True
    if is_t65_source(src):
        return True
    if src in LEGACY_T65_SOURCES:
        return True
    # Older sync rows may lack Source; keep target birth-year T65 only.
    if not src:
        try:
            import geo_policy
            return geo_policy.birthday_is_target(birthday)
        except Exception:
            return birth_year(birthday) == 1962
    return False


def exclude_december_1957_enabled():
    raw = os.getenv("CC_EXCLUDE_DEC_1957", "1")
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def dialer_queue_eligible(source="", birthday=""):
    if exclude_december_1957_enabled() and is_december_1957_cohort(source, birthday):
        return False
    return is_allowed_in_dialer_queue(source, birthday)
