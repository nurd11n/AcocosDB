CLAUDE.md — ACOCOS CRM

Internal system for ACOCOS — a business making and selling women's dresses, costumes, and custom items. Tracks stock, sales, payments, clients, debts; daily Russian report; Telegram/WhatsApp bots and marketing campaigns. 2–4 trusted users, no public site. Read fully before coding; sections marked CHANGED fix real defects in the current build.

Two surfaces, one Django project, one door

/pos/ is the front door. Admin panel (Jazzmin) moves to /panel/ (env ADMIN_URL) — catalog +
reference data: categories, products, variants, clients, settings. Manager page at /pos/ —
mobile-friendly Django views + templates, where managers record sales/payments and pull reports;
this is where daily work happens. Both live in the SAME Django project, sharing models, auth,
and services.py.

ONE login page at /login/ (Django LoginView + django_otp OTPAuthenticationForm — combined
username/password/TOTP in one form, no separate admin login). `/` redirects to /pos/ if
authenticated, else /login/. /panel/ redirects unauthenticated/unauthorized hits to the same
/login/, never its own form. LOGIN_REDIRECT_URL=/pos/, LOGOUT_REDIRECT_URL=/login/. Owner sees
an «Админпанель» link in the /pos/ header; Editor/Viewer do not. Do NOT build a native app or JS
SPA: a responsive page needs no app store, no API/token layer, no build step — one codebase,
instant updates, any phone. Decided, not open.

Fixed stack

Python 3.12, Django 5.x, django-jazzmin, PostgreSQL 16, Redis, openpyxl, HTMX (vendored in
static/, never a CDN), aiogram 3.x, official Meta WhatsApp Cloud API (never unofficial
gateways), django-environ. Docker Compose (web, db, redis, bot, scheduler, backup; prod overlay
adds Caddy HTTPS). No FastAPI/Flask/SPA, no Celery/queues/Kubernetes.

Core principle — everything flows from the SALE

A manager does ONE thing: record a sale (client + item lines + amount paid). Confirming it
atomically decrements stock, counts revenue, updates debt, writes history — always through
services.confirm_sale(), never reimplemented in a view. Stock, revenue, and debt are NEVER typed
by hand — they are consequences.

Data model

Category → Product → ProductVariant. Variant: SKU, размер, цвет, cost_price (себестоимость),
sale_price, currency, photo, low_stock_threshold, is_active. Client: first_name, last_name,
phone (unique), note, source, marketing_consent, telegram_chat_id (nullable), whatsapp_opted_in,
is_active. Debt + purchase history are DERIVED, shown on the client page. SaleOrder: client
(optional = walk-in), channel, status (draft/confirmed/cancelled), total, currency, timestamps,
created_by. SaleItem: variant, qty, unit_price, currency. Payment (inline in the sale): amount,
currency, method, reviewed, reviewed_by, reviewed_at, created_by. Sale payment status is
DERIVED, never typed: оплачено (paid ≥ total), частично (0 < paid < total), долг (paid = 0).
StockMovement (ledger): production_in, purchase_in, return_in (+); sale_out, writeoff_out (−);
adjustment (either sign, reason required). Money: DecimalField(12, 2), never float.
on_delete=PROTECT on product/variant/client refs from sales.

/pos/ — the manager terminal, three screens

Mobile-first, minimal, fast. Every screen `login_required` + `otp_required` (when OTP_ENABLED).
Viewer may open Сегодня/Клиенты but never Новая продажа, and sees no confirm/cancel buttons
anywhere.

Новая продажа: a draft SaleOrder is created server-side the moment the manager starts, so a
reload never loses the basket (cleanup command purges drafts >24h old). Client search via HTMX
autocomplete (last 10 before typing), inline "+ Новый клиент" (имя/фамилия/телефон), or «Без
клиента» walk-in. Product photo grid (not a dropdown) with live stock/price/currency, searchable
by name AND SKU; mobile input modes on qty/amount/phone. Live «Итого/Оплачено/ Остаток» with «≈
X сом» beside non-KGS amounts. Confirm is a plain (non-HTMX) form POST calling
services.confirm_sale() only — disabled client-side on submit AND idempotent server-side (a
duplicate POST redirects to the same result, never double-sells). Stock is re-checked at confirm
time under a row lock: an oversell fails with a Russian error, keeps the basket, never a 500.
Result screen shows remaining stock, client debt, «Отменить продажу» undo, «Оформить возврат»
(partial return), and a WhatsApp receipt link (Editor: own same-day; Owner: any).

