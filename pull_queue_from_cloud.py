"""
Pull the dialer's call list FROM the cloud Command Center (single source of truth).

The dashboard ranks every lead; this fetches that exact ranked order and writes it to
the dialer's contacts.xlsx. Result: the dashboard's top lead = the dialer's call #1,
ALWAYS. Run this right before a dial session (the launcher can call it automatically).

Config (env or defaults below):
    CC_CLOUD_URL    = https://68.183.116.10.sslip.io
    CC_USERNAME / CC_PASSWORD   (dashboard login)
    CC_QUEUE_OWNER  = chris | will   (whose queue)
    CC_QUEUE_OUT    = path to the dialer's contacts.xlsx
"""
import os
import sys

import pandas as pd
import requests

URL = os.getenv("CC_CLOUD_URL", "https://68.183.116.10.sslip.io").rstrip("/")
USER = os.getenv("CC_USERNAME", "chris")
PW = os.getenv("CC_PASSWORD", "")
OWNER = os.getenv("CC_QUEUE_OWNER", "chris")
OUT = os.getenv("CC_QUEUE_OUT", r"C:\dialer\MyDialer_TODAY_READY\MyDialer\contacts.xlsx")
LIMIT = os.getenv("CC_QUEUE_LIMIT", "1000")

COLUMNS = ["Queue_Type", "Name", "Phone", "Address", "City", "County", "Birthday",
           "Last_Disposition", "Last_Call_Time", "Source", "Lead_Score", "person_id"]


def main():
    if not PW:
        sys.exit("Set CC_PASSWORD (dashboard password) first.")
    try:
        r = requests.get(f"{URL}/api/dialer-queue", params={"owner": OWNER, "limit": LIMIT},
                         auth=(USER, PW), timeout=30)
    except Exception as exc:
        sys.exit(f"Could not reach Command Center ({URL}): {exc}")
    if r.status_code != 200:
        sys.exit(f"Command Center returned {r.status_code}: {r.text[:200]}")
    queue = r.json().get("queue", [])
    if not queue:
        print("Cloud queue is empty — nothing to dial. (contacts.xlsx left unchanged.)")
        return
    df = pd.DataFrame(queue, columns=COLUMNS)
    folder = os.path.dirname(os.path.abspath(OUT))
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = OUT + ".tmp.xlsx"
    df.to_excel(tmp, index=False, engine="openpyxl")
    os.replace(tmp, OUT)
    by = {}
    for q in queue:
        by[q["Queue_Type"]] = by.get(q["Queue_Type"], 0) + 1
    print(f"Pulled {len(df)} leads from the dashboard -> {OUT}")
    print("  call order matches the dashboard exactly. Breakdown:", by)
    print("  call #1:", queue[0]["Name"], "|", queue[0]["City"], "|", queue[0]["Phone"])


if __name__ == "__main__":
    main()
