CLAUDE.md — ACOCOS CRM

Internal system for ACOCOS — a business making and selling women's dresses, costumes, and custom items. Tracks stock, sales, payments, clients, debts; daily Russian report; Telegram/WhatsApp bots and marketing campaigns. 2–4 trusted users, no public site. Read fully before coding; sections marked CHANGED fix real defects in the current build.

Two surfaces, one Django project, one door

/pos/ is the front door. Admin panel (Jazzmin) moves to /panel/ (env ADMIN_URL) — catalog +
reference data: categories, products, variants, clients, settings. Manager page at /pos/ —
mobile-friendly Django views + templates, where managers record sales/payments and pull reports;
this is where daily work happens. Both live in the SAME Django project, sharing models, auth,
and services.py.

ONE login page at /login/ — username + password + a one-time code, all in a single POST
(django LoginView + django-otp; see apps.core.auth_views), no separate admin login. Gated by
settings.OTP_ENABLED (True in prod, False in dev/tests). A fresh superuser has no device yet —
`manage.py addstatictoken <username>` prints a one-time bootstrap code that logs in like a real
device; enroll a real TOTP device straight after at /otp/enroll/ (README has the full runbook).
`/` redirects to /pos/ if authenticated, else /login/. /panel/ redirects unauthenticated/unauthorized hits to the same
/login/, never its own form. LOGIN_REDIRECT_URL=/pos/, LOGOUT_REDIRECT_URL=/login/. Owner sees
an «Админпанель» link in the /pos/ header; Editor/Viewer do not. Do NOT build a native app or JS
SPA: a responsive page needs no app store, no API/token layer, no build step — one codebase,
instant updates, any phone. Decided, not open.

Fixed stack

Python 3.12, Django 5.x, django-jazzmin, PostgreSQL 16, Redis, openpyxl, HTMX (vendored in
static/, never a CDN), aiogram 3.x, official Meta WhatsApp Cloud API (never unofficial
gateways), django-environ. Docker Compose (web, db, redis, bot, scheduler, backup; prod overlay
adds Caddy HTTPS). No FastAPI/Flask/SPA, no Celery/queues/Kubernetes. REMINDER: no Docker on this
dev machine yet, so web/bot run bare against sqlite (DATABASE_URL commented out in .env) — before
ship, verify `docker compose up` still brings the full stack up from scratch and restore it.

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
currency, method, created_by, plus reviewed/reviewed_by/reviewed_at — DEPRECATED (the day-end
review queue that wrote them is gone, see Payments below), columns kept for history, never
written to. Sale payment status is
DERIVED, never typed: оплачено (paid ≥ total), частично (0 < paid < total), долг (paid = 0).
StockMovement (ledger): production_in, purchase_in, return_in (+); sale_out, writeoff_out (−);
adjustment (either sign, reason required). Money: DecimalField(12, 2), never float.
on_delete=PROTECT on product/variant/client refs from sales.

available = on_hand − reserved (apps.inventory.services.available_by_variant/available_for),
NEVER raw on_hand — the true ceiling for what a walk-in can buy. reserved comes from apps.orders
(open production orders, see below); when no orders app data applies it's simply 0. Every place
that decides "how much can she sell" — the cart-time cap, the product tile, confirm_sale's own
guard — reads AVAILABLE, never a raw StockMovement sum, so reserved stock can never leak to a
walk-in even if the cart-time cap is bypassed.

/pos/ — the manager terminal, three screens

Mobile-first, minimal, fast. Every screen requires login AND 2FA verification (see
apps.pos.decorators.pos_view -> apps.core.otp.verified — same gate /panel/ and /dashboard/ use).
Viewer may open
Сегодня/Клиенты but never Новая продажа, and sees no confirm/cancel buttons anywhere.

Новая продажа: a draft SaleOrder is created server-side the moment the manager starts, so a
reload never loses the basket (cleanup command purges drafts >24h old). Client search via HTMX
autocomplete (last 10 before typing), inline "+ Новый клиент" (имя/фамилия/телефон), or «Без
клиента» walk-in. Product photo grid (not a dropdown) with live stock/price/currency, searchable
by name AND SKU; mobile input modes on qty/amount/phone. Live «Итого/Оплачено/ Остаток» with «≈
X сом» beside non-KGS amounts. Confirm asks for one explicit tap on a styled confirmation dialog
("Подтвердить продажу на N сом?" — static/pos/js/safety.js + confirm.js, data-confirm on the
form) before it ever POSTs — deliberate money-safety friction on the single most frequent action
in the app, not an oversight (2026-08-18 audit, L4: previously mis-described here as a plain
one-step form). Once confirmed, it's a plain (non-HTMX, but offline-safe: a failed fetch shows a
clear Russian "not saved" message instead of the browser's generic network error) form POST
calling services.confirm_sale() only — disabled client-side on submit AND idempotent server-side
(a duplicate POST redirects to the same result, never double-sells). Stock is re-checked at confirm
time under a row lock against AVAILABLE (on_hand − reserved): an oversell fails with a Russian
error, keeps the basket, never a 500. Result screen shows remaining stock, client debt,
«Отменить продажу» undo, «Оформить возврат» (partial return), «Скачать чек», and «Отправить чек
в WhatsApp».

