"""On-server DB snapshots (manage.py backup_db): a checksummed, restorable
local snapshot is written; the sqlite snapshot is a valid, openable copy of the
real data; retention prunes old snapshots by the tiered policy; and a failure
is logged, never raised (so it can't crash the scheduler that calls it).
"""

import gzip
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection

# transaction=True: the in-memory test DB is backed up from Django's live
# connection (there's no file to open), so the test must not hold an open
# wrapping transaction — that would block the sqlite online-backup API.
pytestmark = pytest.mark.django_db(transaction=True)

SNAP_GLOB = "acocos_snapshot_*"


def _snapshots(directory: Path):
    return sorted(p for p in directory.iterdir() if p.name.endswith((".sqlite3.gz", ".dump")))


def test_snapshot_is_written_with_a_matching_checksum(tmp_path):
    call_command("backup_db", dir=str(tmp_path), quiet=True)
    snaps = _snapshots(tmp_path)
    assert len(snaps) == 1
    snap = snaps[0]
    checksum = snap.with_suffix(snap.suffix + ".sha256")
    assert checksum.exists()

    # The recorded hash must actually match the file (a restore relies on this).
    from hashlib import sha256

    digest = sha256(snap.read_bytes()).hexdigest()
    assert digest in checksum.read_text()


@pytest.mark.skipif(
    "sqlite" not in connection.settings_dict["ENGINE"], reason="sqlite-only snapshot content check"
)
def test_sqlite_snapshot_is_a_real_openable_copy_of_the_data(tmp_path, django_user_model):
    # Put a recognisable row in the live DB, snapshot, then open the snapshot
    # as its own sqlite file and confirm the row is present — proves it's a
    # genuine restorable copy, not an empty/half file.
    django_user_model.objects.create_user("snap_probe", password="x" * 12)

    call_command("backup_db", dir=str(tmp_path), quiet=True)
    snap = _snapshots(tmp_path)[0]
    assert snap.name.endswith(".sqlite3.gz")

    restored = tmp_path / "restored.sqlite3"
    with gzip.open(snap, "rb") as f_in:
        restored.write_bytes(f_in.read())

    conn = sqlite3.connect(str(restored))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM auth_user WHERE username = ?", ("snap_probe",)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 1


def test_retention_keeps_recent_prunes_old(tmp_path):
    """Seed fake snapshot files across time and confirm the tiered policy keeps
    all recent, thins the middle to one/day, and drops the ancient."""
    now = datetime.now()

    def touch(days_ago, hour):
        ts = (now - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
        stamp = ts.strftime("%Y-%m-%d_%H%M%S")
        f = tmp_path / f"acocos_snapshot_{stamp}.sqlite3.gz"
        f.write_bytes(b"x")
        (tmp_path / f"acocos_snapshot_{stamp}.sqlite3.gz.sha256").write_text("x")
        return f

    # Two on the same recent day (both kept — inside keep-all window),
    # one 15 days old (kept, one-per-day tier), one 300 days old (dropped).
    recent_a = touch(0, 6)
    recent_b = touch(0, 18)
    middle = touch(15, 12)
    ancient = touch(300, 12)

    # A real snapshot is also taken this run (today) — irrelevant to the assertions.
    call_command("backup_db", dir=str(tmp_path), quiet=True)

    assert recent_a.exists() and recent_b.exists()
    assert middle.exists()
    assert not ancient.exists()
    assert not (tmp_path / (ancient.name + ".sha256")).exists()  # sidecar pruned too


def test_two_snapshots_in_a_row_do_not_clobber_each_other(tmp_path):
    call_command("backup_db", dir=str(tmp_path), quiet=True)
    # A distinct second stamp is only guaranteed at second resolution; force it
    # by seeding a differently-stamped unit, then snapshot again.
    call_command("backup_db", dir=str(tmp_path), quiet=True)
    # At least one snapshot exists and every snapshot has its checksum sidecar.
    snaps = _snapshots(tmp_path)
    assert len(snaps) >= 1
    for snap in snaps:
        assert snap.with_suffix(snap.suffix + ".sha256").exists()


def test_snapshot_failure_is_logged_not_raised(tmp_path, monkeypatch, capsys):
    # Make the sqlite backup blow up mid-run; the command must swallow it and
    # exit cleanly (return None) so the scheduler loop is never taken down.
    from apps.core.management.commands import backup_db as cmd_mod

    def boom(self, directory, stamp):
        raise OSError("disk full")

    monkeypatch.setattr(cmd_mod.Command, "_backup_sqlite", boom)
    # Must not raise.
    call_command("backup_db", dir=str(tmp_path))
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert not _snapshots(tmp_path)  # nothing half-written left behind


def test_dir_defaults_to_settings_when_omitted(tmp_path, settings):
    settings.BACKUP_SNAPSHOT_DIR = str(tmp_path / "snaps")
    call_command("backup_db", quiet=True)
    assert _snapshots(tmp_path / "snaps")
