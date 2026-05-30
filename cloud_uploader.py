"""
Command Center — laptop uploader.

Runs on the machine where the dialers write dial_results.xlsx. Every minute it
securely pushes each agent's results to the cloud Command Center so the hosted
dashboard shows live data — while the dialers keep running locally, untouched.

Configure (set these before running, or edit the defaults):
    CC_CLOUD_URL   = https://your-droplet-or-domain
    CC_INGEST_TOKEN= the same secret token set on the server
Run:  python cloud_uploader.py
"""
import os
import time

import requests

CLOUD_URL = os.getenv("CC_CLOUD_URL", "").rstrip("/")
TOKEN = os.getenv("CC_INGEST_TOKEN", "")
INTERVAL = int(os.getenv("CC_UPLOAD_SECONDS", "60"))

# (agent, local results file). Mirrors the dialers' real output locations.
UPLOADS = [
    ("chris", r"C:\dialer\MyDialer_TODAY_READY\MyDialer\dial_results.xlsx"),
    ("will",  r"C:\Users\chris\OneDrive\Documents\Dialer\Will_Dialer\dial_results.xlsx"),
]


def push_once():
    if not CLOUD_URL or not TOKEN:
        print("Set CC_CLOUD_URL and CC_INGEST_TOKEN first."); return
    for agent, path in UPLOADS:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    f"{CLOUD_URL}/api/ingest?agent={agent}",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    files={"file": (f"{agent}.xlsx", fh)},
                    timeout=30,
                )
            ts = time.strftime("%H:%M:%S")
            print(f"  {ts}  {agent}: {r.status_code} {r.text[:80]}")
        except Exception as exc:
            print(f"  upload error ({agent}): {exc}")


def main():
    print(f"Command Center uploader -> {CLOUD_URL or '(unset)'} every {INTERVAL}s")
    while True:
        push_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
