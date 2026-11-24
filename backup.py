"""Scheduled backup client for CattleCare.

Usage:
  set FARM_ADMIN_TOKEN=... and APP_URL=https://your-domain
  python backup.py

Writes a timestamped JSON file into BACKUP_DIR (default ./backups) and removes
files older than BACKUP_RETENTION_DAYS (default 30). Schedule this with cron or
a systemd timer, then copy the directory somewhere off-host.
"""

import os
import json
import datetime
import urllib.request

ADMIN_TOKEN = os.environ.get("FARM_ADMIN_TOKEN")
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "backups"
))
RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))


def main():
    if not ADMIN_TOKEN:
        raise SystemExit("FARM_ADMIN_TOKEN is not set.")

    request = urllib.request.Request(
        APP_URL + "/api/export",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"cattlecare_backup_{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)

    cutoff = datetime.datetime.now() - datetime.timedelta(days=RETENTION_DAYS)
    removed = 0
    for name in os.listdir(BACKUP_DIR):
        if not name.startswith("cattlecare_backup_") or not name.endswith(".json"):
            continue
        candidate = os.path.join(BACKUP_DIR, name)
        modified = datetime.datetime.fromtimestamp(os.path.getmtime(candidate))
        if modified < cutoff:
            os.remove(candidate)
            removed += 1

    print(f"Backup written to {path} ({data.get('totalRecords', '?')} records)")
    print(f"Removed {removed} backup(s) older than {RETENTION_DAYS} days")


if __name__ == "__main__":
    main()
