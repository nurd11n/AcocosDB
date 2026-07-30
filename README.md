# ACOCOS CRM

Internal system for ACOCOS — inventory, sales, payments, clients, debts, Заказы
(production orders), a daily Russian report, and a dashboard. Two surfaces, one
Django project: the manager terminal at `/pos/` (an installable PWA) and the
admin panel at `/panel/`. See `CLAUDE.md` for the full design.

Telegram/WhatsApp bots and marketing broadcasts exist in the codebase but are
**not production-ready** — they ship behind feature flags, off by default (see
**Feature flags** below). Everything else is. See `HANDOFF.md` (Russian) for
what to tell the client is live today.

Local dev without HTTPS: `make dev` (uses `docker-compose.yml`, DEBUG on).
`make test` runs the suite **and** `manage.py check --deploy`.

Health probe: `GET /healthz/` → `200 ok` when DB + cache are up, `503` otherwise.

### Login

Plain username + password at `/login/` — the same page gates both `/pos/` and
`/panel/` (admin). No second factor: this is 2-4 trusted staff on their own
devices. Brute force is still capped by django-axes (5 failures → 1-hour lock).
Create the first account with `createsuperuser` (below) and log straight in.

## Deploy runbook — empty VPS to a working ACOCOS

Follow every numbered step in order, on a fresh Ubuntu/Debian VPS. Nothing
here needs guessing — copy-paste each block, then move to the next.

**1. VPS prep.**
```bash
# Docker Engine + Compose plugin (see docs.docker.com/engine/install for your distro):
curl -fsSL https://get.docker.com | sh
# Firewall: SSH + HTTP/HTTPS only — everything else (Postgres 5432, Redis 6379,
# the Django dev port) must NEVER be reachable from outside the box; Caddy is
# the only public entry point and it proxies to `web` over the private compose network.
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```
Disable SSH password auth (keys only) in `/etc/ssh/sshd_config`
(`PasswordAuthentication no`, `PermitRootLogin prohibit-password`), then
`sudo systemctl restart sshd`. Confirm you can still log in with your key
**before** closing the current session.

**2. Clone the repo.**
```bash
git clone <your-repo-url> acocosDB && cd acocosDB
```

**3. `.env` from `.env.example` — every value explained inline.**
```bash
cp .env.example .env
```
Fill in, reading each comment in the file as you go:
- `SECRET_KEY` — generate with `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`. Never the placeholder, never reused from dev.
- `DEBUG=False`, `ALLOWED_HOSTS=your-domain.example`, `CSRF_TRUSTED_ORIGINS=https://your-domain.example`.
- `DATABASE_URL` / `POSTGRES_*` — pick a real `POSTGRES_PASSWORD`, matching in both.
- `REDIS_URL=redis://redis:6379/0` — prod refuses to start without this (see `config/settings/prod.py`).
- `DOMAIN=your-domain.example` — Caddy requests its Let's Encrypt cert for exactly this.
- `EMAIL_*` + `REPORT_RECIPIENTS` — the daily report's delivery address(es).
- `ADMIN_URL` — keep the default or pick your own `/panel/`-style path.
- `BOTS_ENABLED=False`, `WHATSAPP_ENABLED=False`, `CAMPAIGNS_ENABLED=False` — see **Feature flags** below; leave all three False at go-live.
- Backups (`AGE_RECIPIENT`, `RCLONE_*`, `DRILL_CHAT_ID`) — filled in step 12, not now.

**4. DNS.** Point `your-domain.example`'s A record at the VPS's public IP.
Confirm propagation before continuing: `dig +short your-domain.example` should
print the VPS IP. Caddy (step 5) cannot get an HTTPS certificate until this
resolves.

**5. Bring the stack up.**
```bash
make composecheck   # validates BOTH docker-compose.yml and docker-compose.prod.yml
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps          # everything should be "running (healthy)" within ~30s
docker compose -f docker-compose.prod.yml logs caddy | tail -20   # confirm the cert was issued, no TLS errors
```

