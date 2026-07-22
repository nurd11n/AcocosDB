#!/usr/bin/env python
"""Tiny daily job runner for the `scheduler` service (docker-compose.prod.yml).

Two fixed times a day, in TIME_ZONE:
  RATES_HOUR  (default 08:00) — refresh NBKR FX rates at the start of the day, so
              the dashboard's «≈ $ / ₽» view uses today's official rate.
  REPORT_HOUR (default 21:00) — refresh rates again, purge stale draft sales, then
              send the daily report.

A full scheduler (Celery/cron) is overkill for two jobs a day; this reads env
directly so it needs no django.setup(). Every command runs check=False — a
hiccup in one never blocks the others, and fetch_rates keeps the last known rate
on failure (a sale must never break for lack of a fresh rate).
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RATES_HOUR = os.environ.get("RATES_HOUR", "08:00")
REPORT_HOUR = os.environ.get("REPORT_HOUR", "21:00")
TZ = ZoneInfo(os.environ.get("TIME_ZONE", "Asia/Bishkek"))

# (HH:MM, [management commands to run, in order])
JOBS = [
    (RATES_HOUR, ["fetch_rates"]),
    (REPORT_HOUR, ["fetch_rates", "cleanup_draft_sales", "send_daily_report"]),
]


def _next_occurrence(hhmm: str, now: datetime) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _run(commands):
    for cmd in commands:
        print(f"Running {cmd}...", flush=True)
        subprocess.run([sys.executable, "manage.py", cmd], check=False)


def main():
    times = ", ".join(hhmm for hhmm, _ in JOBS)
    print(f"Scheduler started. Daily jobs at {times} ({TZ}).", flush=True)
    while True:
        now = datetime.now(TZ)
        # The soonest job across all configured times.
        when, commands = min(
            ((_next_occurrence(hhmm, now), cmds) for hhmm, cmds in JOBS),
            key=lambda pair: pair[0],
        )
        wait = (when - now).total_seconds()
        print(f"Sleeping {wait / 3600:.1f}h until {when:%H:%M}.", flush=True)
        time.sleep(wait)
        _run(commands)


if __name__ == "__main__":
    main()
