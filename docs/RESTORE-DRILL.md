# Restore drill — run this for real, on the production host

A backup that has never been restored is not a backup (finding H1, the
2026-08-18 audit — see `docs/АУДИТ.md`). This is the focused runbook for
proving, right now, that the encrypted offsite chain actually comes back:
dump → encrypt → offsite → decrypt → restore → real data. It assumes the
stack is already deployed and the one-time backup setup in `README.md`
("One-time backup setup & deploy-day verification") has already been done —
this is the drill you re-run afterward, on demand, whenever you want proof.

The `restore-drill` service already does this automatically once a week and
Telegrams the result — this doc is for triggering ONE drill on demand and
reading its result yourself, right now, rather than waiting for Sunday or
digging through container logs.

**After deploying this fix for the first time (or after the 2026-08-18
incident's fix specifically — rebuilt image, real `secrets/rclone.conf` and
`secrets/age_identity.txt` in place), run `sh docker/verify_deployment.sh`
first.** It runs everything below in one shot — a real backup cycle, a real
restore drill, checks for stray plaintext left over from before the fix —
and prints a plain PASS/FAIL summary instead of you having to read and
interpret raw command output by hand.

**This deployment's offsite remote is `gdrive_crypt:`** — Google Drive
wrapped in an rclone crypt remote (`drive.file` scope), not the bare
Backblaze B2 remote README.md's setup walkthrough uses as its example.
Two independent layers of encryption are in play, and it matters which is
which:
1. **age** — applied by `backup.sh` itself, to the dump's *content*, before
   rclone ever sees the file. This is what `AGE_RECIPIENT`/the private key
   control, and it's what actually protects client data.
2. **rclone crypt** — applied automatically by rclone on every read/write
   through the `gdrive_crypt:` remote name (filenames and file content both
   obfuscated on Google's side). This is a second, independent layer
   protecting against Google Drive itself being compromised or misconfigured
   (e.g. a wrong sharing setting) — it is NOT a substitute for the age layer
   and does not change anything about how age decryption works.

Interacting via the `gdrive_crypt:` remote name (not the underlying bare
Drive remote) makes rclone decrypt the crypt layer transparently — `rclone
ls`/`copy` against `gdrive_crypt:` show real filenames and give you back the
same `.dump.age` file `backup.sh` produced, still age-encrypted, exactly as
if you'd read it from local disk.

## Before you run it — checklist

- [ ] `docker compose -f docker-compose.prod.yml ps` shows `db` and `backup`
      healthy/running, and at least one `acocos_*.dump.age` file exists
      under `./backups/` (run `ls -1t backups/acocos_*.dump.age | head -1`
      to check — if empty, the `backup` service hasn't completed a cycle
      yet; wait for one or trigger it manually first, see README step 5).
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
from the LOCAL `./backups/` directory — it never touches `gdrive_crypt:`.
That's fine day to day (the local copy is usually there), but the entire
point of an offsite copy is the case where it ISN'T — a dead server, a
fresh VPS, local disk gone. To prove (or actually perform) a restore from
the offsite copy itself:

```bash
# 1. See what's really out there (real filenames/sizes — the crypt layer is
#    transparent through this remote name):
rclone ls gdrive_crypt:db

# 2. Pull the newest dump back down (to your workstation, or a fresh server —
#    anywhere with the age PRIVATE key, never left only on the old server):
rclone copy gdrive_crypt:db/acocos_<stamp>.dump.age .
rclone copy gdrive_crypt:db/acocos_<stamp>.dump.sha256 .

# 3. Decrypt the age layer (this is the step that actually needs the
#    private key — rclone already handled the crypt layer in step 2):
age -d -i secrets/age_identity.txt -o restore.dump acocos_<stamp>.dump.age

# 4. Verify before trusting it:
sha256sum restore.dump   # compare against acocos_<stamp>.dump.sha256

# 5. Restore as in README's "Emergency restore" section (pg_restore into
#    either a throwaway DB to verify, or --clean --if-exists into a real one
#    to actually recover).
```

If step 1 shows nothing, or shows only obfuscated/garbled names, check
you're using the `gdrive_crypt:` remote name and not the bare underlying
Drive remote — `rclone listremotes` on the host that has `rclone.conf`
configured shows what's actually set up.

## What this drill does NOT prove

It proves the *chain* works (encryption, offsite round-trip, decryption,
restore, and that restored data looks sane). It does not prove:
- the backup covers `media/` (uploaded product photos) — that's a separate
  `rclone sync` in `backup.sh`, not restored or checked by this drill;
- a restore under real outage conditions (a dead server, DNS, a fresh VPS)
  — see README's "Emergency restore" section for that fuller scenario;
- that `AGE_RECIPIENT`/`RCLONE_REMOTE` are correctly set on THIS host
  specifically — a drill run from a misconfigured host with a stale local
  copy of an old dump can still "pass" against outdated data. Cross-check
  the dump's timestamp in the success message against `date`.
