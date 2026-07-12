#!/usr/bin/env python
"""Sleeps until REPORT_HOUR (HH:MM, TIME_ZONE) every day, then runs
`manage.py send_daily_report`. Runs as the `scheduler` service in
docker-compose.prod.yml — a full scheduler (Celery/cron) is overkill for one
job a day; this reads env directly so it doesn't need django.setup().
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

REPORT_HOUR = os.environ.get("REPORT_HOUR", "21:00")
TZ = ZoneInfo(os.environ.get("TIME_ZONE", "Asia/Bishkek"))


def seconds_until_next_run() -> float:
    hour, minute = (int(x) for x in REPORT_HOUR.split(":"))
    now = datetime.now(TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main():
    print(f"Scheduler started. Daily report runs at {REPORT_HOUR} ({TZ}).", flush=True)
    while True:
        wait = seconds_until_next_run()
        print(f"Sleeping {wait / 3600:.1f}h until next report.", flush=True)
        time.sleep(wait)
        print("Running send_daily_report...", flush=True)
        subprocess.run([sys.executable, "manage.py", "send_daily_report"], check=False)


if __name__ == "__main__":
    main()
