"""Shared Greensboro-area dialing policy."""
import os
import re
from datetime import datetime


ALLOWED_CITY_NAMES = {
    "archdale",
    "browns summit",
    "burlington",
    "climax",
    "colfax",
    "elon",
    "forest oaks",
    "gibsonville",
    "graham",
    "greensboro",
    "haw river",
    "high point",
    "jamestown",
    "julian",
    "kernersville",
    "liberty",
    "mcleansville",
    "mebane",
    "oak ridge",
    "pleasant garden",
    "randleman",
    "reidsville",
    "sedalia",
    "sophia",
    "stokesdale",
    "summerfield",
    "thomasville",
    "trinity",
    "whitsett",
}

CITY_ALIASES = {
    "brown summit": "browns summit",
    "browns summit": "browns summit",
    "highpoint": "high point",
    "mc leansville": "mcleansville",
    "mcleansville": "mcleansville",
    "pleasant gdn": "pleasant garden",
    "pleasant garden": "pleasant garden",
}

# Approximate drive-time ordering only. The whitelist above decides eligibility.
CITY_DRIVE_MIN = {
    "greensboro": 0,
    "mcleansville": 8,
    "pleasant garden": 12,
    "jamestown": 14,
    "sedalia": 14,
    "browns summit": 14,
    "forest oaks": 14,
    "high point": 18,
    "colfax": 18,
    "summerfield": 18,
    "whitsett": 18,
    "julian": 20,
    "gibsonville": 20,
    "oak ridge": 20,
    "climax": 22,
    "archdale": 22,
    "stokesdale": 24,
    "trinity": 24,
    "elon": 24,
    "thomasville": 26,
    "kernersville": 26,
    "burlington": 28,
    "randleman": 28,
    "sophia": 30,
    "graham": 30,
    "reidsville": 30,
    "haw river": 32,
    "liberty": 34,
    "mebane": 34,
}
DEFAULT_DRIVE_MIN = 45


def normalize_city(city):
    key = re.sub(r"[^a-z0-9 ]+", " ", str(city or "").lower())
    key = re.sub(r"\s+", " ", key).strip()
    return CITY_ALIASES.get(key, key)


def city_is_allowed(city):
    return normalize_city(city) in ALLOWED_CITY_NAMES


def drive_minutes(city):
    return CITY_DRIVE_MIN.get(normalize_city(city), DEFAULT_DRIVE_MIN)


def filter_enabled(env_name="CC_GEO_FILTER_ENABLED", default=True):
    raw = os.getenv(env_name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def target_birth_years():
    # Cohorts we actively work:
    #   1961 -> genuine T65 turning 65 in 2026 (OSCR monthly birthday lists, e.g. T65_Nov_NC)
    #   1962 -> the original Dialer_Existing list + next year's T65
    # Override per-deployment with CC_TARGET_BIRTH_YEARS (comma-separated). As OSCR
    # rolls into new birth-year cohorts, add the year here or via the env var.
    raw = os.getenv("CC_TARGET_BIRTH_YEARS", "1961,1962")
    years = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit() and len(part) == 4:
            years.add(int(part))
    return years or {1961, 1962}


def birth_year(birthday):
    text = str(birthday or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().year
        except (TypeError, ValueError):
            continue
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return int(match.group(0)) if match else None


def birthday_is_target(birthday):
    year = birth_year(birthday)
    return year in target_birth_years()
