# ACOCOS — Client-facing bots (Telegram + WhatsApp)

Spec for the **customer-facing** bot experience. Read CLAUDE.md and the Phase 4 bot spec
first — the staff bot, the two-bot split, the auto-reply, and human handoff already exist.
This document defines what *clients* can do, how orders arrive from them, how broadcasts
work, and how the owner runs it all from one place. No new libraries. Build in the order
given: each part ships working value on its own.

---

## 1. Strategy — what each channel is actually for

These are not the same product and must not be built as one.

| | **Telegram** | **WhatsApp** |
|---|---|---|
| Reach | Only users who pressed `/start` | Anyone who messages the business number |
| Outbound | Free, unlimited, rich (photos, buttons) | Templates only outside 24h, paid, ban risk |
| Role | **Brand channel** — catalog, broadcasts, self-service | **Service channel** — answering, order status |
| Build | Rich menus, inline buttons, media groups | Simple text + images, numbered menus |

**The strategic consequence:** Telegram is where the relationship lives; WhatsApp is where
the conversation happens. Do not attempt rich interactive menus on WhatsApp — build there
for someone typing a sentence, not tapping a button.

**The one metric that governs everything:** how many clients have a `telegram_chat_id`.
Every broadcast feature is worthless below a real audience. Surface this number
prominently in the panel and dashboard: «Доступно в Telegram: N из M клиентов».

## 2. Client lifecycle the bots must support

```
Discovery → Subscribe → Browse → Ask → Request (заявка) → Order → Track → Receive → Return
```

Each stage below maps to a concrete feature. Build stages 2–5 first; they carry the value.

---

## 3. Telegram client bot — full specification

Separate token (`TELEGRAM_CLIENT_TOKEN`), separate Dispatcher, **no shared handler surface
with the staff bot**. A client must never be able to reach stock totals, revenue, cost
price, other clients, or aggregate data. Test this explicitly.

### 3.1 Onboarding (`/start`)

1. Greeting in Russian with the ACOCOS name and one line on what the bot does.
2. `KeyboardButton(request_contact=True)` → «Поделиться номером». Telegram returns a
   **verified** phone. Match to an existing Client by phone, or create one with
   `source='telegram'`. Store `telegram_chat_id`.
3. If matched to an existing client, greet by name and mention open orders if any.
4. Ask consent explicitly: «Присылать новинки?» → Да / Нет. Sets `marketing_consent`.
   Never assume consent from `/start` alone.
5. Deep links: `t.me/<bot>?start=<payload>` — support payloads for `product_<sku>`
   (opens that item), `order_<id>` (opens that order), `qr` (from printed tags).

### 3.2 Persistent main menu (reply keyboard, always visible)

```
🛍 Каталог        📦 Мои заказы
❤️ Избранное      💬 Написать нам
ℹ️ О нас
```
Five buttons maximum. Anything more and it stops being scannable on a phone.

### 3.3 Каталог

- Browse by category first (fewer taps than a flat list), then paginated items.
- Each item: photo, name, price in сом, and **available sizes** («46, 48 — есть; 50 — нет»).
  Read live availability (on_hand − reserved), never a cache.
- Inline buttons per item: `Хочу заказать` · `❤️ В избранное` · `Уведомить о поступлении`
  (shown only when that size is out of stock).
- Text search works too: any message matching a product name or SKU returns matches.
- Out-of-stock items stay visible with an alert option — do not hide them. A hidden item
  cannot generate demand signal.

### 3.4 Заявка — the client order request (core new logic)

**A client request is NOT an order.** It never reserves stock, never sets a price, never
creates production work. It is an inbound lead that staff convert.

Flow: item → size/colour → quantity → optional note («нужно к 15 августа») → confirm →
creates `OrderRequest(status='новая')`.

- Client immediately sees: «Заявка №14 принята. Мы свяжемся с вами для подтверждения.»
  Never «заказ принят» — do not promise what has not been confirmed.
- Staff receive an instant Telegram notification with an «Открыть» deep link to the panel.
- In the panel, staff either **Подтвердить** (converts to a real `Order` with due date and
  confirmed price — reusing the existing order creation service) or **Отклонить** with a
  short reason.
- Client is notified of either outcome, with the reason if declined.
- A client may cancel their own request while it is still `новая`.
- Rate limit: max 5 open requests per client; beyond that, ask them to write instead.

### 3.5 Мои заказы — self-service status (kills her most repeated message)

