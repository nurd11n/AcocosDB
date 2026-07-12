CLAUDE.md — ACOCOS CRM

Internal business database + admin panel for ACOCOS, a small business that makes and
sells women's dresses, costumes, and other custom items. Tracks inventory, sales, clients,
debts; emails a daily report; runs Telegram + WhatsApp bots as a lightweight CRM. Operated
only through the admin panel by 2–4 trusted people. No public website. Read this fully
before writing code. If the repo already has an ACOCOS project, adapt it to this spec.

Fixed stack — do not change


Python 3.12, Django 5.x. Not FastAPI/Flask, no JS SPA.
Admin theme: django-jazzmin.
PostgreSQL 16 + Redis (cache + sessions), all via Docker Compose, deploy-ready.
Telegram: aiogram 3.x. WhatsApp: official Meta Cloud API webhook only — never
unofficial gateways (they get business numbers banned).
Excel: openpyxl. Config via django-environ; secrets only in .env.
No Celery/queues/Kubernetes. A sleep-loop scheduler container + management commands
are enough at this scale.


Admin at the ROOT URL, basic interface


Mount admin at /: urlpatterns = [path("", admin.site.urls)], with /wa/webhook/,
/i18n/, and set_language registered BEFORE the admin catch-all.
Root-mounted login is easy for scanners to find, so hardening is mandatory:
django-otp TOTP 2FA (env OTP_ENABLED, default True in prod) and django-axes
(lock account 1h after 5 failed logins).
Keep it basic: no custom dashboards or extra JS beyond Jazzmin. Clean list pages with
search + filters on every model.


Two languages everywhere EXCEPT item data


Full Django i18n, English + Russian. Every model verbose_name, field label, choice
label, help text, admin action, and validation message wrapped in gettext_lazy and
translated in locale/ru/LC_MESSAGES/django.po. Compile .mo in the Docker build
(install gettext in the image).
Language switch: LocaleMiddleware + Jazzmin's own native language dropdown
(JAZZMIN_SETTINGS["language_chooser"] = True — this Jazzmin version ships a real
topbar dropdown backed by a POST form to Django's set_language view out of the box;
no custom template or view needed). Language is per-session.
Item DATA is never translated: product names, descriptions, notes, client names are
single plain fields stored exactly as typed (owner enters Russian; shown identically in
both UI languages). Do NOT create name_en/name_ru pairs.


Dark and light themes


Jazzmin config: JAZZMIN_SETTINGS["show_theme_chooser"] = True enables a native
topbar dropdown (Light / Dark / Auto); JAZZMIN_UI_TWEAKS["default_theme_mode"] = "auto"
sets the starting point to follow OS preference. (Older Jazzmin versions only had
OS-preference with no live toggle via "dark_mode_theme" — that key is deprecated in
the version pinned here; default_theme_mode + show_theme_chooser is the real mechanism.)
Verify both render on the login page, list pages, and forms.


Three roles

setup_roles management command creates two Groups; Superuser uses Django's superuser flag.
Enforcement via Django permissions only, no custom middleware.


Superuser — everything, incl. Users, Groups, TOTP devices, bot users, WhatsApp
logs, request stats, cost prices, and profit.
Editor (staff, group "Editor") — add/change/view on business models only
(categories, products, variants, stock movements, clients, interactions, sales,
payments). NO delete. Must NOT see any "development stuff": Users, Groups, Permissions,
TOTP devices, axes logs, bot users, sessions — these don't even appear in the sidebar.
Cost price fields/columns hidden (override get_fields/get_list_display).
Viewer (staff, group "Viewer") — view-only on the same business models; no add/
change/delete buttons; same dev models hidden; no cost prices.


Acceptance: log in as each role; sidebar, columns, and form fields match this exactly.

History and logs of every edit


django-simple-history on Product, ProductVariant, Client, SaleOrder, Payment
(+ HistoryRequestMiddleware so the editing user is recorded). Each object gets a
History view: who changed what, when.
StockMovement ledger is append-only: has_change_permission and
has_delete_permission return False for everyone (incl. superusers). Correct stock
only by adding an adjustment movement with a required reason.
Keep Django's built-in LogEntry visible to superusers. Store all incoming/outgoing
bot messages in a read-only model.


Data model — core rules


