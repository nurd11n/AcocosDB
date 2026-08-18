"""End-to-end coverage for docker/backup.sh and docker/restore_drill.sh — the
real encrypt-offsite-restore chain, against a real throwaway PostgreSQL
database, not mocked.

tests/test_backup_db.py covers a DIFFERENT, simpler mechanism
(`manage.py backup_db`, an on-server sqlite/local snapshot) and does not
exercise these two shell scripts at all — that gap is finding H1 of the
2026-08-18 audit (docs/АУДИТ.md): the scripts were logically sound on
reading, but nothing ever actually ran them. This file runs the real
scripts as real subprocesses: seed data -> backup.sh (dump -> checksum ->
age-encrypt -> rclone sync) -> restore_drill.sh (decrypt -> checksum verify
-> pg_restore into a scratch DB -> row-count/freshness assertions), and
separately proves the M3 hardening (fail loudly + alert instead of silently
degrading) actually fires.

Requires: a real PostgreSQL connection (DATABASE_URL pointing at Postgres,
not sqlite — pg_dump/pg_restore don't work against sqlite at all), and the
`age` and `rclone` binaries on PATH (both ship in docker/backup.Dockerfile;
locally: `brew install age rclone` / `apt-get install age rclone`). Skips
cleanly, with a clear reason, when either precondition isn't met — mirrors
the existing skip pattern in test_backup_db.py's Postgres-incompatible test.
"""

import http.server
import shutil
import subprocess
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SH = REPO_ROOT / "docker" / "backup.sh"
RESTORE_DRILL_SH = REPO_ROOT / "docker" / "restore_drill.sh"

_NOT_POSTGRES = "postgresql" not in connection.settings_dict["ENGINE"]
_MISSING_TOOLS = shutil.which("age") is None or shutil.which("rclone") is None

pytestmark = [
    pytestmark,
    pytest.mark.skipif(_NOT_POSTGRES, reason="backup/restore scripts need a real Postgres connection"),
    pytest.mark.skipif(_MISSING_TOOLS, reason="age and/or rclone not on PATH"),
]


def _pg_env():
    """(host, port, user, dbname) for the CURRENTLY connected test database,
    exactly as pg_dump/psql/pg_restore need to reach it — not the configured
    NAME, since Django swaps in a test_-prefixed database name at test run
    time.

    HOST/PORT can arrive either as top-level settings_dict keys (a DSN like
    postgresql://host:port/name) or inside OPTIONS (a DSN using ?host=&port=
    query params, the form a Unix-socket connection like
    postgresql://user@/name?host=/tmp&port=5599 takes) — check OPTIONS first
    since that's what this project's own local-Postgres testing recipe uses.
    """
    d = connection.settings_dict
    options = d.get("OPTIONS") or {}
    return {
        "POSTGRES_HOST": str(options.get("host") or d["HOST"] or "localhost"),
        "POSTGRES_PORT": str(options.get("port") or d["PORT"] or 5432),
        "POSTGRES_USER": d["USER"] or "",
        "POSTGRES_PASSWORD": d["PASSWORD"] or "",
        "POSTGRES_DB": connection.connection.info.dbname if connection.connection else d["NAME"],
    }


def _seed_real_data():
    """A minimal but real confirmed sale — exactly what restore_drill.sh's
    own sanity assertions check for (products/sales/clients > 0, newest
    confirmed sale < 8 days old)."""
    from apps.clients.models import Client
    from apps.inventory.models import Category, Product, ProductVariant
    from apps.inventory.services import add_movement
    from apps.sales.models import SaleItem, SaleOrder
    from apps.sales.services import confirm_sale

    category = Category.objects.create(name="BackupDrillCat")
    product = Product.objects.create(category=category, name="BackupDrillDress")
    variant = ProductVariant.objects.create(
        product=product,
        sku="BD-001",
        size="M",
        color="Blue",
        cost_price=Decimal("500"),
        sale_price=Decimal("1000"),
        currency="KGS",
    )
    add_movement(variant, "purchase_in", 5, reason="test seed")

    client = Client.objects.create(first_name="DrillClient", phone="+996700000099")

    order = SaleOrder.objects.create(client=client, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)
    return order


