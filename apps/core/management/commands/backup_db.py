"""On-server database snapshots — the always-available backup floor.

Writes a timestamped, checksummed snapshot of the database into a local
directory on the server (settings.BACKUP_SNAPSHOT_DIR, default
backups/snapshots/) so a copy always exists close at hand, then prunes old
ones by a tiered retention policy. Runs in ANY environment with no external
services to configure:

  * sqlite   — a consistent ONLINE snapshot via the sqlite3 backup API (safe
               while the app is live; never locks the file out from under a
               running server), gzip-compressed.
  * postgres — pg_dump -Fc (custom compressed format), IF pg_dump is on PATH.
               If it isn't (e.g. the slim Django image), this logs a clear
               note and exits cleanly — the prod Docker `backup` service owns
               the postgres path there, with encryption + offsite B2 on top.

This is the FLOOR; the Docker `backup` service (docker/backup.sh) is the
prod belt-and-suspenders that adds age encryption and offsite B2. Neither
depends on the other. Every failure here is logged and the command exits 0
where it safely can, so a hiccup never crashes the scheduler that calls it.

Run:  python manage.py backup_db [--dir PATH] [--keep-all-days N] [--quiet]
Scheduled: scheduler.py runs it at each time in settings.SNAPSHOT_HOURS.
"""

import gzip
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

STAMP_FMT = "%Y-%m-%d_%H%M%S"
STAMP_RE = re.compile(r"acocos_snapshot_(\d{4}-\d{2}-\d{2}_\d{6})\.")


