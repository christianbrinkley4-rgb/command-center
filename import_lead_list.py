"""
Import an Excel/CSV lead list into the cloud Command Center via /agent/leads/import.

The endpoint handles everything server-side: dedup, retention of DNC-labelled leads and all their numbers, the T65 birth-year gate (only for T65_*/t65 leads), and
auto-scoring. Geo is applied later at queue-build time, so leads outside the Greensboro
whitelist still import but won't be dialed.

Usage:
    set CC_AGENT_TOKEN=...   (or pass --token)
    python import_lead_list.py --file "C:\\path\\list.xlsx" --source T65_March_NC_1962 \
        --owner chris --lead-type t65 --lead-score 80 [--dry-run]

Re-running is safe: the server dedups by person_key and phone.
"""
import argparse
import os
import sys

import requests

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl required: pip install openpyxl")

# Local-only prediction helpers (best effort; the server is the source of truth).
try:
    import geo_policy
    _HAVE_GEO = True
except Exception:
    _HAVE_GEO = False

DNC_VALUES = {"DNC", "DO NOT CALL", "DONOTCALL", "DO-NOT-CALL", "DO NOT CONTACT", "OPT OUT", "OPTOUT", "STOP"}


def read_rows(path):
    if path.lower().endswith(".csv"):
        import csv
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [dict(r) for r in csv.DictReader(f)]
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    hdr = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
    out = []
    for r in rows[1:]:
        rec = {hdr[i]: ("" if v is None else v) for i, v in enumerate(r) if i < len(hdr)}
        if any(str(v).strip() for v in rec.values()):
            out.append(rec)
    return out


def is_dnc(rec):
    return any(str(v).strip().upper() in DNC_VALUES for v in rec.values())


def city_of(rec):
    for k, v in rec.items():
        if str(k).strip().lower().replace("_", "") in ("homecity", "city", "town"):
            return str(v).strip()
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--owner", default="chris")
    ap.add_argument("--lead-type", default="t65")
    ap.add_argument("--lead-score", type=int, default=80)
    ap.add_argument("--url", default=os.getenv("CC_CLOUD_URL", "https://68.183.116.10.sslip.io"))
    ap.add_argument("--token", default=os.getenv("CC_AGENT_TOKEN", ""))
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = read_rows(args.file)
    if not rows:
        sys.exit("No rows found in file.")

    # Local prediction so the operator sees the expected outcome before any write.
    dnc = sum(1 for r in rows if is_dnc(r))
    clean = rows
    geo_ok = sum(1 for r in clean if _HAVE_GEO and geo_policy.city_is_allowed(city_of(r)))
    print(f"File: {args.file}")
    print(f"  total rows:        {len(rows)}")
    print(f"  DNC (included):   {dnc}")
    print(f"  rows (will send): {len(clean)}")
    if _HAVE_GEO:
        print(f"  of all rows, geo-eligible (dialable): {geo_ok}  (rest import but aren't queued)")
    print(f"  source={args.source} owner={args.owner} lead_type={args.lead_type} score={args.lead_score}")

    payload_rows = []
    for r in rows:
        rec = {str(k): v for k, v in r.items()}
        rec["lead_type"] = args.lead_type
        rec["lead_score"] = args.lead_score
        payload_rows.append(rec)

    if args.dry_run:
        print("\n[DRY RUN] No data sent. Sample record that WOULD be posted:")
        print(" ", payload_rows[0])
        return

    if not args.token:
        sys.exit("No token. Set CC_AGENT_TOKEN env or pass --token.")

    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
    totals = {"imported": 0, "duplicates": 0, "suppressed": 0, "ineligible_age": 0}
    url = args.url.rstrip("/") + "/agent/leads/import"
    for i in range(0, len(payload_rows), args.batch):
        chunk = payload_rows[i:i + args.batch]
        resp = requests.post(url, json={"source": args.source, "owner": args.owner, "leads": chunk},
                             headers=headers, timeout=120)
        if resp.status_code != 200:
            sys.exit(f"Import failed ({resp.status_code}): {resp.text[:300]}")
        j = resp.json()
        for k in totals:
            totals[k] += int(j.get(k, 0))
        print(f"  batch {i // args.batch + 1}: {j.get('imported',0)} imported, "
              f"{j.get('duplicates',0)} dup, {j.get('suppressed',0)} suppressed")
    print("\nDONE. Totals:", totals)


if __name__ == "__main__":
    main()