Сегодня: today's sales, revenue per currency + a «≈ сом» grand total, unreviewed payment count,
«Скачать отчёт» (xlsx; cost-price data inside means the download button itself stays
superuser-only — cost prices inside). Клиенты: search → per-currency debt + history + WhatsApp
debt-reminder link. Read-only.

Currency (KGS сом / RUB / USD) + auto rates from NBKR

Every money field stores its OWN currency; never auto-convert on save. ExchangeRate (date,
currency, rate vs KGS, source). fetch_rates pulls nbkr.kg/XML/daily.xml daily (scheduler, before
the report), upserts today's rates, never clobbers a manual Owner override, and on failure keeps
the last known rate without crashing a sale. Rates are display/report-only — «≈ X сом» beside
every non-KGS amount, POS included.

CHANGED — Stock: keep the ledger, hide the plumbing

Stock = SUM(StockMovement.quantity) per variant. Nobody opens the raw movements form: «Принять
на склад» / «Списать» / «Пересчёт» live on the ProductVariant list, each asks a number (+ reason
for пересчёт) and writes the movement behind the scenes. StockMovement is append-only
(has_change_permission/has_delete_permission → False, even for superusers) and out of the
sidebar. Stock is an annotated column on Product/Variant lists — no N+1.

CHANGED — Payments: inside the sale, count now, review at day-end

Payments are inline rows in the SaleOrder, never a standalone creation screen, and count
immediately toward revenue and debt — no approval gate. Day-end review = an in-panel list of
today's unreviewed payments + bulk «Отметить проверенным» (Owner only). Editing/voiding a
reviewed payment is Owner-only and writes a reversing entry, never a silent delete. Guardrail:
confirm explicitly on overpayment or a payment ≥2× the total.

CHANGED — Sidebar and dark theme

Sidebar groups: Продажи · Склад (Products, Categories) · Клиенты (Clients, Interactions) ·
Рассылки · Отчёты · Система (Owner only: Users, Groups, bot users, bot messages, exchange rates,
TOTP, axes, stats). StockMovement and standalone Payment stay out of the sidebar. Dark theme:
higher-contrast Jazzmin dark_mode_theme; every field, select, and the «Тип» dropdown clearly
outlined in both modes, on /panel/ and /pos/.

Roles

setup_roles builds two Groups; Owner = Django superuser. Django permissions only. Owner —
everything incl. Система, cost prices, profit, campaigns, reviews, Админпанель link.
Editor/Manager (staff) — business models + full /pos/; add/change/view; NO delete; NO Система;
cost prices hidden. Viewer (staff) — view-only, same hidden models, no cost prices, /pos/
read-only (Сегодня/Клиенты, no sale, no confirm/cancel).

Security, history, backups

Passwords Argon2 (PBKDF2 kept for silent upgrade on next login). /login/ = django-otp TOTP 2FA
(default on in prod) + django-axes (1h lock / 5 fails), the single gate for /panel/ and /pos/.
Strict CSP: script-src 'self', no inline scripts (/pos/ JS external). Accepted tradeoff: /panel/
(Jazzmin) is CSP-EXCLUDED — its third-party inline scripts can't be nonce'd; tolerable since
/panel/ is Owner-only behind 2FA, not public. Secure/HttpOnly/SameSite cookies, HSTS, nosniff,
Referrer-Policy, X-Frame-Options DENY; `check --deploy` stays clean. WhatsApp webhook verifies
Meta's HMAC (X-Hub-Signature-256, fail-closed) + payload cap + per-IP rate limit. DB
CheckConstraints back the money/stock rules (nonzero movement, signed intake/outgoing, positive
payment/qty, nonneg total). django-simple-history on Product, ProductVariant, Client, SaleOrder,
Payment; LogEntry for Owner; bot messages read-only.

Backups: pg_dump -Fc every 6h → sha256 → age-encrypted (private key OFF the server) → rclone to
Backblaze B2 (dumps AND media/); tiered retention (4/day×7d · daily×30d · weekly×6mo); weekly
restore drill into a scratch DB that asserts row counts + freshness and Telegrams the Owner. The
Postgres data volume MUST sit on an encrypted host disk (LUKS / provider-encrypted) — Docker
volumes are not encrypted by default. Restore runbook: README.md.

