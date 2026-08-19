#!/bin/sh
# Encrypted, checksummed, offsite backups every 6 hours.
#
# Each run: pg_dump -Fc  ->  sha256  ->  age-encrypt  ->  rclone to B2  ->  prune.
# Dumps hold client names, phones, and debts, so they are encrypted BEFORE they
# ever leave the box with an age recipient whose private key is NOT on the server
# (set AGE_RECIPIENT in .env). Local copy AND offsite copy: a fire, an rm -rf, and
# ransomware are different threats. Retention is tiered (see prune_backups.py).
#
# Required env (.env): POSTGRES_USER, POSTGRES_DB, AGE_RECIPIENT, RCLONE_REMOTE.
# Both AGE_RECIPIENT and RCLONE_REMOTE are REQUIRED, not optional (2026-08-18
# audit, M3): a backup job that silently degrades to unencrypted local-only
# dumps is worse than one that refuses to run at all — nobody watches container
# logs for a stray WARNING line, but everybody notices a container stuck
# restarting in `docker compose ps`. Optional: TELEGRAM_STAFF_TOKEN +
# DRILL_CHAT_ID (same chat restore_drill.sh already alerts) to get a Telegram
# ping on failure instead of only a log line.
# POSTGRES_HOST/POSTGRES_PORT (default db/5432, the Compose service),
# BACKUP_DIR (default /backups, the Compose volume mount), and
# RCLONE_CONFIG_PATH (default /root/.config/rclone/rclone.conf, the Compose
# mount target) let a test point this script at a throwaway Postgres
# instance and scratch files instead of touching prod.
#
# Optional: HEALTHCHECKS_PING_URL (a healthchecks.io check URL, or anything
# speaking the same GET-to-mark-alive protocol). Pinged once at the end of
# EVERY completed cycle. This is deliberately a different failure mode than
# the ❌/⚠️ Telegram alerts above: those fire when the script runs and finds
# something wrong; this catches the script not running AT ALL — the host is
# down, the container never started, `restart: unless-stopped` is looping on
# a crash before ever reaching this line — cases where nothing here ever gets
# a chance to alert on its own. healthchecks.io (or equivalent) alerts you
# when an expected ping DOESN'T arrive in time, so the silence itself is the
# signal. Never fails the cycle if the ping itself doesn't get through.
set -eu

BACKUP_DIR=${BACKUP_DIR:-/backups}
INTERVAL=${BACKUP_INTERVAL_SECONDS:-21600} # 6 hours
DB_HOST=${POSTGRES_HOST:-db}
DB_PORT=${POSTGRES_PORT:-5432}

# pg_dump reads PGPASSWORD, not POSTGRES_PASSWORD (that name is specific to
# the official postgres image's own first-run init, not a libpq client
# convention) — without this, pg_dump has no password to offer the `db`
# service over the network and every cycle fails auth (mirrors
# restore_drill.sh, which already does this correctly for its psql/pg_restore
# calls).
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

# Same alert pattern as restore_drill.sh's notify() — deliberately duplicated
# rather than shared, since these are two independent POSIX sh scripts with no
# common entrypoint to source a shared file from in the backup image.
notify() {
  # $1 = message. Never fails the caller if Telegram is unreachable — the
  # caller's own exit code (not this function) is what actually signals
  # success/failure to `docker compose ps` / restart policy.
  # TELEGRAM_API_BASE (default the real API) lets a test point this at a
  # local mock server to assert the alert actually fires, without hitting
  # the network or a real bot token.
  if [ -n "${TELEGRAM_STAFF_TOKEN:-}" ] && [ -n "${DRILL_CHAT_ID:-}" ]; then
    curl -s -m 20 -o /dev/null \
      "${TELEGRAM_API_BASE:-https://api.telegram.org}/bot${TELEGRAM_STAFF_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${DRILL_CHAT_ID}" \
      --data-urlencode "text=$1" || true
  fi
  echo "[backup] $1" >&2
}

ping_healthcheck() {
  # Dead-man's-switch ping — see the HEALTHCHECKS_PING_URL note above. A GET
  # is all healthchecks.io's protocol needs; -fsS -m 10 --retry 2 keeps this
  # from ever hanging the backup cycle, and the || true means a monitoring
  # ping failing never fails (or even logs loudly for) the backup itself.
  if [ -n "${HEALTHCHECKS_PING_URL:-}" ]; then
    curl -fsS -m 10 --retry 2 -o /dev/null "$HEALTHCHECKS_PING_URL" || true
  fi
}

# Fail fast, before the loop even starts: a misconfigured backup job should
# never begin producing degraded output, it should refuse to run at all.
if [ -z "${AGE_RECIPIENT:-}" ]; then
  notify "❌ ACOCOS backup: AGE_RECIPIENT не задан — резервное копирование остановлено, дампы НЕ создаются."
  exit 1
fi
if [ -z "${RCLONE_REMOTE:-}" ]; then
  notify "❌ ACOCOS backup: RCLONE_REMOTE не задан — офсайт-копирование не настроено, резервное копирование остановлено."
  exit 1
