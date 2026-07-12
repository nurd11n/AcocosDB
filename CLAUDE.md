# CLAUDE.md — ACOCOS Admin Panel

Internal inventory, sales, and client-management system for **ACOCOS** — a small business
that manufactures and sells women's dresses and costumes. This is a private tool for 2–4
trusted users (owner + staff), not a public website. Read this file fully before changing code.

## What this system does

1. **Inventory** — how many units of each product/variant exist, cost price, sale price,
   and what remains after sales.
2. **Sales** — record sales, auto-decrement stock, revenue and profit reports.
3. **Clients (mini-CRM)** — contacts, purchase history, debts/installments, interaction log.
4. **Telegram bot** — owner/staff check stock, record sales, and get daily summaries by phone.
5. **Phase 2 (only when explicitly requested):** Bitrix24 sync, WhatsApp Cloud API bot.

## Tech stack — decided, do not change without discussion

- Python 3.12, **Django 5.x**. NOT FastAPI: Django's built-in admin is 80% of this product,
  and the chosen admin theme is Django-only.
- Admin UI: **django-unfold** (modern, actively maintained). Jazzmin is the fallback only
  if Unfold blocks us on something specific.
- **PostgreSQL 16** in Docker. SQLite is acceptable only for throwaway local experiments.
- Telegram bot: **aiogram 3.x**, running as a separate process that reuses the Django ORM
  (`django.setup()` in `bot/main.py`). No duplicate DB layer.
- Config via **django-environ**; every secret lives in `.env` (never committed).
- Deploy: Docker Compose (`web`, `db`, `bot`, `caddy`, `backup`). Caddy terminates HTTPS.
- No Celery/Redis/queues until a real need appears. Cron + management commands are enough.

## Repository layout

```
acocos/
├── config/            # settings/ (base.py, dev.py, prod.py), urls.py, wsgi.py
├── apps/
│   ├── inventory/     # Product, ProductVariant, StockMovement
│   ├── sales/         # SaleOrder, SaleItem, Payment
│   ├── clients/       # Client, Interaction, debt logic
│   └── reports/       # read-only owner reports (revenue, profit, remaining stock)
├── bot/               # aiogram bot — separate entrypoint, shares the service layer
├── integrations/      # bitrix.py, whatsapp.py — Phase 2, keep EMPTY until asked
├── docker/            # Dockerfiles, Caddyfile, backup scripts
├── .env.example       # every variable documented here
└── Makefile
```

## Data model — hard rules

- `Product` → `ProductVariant` (size, color, SKU). **Stock lives at the variant level.**
- Stock is never a hand-editable integer. Current stock = SUM over `StockMovement` rows
  (types: `PRODUCTION_IN`, `PURCHASE_IN`, `SALE_OUT`, `WRITEOFF_OUT`, `ADJUSTMENT`).
  Corrections happen via an "inventory count" admin action that creates an ADJUSTMENT
  movement with a required reason — never by editing a number in place.
- Money is always `DecimalField(max_digits=12, decimal_places=2)`. **Never float.**
  Default currency KGS; keep a `currency` field on money-bearing models for the future.
- A sale = `SaleOrder` + `SaleItem` lines. Confirming a sale creates `SALE_OUT` movements
  inside `transaction.atomic()` with `select_for_update()` on the affected variants.
  Stock must never go negative — raise `ValidationError`, don't clamp silently.
- Client debt is **derived**: SUM(confirmed sale totals) − SUM(payments). Never store a
  mutable "debt" field on `Client`.
- `on_delete=PROTECT` wherever a Product/Variant/Client is referenced by a sale.
  Prefer `is_active=False` over deletion for products and clients.
- Cost price and profit are sensitive: visible to the **Owner** role only (admin, bot,
  and reports all enforce this).

## Commands