Two UI languages, Russian reports

Full i18n EN + RU: every verbose_name, label, choice, help text, action, validation message, and
every /pos/ + /login/ string via gettext_lazy, translated in locale/ru/LC_MESSAGES/django.po,
compiled in the Docker build. LANGUAGE_CODE=ru (Russian-first, still switchable). Item data is
never translated. Reports are ALWAYS Russian.

Daily report & scheduled jobs

send_daily_report → ONE .xlsx, Russian headers, sheets Продажи · Остаток (cost price + stock
value here only) · Долги · Не проверено. Emailed (EMAIL_* + REPORT_RECIPIENTS) AND sent as a
Telegram document to BotUsers with receives_reports=True. --format csv writes UTF-8-BOM CSV.
Scheduler runs it at REPORT_HOUR (default 21:00, Asia/Bishkek).

Notes/tasks (Заметки): Note.completed_at is set/cleared ONLY by services.apply_done — never by
hand; the POS toggle and /panel/ admin both go through it. Marking done starts a 4-week purge
clock, un-marking resets it. purge_completed_notes hard-deletes notes done 28+ days; run via
host crontab (NOT Celery, NOT the scheduler container) — see README Scheduled jobs.

Bots — query + auto-log

Telegram (long polling), allowlisted BotUser: /stock, /today, /client, /debts, /restock,
/lapsed; unknown IDs ignored. Clients (not on the allowlist) /start + share contact to
subscribe, «СТОП» to unsubscribe. WhatsApp webhook: GET verification + signed POST, same
commands as text; every inbound message auto-matches/creates a Client by phone and logs an
Interaction. Both channels share ONE reply/service layer.

Рассылки (campaigns) — read the constraints, they are not optional

Campaign (name, text_ru, M2M products, channel, status, created_by) + CampaignRecipient (client,
status, error, sent_at). Compose once, pick products + audience (consent / bought before / has
debt) → preview count → send via a management command processing recipients one by one,
recording per-recipient status. Never a blind loop with no record.

Telegram (built): only clients with a known telegram_chat_id are reachable (no messaging by
phone). Photos as a media group + caption; throttle ~1 msg/sec, retry 429 honoring retry_after;
show the reachable count honestly. WhatsApp (later, feature-flagged): approved Meta template
only outside the 24h window, genuine opt-in, stop on quality warnings — never free-form bulk.
Both: honor marketing_consent; auto-unsubscribe on «СТОП»/«STOP»; log each send as an
Interaction; never message a client twice per campaign.

Tests (must pass)

Stock = sum of movements; confirm decrements + stores total; oversell raises + changes nothing;
cancel/return restock; debt = sales − payments per currency; status derives; adjustment needs
reason; fetch_rates survives network failure; Editor sees no cost/Система; Viewer can't write;
campaign skips no-consent/no-chat_id; double-submit = one sale; draft cleanup only stale; DB
constraints reject bad money/stock even via bulk_create; Argon2 upgrade on login; webhook HMAC
(valid passes, tampered/missing rejected); grid ≤4 queries at 10 and 500 rows; stale cache never
oversells; /healthz/ 200/503; 500 page renders with DB down; POS access matches role; notes
purge deletes 28+ day-old done items, keeps the rest.

Definition of done

/ → /pos/ (or /login/); one shared login; Owner sees Админпанель, Editor/Viewer don't. Full sale
+ payment in /pos/ under a minute, nothing typed by hand; reload keeps the basket; double-tap =
one sale; oversell shows a Russian error, keeps the basket, no 500. Result shows stock + debt +
working undo. Every amount shows currency + «≈ сом». Stock only via Принять/Списать/Пересчёт;
ledger uneditable, not in sidebar. Roles enforced exactly; Viewer never writes. Reports stay
Russian, Cyrillic intact. Telegram campaign sends photos with per-recipient status; WhatsApp
flag-gated + template-only. All tests green; docker compose up brings the stack up from scratch.

Do NOT

Type stock/revenue/debt by hand · float for money · auto-convert stored currency · translate
item data · expose Система to Editor/Viewer · build a native app or SPA · add Celery/queues ·
load HTMX from a CDN · duplicate sale/stock/money logic outside services.py · free-form bulk
WhatsApp or unofficial gateways · message clients without consent · set Note.completed_at by
hand outside services.py · commit .env, dumps, media, or .mo files.
