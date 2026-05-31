"""
EXAMPLE lead-puller agent — a working template.

Reads a CSV/XLSX of leads and pushes them into the Command Center via the Agent
API. This is the pattern any real lead source (OSCR export, a T65 vendor file, a
scraper's output) plugs into — point it at the file, run it, leads appear in the
dashboard under 'new' and flow into the call queue.

USAGE
    set CC_CLOUD_URL=https://68.183.116.10.sslip.io
    set CC_AGENT_TOKEN=<from DEPLOY_SECRETS.txt>
    python example_lead_puller.py path\\to\\leads.csv --source T65_June2026 --owner chris

The file just needs recognizable columns. These header names are auto-detected
(case-insensitive): name / full name / first name+last name, phone / mobile / cell /
number, city, county, birthday / dob, address, email, source, lead_type.

Returns a summary: imported / duplicates / suppressed.
"""
import argparse
import os
import sys

import requests

try:
    import pandas as pd
except ImportError:
    pd = None

CLOUD_URL = os.getenv("CC_CLOUD_URL", "http://localhost:5055").rstrip("/")
TOKEN = os.getenv("CC_AGENT_TOKEN", "")

# header alias -> canonical field the API expects
ALIASES = {
    "name": "name", "full name": "name", "fullname": "name",
    "first name": "first_name", "firstname": "first_name",
    "last name": "last_name", "lastname": "last_name",
    "phone": "phone", "phone number": "phone", "mobile": "phone", "cell": "phone",
    "cell phone": "phone", "number": "phone", "telephone": "phone",
    "city": "city", "town": "city",
    "county": "county",
    "birthday": "birthday", "dob": "birthday", "date of birth": "birthday", "birthdate": "birthday",
    "address": "address", "street address": "address",
    "email": "email", "email address": "email",
    "source": "source", "lead source": "source",
    "lead_type": "lead_type", "lead type": "lead_type", "product": "lead_type",
}


def read_rows(path):
    if pd is None:
        sys.exit("pandas is required: pip install pandas openpyxl")
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    else:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    # map headers to canonical names
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in ALIASES:
            rename[col] = ALIASES[key]
    df = df.rename(columns=rename)
    return df.to_dict(orient="records")


def chunk(items, n=500):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def main():
    ap = argparse.ArgumentParser(description="Push a CSV/XLSX of leads into Command Center.")
    ap.add_argument("file", help="Path to the leads CSV or XLSX")
    ap.add_argument("--source", default="", help="Lead source tag, e.g. T65_June2026")
    ap.add_argument("--owner", default="", help="chris or will")
    ap.add_argument("--url", default=CLOUD_URL)
    ap.add_argument("--token", default=TOKEN)
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"File not found: {args.file}")

    rows = read_rows(args.file)
    print(f"Read {len(rows)} rows from {os.path.basename(args.file)}")

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    totals = {"imported": 0, "duplicates": 0, "suppressed": 0}
    for batch in chunk(rows):
        payload = {"source": args.source, "owner": args.owner, "leads": batch}
        r = requests.post(f"{args.url}/agent/leads/import", json=payload, headers=headers, timeout=60)
        if r.status_code != 200:
            print(f"  ERROR {r.status_code}: {r.text[:200]}")
            continue
        j = r.json()
        for k in totals:
            totals[k] += j.get(k, 0)
        print(f"  batch: +{j.get('imported',0)} new, {j.get('duplicates',0)} dup, {j.get('suppressed',0)} suppressed")

    print(f"\nDONE -> imported {totals['imported']}, duplicates {totals['duplicates']}, "
          f"suppressed(opted-out) {totals['suppressed']}")
    print(f"View them at {args.url}  (Lead Book, filter stage = new)")


if __name__ == "__main__":
    main()