def _sha256(path: Path) -> str:
    h = sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Command(BaseCommand):
    help = "Take a local, checksummed database snapshot and prune old ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir", default=None, help="Snapshot directory (default settings.BACKUP_SNAPSHOT_DIR)."
        )
        parser.add_argument("--keep-all-days", type=int, default=None)
        parser.add_argument("--quiet", action="store_true", help="Only print on error.")

    def handle(self, *args, **options):
        self._quiet = options["quiet"]
        directory = Path(options["dir"] or settings.BACKUP_SNAPSHOT_DIR)
        directory.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime(STAMP_FMT)
        engine = connection.settings_dict["ENGINE"]

        try:
            if "sqlite" in engine:
                snapshot = self._backup_sqlite(directory, stamp)
            elif "postgresql" in engine or "postgis" in engine:
                snapshot = self._backup_postgres(directory, stamp)
            else:
                self.stderr.write(f"backup_db: unsupported DB engine {engine!r} — nothing done.")
                return
        except Exception as exc:  # never let a snapshot failure crash the scheduler
            self.stderr.write(f"backup_db: snapshot FAILED — {exc}")
            return

        if snapshot is None:
            return  # a clean, logged skip (e.g. postgres with no pg_dump here)

        # Checksum sidecar so a restore can prove the file is intact.
        digest = _sha256(snapshot)
        checksum_path = snapshot.with_suffix(snapshot.suffix + ".sha256")
        checksum_path.write_text(f"{digest}  {snapshot.name}\n", encoding="utf-8")

        size_mb = snapshot.stat().st_size / (1024 * 1024)
        self._say(f"backup_db: wrote {snapshot.name} ({size_mb:.1f} MB), sha256 {digest[:12]}…")

        keep_all = (
            options["keep_all_days"]
            if options["keep_all_days"] is not None
            else settings.SNAPSHOT_KEEP_ALL_DAYS
        )
        self._prune(directory, keep_all)

    # -- engine-specific dumps -------------------------------------------------

    def _backup_sqlite(self, directory: Path, stamp: str) -> Path:
        """Consistent online copy via the sqlite3 backup API, then gzip. The
        backup API reads a transactionally-consistent view even while the app
        writes, so there's no downtime and no half-written snapshot.

        For a real file-backed DB (prod/dev) we open a DEDICATED connection to
        the file — never touching the app's own connection, and never blocked
        by it. For an in-memory / shared-cache DB (only the test suite) we fall
        back to Django's live connection, since there's no file to open."""
        final = directory / f"acocos_snapshot_{stamp}.sqlite3.gz"
        db_name = connection.settings_dict["NAME"]
        is_file_db = (
            isinstance(db_name, (str, Path))
            and str(db_name) not in ("", ":memory:")
            and not str(db_name).startswith("file:")
        )

        # Copy into a temp .sqlite3, then gzip it into place — so a reader never
        # sees a partial .gz and the final file appears atomically.
        fd, tmp_name = tempfile.mkstemp(suffix=".sqlite3", dir=directory)
        os.close(fd)
        tmp = Path(tmp_name)
        src = None
        try:
            if is_file_db:
                src = sqlite3.connect(str(db_name))
            else:
                connection.ensure_connection()
                src = connection.connection
            dest = sqlite3.connect(str(tmp))
            try:
                src.backup(dest)
            finally:
                dest.close()
            with open(tmp, "rb") as f_in, gzip.open(final, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)
        finally:
            if is_file_db and src is not None:
                src.close()
            tmp.unlink(missing_ok=True)
        return final

    def _backup_postgres(self, directory: Path, stamp: str) -> Path | None:
        """pg_dump -Fc when the binary is available. Returns None (a clean,
        logged skip) if pg_dump isn't installed — in the slim Django image the
        dedicated Docker `backup` service handles postgres instead, so this is
        never a hard error."""
        if shutil.which("pg_dump") is None:
            self._say(
                "backup_db: pg_dump not found — postgres snapshots here are handled by the "
                "dedicated Docker `backup` service; skipping."
            )
            return None

        db = connection.settings_dict
        final = directory / f"acocos_snapshot_{stamp}.dump"
        env = os.environ.copy()
        if db.get("PASSWORD"):
            env["PGPASSWORD"] = db["PASSWORD"]
        cmd = ["pg_dump", "-Fc", "-f", str(final)]
        if db.get("HOST"):
            cmd += ["-h", db["HOST"]]
        if db.get("PORT"):
            cmd += ["-p", str(db["PORT"])]
        if db.get("USER"):
            cmd += ["-U", db["USER"]]
        cmd.append(db["NAME"])

        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            final.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump exited {result.returncode}: {result.stderr.strip()[:300]}")
        return final

    # -- retention -------------------------------------------------------------

    def _prune(self, directory: Path, keep_all_days: int) -> None:
        """Tiered retention: keep every snapshot for keep_all_days, then one
        per calendar day up to SNAPSHOT_KEEP_DAILY_DAYS, then one per ISO week
        up to SNAPSHOT_KEEP_WEEKLY_DAYS, drop the rest. A snapshot 'unit' is
        the dump plus its .sha256, dropped together."""
        now = datetime.now()
        keep_daily = settings.SNAPSHOT_KEEP_DAILY_DAYS
        keep_weekly = settings.SNAPSHOT_KEEP_WEEKLY_DAYS

        units: dict[str, list[Path]] = {}
        for path in directory.iterdir():
            m = STAMP_RE.search(path.name)
            if m:
                units.setdefault(m.group(1), []).append(path)

        keep: set[str] = set()
        seen_day: set[str] = set()
        seen_week: set[str] = set()
        for stem in sorted(units, reverse=True):  # newest first
            try:
                ts = datetime.strptime(stem, STAMP_FMT)
            except ValueError:
                keep.add(stem)  # unparseable — never delete what we don't understand
                continue
            age = now - ts
            if age <= timedelta(days=keep_all_days):
                keep.add(stem)
            elif age <= timedelta(days=keep_daily):
                day = ts.strftime("%Y-%m-%d")
                if day not in seen_day:
                    seen_day.add(day)
                    keep.add(stem)
            elif age <= timedelta(days=keep_weekly):
                week = ts.strftime("%G-W%V")
                if week not in seen_week:
                    seen_week.add(week)
                    keep.add(stem)

        removed = 0
        for stem, paths in units.items():
            if stem not in keep:
                for p in paths:
                    p.unlink(missing_ok=True)
                    removed += 1
        self._say(f"backup_db: retention — {len(keep)} snapshot(s) kept, {removed} file(s) pruned.")

    def _say(self, msg: str) -> None:
        if not self._quiet:
            self.stdout.write(msg)
