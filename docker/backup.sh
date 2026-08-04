#!/bin/sh
# Encrypted, checksummed, offsite backups every 6 hours.
#
# Each run: pg_dump -Fc  ->  sha256  ->  age-encrypt  ->  rclone to B2  ->  prune.
# Dumps hold client names, phones, and debts, so they are encrypted BEFORE they
# ever leave the box with an age recipient whose private key is NOT on the server
# (set AGE_RECIPIENT in .env). Local copy AND offsite copy: a fire, an rm -rf, and
# ransomware are different threats. Retention is tiered (see prune_backups.py).
#
# Required env (.env): POSTGRES_USER, POSTGRES_DB, AGE_RECIPIENT.
# Optional: RCLONE_REMOTE (e.g. "b2:acocos-backups") to enable offsite sync.
set -eu

BACKUP_DIR=/backups
INTERVAL=${BACKUP_INTERVAL_SECONDS:-21600} # 6 hours

# pg_dump reads PGPASSWORD, not POSTGRES_PASSWORD (that name is specific to
# the official postgres image's own first-run init, not a libpq client
# convention) — without this, pg_dump has no password to offer the `db`
# service over the network and every cycle fails auth (mirrors
# restore_drill.sh, which already does this correctly for its psql/pg_restore
# calls).
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

mkdir -p "$BACKUP_DIR"

while true; do
  STAMP=$(date +%Y-%m-%d_%H%M%S)
  BASE="$BACKUP_DIR/acocos_$STAMP.dump"
  echo "[backup] $STAMP starting"

  # 1. Dump (custom format, compressed).
  if ! pg_dump -Fc -h db -U "$POSTGRES_USER" "$POSTGRES_DB" > "$BASE"; then
    echo "[backup] pg_dump FAILED — skipping this cycle" >&2
    rm -f "$BASE"
    sleep "$INTERVAL"
    continue
  fi

  # 2. Checksum the plaintext dump so a restore can prove integrity.
  sha256sum "$BASE" | sed "s|$BACKUP_DIR/||" > "$BASE.sha256"

  # 3. Encrypt with age; remove the plaintext so only the .age copy remains.
  if [ -n "${AGE_RECIPIENT:-}" ]; then
    age -r "$AGE_RECIPIENT" -o "$BASE.age" "$BASE"
    rm -f "$BASE"
    echo "[backup] encrypted -> $(basename "$BASE").age"
  else
    echo "[backup] WARNING: AGE_RECIPIENT unset — leaving dump UNENCRYPTED" >&2
  fi

  # 4. Offsite: sync the encrypted dumps + checksums + media to B2.
  if [ -n "${RCLONE_REMOTE:-}" ]; then
    rclone sync "$BACKUP_DIR" "$RCLONE_REMOTE/db" \
      --include "*.age" --include "*.sha256" || echo "[backup] rclone db sync failed" >&2
    rclone sync /media "$RCLONE_REMOTE/media" || echo "[backup] rclone media sync failed" >&2
    echo "[backup] offsite sync done"
  else
    echo "[backup] RCLONE_REMOTE unset — offsite sync skipped"
  fi

  # 5. Prune old local backups by the retention tiers (offsite mirrors via sync).
  python3 /usr/local/bin/prune_backups.py "$BACKUP_DIR" || echo "[backup] prune failed" >&2

  # BACKUP_RUN_ONCE=1 runs a single cycle and exits — used for the deploy-day
  # manual verification (see README). Unset (the service default) loops forever.
  if [ -n "${BACKUP_RUN_ONCE:-}" ]; then
    echo "[backup] $STAMP done (run-once)"
    exit 0
  fi
  echo "[backup] $STAMP done; sleeping ${INTERVAL}s"
  sleep "$INTERVAL"
done
