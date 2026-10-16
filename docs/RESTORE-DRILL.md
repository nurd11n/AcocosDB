# Backup restore drill

Verify the encrypted backup chain against a throwaway PostgreSQL database:
dump → encrypt → offsite copy → decrypt → restore → validate.
Complete the [backup setup](DEPLOYMENT.md#backups) before running a drill.

The `restore-drill` service runs weekly. To validate a deployment on demand,
run `sh docker/verify_deployment.sh`: it triggers a backup and restore cycle,
checks for unexpected plaintext backups, and prints a PASS/FAIL summary.

The offsite destination comes from `RCLONE_REMOTE` in the ignored `.env`.
The examples below use `YOUR_CRYPT_REMOTE:` as a placeholder for an rclone
crypt remote. Replace it with your configured remote; a crypt layer is optional
and separate from the age encryption applied by the backup scripts.

When using a crypt remote, access files through that remote so rclone decrypts
its layer transparently. The downloaded dump remains age-encrypted and still
requires the private age identity to restore.

## Before you run it — checklist

- [ ] `docker compose -f docker-compose.prod.yml ps` shows `db` and `backup`
      healthy/running, and at least one `acocos_*.dump.age` file exists
      under `./backups/` (run `ls -1t backups/acocos_*.dump.age | head -1`
      to check — if empty, the `backup` service hasn't completed a cycle
      yet; wait for one or trigger it manually first, see the deployment guide).
- [ ] `./secrets/age_identity.txt` exists on the server (the restore-drill
      container needs it to decrypt) — but confirm the PRIVATE key is **not
      only** here: it must also be in a password manager or otherwise
      off-server. If this file is the only copy anywhere, losing this
      server loses the ability to ever decrypt any backup, which defeats
      the entire point. Fix that before trusting the drill's "success."
- [ ] `.env` has `AGE_RECIPIENT`, `RCLONE_REMOTE`, `TELEGRAM_STAFF_TOKEN`,
      and `DRILL_CHAT_ID` all filled in (`grep -E
      'AGE_RECIPIENT=|RCLONE_REMOTE=|DRILL_CHAT_ID=' .env` — none blank).

## Run one drill

```bash
cd /path/to/acocosDB   # project root, on the server
docker compose -f docker-compose.prod.yml run --rm \
  -e DRILL_RUN_ONCE=1 restore-drill
echo "exit code: $?"
```

`DRILL_RUN_ONCE=1` runs exactly one drill cycle and exits — with the
container's real exit status reflecting the result (0 = passed, 1 =
failed) — instead of the always-running weekly loop, so you get an answer
in seconds, not up to seven days.

## Reading the result

**Success** looks like this (both in the terminal and as a Telegram message
to `DRILL_CHAT_ID`):

```
[drill] restoring /backups/acocos_2026-08-18_060000.dump.age
[drill] ✅ Проверка бэкапа ACOCOS пройдена: товаров 214, продаж 1893, клиентов 340; последняя продажа 0 дн. назад.
exit code: 0
```

That means: the newest encrypted dump was found, decrypted with the age
private key, its checksum matched what was recorded at backup time,
`pg_restore` rebuilt it into a throwaway database without error, and that
restored database has real products/sales/clients and the newest confirmed
sale is less than 8 days old (i.e. the backup isn't stale). The throwaway
database is dropped automatically either way — nothing lingers.

**Failure** always ends `exit code: 1` and a ❌ message. What each one means:

| Message contains | Meaning | What to do |
|---|---|---|
| «не найден ни один зашифрованный дамп» | No `acocos_*.dump.age` file in `./backups/` | The `backup` service hasn't produced one yet, or `BACKUP_DIR`/volume mount is wrong. Check `docker compose logs backup`. |
| «не удалось расшифровать дамп» | `age -d` failed | Wrong/corrupted `age_identity.txt`, or the dump was encrypted to a different `AGE_RECIPIENT` than this key decrypts. Compare the public key in `.env`'s `AGE_RECIPIENT` against `age-keygen -y secrets/age_identity.txt`. |
| «контрольная сумма не совпала — дамп повреждён» | The decrypted dump's sha256 doesn't match the `.sha256` sidecar recorded at backup time | The dump (or its sidecar) was corrupted or tampered with in transit/at rest. Do not trust this dump — try an older one and investigate how it got corrupted. |
| «pg_restore завершился с ошибкой» | The decrypted, checksum-verified dump failed to restore into Postgres | Likely a Postgres version mismatch or a genuinely malformed dump (rare if the checksum passed). Check the full container log for `pg_restore`'s own stderr. |
| «данные подозрительны» | Restore succeeded, but row counts are zero or the newest sale is 8+ days old | The dump is technically restorable but doesn't look like real, current business data — investigate before trusting it as a fallback. |

If you get any ❌, the automated weekly drill will alert again on its own
schedule, but don't wait for that — a failed drill means the backup chain
is not currently proven, and the same class of problem may affect the NEXT
scheduled backup too, so track it down now.

## Restoring from the offsite copy directly (not the local drill)

The drill above (`restore-drill` service / `DRILL_RUN_ONCE=1`) always reads
from the LOCAL `./backups/` directory — it never touches `YOUR_CRYPT_REMOTE:`.
That's fine day to day (the local copy is usually there), but the entire
point of an offsite copy is the case where it ISN'T — a dead server, a
fresh VPS, local disk gone. To prove (or actually perform) a restore from
the offsite copy itself:

```bash
# 1. See what's really out there (real filenames/sizes — the crypt layer is
#    transparent through this remote name):
rclone ls YOUR_CRYPT_REMOTE:db

# 2. Pull the newest dump back down (to your workstation, or a fresh server —
#    anywhere with the age PRIVATE key, never left only on the old server):
rclone copy YOUR_CRYPT_REMOTE:db/acocos_<stamp>.dump.age .
rclone copy YOUR_CRYPT_REMOTE:db/acocos_<stamp>.dump.sha256 .

# 3. Decrypt the age layer (this is the step that actually needs the
#    private key — rclone already handled the crypt layer in step 2):
age -d -i secrets/age_identity.txt -o restore.dump acocos_<stamp>.dump.age

# 4. Verify before trusting it:
sha256sum restore.dump   # compare against acocos_<stamp>.dump.sha256

# 5. Restore as in the deployment guide's "Emergency restore" section (pg_restore into
#    either a throwaway DB to verify, or --clean --if-exists into a real one
#    to actually recover).
```

If step 1 shows nothing, or shows only obfuscated/garbled names, check
you're using the `YOUR_CRYPT_REMOTE:` remote name and not the bare underlying
Drive remote — `rclone listremotes` on the host that has `rclone.conf`
configured shows what's actually set up.

## What this drill does NOT prove

It proves the *chain* works (encryption, offsite round-trip, decryption,
restore, and that restored data looks sane). It does not prove:
- the backup covers `media/` (uploaded product photos) — that's a separate
  `rclone sync` in `backup.sh`, not restored or checked by this drill;
- a restore under real outage conditions (a dead server, DNS, a fresh VPS)
  — see the deployment guide's "Emergency restore" section for that fuller scenario;
- that `AGE_RECIPIENT`/`RCLONE_REMOTE` are correctly set on THIS host
  specifically — a drill run from a misconfigured host with a stale local
  copy of an old dump can still "pass" against outdated data. Cross-check
  the dump's timestamp in the success message against `date`.