CHANGED — receipts are PDF-ONLY, no web link. «Скачать чек» (apps.pos.views.receipt_download,
any staff who can view the sale) generates the PDF fresh from the database on that one request
and streams it straight back — nothing is stored (a saved file goes stale the moment a return or
repayment changes the numbers; the database never does) and nothing is written (rendering a
receipt is a pure read, asserted by a row-count test — this was the ghost-payment source).
«Отправить чек в WhatsApp» (apps.pos.views.share_receipt, Editor: own same-day; Owner: any,
POST-only + logged as an Interaction) opens wa.me with a short, LINK-FREE thank-you — wa.me
cannot attach a file, so she attaches the PDF she already downloaded herself, inside WhatsApp.
There is no public receipt page, no signed token, no domain or URL ever sent to a client — that
whole surface (the old `/r/<token>/` route, its view, and apps.pos.receipts.make_receipt_token)
is deleted, not hidden. The PDF itself (apps.pos.receipts.receipt_context +
templates/receipts/receipt.html, styled by static/pos/receipt.css): an ACOCOS wordmark, a status
badge — «ОПЛАЧЕНО» / «ЧАСТИЧНО — остаток X» / «ДОЛГ X» — labelled by WORD, not colour alone (A4,
legible printed in black and white), items grouped by product+size with colours nested beneath
and a totals row carrying BOTH total units and total money, client FIRST NAME only (never the
staff-only descriptor, never the phone), and Оплачено/Остаток shown only when the sale isn't
fully paid — a paid receipt shows just Итого.

CHANGED — cart-time stock cap: a qty typed/pasted above what's available clamps to the max
(across ALL cart lines of that same variant combined, not per line — adding the same variant
twice can't bypass it) and shows «Доступно только N шт» in --partial, never silently accepted.
A tile at 0 available is non-clickable server-side too (item_add rejects it), not just dimmed
CSS. This is UX only — confirm_sale re-checks against AVAILABLE regardless, since stock (or a
reservation) can change between the cart being built and confirm.

CHANGED — the cart rail never lets the money bar/confirm button scroll out of view: on desktop
(≥768px) #sale-body is a flex column capped to the viewport height — the Позиции list is its own
`overflow-y: auto` region (with a top/bottom mask fade), Оплата and the money bar stay
`flex: 0 0 auto`, always visible regardless of item count. Mobile keeps its existing behaviour
(money bar `position: fixed` above the bottom nav — already always visible). Cart lines render
one row each (name + qty×price + total + remove), not a stack, so more fit before scrolling.
Money always renders via the `money` filter (apps.pos.templatetags.pos_extras) — «3 800 сом» /
«874,50 сом», NBSP-grouped thousands, the currency SYMBOL never the KGS/RUB/USD code, cents
dropped only when exactly zero — never a bare `{{ amount }} {{ currency }}` anywhere in /pos/.

Сегодня: today's sales, revenue per currency + a «≈ сом» grand total,
«Скачать отчёт» (xlsx; cost-price data inside means the download button itself stays
superuser-only — cost prices inside). Клиенты: search → per-currency debt + history + WhatsApp
debt-reminder link. Read-only.

Currency (KGS сом / RUB / USD) + auto rates from NBKR — hardened

Every money field stores its OWN currency. A payment in another currency is CONVERTED to the
order's currency at the NBKR rate FROZEN onto that payment (Payment.rate_to_kgs, set once on
first save, never recomputed) and DOES count toward the balance + client debt. Voiding a payment
reuses that exact frozen rate for the reversal (services.void_payment), so a foreign payment's
debt effect cancels EXACTLY even if today's rate has since moved — never re-derived from the
current rate. ExchangeRate = ONE row per currency (the current rate vs KGS); refreshed BOTH
automatically (scheduler.py, RATES_HOUR + again before REPORT_HOUR) AND on demand with the Курс
«Обновить» button (apps.pos.views.refresh_rates → fetch_nbkr_rates, nbkr.kg/XML/daily.xml),
overwriting in place — no dated pile-up; on failure keeps the last rate, never crashes a sale.
Every rate that actually changes (refresh or manual) writes a RateChangeLog row (user, old/new
value, source) — Owner-only, read-only in the panel. «≈ X сом» shows beside every non-KGS
amount, POS included.

Rate permissions: refreshing from NBKR is Editor/Manager + Owner (apps.pos.views.refresh_rates,
require_can_sell — it only pulls the official number). Hand-entering/overriding a rate — the
ExchangeRate admin, or a payment's rate_override field — is OWNER ONLY, enforced server-side
(ExchangeRateAdmin.has_add/change_permission, apps.pos.views._check_rate_override raising
PermissionDenied) not just hidden in the UI; a non-Owner POSTing rate_override gets a 403. The
Owner's override is for the real spread booths charge vs the official NBKR number: it stores
rate_source='manual' + rate_official (the NBKR rate at that moment, so the spread is
reconstructable later) on the Payment, and warns — never blocks — when it deviates >5% from
official (settings.MANUAL_RATE_DEVIATION_WARN_PCT).

