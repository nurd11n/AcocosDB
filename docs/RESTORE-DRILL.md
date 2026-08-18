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