```
make dev             # docker compose up: web + db with hot reload
make migrate         # manage.py migrate
make makemigrations  # manage.py makemigrations
make test            # pytest -x
make lint            # ruff check && ruff format --check
make bot             # run the Telegram bot locally
make backup          # manual pg_dump + media snapshot into ./backups
make restore FILE=…  # restore a dump into the LOCAL db only
make superuser       # create an admin user
```

## Security — non-negotiable

- Admin is served at `/panel/` (path comes from env), never the default `/admin/`.
- **TOTP 2FA is mandatory for every account**: django-otp + `OTPAdminSite`. No exceptions,
  no "temporary" accounts without it.
- **django-axes**: lock the account for 1 hour after 5 failed login attempts.
- Prod settings: `DEBUG=False`, explicit `ALLOWED_HOSTS`, `SECURE_HSTS_SECONDS`,
  `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT=True`.
- Sessions expire after 12 hours and on browser close.
- Two roles via Django groups: **Owner** (everything, incl. cost/profit and user admin)
  and **Staff** (record sales, view stock — no cost price, no user management).
- Bot access: allowlist of Telegram user IDs stored in DB (`bot.models.BotUser`).
  Every handler checks it first. Unknown users get silence, not an error message.
- Secrets exist only in `.env`. If a secret ever lands in git history, **rotate it** —
  deleting the commit is not enough.
- Audit trail: `django-simple-history` on Product, ProductVariant, SaleOrder, Client,
  Payment. Admin logins are logged.

## Backups — non-negotiable

- Nightly `pg_dump -Fc` from the `backup` container cron at 03:00 Asia/Bishkek.
- Two copies: local `./backups` volume **and** offsite (Backblaze B2 via rclone).
- Retention: 7 daily, 4 weekly, 6 monthly. `media/` (product photos) syncs nightly too.
- A backup that was never restored is not a backup: run a restore drill into a scratch
  DB after every schema milestone (`make restore` against local).

## Conventions

- ruff (lint + format), line length 100. Type hints on all services and bot handlers.
- Business logic lives in `apps/<app>/services.py`. Admin classes, views, and bot
  handlers only call services — no stock or money math inside admin/templates/handlers.
- Every schema change ships with its migration in the same commit. Never edit an applied
  migration; never delete migrations to "clean up".
- Tests: pytest-django + factory_boy. Anything touching stock math, debt math, or sale
  confirmation MUST have tests before it merges.
- Admin UX per app in `admin.py` using Unfold components. Variant list page must show:
  SKU, product, size/color, current stock, cost (Owner only), price, last movement date.
- User-facing strings wrapped in `gettext_lazy` — English now, Russian translation later.

## Telegram bot rules

- Read commands: `/stock <name or SKU>`, `/today` (sales + revenue summary),
  `/client <phone>` (history + current debt).
- Recording a sale is a guided dialog that ends with an explicit inline confirm button.
- The bot calls the same `services.py` functions as the admin — zero duplicated logic.
- Staff-role bot users never see cost price or profit numbers.

## Phase 2 — Bitrix24 / WhatsApp (do NOT build until explicitly asked)

- Bitrix24: REST via inbound webhook, code isolated in `integrations/bitrix.py`.
  Our DB is the source of truth for inventory and debts; Bitrix owns the lead pipeline.
  Store `bitrix_id` on `Client`; every sync job must be idempotent.
- WhatsApp: **official Meta Cloud API only**. No green-api or other unofficial gateways —
  they get business numbers banned.

## Do NOT

- Do not switch frameworks, add Celery/Redis/Kubernetes, or introduce a JS SPA frontend.
- Do not use float for money, or edit stock quantities directly.
- Do not weaken security settings to "simplify local dev" — that's what `dev.py` is for.
- Do not run `flush`, destructive migrations, or restores against prod data without the
  human explicitly confirming in that session.
- Do not commit `.env`, database dumps, or `media/`.
- Do not add new dependencies when Django or the existing stack already covers the need.
