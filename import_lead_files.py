"""
Import the monthly T65 birthday lists into the dialer's contacts.xlsx.

Handles the real-world mess of these exports:
  * data sometimes lives on a secondary sheet (the Feb file's 'NC' sheet) -> we read
    whichever sheet has the most rows.
  * two phone columns (PhoneT, HomePhone); HomePhone is frequently the literal 'DNC'.
    Keep every valid number, including DNC-labelled numbers. A literal DNC with no
    digits is not a phone number; other phone fields on that lead are still used.
  * split address (HomeStreet/HomeCity/HomeState/HomePostalCode) -> one Address string.
  * statewide NC lists -> only Greensboro-area (geo whitelist) leads go on the dial
    list; out-of-area leads are parked in a sidecar so nothing is ever lost.

Dedups against phones already in contacts.xlsx (no double-dialing), tags each lead with
a per-month Source, backs up the file, then appends. Re-run enrich_contacts_home_value.py
afterward to stamp home values on the new rows.

    python import_lead_files.py                 # the 7 default Downloads files
    python import_lead_files.py --dry-run        # report only, write nothing
    python import_lead_files.py FILE [FILE ...]  # specific files
"""
import argparse
import os
import re
import shutil
from datetime import datetime

import openpyxl
import pandas as pd

import geo_policy
import db

DEFAULT_CONTACTS = r"C:\dialer\MyDialer_TODAY_READY\MyDialer\contacts.xlsx"
DOWNLOADS = r"C:\Users\chris\Downloads"
DEFAULT_FILES = [
    "T65 2092 September NC 1961 Birthdays.xlsx",
    "T65 2092 October 1961 NC Birthdays.xlsx",
    "T65 2092 November 1961 NC Birthdays (1).xlsx",
    "T65 2092 NC December 1961 Birthdays.xlsx",
    "T65 2092 NC January 1962 Birthdays (1).xlsx",
    "T65 2092 February 1962 NC Birthdays.xlsx",
    "T65 2092 March NC_1962 Birthdays (1).xlsx",
]
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def norm_phone(value):
    d = re.sub(r"\D", "", str(value or ""))
    if len(d) == 11 and d[0] == "1":
        return "+" + d
    if len(d) == 10:
        return "+1" + d
    return ""


def best_sheet_df(path):
    """Read the populated sheet (some exports hide data on a secondary 'NC' tab)."""
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        name = max(wb.sheetnames, key=lambda s: wb[s].max_row)
    finally:
        wb.close()
    return pd.read_excel(path, sheet_name=name, dtype=str, keep_default_na=False)


def find_col(df, *names):
    low = {str(c).strip().lower(): c for c in df.columns}
    for n in names:
        if n in low:
            return low[n]
    return ""


def source_from_filename(path):
    base = os.path.basename(path).lower()
    month = next((m for m in MONTHS if m in base), "")
    ym = re.search(r"\b(1961|1962)\b", base)   # NOT the '2092' code
    year = ym.group(1) if ym else ""
    if month and year:
        return f"T65_{month.capitalize()}_NC_{year}"
    return os.path.splitext(os.path.basename(path))[0]


def lead_phones(row, phone_cols):
    """All distinct valid numbers; DNC metadata never discards a lead."""
    return db.lead_phones({f"phone_{i}": row.get(c, "") for i, c in enumerate(phone_cols)})


def build_address(row, c_addr, c_street, c_city, c_state, c_zip):
    if c_addr and str(row.get(c_addr, "")).strip():
        return str(row.get(c_addr)).strip()
    parts = [str(row.get(c, "")).strip() for c in (c_street, c_city, c_state, c_zip) if c]
    return ", ".join(p for p in parts if p)


def make_name(row, c_name, c_first, c_last):
    if c_name and str(row.get(c_name, "")).strip():
        return str(row.get(c_name)).strip()
    first = str(row.get(c_first, "")).strip() if c_first else ""
    last = str(row.get(c_last, "")).strip() if c_last else ""
    return f"{first} {last}".strip()


