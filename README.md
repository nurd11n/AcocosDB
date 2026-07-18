# ACOCOS CRM

Internal system for ACOCOS — inventory, sales, payments, clients, debts, a daily
Russian report, Telegram/WhatsApp bots, and marketing broadcasts. Two surfaces,
one Django project: the manager terminal at `/pos/` (an installable PWA) and the
admin panel at `/panel/`. See `CLAUDE.md` for the full design.

## Run it

```bash
cp .env.example .env          # then fill in the values (see comments in the file)
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
docker compose -f docker-compose.prod.yml exec web python manage.py setup_roles
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

Local dev without HTTPS: `make dev` (uses `docker-compose.yml`, DEBUG on).
`make test` runs the suite **and** `manage.py check --deploy`.

Health probe: `GET /healthz/` → `200 ok` when DB + cache are up, `503` otherwise.

### First login with 2FA (TOTP bootstrap)

In production `OTP_ENABLED=True`, so the login form demands a 6-digit code. A
freshly created superuser has **no** TOTP device yet — so it can't log in to
enroll one. Break the chicken-and-egg with a one-time static token:

```bash
# 1. Mint a single-use login code for the account (note what it prints):
docker compose -f docker-compose.prod.yml exec web \
  python manage.py addstatictoken <username>          # e.g. prints  42qfwkse

# 2. Go to https://YOUR-DOMAIN/login/ and sign in with:
#      username + password + that token in the "Код подтверждения" field.
#    (This burns the token — it works once.)

# 3. Now enroll a real authenticator: in /panel/ → OTP → "TOTP devices" →
#    Add, pick your user, save, then scan the QR with Google Authenticator /
#    Aegis / 1Password and enter the confirmation code. Done.
#
# From here on you log in with the rotating code from that app. If you ever get
# locked out, run addstatictoken again from the server shell.
```

Verified end-to-end on a clean database: with no code the login is refused; with
the static token it succeeds and lands on `/pos/`.

## Backups

The `backup` service dumps Postgres every 6 hours, checksums it, encrypts it with
**age** (to `AGE_RECIPIENT`, whose private key is NOT on the server), and `rclone
sync`s the encrypted dumps + `media/` offsite to Backblaze B2. Retention is tiered
(4×/day for 7 days, daily for 30 days, weekly for 6 months). The `restore-drill`
service restores the latest dump into a throwaway DB weekly, asserts the data is
sane, and Telegrams the Owner the result.

### One-time backup setup & deploy-day verification (do this by hand, once)

Run every command from the project root **on the server**, except where it says
"on your workstation." Replace `YOUR-BUCKET` throughout. This proves the whole
chain — dump → encrypt → offsite → pull back → decrypt → restore — before you
trust it. Do not skip the verification half.

**1. Generate the age keypair (on your workstation, NOT the server).**
```bash
age-keygen -o age_identity.txt
# Prints "Public key: age1xxxx...". The file also contains the line
# "AGE-SECRET-KEY-1...". That secret is the ONLY thing that can decrypt a backup.
```

**2. Store the PRIVATE key where it will survive the server dying.**
```bash
# Put the whole age_identity.txt into your password manager (1Password/Bitpass/…).
# The private key must NOT live only on the server — if the box is lost, an
# on-server-only key is lost with it and every backup is unreadable.
# The restore-drill container needs a copy to run weekly, so also place one here:
mkdir -p secrets && chmod 700 secrets
mv age_identity.txt secrets/            # ./secrets is gitignored
chmod 600 secrets/age_identity.txt
```

**3. Fill in `.env`** (the public key + Backblaze B2 creds + drill chat):
```bash
AGE_RECIPIENT=age1xxxx...               # the PUBLIC key from step 1
RCLONE_REMOTE=b2:YOUR-BUCKET            # empty = local-only, no offsite
RCLONE_CONFIG_B2_TYPE=b2
RCLONE_CONFIG_B2_ACCOUNT=<b2 keyID>
RCLONE_CONFIG_B2_KEY=<b2 applicationKey>
DRILL_CHAT_ID=<owner's Telegram chat id>
```
Create the private B2 bucket named `YOUR-BUCKET` in the Backblaze console first.

**4. Load `.env` into your shell** (so `$POSTGRES_USER` etc. work below):
```bash
set -a; . ./.env; set +a
```

**5. Bring up the database, then run ONE manual backup** (real encrypted path):
```bash
docker compose -f docker-compose.prod.yml up -d db
docker compose -f docker-compose.prod.yml run --rm -e BACKUP_RUN_ONCE=1 backup
# Watch the log: dump → sha256 → "encrypted -> …age" → "offsite sync done".
```

**6. Confirm the encrypted dump exists locally AND in B2:**
```bash
ls -1 backups/acocos_*.dump.age backups/acocos_*.dump.sha256   # local
docker compose -f docker-compose.prod.yml run --rm --entrypoint rclone backup \
  ls "b2:YOUR-BUCKET/db"                                        # offsite