fi
# Same production incident class as restore_drill.sh's AGE_IDENTITY_FILE
# check (docs/АУДИТ-follow-up.md F2): docker-compose.prod.yml bind-mounts
# ./secrets/rclone.conf into the container. If that host file never existed
# when the container was created, Docker silently substitutes an empty
# DIRECTORY at this path instead of erroring — rclone then fails every
# single sync with a confusing "didn't find section in config file" or
# "is a directory" error, indistinguishable in logs from a routine problem.
# Only a DIRECTORY here is wrong — the path simply not existing is fine
# (valid for a local-filesystem RCLONE_REMOTE or a plain RCLONE_CONFIG_*
# env-var remote that needs no file at all, exactly what this project's own
# tests use), and an empty real file is also fine (a deliberate placeholder
# — see the mount's own comment in docker-compose.prod.yml).
RCLONE_CONFIG_PATH="${RCLONE_CONFIG_PATH:-/root/.config/rclone/rclone.conf}"
if [ -d "$RCLONE_CONFIG_PATH" ]; then
  notify "❌ ACOCOS backup: $RCLONE_CONFIG_PATH — это директория, а не файл (secrets/rclone.conf не был создан на хосте до запуска контейнера) — офсайт-копирование сломано."
  exit 1
fi

mkdir -p "$BACKUP_DIR"

while true; do
  STAMP=$(date +%Y-%m-%d_%H%M%S)
  BASE="$BACKUP_DIR/acocos_$STAMP.dump"
  echo "[backup] $STAMP starting"

  # 1. Dump (custom format, compressed). A transient failure here (DB briefly
  # restarting, a network blip) is worth retrying next cycle rather than
  # crash-looping the whole container — unlike a missing AGE_RECIPIENT/
  # RCLONE_REMOTE or a failed encryption, this one is plausibly self-healing.
  if ! pg_dump -Fc -h "$DB_HOST" -p "$DB_PORT" -U "$POSTGRES_USER" "$POSTGRES_DB" > "$BASE"; then
    rm -f "$BASE"
    notify "❌ ACOCOS backup: pg_dump завершился с ошибкой ($STAMP) — повтор через ${INTERVAL}с."
    sleep "$INTERVAL"
    continue
  fi

  # 2. Checksum the plaintext dump so a restore can prove integrity. Two
  # steps, not a `sha256sum | sed` pipe: under `set -eu` (no `pipefail` in
  # POSIX sh) a pipeline's exit status is only its LAST command's, and sed
  # always succeeds even on empty input — a failing sha256sum would silently
  # produce an empty/wrong .sha256 file instead of stopping the cycle.
  if ! sha256sum "$BASE" > "$BASE.sha256.tmp"; then
    rm -f "$BASE" "$BASE.sha256.tmp"
    notify "❌ ACOCOS backup: sha256sum завершился с ошибкой ($STAMP) — незашифрованный дамп удалён, не оставлен на диске без контрольной суммы."
    exit 1
  fi
  sed "s|$BACKUP_DIR/||" "$BASE.sha256.tmp" > "$BASE.sha256"
  rm -f "$BASE.sha256.tmp"

  # 3. Encrypt with age; remove the plaintext so only the .age copy remains.
  # A failed encryption is fatal (M3): it must never leave an unencrypted
  # dump sitting on disk, and it must never be treated as "fine, try again
  # later" the way a transient pg_dump hiccup is — an age failure usually
  # means a real misconfiguration (bad recipient, disk full), not a blip.
  if ! age -r "$AGE_RECIPIENT" -o "$BASE.age" "$BASE"; then
    rm -f "$BASE" "$BASE.age" "$BASE.sha256"
    notify "❌ ACOCOS backup: шифрование age завершилось с ошибкой ($STAMP) — незашифрованный дамп удалён, не оставлен на диске."
    exit 1
  fi
  rm -f "$BASE"
  echo "[backup] encrypted -> $(basename "$BASE").age"

  # 4. Offsite: sync the encrypted dumps + checksums + media to B2. A single
  # sync failure stays non-fatal (retried next cycle, like pg_dump above) —
  # the encrypted dump is still safe on local disk either way — but is now
  # alerted, where it previously only logged a stderr line nobody watches.
  if ! rclone sync "$BACKUP_DIR" "$RCLONE_REMOTE/db" --include "*.age" --include "*.sha256"; then
    notify "⚠️ ACOCOS backup: rclone sync (db) завершился с ошибкой ($STAMP) — локальная копия зашифрована и цела, офсайт-копия не обновлена."
  fi
  if ! rclone sync /media "$RCLONE_REMOTE/media"; then
    notify "⚠️ ACOCOS backup: rclone sync (media) завершился с ошибкой ($STAMP)."
  fi
  echo "[backup] offsite sync done"

  # 5. Prune old local backups by the retention tiers (offsite mirrors via sync).
  # Resolved relative to this script's own location (both are copied to
  # /usr/local/bin together by backup.Dockerfile) rather than a hardcoded
  # absolute path, so the same script also runs unmodified straight out of
  # the repo checkout (docker/backup.sh + docker/prune_backups.py) for tests.
  SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
  python3 "$SCRIPT_DIR/prune_backups.py" "$BACKUP_DIR" || notify "⚠️ ACOCOS backup: prune_backups.py завершился с ошибкой ($STAMP) — старые локальные копии не подчищены, сам бэкап тем не менее сделан и выгружен."

  # 6. Dead-man's-switch ping: the cycle reached the end (dump + encrypt
  # succeeded — an offsite-sync or prune hiccup above already alerted on its
  # own and doesn't block this). See the HEALTHCHECKS_PING_URL note up top.
  ping_healthcheck

  # BACKUP_RUN_ONCE=1 runs a single cycle and exits — used for the deploy-day
  # manual verification (see README). Unset (the service default) loops forever.
  if [ -n "${BACKUP_RUN_ONCE:-}" ]; then
    echo "[backup] $STAMP done (run-once)"
    exit 0
  fi
  echo "[backup] $STAMP done; sleeping ${INTERVAL}s"
  sleep "$INTERVAL"
done