Staleness is visible, never blocking: the Курс card shows each rate's date and an age badge —
0-1 days = fresh (--paid), 2-3 days = aging/normal (--partial, NBKR skips weekends/holidays),
4+ days = stale (--debt, «Курс устарел»). A foreign payment always shows its calm conversion math
inline («10 USD × 87,45 = 874,50 сом · НБКР 24.07.2026», or «курс вручную») with NO alarm styling
for the common case. An explicit second confirmation (a distinct required checkbox, not the same
primary button) is required only when the rate is stale (4+ days), was manually overridden, or
the converted amount exceeds settings.LARGE_PAYMENT_THRESHOLD_KGS (env, default 10 000 сом) — a
fresh, small, NBKR-priced payment takes one tap. A sale counts as fully paid once the remainder
is ≤ settings.PAYMENT_ROUNDING_TOLERANCE (1.00 сом) — sub-сом currency-conversion residue clears
the debts list and shows «оплачено» instead of lingering as a ghost debt forever. A missing rate
for a genuinely foreign payment (currency differs from the order's) raises a clear Russian error
and saves nothing — same-currency-as-order payments (including a non-KGS order paid in its own
currency) need no rate at all and skip the conversion UI entirely.

Change (сдача) — computed, never typed, at one rate

An entered payment above the sale's balance forks explicitly — «Сдача» (handed back), «В счёт
долга» (applied to the client's other outstanding sales), or «Аванс» (kept as credit) — never
auto-picked; the latter two need a client on the sale (disabled for walk-ins) and are
mathematically identical (full gross amount counts, no change), differing only in the label
audited on the Payment. What reduces a balance is always the NET: Payment.net_applied_kgs =
amount_kgs − change_amount_kgs, never the gross amount — every balance/debt/report sums this,
never amount alone (SaleOrder.paid_amount, apps.clients.services.client_debts_by_currency,
SaleOrderAdmin, the daily report). Change is computed in KGS (the ideal excess this ONE payment
creates), rounded DOWN to settings.CHANGE_ROUNDING_STEP (env, default 1.00 сом) for the
client-facing figure; the residue lands in change_rounding_kgs (always ≥ 0, reported, never
dropped — the daily report's Продажи sheet sums it into a day-end till-drift total). Change is
converted at the SAME frozen rate as the payment that created it — never a second lookup — so its
currency is restricted to the till (order's own currency) or the payment's own currency; a sale
must end fully paid whenever change > 0, enforced in services.record_payment (raises otherwise).
The computed change is read-only by default; a manual «Изменить сумму сдачи» adjustment is
allowed within ± CHANGE_ROUNDING_STEP × 2 for Editor/Manager (with a required Russian reason,
stored on the payment) — anything wider is OWNER-only, enforced server-side
(apps.pos.views._check_change_override raising PermissionDenied, never just hidden in the UI). The
POS double-check panel always shows the full picture (what the client gave, the rate, what's
credited, the balance, the computed change + its rounding) before confirming, with a prominent
--partial warning only when the payment itself crossed a currency boundary (never on every
same-currency payment) — same risk-ack mechanism as a stale/manual rate, also triggered by change
exceeding settings.CHANGE_CONFIRM_THRESHOLD_KGS (env, default 5 000 сом). Voiding a payment that
gave change reverses net_applied_kgs at that payment's frozen rate (mirrors every money field's
sign, including change and its rounding, so a void never double-counts the day's till-drift with
an undone transaction). A client's negative pooled debt (paid more than owed) displays as «Аванс
N» in --paid (apps.clients.services.client_credits) — debt-reminder logic stays debt-only, never
nudging a client who's in credit.

Заказы (apps.orders) — production orders, do NOT deduct stock

She manufactures: a client can order items that don't exist yet. Order: client (required),
created_by, due_date, status (новый → в производстве → готов → выдан → отменён), note,
currency. OrderItem: variant, quantity, unit_price, currency, produced_qty (default 0, written
ONLY by services.mark_produced). An Order never touches StockMovement — no draft/confirm split
like a sale either, since nothing risky happens until production/handover: it exists as a real
row the moment a client is picked, and its detail page doubles as the builder (add items, no
stock cap — ordering unproduced goods is the entire point; current stock shows as information
only). «Что производить» (apps.orders.services.production_queue, /orders/queue/) aggregates
across новый/в производстве orders: need = SUM(ordered qty) − on_hand stock, per variant, sorted
by nearest due_date; rows with need ≤ 0 show «есть на складе», de-emphasised but NOT hidden (she
still must not sell those to a walk-in). Never stored — computed every time, like stock and debt.

Reservation: reserved = SUM(OrderItem.quantity) over orders in новый/в производстве/готов — the
FULL line quantity, never reduced by produced_qty, because a produced-but-not-handed-over unit
still belongs to that order (see Data model's `available` above). готов is included here (still
promised, not yet delivered) but excluded from the production queue (nothing left to produce).
Cancelling or delivering an order releases its reservation automatically — it just falls outside
this status filter.

«Произведено N шт» (services.mark_produced, atomic) writes a PRODUCTION_IN movement and
increments produced_qty; when every line is fully produced the order auto-advances to готов.
Deposit (аванс) reuses the EXISTING Payment mechanism — never a second payments/conversion path
— via Payment.production_order (a new nullable FK alongside the existing `order`), frozen-rate
snapshotted exactly like a sale payment. «Выдать заказ» (services.hand_over, atomic) flips the
order to выдан FIRST (releasing its own reservation so it can't block its own conversion), THEN
builds a real SaleOrder from the order's lines and calls the EXISTING services.confirm_sale —
the only place stock ever leaves the system, exactly once — then re-links any deposit Payment to
the new SaleOrder so paid_amount/balance pick it up immediately; the remaining balance is taken
through the normal /pos/ payment panel afterward, change included. Order.sale_order links both
ways. Overdue = due_date < today (Asia/Bishkek LOCAL date, never UTC) and status not
выдан/отменён, shown in --debt with «просрочен», never blocking anything.

CHANGED — Stock: keep the ledger, hide the plumbing

Stock = SUM(StockMovement.quantity) per variant. Nobody opens the raw movements form: «Принять
на склад» / «Списать» / «Пересчёт» live on the ProductVariant list, each asks a number (+ reason
for пересчёт) and writes the movement behind the scenes. StockMovement is append-only
(has_change_permission/has_delete_permission → False, even for superusers) and out of the
sidebar. Stock is an annotated column on Product/Variant lists — no N+1.

CHANGED — Payments: inside the sale, count now

Payments are inline rows in the SaleOrder, never a standalone creation screen, and count
immediately toward revenue and debt — no approval gate. Voiding a payment writes a reversing
entry, never a silent delete. Guardrail: confirm explicitly on overpayment or a payment ≥2× the
total.

REMOVED (2026-08) — the day-end payment-review queue (an in-panel changelist of unreviewed
payments + bulk «Отметить проверенным», Owner only) was unused and is gone: the admin
action/page/view, its sidebar entry (`reports.DailyReview`), the «Проверено» column in
forms/templates, the «Не проверено» sheet in the daily xlsx, and the Сегодня unreviewed-payment
counter. Payment.reviewed/reviewed_by/reviewed_at stay on the model — DEPRECATED, never written
to, columns not dropped so history stays intact and re-enabling later is cheap — and still GATE
two things for whatever was already marked reviewed before removal: editing a payment inline
(PaymentInlineForm) and voiding one (services.void_payment) both stay Owner-only for an
already-reviewed row. Do not resurrect the write path without re-reading apps/sales/models.py's
Payment docstring first.

CHANGED — Sidebar and dark theme

Sidebar groups: Продажи · Заказы · Склад (Products, Categories) · Клиенты (Clients,
Interactions) · Рассылки · Система (Owner only: Users, Groups, bot users, bot messages,
exchange rates, axes, stats, plus two custom links — Дашборд and a read-only stock-overview
page also confusingly labelled «Склад», not to be confused with the Склад APP group above).
No standalone «Отчёты» group any more — it existed only to host DailyReview, the payment-review
queue's proxy model (removed 2026-08); those two custom links moved into «Система» so they
don't vanish from the sidebar along with it (Jazzmin only shows an app group with ≥1 registered
admin model — see config/settings/base.py's JAZZMIN_SETTINGS comment). StockMovement and
standalone Payment stay out of the sidebar,
same for the reservation ledger — nothing about `reserved`/`available` is ever hand-editable.
Dark theme:
higher-contrast Jazzmin dark_mode_theme; every field, select, and the «Тип» dropdown clearly
outlined in both modes, on /panel/ and /pos/.

Roles

setup_roles builds two Groups; Owner = Django superuser. Django permissions only. Owner —
everything incl. Система, cost prices, profit, campaigns, Админпанель link, and orders
(apps.orders is in BUSINESS_APPS, but cancelling/deleting a Заказ is Owner-only regardless of
the `change`/`delete` permission grant — enforced explicitly in the admin and the POS cancel
view, not just hidden). Owner also solely owns Рассылки (campaigns is deliberately kept OUT of
BUSINESS_APPS, and CampaignAdmin's has_*_permission methods re-check is_superuser regardless —
a non-Owner staff user hitting a campaign admin URL directly gets 403, not just a hidden link).
Editor/Manager (staff) — business models (now incl. apps.inbox: Заявки, Избранное, StockAlert)
+ full /pos/ + full Заказы (create, deposit, mark produced, hand over) + full /inbox/ (reply,
confirm/decline заявки — gated on `inbox.add_orderrequest`); add/change/view; NO delete; NO
Система; NO Рассылки; cost prices hidden. Viewer (staff) — view-only, same hidden models, no
cost prices, /pos/ + Заказы + /inbox/ read-only (Сегодня/Клиенты/Заказы/Входящие — can open a
thread, cannot reply, confirm, or decline).

Security, history, backups

Passwords Argon2 (PBKDF2 kept for silent upgrade on next login). /login/ = username + password +
a one-time code (django-otp: TOTP or a static backup code, gated by OTP_ENABLED) + django-axes
(1h lock / 5 fails — a wrong code counts toward the lockout exactly like a wrong password, see
apps.core.auth_views), the single gate for /panel/ and /pos/. Every protected surface checks
`request.user.is_verified()`, not just `is_authenticated` (apps.core.otp.verified).
Strict CSP: script-src 'self', no inline scripts (/pos/ JS external). Accepted tradeoff: /panel/
(Jazzmin) is CSP-EXCLUDED — its third-party inline scripts can't be nonce'd; tolerable since
/panel/ is Owner-only behind login, not public. Secure/HttpOnly/SameSite cookies, HSTS, nosniff,
Referrer-Policy, X-Frame-Options DENY; `check --deploy` stays clean. WhatsApp webhook verifies
Meta's HMAC (X-Hub-Signature-256, fail-closed) + payload cap + per-IP rate limit. DB
CheckConstraints back the money/stock rules (nonzero movement, signed intake/outgoing, positive
payment/qty, nonneg total). django-simple-history on Product, ProductVariant, Client, SaleOrder,
Payment; LogEntry for Owner; bot messages read-only.

Backups: pg_dump -Fc every 6h → sha256 → age-encrypted (private key OFF the server) → rclone to
the configured offsite remote (Backblaze B2 by default; a remote needing OAuth/a crypt layer,
e.g. Google Drive via rclone crypt, needs a real rclone.conf mounted at ./secrets/rclone.conf —
see docker-compose.prod.yml's backup service) (dumps AND media/); tiered retention (4/day×7d ·
daily×30d · weekly×6mo); weekly restore drill into a scratch DB that asserts row counts +
freshness and Telegrams the Owner. AGE_RECIPIENT and RCLONE_REMOTE are REQUIRED, not optional
(2026-08-18 audit, M3) — backup.sh refuses to run (exits non-zero, alerts) rather than silently
degrading to unencrypted or local-only dumps; a failed encryption never leaves a plaintext .dump
on disk. Optional HEALTHCHECKS_PING_URL adds a dead-man's-switch ping on every completed cycle,
catching the backup not running AT ALL (host down, container never starts) — a different failure
mode than the Telegram alerts above, which only fire when the script runs and finds something
wrong. The Postgres data volume MUST sit on an encrypted host disk (LUKS / provider-encrypted) —
Docker volumes are not encrypted by default. Restore runbook: README.md (full setup) and
docs/RESTORE-DRILL.md (on-demand drill + reading results, right now, without waiting for Sunday).

Two UI languages, Russian reports

Full i18n EN + RU: every verbose_name, label, choice, help text, action, validation message, and
every /pos/ + /login/ string via gettext_lazy, translated in locale/ru/LC_MESSAGES/django.po,
compiled in the Docker build. LANGUAGE_CODE=ru (Russian-first, still switchable). Item data is
never translated. Reports are ALWAYS Russian.

Daily report & scheduled jobs

send_daily_report → ONE .xlsx, Russian headers, sheets Продажи · Остаток (cost price + stock
value here only) · Долги · Заказы (open orders, due dates, deposits, remaining).
Emailed (EMAIL_* + REPORT_RECIPIENTS) AND sent as a Telegram document to BotUsers with
receives_reports=True. --format csv writes UTF-8-BOM CSV. Scheduler runs it at REPORT_HOUR
(default 21:00, Asia/Bishkek), alongside audit_stale_totals (2026-08-18 audit, M2 — read-only,
Telegrams the Owner ONLY when a confirmed sale's stored total no longer matches its line items,
silent otherwise, same pattern as send_security_digest below) and send_security_digest. The
dashboard also gets two Заказы panels — «Заказы в работе»
(open-order count + KGS value) and «К производству» (top variants by need, linking to the full
queue) — deliberately NOT touched by the dashboard's сом/$/₽ view-currency toggle.

Notes/tasks (Заметки): Note.completed_at is set/cleared ONLY by services.apply_done — never by
hand; the POS toggle and /panel/ admin both go through it. Marking done starts a 4-week purge
clock, un-marking resets it. purge_completed_notes hard-deletes notes done 28+ days; run via
host crontab (NOT Celery, NOT the scheduler container) — see README Scheduled jobs.

Bots — TWO Telegram bots + WhatsApp, staff query layer + client experience

CHANGED — not production-ready yet: BOTS_ENABLED / WHATSAPP_ENABLED / CAMPAIGNS_ENABLED (.env,
all default False) gate every surface below off — bot/main.py idles (no polling, no restart-
loop), /wa/webhook/ 404s outright, the Рассылки admin + send_campaign refuse even for the Owner,
and the Входящие nav item + dashboard bot panels hide when both channels are off. See README's
"Feature flags" section. Everything below describes what runs once a flag is flipped True.

Two separate Telegram bots, two tokens, two Dispatchers, zero shared handlers (bot/staff_bot.py,
bot/client_bot.py — tests/test_bots.py enforces the split structurally). Staff bot: long polling,
allowlisted BotUser only, /stock, /today, /client, /debts, /restock, /lapsed; unknown IDs
ignored, silently. Client bot: public, anyone may /start — a persistent 5-button reply-keyboard
menu (🛍 Каталог · 📦 Мои заказы · ❤️ Избранное · 💬 Написать нам · ℹ️ О нас), catalog browsing by
category with LIVE availability (reads apps.inventory.services.available_for, never a cache —
CLIENT_BOTS.md), inline «Хочу заказать» / «❤️» / «Уведомить о поступлении» per variant, text
search by name/SKU, deep links (t.me/<bot>?start=product_<sku>). Sharing a phone links
telegram_chat_id (reachability) but does NOT itself set marketing_consent — that's a SEPARATE
explicit «Присылать новинки?» Да/Нет step (apps.clients.services.set_marketing_consent), asked
right after; never assume consent from /start alone. «СТОП»/«stop» unsubscribes (consent only,
chat_id kept).

WhatsApp webhook: GET verification + signed POST (HMAC, fail-closed). Two DIFFERENT reply
layers on purpose: apps.wa.replies (stock_reply/today_reply/debts_reply/etc., staff-only
aggregates, accepts an arbitrary query — used EXCLUSIVELY by bot/staff_bot.py) vs.
apps.wa.client_replies.build_client_reply (client-safe, scoped to the ONE resolved Client,
never an aggregate) — the webhook (apps/wa/views.py) calls ONLY the latter. Client-safe replies:
numbered menu (1 Каталог · 2 Мой заказ · 3 Оплата и доставка · 4 Написать менеджеру) on «меню» or
an unrecognised greeting; order-status intent («когда готово», «мой заказ», «заказ №14» — scoped
to that client's own orders only, via a strict `\bзаказ\s*№?\s*(\d+)\b` regex so "заказать" (a
заявка-intent verb) is never confused with a status lookup); a free-text заявка intent («хочу
заказать...») creates an OrderRequest with the raw message and hands off to a human — no guided
multi-step flow on WhatsApp, ever; catalog lookup shows «есть»/«нет» per size, never a raw stock
number. Every auto-reply ends with «Напишите "менеджер" — ответит человек.».

Human handoff (WhatsApp): Client.human_handoff_until — set on the «менеджер» keyword, on 3+
client messages within 2 minutes (apps.clients.services.recent_message_burst), or when staff
reply from /inbox/ (apps.clients.services.start_handoff, 6h). While handed off, the webhook logs
the inbound message but sends NO automated reply (apps.clients.services.is_handed_off gates it)
— the bot never talks over a manager mid-conversation.

Заявки — the client-facing order-request loop (apps.inbox), NOT an Order

A заявка (OrderRequest: client, status[новая|подтверждена|отклонена|отменена], source[telegram|
whatsapp], note, raw_message, handled_by, handled_at, decline_reason, order→FK nullable to
apps.orders.Order) is an inbound LEAD, never an order: it NEVER reserves stock, sets a price, or
creates production work by itself (apps.inbox.services.create_order_request only writes
OrderRequest + OrderRequestItem rows and pings staff over the STAFF Telegram token). Max 5 open
(новая) requests per client — beyond that, ask them to write instead. Staff confirm
(apps.inbox.services.confirm_order_request — reuses apps.orders.services.create_order, the SAME
path /orders/ uses, never a second order-creation path; links OrderRequest.order both ways) or
decline (with a required reason). The client is notified of either outcome
(apps.inbox.services.send_to_client: Telegram first if reachable, WhatsApp otherwise) — «Заявка
№14 принята» never «заказ принят», since nothing is confirmed yet. A client may cancel their own
request while it's still новая.

Favourite (client, variant) — per-client wishlist, toggled from a catalog inline button. The
AGGREGATE across clients («Чаще всего в избранном», apps.inbox.services.top_favourited) is free
demand research surfaced on the dashboard and available as a campaign audience segment
(Campaign.only_favourited_product) — the highest-converting broadcast segment by far.

StockAlert (client, variant, notified_at) — «Уведомить о поступлении», shown only when a size is
out of stock. Fires (apps.inbox.services.notify_back_in_stock) ONLY on a real intake movement
(PRODUCTION_IN/PURCHASE_IN — see apps.inventory.services.add_movement's hook), never on a bare
availability read/cache refresh; oldest waiter first, notified_at set once so nobody is pinged
twice for the same wait.

Order-ready push: the moment apps.orders.services.mark_produced auto-advances an Order to готов,
apps.inbox.services.notify_order_ready fires exactly once — this single push is what kills «когда
будет готово?» traffic. «Мои заказы» (Telegram menu) / «мой заказ» (WhatsApp) show the SAME
per-client-scoped status in plain Russian: Принят · В производстве · Готов к выдаче · Выдан.

BotContent (key, title_ru, body_ru, is_active) — «О нас»: hours, address, size guide, delivery,
returns. Owner-only to edit (like ExchangeRate/Campaign), never hardcoded in bot/wa code.

/inbox/ — the ONE place to operate, both channels in one stream

BotMessage gained a nullable `client` FK (apps.core) so every bot message — Telegram AND
WhatsApp, in and out — threads to one Client. /inbox/ (apps.inbox.views, Editor/Manager write,
Viewer read-only via the standard `inbox.add_orderrequest` permission check): a newest-first
stream of the latest message per client (filters: unanswered · has заявка · has debt · channel),
opening a client shows the FULL cross-channel history plus a side panel (debt, open orders,
заявки with inline Подтвердить/Отклонить, last purchase, favourites). Replying routes through
whichever channel reaches the client (send_to_client: Telegram if chat_id known, else WhatsApp)
and triggers the 6h handoff automatically. A nav badge shows the open-заявка count.

Рассылки (campaigns) — read the constraints, they are not optional

Campaign (name, text_ru, M2M products, channel, status, only_bought_before, only_with_debt,
lapsed_days, only_favourited_product, created_by) + CampaignRecipient (client, status, error,
sent_at). Compose once, pick products + audience → preview count → send via
`send_campaign <id>` processing recipients one by one, recording per-recipient status. Never a
blind loop with no record. The audience floor is CHANNEL-SPECIFIC: Telegram requires a known
telegram_chat_id; WhatsApp requires whatsapp_opted_in=True (never just an inbound message).
EVERY audience, regardless of channel, excludes anyone already at 2 sent broadcasts in the
trailing 7 days (apps.campaigns.services.MAX_BROADCASTS_PER_CLIENT_PER_WEEK) — enforced in
campaign_audience itself, so the preview count and the actual send can never disagree, and this
is never a UI toggle. send_campaign refuses to send between 22:00–09:00 Asia/Bishkek
(`_within_quiet_hours`) — re-run it in the morning; `--ignore-quiet-hours` exists for tests/
manual override only, never wired to the scheduler.

Telegram: photos as a media group + caption; throttle ~20 msg/sec, retry up to 3× on HTTP 429
honoring retry_after exactly, then mark that recipient failed and continue — one bad chat never
stalls a campaign. WhatsApp: feature-flagged off by default (WHATSAPP_BROADCAST_ENABLED +
WHATSAPP_TEMPLATE_NAME both required to send) — approved Meta template only, ONE hero image +
one CTA (when the client replies, the 24h window opens and the rest can go free-form at no extra
cost), opted-in clients only, never free-form bulk. Both channels: honor marketing_consent
(Telegram) / whatsapp_opted_in (WhatsApp); «СТОП»/«STOP» unsubscribes BOTH
(apps.wa.client_replies clears whatsapp_opted_in too, mirroring Telegram's consent clear); log
each send as an Interaction; never message a client twice per campaign (CampaignRecipient's
unique constraint, checked on every re-run).

«Доступно в Telegram: N из M клиентов» (apps.inbox.services.telegram_reach) — CLIENT_BOTS.md's
"one metric that governs everything": every broadcast feature is worthless below a real
audience. Shown on the dashboard alongside «Чаще всего в избранном» (top_favourited).

Tests (must pass)

Stock = sum of movements; confirm decrements + stores total; oversell raises + changes nothing;
cancel/return restock; debt = sales − payments per currency; status derives; adjustment needs
reason; fetch_rates survives network failure; Editor sees no cost/Система; Viewer can't write;
campaign skips no-consent/no-chat_id; double-submit = one sale; draft cleanup only stale; DB
constraints reject bad money/stock even via bulk_create; Argon2 upgrade on login; webhook HMAC
(valid passes, tampered/missing rejected); grid ≤4 queries at 10 and 500 rows; stale cache never
oversells; /healthz/ 200/503; 500 page renders with DB down; POS access matches role; notes
purge deletes 28+ day-old done items, keeps the rest. Multi-currency: a Manager POSTing a manual
rate gets 403; voiding a foreign payment restores debt to EXACTLY its pre-payment value even
after the rate moved; a sub-сом payment residue marks a sale paid and drops off the debts list;
a same-currency-as-order payment needs no rate and skips the conversion UI; a missing rate for a
genuinely foreign payment saves nothing; changing a rate afterward never alters a past payment's
stored value; Decimal only, no float, anywhere in the conversion/reversal path. Change: same- and
cross-currency change compute and round correctly at the payment's own rate; change given while a
balance remains is rejected; negative change is impossible even via bulk_create; net_applied_kgs
(never gross) drives every balance/debt/report; «В счёт долга»/«Аванс» reduce the client's other
debt and display negative balance as «Аванс»; voiding a payment with change restores exact
pre-payment debt after a rate move; a Manager POSTing an out-of-band change amount gets 403.
Stock cap: adding the same variant across two cart lines caps against the TOTAL, not per line; a
crafted POST above available stock is clamped, never saved at face value; a 0-available tile
can't be added even via a direct POST. Заказы: production need = ordered − on_hand, never
negative-displayed; reservation reduces available and a walk-in cannot buy reserved stock even
bypassing the cart cap; the same variant across two orders aggregates into ONE queue row; marking
produced writes a PRODUCTION_IN movement and raises on_hand (not available, until delivered);
hand_over deducts stock exactly once, carries the deposit over, links order↔sale both ways;
cancelling an order releases its reservation; overdue detection uses Asia/Bishkek local date, not
UTC. Client bots: the client Telegram bot cannot return cost price, revenue, stock totals, or
another client's data (asserted on rendered/source text); contact share links a verified phone
and stores chat_id WITHOUT setting marketing_consent; a separate explicit Да/Нет step sets it;
a заявка never reserves stock and never creates an Order until staff confirm; confirming creates
EXACTLY one Order and links both ways; declining notifies the client with the reason; a client
can cancel their own новая request; the 5-open-requests rate limit raises past that; order status
change to готов pushes a notification once and only once; back-in-stock fires on
PRODUCTION_IN/PURCHASE_IN and NOT on a bare availability read/cache refresh; toggling a favourite
twice removes it, and top_favourited aggregates correctly across clients. WhatsApp client-safe
replies never leak total_kgs/cost_price/revenue/profit; catalog availability shows «есть»/«нет»,
never a raw stock number; an order-status query is scoped to that client's own orders only, and
"заказать" (заявка intent) is never confused with a "заказ №N" status lookup; «менеджер» and a
3-messages-in-2-minutes burst both trigger handoff; «СТОП» clears both marketing_consent AND
whatsapp_opted_in. Рассылки: campaign_audience excludes anyone at the 2-per-week cap regardless
of channel; a WhatsApp campaign's audience requires whatsapp_opted_in, not just consent;
send_campaign refuses to send during quiet hours (22:00-09:00 Asia/Bishkek) and sends once
`--ignore-quiet-hours` is passed; a WhatsApp send with WHATSAPP_BROADCAST_ENABLED=False raises;
send_campaign is idempotent across a killed/re-run process. Roles: a Manager/Editor hitting the
campaign admin directly gets 403 regardless of the URL; a Viewer can open an /inbox/ thread but
POSTing a reply or a заявка confirm/decline gets 403; an Editor can do both; replying from
/inbox/ sets Client.human_handoff_until. Prod readiness (tests/test_prod_flags.py,
tests/test_import_catalog.py): every bot surface gates off cleanly when its flag is False with
nothing else breaking; /healthz/ 503s on a DB-down simulation too, not just cache; xlsx import
validates every row before writing anything, is idempotent by SKU, and never doubles opening
stock on a re-run.

Definition of done

/ → /pos/ (or /login/); one shared login; Owner sees Админпанель, Editor/Viewer don't. Full sale
+ payment in /pos/ under a minute, nothing typed by hand; reload keeps the basket; double-tap =
one sale; oversell shows a Russian error, keeps the basket, no 500. A cashier cannot put 100 of
something in the cart when 15 exist — capped at add time, across all lines, rejected server-side
regardless. The cart rail's money bar and confirm button never scroll out of view, at any item
count. Result shows stock + debt + working undo. Every amount shows currency + «≈ сом», formatted
«3 800 сом» never «3800.00 KGS». Stock only via Принять/Списать/Пересчёт; ledger uneditable, not
in sidebar. She can order unproduced goods, take a deposit in any currency, see «к производству»
sorted by due date, mark items produced, and hand an order over as a normal sale that deducts
stock exactly once. Roles enforced exactly; Viewer never writes. Reports stay Russian, Cyrillic
intact. Telegram campaign sends photos with per-recipient status; WhatsApp flag-gated +
template-only, both channels respect the 2/week cap and quiet hours. A client finds ACOCOS,
shares their phone (linked, not yet consenting), explicitly opts into news, browses the catalog
with live availability, and submits a заявка — staff see it in /inbox/, confirm it in one tap
into a real Order with stock reserved only at that moment, and the client is notified
automatically. A client asking «когда готово?» on WhatsApp gets an accurate, own-orders-only
status instantly, and a rapid burst of messages (or «менеджер») hands the conversation to a human
without the bot talking over them. The owner composes one broadcast, sees the honest reachable
count first (Telegram-chat-linked or WhatsApp-opted-in, whichever the channel demands), and every
conversation from both channels is answerable from one /inbox/ page. «Доступно в Telegram: N из
M» is visible on the dashboard. All tests green; docker compose up brings the stack up from
scratch. A clean VPS goes from empty to a working ACOCOS by following README's deploy runbook
alone, with BOTS_ENABLED/WHATSAPP_ENABLED/CAMPAIGNS_ENABLED all False and nothing broken by their
absence; the backup round-trip (dump → encrypt → offsite → pull back → decrypt → restore →
assert rows) is verified before go-live, not assumed working.

Do NOT

Type stock/revenue/debt by hand · float for money · auto-convert stored currency · translate
item data · expose Система to Editor/Viewer · build a native app or SPA · add Celery/queues ·
load HTMX from a CDN · duplicate sale/stock/money logic outside services.py · free-form bulk
WhatsApp or unofficial gateways · message clients without consent · set Note.completed_at by
hand outside services.py · commit .env, dumps, media, or .mo files · let a non-Owner set an
exchange rate · reverse a payment at today's rate instead of its frozen one · block a sale
because a rate is stale · warn on every foreign payment regardless of risk · assume what excess
payment means · let change be freely typed · give change while a balance remains · use a
different rate for change than for the payment it came from · drop rounding residue silently ·
warn on same-currency change · type a "to produce" number by hand · let an Order deduct stock
before production/handover · build a second payments or conversion path for deposits · let
reserved stock be sold to a walk-in · rely on client-side stock caps alone · let the confirm
button leave the viewport · leave money formatted as a bare number + currency code · merge the
client and staff Telegram bots or let either import the other's data layer · expose cost, profit,
stock totals, or another client's data to a client, on either bot · let a bot create an Order or
reserve stock without staff confirmation · promise «заказ принят» for an unconfirmed заявка ·
assume marketing consent from /start or a contact share alone · message anyone without consent ·
exceed 2 broadcasts per client per week, on either channel · send a broadcast between
22:00-09:00 Asia/Bishkek · send free-form WhatsApp outside the 24h window · use an unofficial
WhatsApp gateway · build a guided multi-step order flow on WhatsApp · let a failed broadcast
recipient stall the whole campaign · use an LLM for intent parsing · hardcode «О нас» content ·
let the WhatsApp auto-reply talk over a human who's already answering in /inbox/ · ship with
DEBUG=True, the dev SECRET_KEY, or an unverified backup chain · leave a bot code path reachable
(polling, webhook, admin, nav link) when its feature flag is off — a flag must fully gate it, not
just hide a link to it.