# If you see the .age (and NO plaintext .dump), encryption + offsite both work.
```

**7. Pull the dump back FROM B2** (proves the offsite copy is real, not just local):
```bash
docker compose -f docker-compose.prod.yml run --rm --entrypoint rclone backup \
  copy "b2:YOUR-BUCKET/db" /backups/from_b2 --include "acocos_*"
ls -1t backups/from_b2/acocos_*.dump.age | head -1     # note this <stamp>
```

**8. Decrypt it** (on your workstation, where the private key is — or on the
server using ./secrets):
```bash
age -d -i secrets/age_identity.txt \
    -o /tmp/verify.dump backups/from_b2/acocos_<stamp>.dump.age
```

**9. Verify the checksum matches what was recorded at backup time:**
```bash
sha256sum /tmp/verify.dump
cat backups/from_b2/acocos_<stamp>.dump.sha256     # the two hashes must match
```

**10. Restore into a THROWAWAY scratch DB and assert it has real tables/rows:**
```bash
docker compose -f docker-compose.prod.yml exec db \
  psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE verify_restore;"
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U "$POSTGRES_USER" -d verify_restore --no-owner < /tmp/verify.dump
docker compose -f docker-compose.prod.yml exec db \
  psql -U "$POSTGRES_USER" -d verify_restore \
  -c "SELECT count(*) FROM inventory_product; SELECT count(*) FROM sales_saleorder;"
# On a brand-new install these may be 0 — that's fine; a clean restore with the
# tables present is the proof. (Seed one product+sale first if you want nonzero.)
```

**11. Drop the scratch DB and clean up:**
```bash
docker compose -f docker-compose.prod.yml exec db \
  psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE verify_restore;"
rm -f /tmp/verify.dump; rm -rf backups/from_b2
```

**12. Start the full stack; the automated backup + weekly drill now run on their own:**
```bash
docker compose -f docker-compose.prod.yml up -d
# Confirm the loop is alive:
docker compose -f docker-compose.prod.yml logs --tail=20 backup
```

### Emergency restore (when something is on fire)

You need the encrypted dump (`acocos_<stamp>.dump.age`, in `./backups/` or from
B2) and the age **private** key. If the box is gone, get the dump back first:
`rclone copy b2:YOUR-BUCKET/db ./backups --include "acocos_*"`.

```bash
set -a; . ./.env; set +a
ls -1t backups/acocos_*.dump.age | head                    # pick the newest
age -d -i secrets/age_identity.txt -o restore.dump backups/acocos_<stamp>.dump.age
sha256sum restore.dump                                     # vs the .sha256
docker compose -f docker-compose.prod.yml up -d db
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < restore.dump
docker compose -f docker-compose.prod.yml up -d            # start everything
# Confirm: GET /healthz/ returns 200 and /pos/ shows today's data.
```

For a **local scratch** restore (never prod), `make restore FILE=…` refuses to
run when `DJANGO_SETTINGS_MODULE` looks like production.

> A backup that has never been restored is not a backup. The weekly drill
> Telegrams the Owner success or failure — read those messages.
