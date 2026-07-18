# Backup/restore-drill image: postgres client tools + age (encryption) + rclone
# (offsite sync) + python3 (tiered retention pruning). Kept separate from the app
# image so the app doesn't carry backup tooling.
FROM postgres:16

RUN apt-get update && apt-get install -y --no-install-recommends \
        age rclone python3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/backup.sh /usr/local/bin/backup.sh
COPY docker/restore_drill.sh /usr/local/bin/restore_drill.sh
COPY docker/prune_backups.py /usr/local/bin/prune_backups.py
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore_drill.sh
