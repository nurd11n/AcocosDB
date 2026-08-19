#!/bin/sh
# Run this ON THE PRODUCTION HOST, after deploying the Phase 1 backup-chain
# fix, to prove — not assume — the whole chain actually works end to end.
# Exits 0 only if every check below passes; prints a clear PASS/FAIL summary
# either way. Safe to re-run any time; makes no destructive changes (the
# restore drill it triggers uses its own throwaway scratch database, and is
# dropped automatically whether it passes or fails).
#
# Usage: sh docker/verify_deployment.sh [path-to-docker-compose.prod.yml]
set -u

COMPOSE_FILE="${1:-docker-compose.prod.yml}"
FAILED=0

pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAILED=1; }

dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

echo "=== 1. Both services are up, not restart-looping (read this yourself — see note) ==="
# Deliberately NOT auto-parsed into pass/fail: docker compose ps's table
# columns are whitespace-separated but the COMMAND column's own value
# ("sh /usr/local/bin/backup.sh") contains embedded spaces inside quotes,
# which breaks naive column splitting (verified against real output before
# writing this — a naive parse silently matched nothing). A --format Go
# template would avoid that, but this script has no real Docker to verify
# the exact template output against, and a confidently-wrong PASS here is
# worse than asking you to glance at two lines yourself.
dc ps backup restore-drill 2>&1 | sed 's/^/  /'
echo "  ^ confirm both rows say Up (not Restarting/Exited) before continuing."

echo ""
echo "=== 2. Force one real backup cycle ==="
BACKUP_OUT=$(dc run --rm -e BACKUP_RUN_ONCE=1 backup 2>&1)
echo "$BACKUP_OUT" | sed 's/^/  /'
if echo "$BACKUP_OUT" | grep -q "done (run-once)"; then
  pass "backup cycle completed"
else
  fail "backup cycle did not complete cleanly (no 'done (run-once)' line above)"
fi
if echo "$BACKUP_OUT" | grep -qi "AGE_RECIPIENT не задан\|RCLONE_REMOTE не задан\|директория, а не файл\|шифрование age завершилось"; then
  fail "backup.sh's own fail-fast checks fired — see the ❌ line in its output above, fix that first"
fi
if echo "$BACKUP_OUT" | grep -qi "didn't find section in config file\|rclone sync (db) завершился с ошибкой"; then
  fail "rclone still can't reach the offsite remote for the DB sync — rclone.conf on this host is not the one your remote actually needs"
else
  pass "no rclone config error in backup output"
fi

echo ""
echo "=== 3. Newest backup is encrypted, not plaintext ==="
NEWEST=$(ls -1t backups/acocos_*.dump.age 2>/dev/null | head -n1)
if [ -n "$NEWEST" ]; then
  pass "found $NEWEST"
else
  fail "no acocos_*.dump.age file found in ./backups/ at all"
fi
STRAY_PLAINTEXT=$(find backups -maxdepth 1 -name 'acocos_*.dump' 2>/dev/null)
if [ -z "$STRAY_PLAINTEXT" ]; then
  pass "no stray plaintext .dump files in ./backups/"
else
  fail "plaintext dump(s) still present (pre-fix debris, won't self-clean — delete by hand): $STRAY_PLAINTEXT"
fi

echo ""
echo "=== 4. Real restore drill against the dump just made ==="
DRILL_OUT=$(dc run --rm -e DRILL_RUN_ONCE=1 restore-drill 2>&1)
echo "$DRILL_OUT" | sed 's/^/  /'
if echo "$DRILL_OUT" | grep -qi "пройдена"; then
  pass "restore drill passed"
else
  fail "restore drill did not report success (see ❌ line above for which check failed)"
fi

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo "=== ALL CHECKS PASSED — the backup chain is genuinely working. ==="
  exit 0
else
  echo "=== ONE OR MORE CHECKS FAILED — see ❌ lines above. Not done yet. ==="
  exit 1
fi