Category → Product → ProductVariant (size, color, unique SKU). Supports dresses,
costumes, and custom items (Category is free-form). Stock lives at the variant level
and equals SUM(StockMovement.quantity) — never a hand-editable number.
Movement types: production_in, purchase_in, return_in (positive); sale_out,
writeoff_out (negative); adjustment (either sign, reason required).
Money: DecimalField(max_digits=12, decimal_places=2) everywhere. Never float.
Currency from env (default KGS).
Sale = SaleOrder (client optional for walk-ins; channel instagram/whatsapp/shop/
wholesale; status draft/confirmed/cancelled) + SaleItem lines. Confirming is atomic
(transaction.atomic + select_for_update on variants), writes sale_out movements,
computes and stores the order total, and FAILS with a clear translated message if stock
would go negative. Cancelling writes return_in. Both are admin actions.
Client debt is derived: confirmed sale totals − payments. Never a stored field.
Show it as a client-list column computed in ONE query via correlated subqueries (joined
Sums duplicate rows — don't).
on_delete=PROTECT wherever products/variants/clients are referenced by sales. Admin
lists must be single-query: select_related everywhere, stock annotated in SQL. No N+1.


Daily report — email + Telegram


send_daily_report management command builds ONE .xlsx with three sheets:
Sales (today's confirmed: time, client, channel, items, qty, unit price, total +
totals row), Stock (every active variant: SKU, product, size, color, current stock,
sale price, LOW flag when stock ≤ threshold; cost price + stock value included here only,
owner report), Debts (clients with debt > 0: name, phone, debt, last payment date).
Send via Django email (env: EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD/USE_TLS,
REPORT_RECIPIENTS comma-separated). Subject e.g. ACOCOS daily report — 2026-07-11.
ALSO deliver the same file as a Telegram document to bot users flagged
receives_reports=True (email is the archive, Telegram is what gets read).
Optional --format csv outputs CSV with a UTF-8 BOM (plain UTF-8 CSV shows broken
Cyrillic in Excel). Default xlsx.
A scheduler container sleeps until REPORT_HOUR (env, default 21:00, TIME_ZONE
default Asia/Bishkek) and runs it daily. Makefile report target triggers it manually.


Bots as a lightweight CRM


Telegram (aiogram, long polling): allowlist in a BotUser model (telegram_id, name,
is_active, receives_reports). Unknown IDs silently ignored. Bilingual commands, one
reply each: /stock <sku or name>, /today, /client <phone>, /debts.
WhatsApp webhook: GET verification handshake + POST handling; same commands as text
(stock …/остаток …, today/сегодня).
CRM linking rule (this is what makes bots a CRM): every incoming WhatsApp message
auto-matches a Client by phone; if none exists, create one with source="whatsapp";
auto-log an Interaction(kind="message"). All bot traffic is read-only in the panel.
Telegram and WhatsApp share ONE reply/service layer — no duplicated logic.


Docker / deploy


Services: web (gunicorn + whitenoise), db (postgres:16, healthcheck), redis,
bot, scheduler, backup (nightly pg_dump -Fc → ./backups, 14-day retention);
docker-compose.prod.yml overlay adds Caddy for auto HTTPS (web port not exposed in
prod). Entrypoint: wait for DB → migrate → collectstatic → start.
.env.example documents EVERY variable. Never commit .env.
Makefile: dev, prod, migrate, makemigrations, superuser, roles, test, report, backup, logs.


Tests (must pass before done)

pytest + pytest-django: stock = sum of movements; confirming a sale decrements stock and
stores total; overselling raises and changes nothing; cancelling returns stock; debt =
sales − payments; adjustment requires a reason; Editor can't see cost fields; Viewer has
no change permission.

Definition of done — verify each


 / shows the admin login; 2FA works; 5 bad logins lock the account
 Language switch flips the whole UI EN↔RU; item data stays as entered
 Panel renders in both light and dark per OS preference
 Superuser / Editor / Viewer sidebars + permissions match the roles section exactly
 Every edit to key models shows in its History view with the user recorded
 Stock ledger rows can't be edited or deleted by anyone
 send_daily_report makes a correct 3-sheet xlsx, emails it, sends it to flagged
Telegram users; CSV option opens in Excel with Cyrillic intact
 Incoming WhatsApp from an unknown number creates a Client + Interaction
 All tests green; docker compose up brings the whole stack up from scratch


Do NOT


Translate or duplicate item-data fields; use float for money; use editable stock ints.
Expose any dev/system model to Editor or Viewer.
Add Celery, queues, or a frontend framework. Use unofficial WhatsApp APIs.
Commit .env, dumps, media, or compiled .mo files.