- Lists the client's orders: number, items, status, due date, deposit paid, remaining.
- Status in plain Russian, not internal codes: «Принят» · «В производстве» · «Готов к
  выдаче» · «Выдан».
- **Automatic push on status change** — especially `готов`: «Ваш заказ №14 готов! Ждём вас.»
  This single notification removes most «когда будет готово?» traffic.
- Shows remaining balance in сом. Never shows cost price or margin.
- `/mydebt` equivalent: total outstanding across all orders, plus an «Оплатить» button that
  just tells them how to pay (no payment gateway — see §9).

### 3.6 Избранное + уведомления о поступлении

- Favourites list per client. **Owner-visible aggregate: «Чаще всего добавляют в избранное»
  — that is free demand research** telling her what to produce next.
- Back-in-stock alerts: when a `PRODUCTION_IN` or `PURCHASE_IN` movement raises a watched
  variant's availability above zero, notify everyone waiting, oldest first, throttled.
  Automated demand capture with zero staff effort.

### 3.7 О нас

Static, editable from the admin (a simple `BotContent` key/value model — not hardcoded):
working hours, address with a map link, Instagram, size guide («таблица размеров»), care
instructions, and the return policy. These answer a large share of routine questions.

---

## 4. WhatsApp client experience

Keep it simple. Assume someone typing a sentence, not navigating a menu.

- **Inbound auto-reply** (already built in Phase 4): product name → photo, price, sizes in
  stock. Debt/order words → their own balance and latest order status.
- **Add order-status intent**: «когда готово», «мой заказ», «заказ 14» → status of their
  open orders. This is the highest-value addition to WhatsApp.
- **Numbered menu** on `меню` or an unrecognised greeting — WhatsApp interactive list
  messages where supported, plain numbered text otherwise:
  `1 — Каталог · 2 — Мой заказ · 3 — Оплата и доставка · 4 — Написать менеджеру`
- **Заявка over WhatsApp**: keep it conversational. If a client expresses intent to order,
  create an `OrderRequest` with the raw message text attached and hand off to a human —
  do not attempt a multi-step guided flow on WhatsApp.
- Every auto-reply ends with «Напишите "менеджер" — ответит человек.»
- All Phase 4 handoff rules apply: bot goes silent for 6h on «менеджер», on any staff reply,
  or on 3+ client messages in 2 minutes.

---

## 5. Рассылки — broadcasting new arrivals

### 5.1 Audience segments (composable filters)
- Has `telegram_chat_id` + `marketing_consent` (mandatory base for Telegram)
- Bought before / never bought
- Bought a specific category
- Has outstanding debt / has none
- Inactive 90+ days (win-back)
- Favourited a specific product (highest-converting segment by far)

**Always preview the honest reachable count before sending.** No rounding up.

### 5.2 Telegram broadcast (primary)
- Compose once in the panel: text + select products (photos pulled automatically).
- Send as `sendMediaGroup` (max 10 photos, one caption) plus an inline button linking back
  to the catalog or a specific item deep link.
- Throttle ~20 msg/sec; honour `429 retry_after` exactly; retry 3× with backoff; then mark
  that recipient `failed` and continue. One bad chat must never stall a campaign.
- **Resumable and idempotent**: a killed process resumes from `pending` only. Never message
  the same client twice in one campaign. Test with a mid-send kill.
- Every send logged as an `Interaction`.

### 5.3 WhatsApp broadcast (secondary, flag-gated)
- Template messages only, opted-in clients only, never free-form outside 24h.
- **Cost pattern that matters:** send ONE template with a single hero image and a
  «Показать все новинки» call to action. When the client replies, the 24-hour window opens
  and the full photo set goes free-form at no cost. One paid message, not five.
- Stop immediately on any quality-rating warning and notify the Owner.

### 5.4 Universal rules
- Honour `marketing_consent` and `whatsapp_opted_in` absolutely.
- «СТОП» / `/stop` unsubscribes instantly across both channels, confirmed to the client.
- Max 2 broadcasts per client per week, enforced in code — not policy, code.
- Never broadcast between 22:00 and 09:00 Asia/Bishkek. Queue for morning instead.

---

## 6. One place to operate — the unified inbox

This is the «одно место» requirement. Build it as a panel page, not a separate tool.

- **`/inbox/`** — a single stream of all incoming messages from both channels, newest first.
- Each row: client name (or phone), channel icon, message preview, time, and status badge
  (new / answered / handed off).
