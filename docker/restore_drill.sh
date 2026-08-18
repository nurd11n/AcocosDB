#!/bin/sh
# Weekly restore drill — a backup that has never been restored is not a backup.
#
# Pulls the newest encrypted dump, verifies its checksum, decrypts it, restores
# it into a THROWAWAY database, runs sanity assertions (row counts > 0 on the
# core tables; the newest sale is < 8 days old), drops the scratch DB, and
# Telegrams the Owner the result — success OR failure. A drill nobody reads is
# not a drill.
#
# Required env: POSTGRES_USER, POSTGRES_DB, AGE_IDENTITY_FILE (path to the age
# private key, mounted read-only), TELEGRAM_STAFF_TOKEN, DRILL_CHAT_ID.
# Optional: DRILL_INTERVAL_SECONDS (default 7 days). POSTGRES_HOST/
# POSTGRES_PORT (default db/5432, the Compose service), BACKUP_DIR (default
# /backups, the Compose volume mount), and DRILL_SCRATCH_DB (default
# acocos_restore_drill) let a test point this script at a throwaway Postgres
# instance and scratch directory instead of touching prod.
set -u

INTERVAL=${DRILL_INTERVAL_SECONDS:-604800} # 7 days
BACKUP_DIR=${BACKUP_DIR:-/backups}
SCRATCH_DB=${DRILL_SCRATCH_DB:-acocos_restore_drill}
DB_HOST=${POSTGRES_HOST:-db}
DB_PORT=${POSTGRES_PORT:-5432}

notify() {
  # $1 = message. Never fails the drill if Telegram is unreachable. The staff
  # bot's token — this is an operational alert, not client-facing.
  # TELEGRAM_API_BASE (default the real API) lets a test point this at a
  # local mock server to assert the alert actually fires, without hitting
  # the network or a real bot token.
  if [ -n "${TELEGRAM_STAFF_TOKEN:-}" ] && [ -n "${DRILL_CHAT_ID:-}" ]; then
    curl -s -m 20 -o /dev/null \
      "${TELEGRAM_API_BASE:-https://api.telegram.org}/bot${TELEGRAM_STAFF_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${DRILL_CHAT_ID}" \
      --data-urlencode "text=$1" || true
  fi
  echo "[drill] $1"
}

# Fail fast, before the loop even starts (2026-08-18 production incident,
# docs/АУДИТ-follow-up.md F2): a bind-mounted file whose host source doesn't
# exist yet becomes a silent empty DIRECTORY at that path — age then fails
# with a confusing "is a directory" error, once per cycle, forever, with no
# alert distinguishing it from a routine decrypt failure. Checking for a real
# regular file up front turns that into one unambiguous, loud failure.
if [ ! -f "${AGE_IDENTITY_FILE:-}" ]; then
  notify "❌ Проверка бэкапа ACOCOS: AGE_IDENTITY_FILE не найден или не является файлом (${AGE_IDENTITY_FILE:-не задан}) — восстановление невозможно."
  exit 1
fi

run_drill() {
  LATEST=$(ls -1t "$BACKUP_DIR"/acocos_*.dump.age 2>/dev/null | head -n1)
  if [ -z "$LATEST" ]; then
    notify "❌ Проверка бэкапа ACOCOS: не найден ни один зашифрованный дамп."
    return 1
  fi
  echo "[drill] restoring $LATEST"

  # Verify checksum of the (plaintext) dump if the .sha256 is present.
  PLAIN="/tmp/drill.dump"
  if ! age -d -i "$AGE_IDENTITY_FILE" -o "$PLAIN" "$LATEST"; then
    notify "❌ Проверка бэкапа ACOCOS: не удалось расшифровать дамп."
    return 1
  fi
  SUMFILE="${LATEST%.age}.sha256"
  if [ -f "$SUMFILE" ]; then
    EXPECT=$(awk '{print $1}' "$SUMFILE")
    ACTUAL=$(sha256sum "$PLAIN" | awk '{print $1}')
    if [ "$EXPECT" != "$ACTUAL" ]; then
      notify "❌ Проверка бэкапа ACOCOS: контрольная сумма не совпала — дамп повреждён."
      rm -f "$PLAIN"
      return 1
    fi
  fi

  # Restore into a fresh throwaway DB.
  export PGPASSWORD="${POSTGRES_PASSWORD:-}"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH_DB;" >/dev/null 2>&1
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE $SCRATCH_DB;" >/dev/null 2>&1
  if ! pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$POSTGRES_USER" -d "$SCRATCH_DB" --no-owner "$PLAIN" >/dev/null 2>&1; then
    notify "❌ Проверка бэкапа ACOCOS: pg_restore завершился с ошибкой."
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH_DB;" >/dev/null 2>&1
    rm -f "$PLAIN"
    return 1
  fi

  # Sanity assertions against the restored data.
  Q="SELECT
       (SELECT count(*) FROM inventory_product),
       (SELECT count(*) FROM sales_saleorder),
       (SELECT count(*) FROM clients_client),
       (SELECT COALESCE(EXTRACT(DAY FROM now() - max(confirmed_at)), 999)
          FROM sales_saleorder WHERE status='confirmed');"
  ROW=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$POSTGRES_USER" -d "$SCRATCH_DB" -tA -F'|' -c "$Q" 2>/dev/null)
  PRODUCTS=$(echo "$ROW" | cut -d'|' -f1)
  SALES=$(echo "$ROW" | cut -d'|' -f2)
  CLIENTS=$(echo "$ROW" | cut -d'|' -f3)
  SALE_AGE=$(echo "$ROW" | cut -d'|' -f4 | cut -d'.' -f1)

  psql -h "$DB_HOST" -p "$DB_PORT" -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH_DB;" >/dev/null 2>&1
  rm -f "$PLAIN"

  if [ "${PRODUCTS:-0}" -gt 0 ] && [ "${SALES:-0}" -gt 0 ] && [ "${CLIENTS:-0}" -gt 0 ] \
     && [ "${SALE_AGE:-999}" -lt 8 ]; then
    notify "✅ Проверка бэкапа ACOCOS пройдена: товаров $PRODUCTS, продаж $SALES, клиентов $CLIENTS; последняя продажа $SALE_AGE дн. назад."
    return 0
  fi
  notify "❌ Проверка бэкапа ACOCOS: восстановление прошло, но данные подозрительны (товаров=$PRODUCTS продаж=$SALES клиентов=$CLIENTS возраст_последней_продажи=$SALE_AGE дн)."
  return 1
}

# DRILL_RUN_ONCE=1 runs a single drill and exits with run_drill's own status
# (0 success, 1 failure) instead of looping forever — used for a manual
# on-demand drill (see docs/RESTORE-DRILL.md) and by the test suite, both of
# which need a real exit code to check rather than only tailing logs/Telegram
# of a service that never exits. Mirrors backup.sh's BACKUP_RUN_ONCE.
if [ -n "${DRILL_RUN_ONCE:-}" ]; then
  run_drill
  exit $?
fi

while true; do
  run_drill || echo "[drill] drill failed"
  sleep "$INTERVAL"
done
