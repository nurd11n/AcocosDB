#!/usr/bin/env python
"""Tiny daily job runner for the `scheduler` service (docker-compose.prod.yml).

Fixed times a day, in TIME_ZONE:
  SNAPSHOT_HOURS (default 00:00,06:00,12:00,18:00) — take a local, checksummed
              database snapshot on the server (manage.py backup_db) so a copy
              always exists close at hand, every 6 hours, with tiered
              retention. The prod Docker `backup` service adds encryption +
              offsite B2 on top; this is the always-available floor beneath it.
  RATES_HOUR  (default 08:00) — pull fresh NBKR rates automatically. Staff can
              still refresh on demand any time with the «Обновить» button on
              the POS Курс card (apps.pos.views.refresh_rates) — automatic for
              reliability, manual for control; both stay in place.
  REPORT_HOUR (default 21:00) — pull rates again (so the report reflects the
              latest), purge stale draft sales (the FULL sweep, 24h grace for
              a draft with items/a client), then send the daily report.
  DRAFT_CLEANUP_HOURS (default every hour, on the hour) — an additional, more
              frequent sweep of cleanup_draft_sales for EMPTY drafts only (no
              items, no client — see that command's own docstring): those are
              pure clutter from the moment they're created, so they don't wait
              for REPORT_HOUR to disappear from the Продажи list.

A full scheduler (Celery/cron) is overkill for a handful of jobs a day; this
reads env directly so it needs no django.setup(). Every command runs
check=False — a hiccup in one never blocks the others.
"""

import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RATES_HOUR = os.environ.get("RATES_HOUR", "08:00")
REPORT_HOUR = os.environ.get("REPORT_HOUR", "21:00")
SNAPSHOT_HOURS = os.environ.get("SNAPSHOT_HOURS", "00:00,06:00,12:00,18:00")
DRAFT_CLEANUP_HOURS = os.environ.get(
    "DRAFT_CLEANUP_HOURS", ",".join(f"{h:02d}:00" for h in range(24))
)
TZ = ZoneInfo(os.environ.get("TIME_ZONE", "Asia/Bishkek"))


def _build_jobs() -> list[tuple[str, list[str]]]:
    """Group commands by HH:MM so two jobs at the same time both run (rather
    than one silently winning the min() below). Returns (HH:MM, [commands])."""
    by_time: dict[str, list[str]] = defaultdict(list)
    for hhmm in SNAPSHOT_HOURS.split(","):
        hhmm = hhmm.strip()
        if hhmm:
            by_time[hhmm].append("backup_db")
    by_time[RATES_HOUR].append("fetch_rates")
    for cmd in ("fetch_rates", "cleanup_draft_sales", "send_daily_report"):
        by_time[REPORT_HOUR].append(cmd)
    for hhmm in DRAFT_CLEANUP_HOURS.split(","):
        hhmm = hhmm.strip()
        # No extra flags: the command's own defaults (24h / 1h) are exactly
        # right run hourly too — an in-progress draft that crosses 24h now
        # gets cleaned within the hour instead of waiting for REPORT_HOUR.
        # Skip a slot already covered by REPORT_HOUR's entry above so the
        # command doesn't run twice back to back at the same minute.
        if hhmm and hhmm != REPORT_HOUR:
            by_time[hhmm].append("cleanup_draft_sales")
    return [(hhmm, cmds) for hhmm, cmds in by_time.items()]


JOBS = _build_jobs()


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
