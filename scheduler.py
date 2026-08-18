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
              latest), purge stale draft sales, send the daily report, then
              check today's SecurityEvent counts and Telegram the Owner —
              ONLY if a threshold is exceeded (send_security_digest stays
              silent otherwise, on purpose: see its own module docstring) —
              and finally age out SecurityEvent rows past 90 days, so the
              abuse log stays bounded in the same DB as the sales data.

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
    for cmd in (
        "fetch_rates",
        "cleanup_draft_sales",
        "send_daily_report",
        # Read-only, never writes — order relative to the other jobs here
        # doesn't matter, placed next to send_daily_report for the shared
        # "financial reporting integrity" theme. Silent unless it finds a
        # discrepancy (own module docstring) — same pattern as the security
        # digest below.
        "audit_stale_totals",
        "send_security_digest",
        # Runs AFTER the digest, never before: the digest reads today's rows
        # and this only ever deletes rows 90+ days old, but keeping the order
        # explicit means a future retention change can't silently start
        # deleting the very rows the digest was about to count.
        "purge_security_events",
    ):
        by_time[REPORT_HOUR].append(cmd)
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