def run(files, contacts_path, dry_run=False):
    if not os.path.exists(contacts_path):
        raise SystemExit(f"contacts file not found: {contacts_path}")
    existing = pd.read_excel(contacts_path, dtype=str, keep_default_na=False)
    existing_phones = {norm_phone(p) for p in existing.get("Phone", []) if norm_phone(p)}

    new_rows, out_of_area, seen = [], [], set()
    stats = {"files": 0, "rows": 0, "no_phone": 0, "dup": 0, "in_area": 0, "out_area": 0}

    for path in files:
        if not os.path.exists(path):
            print(f"  MISSING: {path}")
            continue
        df = best_sheet_df(path)
        stats["files"] += 1
        source = source_from_filename(path)
        phone_cols = [c for c in df.columns
                      if any(t in str(c).lower() for t in ["phone", "mobile", "cell", "number", "tel"])]
        c_name = find_col(df, "name", "full name", "fullname")
        c_first = find_col(df, "firstname", "first name", "first")
        c_last = find_col(df, "lastname", "last name", "last")
        c_addr = find_col(df, "address", "home address", "street address")
        c_street = find_col(df, "homestreet", "street", "home street")
        c_city = find_col(df, "homecity", "city", "town")
        c_state = find_col(df, "homestate", "state")
        c_zip = find_col(df, "homepostalcode", "zip", "postal code", "zipcode", "postalcode")
        c_county = find_col(df, "county", "homecounty")
        c_bday = find_col(df, "birthday", "birthdate", "date of birth", "dob")

        for _, row in df.iterrows():
            stats["rows"] += 1
            phones = lead_phones(row, phone_cols)
            if not phones:
                stats["no_phone"] += 1
                continue
            for phone in phones:
                if phone in existing_phones or phone in seen:
                    stats["dup"] += 1
                    continue
                seen.add(phone)
                city = str(row.get(c_city, "")).strip() if c_city else ""
                rec = {
                    "Queue_Type": "never_called",
                    "Name": make_name(row, c_name, c_first, c_last),
                    "Phone": phone,
                    "Address": build_address(row, c_addr, c_street, c_city, c_state, c_zip),
                    "City": city,
                    "County": str(row.get(c_county, "")).strip() if c_county else "",
                    "Birthday": str(row.get(c_bday, "")).strip() if c_bday else "",
                    "Source": source,
                    "Lead_Score": "100",
                }
                if geo_policy.city_is_allowed(city):
                    new_rows.append(rec)
                    stats["in_area"] += 1
                else:
                    out_of_area.append(rec)
                    stats["out_area"] += 1

    print("Files read:        ", stats["files"])
    print("Rows scanned:      ", stats["rows"])
    print("No dialable phone: ", stats["no_phone"], "(blank or no valid digits)")
    print("Duplicates skipped:", stats["dup"], "(already in contacts.xlsx)")
    print("NEW in-area numbers: ", stats["in_area"], "(added to dial list)")
    print("Out-of-area parked:", stats["out_area"], "(sidecar, not dialed)")
    print(f"contacts.xlsx: {len(existing)} -> {len(existing) + len(new_rows)} rows")

    if dry_run:
        print("\n[DRY RUN] nothing written.")
        return stats

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if new_rows:
        backup = f"{contacts_path}.backup_pre_import_{stamp}.xlsx"
        shutil.copy2(contacts_path, backup)
        new_df = pd.DataFrame(new_rows)
        combined = pd.concat([existing, new_df], ignore_index=True).fillna("")
        combined = combined[[c for c in existing.columns] +
                            [c for c in new_df.columns if c not in existing.columns]]
        tmp = f"{contacts_path}.tmp.xlsx"
        combined.to_excel(tmp, index=False, engine="openpyxl")
        os.replace(tmp, contacts_path)
        print(f"\nAppended {len(new_rows)} leads. Backup: {backup}")
    if out_of_area:
        side = os.path.join(os.path.dirname(contacts_path), "out_of_area_leads.xlsx")
        oa = pd.DataFrame(out_of_area)
        if os.path.exists(side):
            prev = pd.read_excel(side, dtype=str, keep_default_na=False)
            oa = pd.concat([prev, oa], ignore_index=True).drop_duplicates("Phone")
        oa.to_excel(side, index=False, engine="openpyxl")
        print(f"Parked {len(out_of_area)} out-of-area leads -> {side}")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="lead files (default: the 7 in Downloads)")
    ap.add_argument("--contacts", default=DEFAULT_CONTACTS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    files = args.files or [os.path.join(DOWNLOADS, f) for f in DEFAULT_FILES]
    run(files, args.contacts, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