class _CapturingTelegramHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for api.telegram.org: records every POST body so a test can
    assert an alert actually fired, without a real bot token or network
    access."""

    received = []  # class-level: shared across requests within one test's server

    def do_POST(self):
        from urllib.parse import parse_qs

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        # curl --data-urlencode form-encodes the text; decode it here so
        # assertions can match the plain Russian alert copy directly.
        text = parse_qs(raw_body).get("text", [""])[0]
        _CapturingTelegramHandler.received.append({"path": self.path, "body": raw_body, "text": text})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):  # silence — keep test output clean
        pass


@pytest.fixture
def telegram_mock():
    _CapturingTelegramHandler.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingTelegramHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _CapturingTelegramHandler.received
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run(script, env, timeout=60):
    return subprocess.run(
        ["sh", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_backup_then_restore_drill_round_trip(tmp_path):
    """The real end-to-end chain: seed -> backup.sh -> restore_drill.sh ->
    assert the data came back correctly, then assert the scratch DB the
    drill created is dropped afterward (no leftover throwaway database)."""
    _seed_real_data()

    backups_dir = tmp_path / "backups"
    offsite_dir = tmp_path / "offsite"
    age_identity = tmp_path / "age_identity.txt"
    subprocess.run(["age-keygen", "-o", str(age_identity)], check=True, capture_output=True)
    pubkey = next(
        line.split(": ", 1)[1].strip()
        for line in age_identity.read_text().splitlines()
        if line.lower().startswith("# public key:") or line.lower().startswith("public key:")
    )

    base_env = {"PATH": __import__("os").environ["PATH"], **_pg_env()}

    backup_result = _run(
        BACKUP_SH,
        {
            **base_env,
            "BACKUP_DIR": str(backups_dir),
            "AGE_RECIPIENT": pubkey,
            "RCLONE_REMOTE": str(offsite_dir),
            "BACKUP_RUN_ONCE": "1",
        },
    )
    assert backup_result.returncode == 0, backup_result.stdout + backup_result.stderr

    dumps = list(backups_dir.glob("*.dump"))
    assert dumps == [], "plaintext dump must never survive a successful backup"
    encrypted = list(backups_dir.glob("*.dump.age"))
    assert len(encrypted) == 1
    assert list(backups_dir.glob("*.dump.sha256"))

    offsite_encrypted = list((offsite_dir / "db").glob("*.dump.age"))
    assert len(offsite_encrypted) == 1, "encrypted dump must be synced offsite"

    scratch_db = "acocos_test_restore_drill"
    drill_result = _run(
        RESTORE_DRILL_SH,
        {
            **base_env,
            "BACKUP_DIR": str(backups_dir),
            "AGE_IDENTITY_FILE": str(age_identity),
            "DRILL_SCRATCH_DB": scratch_db,
            "DRILL_RUN_ONCE": "1",
        },
    )
    assert drill_result.returncode == 0, drill_result.stdout + drill_result.stderr
    assert "пройдена" in drill_result.stdout  # "passed", not "failed"
    assert "товаров 1" in drill_result.stdout
    assert "продаж 1" in drill_result.stdout
    assert "клиентов 1" in drill_result.stdout

    # The throwaway restore DB must not linger after the drill.
    check = subprocess.run(
        [
            "psql",
            "-h",
            base_env["POSTGRES_HOST"],
            "-p",
            base_env["POSTGRES_PORT"],
            "-U",
            base_env["POSTGRES_USER"],
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{scratch_db}';",
        ],
        env={**base_env, "PGPASSWORD": base_env["POSTGRES_PASSWORD"]},
        capture_output=True,
        text=True,
    )
    assert check.stdout.strip() == "", "scratch restore DB must be dropped after the drill"


@pytest.mark.parametrize(
    "missing_var,expected_snippet",
    [
        ("AGE_RECIPIENT", "AGE_RECIPIENT"),
        ("RCLONE_REMOTE", "RCLONE_REMOTE"),
    ],
)
def test_backup_fails_loudly_and_alerts_when_required_var_unset(
    tmp_path, telegram_mock, missing_var, expected_snippet
):
    """M3: a missing AGE_RECIPIENT or RCLONE_REMOTE must exit non-zero and
    alert — never silently degrade to an unencrypted or local-only backup."""
    base_url, received = telegram_mock
    backups_dir = tmp_path / "backups"

    env = {
        "PATH": __import__("os").environ["PATH"],
        **_pg_env(),
        "BACKUP_DIR": str(backups_dir),
        "AGE_RECIPIENT": "age1notreallyused0000000000000000000000000000000000000000",
        "RCLONE_REMOTE": str(tmp_path / "offsite"),
        "BACKUP_RUN_ONCE": "1",
        "TELEGRAM_STAFF_TOKEN": "dummy-token",
        "DRILL_CHAT_ID": "12345",
        "TELEGRAM_API_BASE": base_url,
    }
    env.pop(missing_var, None)

    result = _run(BACKUP_SH, env)

    assert result.returncode != 0
    assert not backups_dir.exists() or list(backups_dir.iterdir()) == []
    assert len(received) == 1, f"expected exactly one alert, got {received}"
    assert expected_snippet in received[0]["text"]


def test_backup_refuses_to_leave_plaintext_dump_after_failed_encryption(tmp_path, telegram_mock):
    """M3: a failed `age` encryption must exit non-zero, alert, and leave NO
    plaintext .dump file behind — the exact scenario the audit flagged."""
    _seed_real_data()
    base_url, received = telegram_mock
    backups_dir = tmp_path / "backups"

    env = {
        "PATH": __import__("os").environ["PATH"],
        **_pg_env(),
        "BACKUP_DIR": str(backups_dir),
        "AGE_RECIPIENT": "not-a-valid-age-recipient",
        "RCLONE_REMOTE": str(tmp_path / "offsite"),
        "BACKUP_RUN_ONCE": "1",
        "TELEGRAM_STAFF_TOKEN": "dummy-token",
        "DRILL_CHAT_ID": "12345",
        "TELEGRAM_API_BASE": base_url,
    }

    result = _run(BACKUP_SH, env)

    assert result.returncode != 0
    assert list(backups_dir.glob("*.dump")) == [], "plaintext dump left on disk after failed encryption"
    assert list(backups_dir.glob("*")) == [], "no artifact of any kind should survive a failed encryption"
    assert len(received) == 1
    assert "шифрование" in received[0]["text"]


def test_restore_drill_fails_loudly_and_alerts_on_corrupted_dump(tmp_path, telegram_mock):
    """restore_drill.sh's existing checksum-mismatch alert, exercised for
    real (not just read) — a corrupted/tampered dump must fail the drill and
    notify, never silently 'restore' bad data."""
    _seed_real_data()
    base_url, received = telegram_mock
    backups_dir = tmp_path / "backups"
    offsite_dir = tmp_path / "offsite"
    age_identity = tmp_path / "age_identity.txt"
    subprocess.run(["age-keygen", "-o", str(age_identity)], check=True, capture_output=True)
    pubkey = next(
        line.split(": ", 1)[1].strip()
        for line in age_identity.read_text().splitlines()
        if line.lower().startswith("# public key:") or line.lower().startswith("public key:")
    )

    base_env = {"PATH": __import__("os").environ["PATH"], **_pg_env()}
    backup_result = _run(
        BACKUP_SH,
        {
            **base_env,
            "BACKUP_DIR": str(backups_dir),
            "AGE_RECIPIENT": pubkey,
            "RCLONE_REMOTE": str(offsite_dir),
            "BACKUP_RUN_ONCE": "1",
        },
    )
    assert backup_result.returncode == 0

    # Tamper with the checksum sidecar so the drill's integrity check fails.
    sumfile = next(backups_dir.glob("*.dump.sha256"))
    sumfile.write_text("0" * 64 + "  tampered\n")

    drill_result = _run(
        RESTORE_DRILL_SH,
        {
            **base_env,
            "BACKUP_DIR": str(backups_dir),
            "AGE_IDENTITY_FILE": str(age_identity),
            "DRILL_SCRATCH_DB": "acocos_test_restore_drill_corrupt",
            "DRILL_RUN_ONCE": "1",
            "TELEGRAM_STAFF_TOKEN": "dummy-token",
            "DRILL_CHAT_ID": "12345",
            "TELEGRAM_API_BASE": base_url,
        },
    )

    assert drill_result.returncode != 0
    assert len(received) == 1
    assert "контрольная сумма не совпала" in received[0]["text"]
