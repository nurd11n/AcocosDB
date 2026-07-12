#!/bin/sh
# Nightly pg_dump with simple retention. Runs inside the `backup` service.
set -e
mkdir -p /backups
while true; do
  STAMP=$(date +%Y-%m-%d_%H%M)
  echo "Backup $STAMP"
  pg_dump -Fc -h db -U "$POSTGRES_USER" "$POSTGRES_DB" > "/backups/acocos_$STAMP.dump"
  # keep 14 days locally; sync /backups offsite with rclone/B2 from the host or a cron
  find /backups -name 'acocos_*.dump' -mtime +14 -delete
  sleep 86400
done
