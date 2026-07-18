#!/usr/bin/env python3
"""Tiered backup retention, applied to a local backup directory.

Keep:
  * every backup from the last 7 days          (the 4×/day cadence => ~28)
  * one per day for backups 7–30 days old      (the newest of each day)
  * one per week for backups 30 days–6 months   (the newest of each ISO week)
  * nothing older than ~6 months

A backup "unit" is the set of files sharing a stem: acocos_<stamp>.dump[.age]
plus its .sha256. Decisions are made on the stamp in the filename, and all files
of a dropped unit are removed together. Offsite (B2) mirrors this via `rclone
sync`, so pruning here prunes there on the next run.
"""

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

STAMP_RE = re.compile(r"acocos_(\d{4}-\d{2}-\d{2}_\d{6})\.dump")


def stem_of(path: Path) -> str | None:
    m = STAMP_RE.search(path.name)
    return m.group(1) if m else None


def main(directory: str) -> None:
    root = Path(directory)
    now = datetime.now()

    # Group files by their backup stamp (one unit = dump + .age + .sha256).
    units: dict[str, list[Path]] = {}
    for path in root.iterdir():
        stem = stem_of(path)
        if stem:
            units.setdefault(stem, []).append(path)

    keep: set[str] = set()
    seen_day: set[str] = set()
    seen_week: set[str] = set()
    # Newest first, so "one per day/week" keeps the newest in each bucket.
    for stem in sorted(units, reverse=True):
        ts = datetime.strptime(stem, "%Y-%m-%d_%H%M%S")
        age = now - ts
        if age <= timedelta(days=7):
            keep.add(stem)
        elif age <= timedelta(days=30):
            day = ts.strftime("%Y-%m-%d")
            if day not in seen_day:
                seen_day.add(day)
                keep.add(stem)
        elif age <= timedelta(days=183):
            week = ts.strftime("%G-W%V")
            if week not in seen_week:
                seen_week.add(week)
                keep.add(stem)
        # older than ~6 months: not kept

    removed = 0
    for stem, paths in units.items():
        if stem not in keep:
            for p in paths:
                p.unlink(missing_ok=True)
                removed += 1
    print(f"[prune] units kept={len(keep)} files removed={removed}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/backups")