**6. Migrate + build the roles.**
```bash
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
docker compose -f docker-compose.prod.yml exec web python manage.py setup_roles
```

**7. Create the Owner account (superuser).**
```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

**8. First login.** Open `https://your-domain.example/` — it redirects to
`/login/`. Log in with the superuser you just created; you land on `/pos/`
and see the «Админпанель» link in the header (Owner-only).

**9. Import the existing catalog** (she already has stock — see **Data
import** below for the file format):
```bash
docker compose -f docker-compose.prod.yml cp ./my_catalog.xlsx web:/app/catalog.xlsx
docker compose -f docker-compose.prod.yml exec web python manage.py import_catalog /app/catalog.xlsx --dry-run
docker compose -f docker-compose.prod.yml exec web python manage.py import_catalog /app/catalog.xlsx
```

**10. Upload product photos.** Either through `/panel/` (each Product's
`photo` field — a thumbnail is generated automatically) or by copying files
directly into the `media` volume:
```bash
docker compose -f docker-compose.prod.yml cp ./photos/. web:/app/media/products/
```

**11. Verify the deploy, end to end:**
```bash
curl -sI https://your-domain.example/healthz/ | head -1     # expect: HTTP/2 200
```
- [ ] `/healthz/` returns 200.
- [ ] HTTPS loads with a valid cert (no browser warning), HTTP redirects to HTTPS.
- [ ] Both themes render correctly — the moon/sun toggle in the header on `/pos/`.
- [ ] A **test sale** completes: `/pos/` → add an item → confirm → the result
      screen shows updated stock and (if a client was picked) debt.
      `Оформить возврат` / `Отменить продажу` both work if you want to undo it.

**12. Backups — do NOT skip this.** Go through the full **Backups** section
below now, before considering the deploy live. It walks the entire chain —
generate keys, configure B2, run one real backup, pull it back from B2,
decrypt, checksum, restore to a scratch DB, assert row counts — and only
starts the automated 6-hourly loop at the very end. A backup nobody has
restored is not a backup.

## Feature flags

Bots are not production-ready — `BOTS_ENABLED`, `WHATSAPP_ENABLED`, and
`CAMPAIGNS_ENABLED` (`.env`) all ship `False`. Everything else — sales,
stock, clients, debts, Заказы, reports, dashboard — works fully with all
three off; nothing else breaks (see `tests/test_prod_flags.py`). Off means:

- `BOTS_ENABLED=False` — the `bot` container idles (no Telegram polling, no restart-loop); the daily report still emails, just skips Telegram delivery.
- `WHATSAPP_ENABLED=False` — `/wa/webhook/` 404s outright, regardless of credentials.
- `CAMPAIGNS_ENABLED=False` — the Рассылки admin section is hidden even from the Owner, and `send_campaign` refuses to run.
- With both `BOTS_ENABLED` and `WHATSAPP_ENABLED` off, the Входящие (inbox) nav item and the dashboard's Telegram-reach/favourites panels are hidden too — nothing links to a dead feature.

Flip a flag to `True` in `.env`, then `docker compose -f docker-compose.prod.yml up -d` to pick it up — only once that channel is actually staffed and ready (a real Telegram bot token / Meta WhatsApp app / an approved broadcast plan), not before.

## Data import

She already has stock — `import_catalog` loads it from an `.xlsx` price list
with these exact Russian column headers (any order, row 1):

```
категория | товар | размер | цвет | артикул | себестоимость | цена | валюта | количество
```

A filled example (with an «Инструкция» sheet) ships at
`docs/catalog_import_example.xlsx` — copy its structure. Notes:

- Every row is validated **before** anything is written; one bad row aborts
  the whole file with a Russian, row-numbered error list (nothing partial
  gets saved — it's one transaction).
- `артикул` (SKU) is the identity key — re-running the same file is safe: it
  corrects category/name/prices in place but never re-adds opening stock or
  creates a duplicate variant.
- `количество` becomes the **opening stock**, written once as a
  `PRODUCTION_IN` movement reasoned «начальный остаток» — never typed
  directly onto a variant, same rule as every other stock change.
- `размер`/`цвет` may be blank; `валюта` must be `KGS`, `USD`, or `RUB`.

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py import_catalog /app/catalog.xlsx --dry-run
docker compose -f docker-compose.prod.yml exec web python manage.py import_catalog /app/catalog.xlsx
```

## Scheduled jobs

The `scheduler` container (`docker-compose.prod.yml`) already runs on its own,
no host cron needed: at each time in `SNAPSHOT_HOURS` (default every 6 hours —
00/06/12/18) it takes a local database snapshot, at `RATES_HOUR` (default
08:00) it pulls fresh NBKR rates, and at `REPORT_HOUR` (default 21:00) it pulls
rates again, purges stale sale drafts, and sends the daily report — see
`scheduler.py`. Staff can still refresh rates on demand any time with the
«Обновить» button on the POS Курс card; the schedule is for reliability, the
button is for control — a stale rate never blocks a sale either way, it just
shows its age.

Not running the full Docker stack (e.g. a bare `python manage.py runserver`
setup)? Start the same loop with `python scheduler.py`, or add snapshots to
host cron directly:

```bash
# Every 6 hours: a local, checksummed DB snapshot into backups/snapshots/.
0 */6 * * * cd /path/to/acocosDB && python manage.py backup_db --quiet >> /var/log/acocos-backup.log 2>&1
```

One job is deliberately **not** in that loop: deleting notes/tasks that have
been marked done for 4+ weeks (28 days). It's a separate host crontab entry
instead, so it can be added or removed independently of the report/rates jobs
and doesn't need the `scheduler` container rebuilt to change its schedule:

```bash
# /etc/crontab or `crontab -e` on the host — runs daily at 03:00 server time.
# Replace /path/to/acocosDB with the real deploy path.
0 3 * * * cd /path/to/acocosDB && docker compose -f docker-compose.prod.yml exec -T web python manage.py purge_completed_notes >> /var/log/acocos-purge-notes.log 2>&1
```

`purge_completed_notes` is idempotent (safe to re-run; matches zero rows once
caught up) and logs the number of notes deleted. Run it by hand any time with
`docker compose -f docker-compose.prod.yml exec web python manage.py purge_completed_notes`.

## Backups

Two layers, so a copy always exists on the box **and** a copy always exists off it:

**1. On-server snapshots — the always-available floor (`manage.py backup_db`).**
A local, checksummed database snapshot written into `backups/snapshots/` every
6 hours (`SNAPSHOT_HOURS`), with tiered retention (keep every snapshot for 7
days, then one/day to 30 days, then one/week to 6 months — `SNAPSHOT_KEEP_*`).
It needs **no** age/rclone/B2 and works in **any** environment — sqlite or
postgres, Docker or bare:

- **sqlite** — a consistent online snapshot via the sqlite3 backup API (safe
  while the app is live), gzip-compressed.
- **postgres** — `pg_dump -Fc` when `pg_dump` is on `PATH`. In the slim Docker
  image it isn't, so there `backup_db` steps aside and the encrypted `backup`
  service (below) owns the postgres path — deliberately, because those dumps
  hold client PII and must be encrypted before they rest on disk.

Take one by hand any time: `python manage.py backup_db`. Restore a sqlite
snapshot with `gunzip -c backups/snapshots/acocos_snapshot_<stamp>.sqlite3.gz > db.sqlite3`
(verify first: `sha256sum -c` against the `.sha256` sidecar).

**2. Encrypted offsite — the prod belt-and-suspenders (`backup` service).**
The `backup` service dumps Postgres every 6 hours, checksums it, encrypts it with
**age** (to `AGE_RECIPIENT`, whose private key is NOT on the server), and `rclone
sync`s the encrypted dumps + `media/` offsite to Backblaze B2. Retention is tiered
(4×/day for 7 days, daily for 30 days, weekly for 6 months). The `restore-drill`
service restores the latest dump into a throwaway DB weekly, asserts the data is
sane, and Telegrams the Owner the result. Setup + deploy-day verification below.

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
