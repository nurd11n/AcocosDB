# ACOCOS — Build Instructions & Roadmap

Companion to `CLAUDE.md`. That file tells Claude Code *how* to behave inside the repo;
this one tells *you* what to build, in what order, how to run it, and what to improve later.

Assumptions to confirm before starting (change in `.env` / settings if wrong):
currency **KGS**, timezone **Asia/Bishkek**, catalog size in the hundreds of SKUs,
2–4 total users, one warehouse/shop location.

---

## 0. Key decisions and why

**Django, not FastAPI.** The core deliverable is an admin panel. Django ships one —
auth, permissions, CRUD, filters, search — and Jazzmin/Unfold are Django admin themes
(they don't exist for FastAPI). FastAPI would mean hand-building all of that for no gain.

**django-unfold, not Jazzmin.** Jazzmin's maintenance has slowed; Unfold is modern
(Tailwind-based), actively developed, and looks better with the same effort. Jazzmin
remains the fallback if Unfold ever blocks a feature.

**Telegram bot first, WhatsApp second.** Telegram Bot API is free, needs no verification,
and works in under an hour. WhatsApp requires the official Meta Cloud API (business
verification, template approval, per-message pricing for business-initiated messages).
Never use unofficial WhatsApp gateways (green-api etc.) — they get numbers banned, and
that would be the stakeholder's actual business number.

**Mini-CRM inside Django first, Bitrix24 optional later.** For a 2–4 person operation,
Client / Debt / Interaction tables in the same admin beat maintaining a two-way Bitrix
sync (mapping, conflicts, auth, monitoring). Bitrix24 has a free tier and a REST API,
so it's scoped as Phase 7 — add it only if she outgrows the built-in CRM.

---

## 1. Prerequisites

- Docker Desktop + git on your machine
- A Telegram account (for @BotFather) — Phase 5
- Later: a VPS (Hetzner CX22-class, ~€5/mo) + a domain — Phase 6
- Later: a Backblaze B2 bucket (offsite backups, costs cents) — Phase 6

## 2. Phase plan — each phase ends with a working system

### Phase 1 — Skeleton + security (1–2 days)
Django project, Docker Compose (web + db), settings split (base/dev/prod), Unfold
installed, admin moved to `/panel/`, django-otp TOTP enforced, django-axes, Owner and
Staff groups, `.env.example`, Makefile, ruff + pytest wired up.
**Done when:** you log in at `/panel/` with a TOTP code, and a Staff user can't see
user management.

Prompt for Claude Code:
> Read CLAUDE.md. Execute Phase 1 from INSTRUCTIONS.md: project skeleton, Docker,
> settings split, Unfold, 2FA with django-otp, axes, Owner/Staff groups, Makefile.
> Stop after `make dev` works and show me how to create the first Owner account.

### Phase 2 — Inventory (2–3 days)
`inventory` app: Category, Product (name, photo, description), ProductVariant (size,
color, SKU, cost_price, sale_price), StockMovement. Admin action "inventory count"
that creates ADJUSTMENT movements. Variant list shows live stock. Excel import of the
initial catalog via django-import-export (the stakeholder almost certainly has a
spreadsheet or notebook today — this is how her data gets in).
**Done when:** she can answer "how many of dress X in size M do we have and what did
each cost" from the panel, and stock can't be edited directly.

### Phase 3 — Sales, clients, debts (2–3 days)
`clients` app: Client (name, phone, note, is_active), Interaction (call/message/visit
log). `sales` app: SaleOrder + SaleItem + Payment; confirming a sale atomically writes
SALE_OUT movements; partial payments supported so debt = sales − payments per client;
a `channel` field on SaleOrder (Instagram / WhatsApp / shop / wholesale).
**Done when:** recording a sale drops stock, an unpaid balance shows on the client,
and deleting a sold product is impossible (PROTECT).

### Phase 4 — Reports (1–2 days)
`reports` app, Owner-only: revenue / cost of goods / profit by period, top products,
remaining stock value, debts list sorted by amount and age, sales by channel.
Export any report to Excel.
**Done when:** the stakeholder opens one page and sees money in, money out, what's left.

### Phase 5 — Telegram bot (2 days)
aiogram 3.x, allowlisted user IDs, `/stock`, `/today`, `/client`, guided "record a
sale" dialog with a confirm button, nightly low-stock alert to the Owner.
**Done when:** she checks stock and records a sale from her phone without opening a laptop.

### Phase 6 — Deploy + backups (1–2 days)
VPS with Docker Compose prod file, Caddy for HTTPS on your domain, UFW allowing only
80/443/SSH, SSH keys only. Nightly `pg_dump -Fc` + media sync to local volume and
Backblaze B2 via rclone; retention 7/4/6; do one full restore drill into a scratch DB.
**More secure option:** put the panel behind Tailscale instead of the public internet —
only devices in your tailnet can even reach the login page. Given your security
background, this is the setup I'd actually recommend; the bot still works either way
since it makes outbound connections only.
**Done when:** you can open the panel from your phone, and you have personally restored
yesterday's backup once.

### Phase 7 (optional) — Bitrix24 sync
Inbound-webhook REST client in `integrations/bitrix.py`. Push clients/deals to Bitrix,
pull lead status back. Our DB stays the source of truth for stock and debts. Only build
this if she actually starts using Bitrix pipelines.

### Phase 8 (optional) — WhatsApp
Official Meta Cloud API: business verification, a webhook endpoint in Django, template
messages for debt reminders and order confirmations. Check current Meta pricing first —
business-initiated template messages are paid per message.

## 3. Local setup (once the repo exists)

```
git clone <repo> && cd acocos
cp .env.example .env        # fill in SECRET_KEY, DB password, BOT_TOKEN
make dev                    # web on http://localhost:8000
make migrate && make superuser
# open http://localhost:8000/ and enroll your TOTP device
```

## 4. Production checklist (Phase 6)

- [ ] `DEBUG=False`, real `ALLOWED_HOSTS`, fresh `SECRET_KEY`
- [ ] HTTPS live (Caddy auto-certs) or Tailscale-only access
- [ ] 2FA enrolled for every account; no shared logins — one account per person
- [ ] django-axes active; SSH by key only; UFW on; fail2ban for SSH
- [ ] Nightly backup ran and the offsite copy exists in B2
- [ ] Restore drill completed once
- [ ] `.env` on server readable only by the app user (chmod 600)
- [ ] Bot token restricted, allowlist populated, unknown users ignored

## 5. Suggestions that make it noticeably better (cheap wins)

1. **Photos on every product** — the stakeholder thinks in dresses, not SKUs. One image
   per product makes the panel instantly usable for her.
2. **Low-stock threshold per variant** → nightly Telegram alert. Prevents "sold what we
   don't have" during busy periods.
3. **Sales channel field** (already in Phase 3) — after a month she'll see whether
   Instagram or the shop actually earns, which changes where she spends time.
4. **Installment schedule (рассрочка)** — due dates on debts + a bot reminder for
   overdue ones. This alone replaces most of her manual chasing.
5. **Excel in, Excel out** — import the starting catalog, export any report. Meets her
   where she is today.
6. **History on everything** (django-simple-history) — when a number looks wrong, you
   see who changed what and when. Also your audit story.
7. **Weekly Telegram digest to the Owner** — Monday morning: last week's revenue,
   profit, top 5 products, total outstanding debt.
8. Later, if she opens a second location: add a `Warehouse` FK on StockMovement. The
   movement-based design makes this a small change, not a rewrite.

## 6. Running costs

VPS ~€5/mo, domain ~$10/yr, Backblaze B2 ~$0–1/mo, Telegram free, Tailscale free tier
fine for this. Bitrix24 free tier exists. WhatsApp Cloud API is the only meaningfully
metered item — verify current Meta pricing before Phase 8.

## 7. First message to send Claude Code

> Read CLAUDE.md and INSTRUCTIONS.md fully. Confirm the assumptions at the top of
> INSTRUCTIONS.md with me, then start Phase 1. Work phase by phase; after each phase,
> stop and walk me through what was built and how to verify the "Done when" criteria.