- Opening a conversation shows full history for that client **across both channels in one
  thread**, plus a side panel with: debt, open orders, заявки, last purchase, favourites.
- **Reply directly from the panel** — the message routes through whichever channel the
  client used. Sending a reply automatically triggers the 6h handoff (bot goes quiet).
- Filters: unanswered · has заявка · has debt · channel.
- Badge counts in the nav so nothing sits unanswered unseen.
- Заявки appear here too, with inline Подтвердить / Отклонить actions.

---

## 7. Data model additions

```
BotContent(key, title_ru, body_ru, is_active)            # О нас, размеры, доставка
OrderRequest(client, status[новая|подтверждена|отклонена|отменена],
             source[telegram|whatsapp], note, raw_message, created_at,
             handled_by, handled_at, decline_reason, order→FK nullable)
OrderRequestItem(request, variant, quantity, note)
Favourite(client, variant, created_at)                    # unique together
StockAlert(client, variant, created_at, notified_at)      # back-in-stock queue
BroadcastLog(client, campaign, channel, sent_at)          # frequency capping
```
Extend `Client`: `telegram_chat_id`, `marketing_consent`, `whatsapp_opted_in`,
`bot_language` (ru default, ky optional later), `human_handoff_until`.

---

## 8. Roles

- **Owner** — everything: broadcasts, bot content, consent overrides, all analytics.
- **Manager** — inbox, replying, confirming/declining заявки, order status updates.
  May *not* send broadcasts (a mistaken blast is expensive and un-undoable).
- **Viewer** — inbox read-only.
Enforce server-side, not by hiding UI. Test a Manager POSTing a broadcast → 403.

---

## 9. Deliberately out of scope — and why

- **In-bot payment.** Payment links in KG mean MBank/Optima integration, PCI concerns, and
  reconciliation complexity. The bot shows the amount and how to pay; staff confirm receipt
  in the panel. Revisit only if she asks.
- **AI/LLM intent parsing.** A dumb matcher that stays silent beats a clever one that
  guesses wrong and creates a customer misunderstanding she must then undo.
- **Client self-service cancellation of confirmed orders.** Production may have started.
  Route to a human.
- **Kyrgyz-language UI.** Structure all strings through gettext so `ky` is a translation
  file later, not a refactor.

---

## 10. Tests

Client bot cannot return cost price, revenue, stock totals, or another client's data
(assert on rendered text) · contact share links a verified phone and stores chat_id ·
`/start` without consent does not set `marketing_consent` · заявка never reserves stock and
never creates an Order until staff confirm · confirming a заявка creates exactly one Order
and links both ways · declining notifies the client with the reason · order status change to
`готов` pushes a notification once and only once · back-in-stock fires on PRODUCTION_IN and
not on a cache refresh · broadcast respects consent, the 2/week cap, and quiet hours ·
broadcast is idempotent across a killed process · «СТОП» blocks the next broadcast on both
channels · Manager cannot send a broadcast (403) · inbox reply triggers handoff · all bot
copy is translated and compiled.

## 11. Definition of done

- [ ] A client finds ACOCOS via QR, presses /start, shares their number, and is matched to
      their existing Client record with purchase history intact
- [ ] They browse the catalog, see live per-size availability, and submit a заявка
- [ ] Staff see it in `/inbox/` within seconds, confirm in one tap, and it becomes a real
      Order with stock reserved only at that moment
- [ ] The client is notified automatically when the order is `готов` — with no staff action
- [ ] A client asking «когда готово?» on WhatsApp gets an accurate status instantly
- [ ] The owner composes one broadcast with 6 new products and sends it to consenting
      Telegram clients, seeing the honest reachable count first
- [ ] Every conversation from both channels is answerable from one panel page
- [ ] «Доступно в Telegram: N из M» is visible on the dashboard
- [ ] All tests green

## 12. Do NOT

Merge client and staff bots · expose cost, profit, stock totals, or other clients' data to
a client · let a bot create an Order or reserve stock without staff confirmation · promise
«заказ принят» for an unconfirmed заявка · message anyone without consent · exceed 2
broadcasts per client per week · send between 22:00–09:00 · free-form WhatsApp outside the
24h window · use unofficial WhatsApp gateways · build a guided multi-step flow on WhatsApp ·
let a failed recipient stall a campaign · use an LLM for intent · hardcode «О нас» content.