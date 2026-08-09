from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Max, Sum, Value
from django.db.models.functions import Replace
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Client, Interaction


@transaction.atomic
def log_whatsapp_interaction(phone: str, text: str) -> Client:
    """CRM-linking rule: every incoming WhatsApp message matches a Client by phone,
    creating one (source=whatsapp) if none exists, and logs it as an Interaction —
    this is what makes the WhatsApp bot a CRM channel, not just a stock lookup tool.
    """
    client, _created = Client.objects.get_or_create(
        phone=phone, defaults={"first_name": phone, "source": Client.WHATSAPP}
    )
    Interaction.objects.create(client=client, kind=Interaction.MESSAGE, note=text)
    return client


def set_marketing_consent(client: Client, consent: bool) -> None:
    """The explicit «Присылать новинки?» Да/Нет step — the ONLY place
    marketing_consent is set True (besides an owner's manual admin edit)."""
    client.marketing_consent = consent
    client.save(update_fields=["marketing_consent"])


def client_by_chat_id(chat_id: int) -> Client | None:
    return Client.objects.filter(telegram_chat_id=chat_id).first()


def log_telegram_interaction(client: Client, text: str) -> None:
    """Mirrors log_whatsapp_interaction's CRM-linking rule for the client
    Telegram bot's «Написать нам» — every free-text message to staff is
    logged against the client, not just answered and forgotten."""
    Interaction.objects.create(client=client, kind=Interaction.MESSAGE, note=text)


def find_client_by_phone(phone: str) -> Client | None:
    """Format-tolerant phone lookup — the ONE phone-matching implementation
    shared by subscribe_telegram (Telegram contact share) and
    apps.clients.management.commands.import_opening_balances (bulk import),
    never duplicated between them.

    Matches on the trailing 9 digits (a KG mobile subscriber number, with any
    country code or trunk prefix stripped) rather than a full exact match —
    «+996700123456», «996700123456», and the local «0700123456» all resolve
    to the same client, which is exactly the without-a-country-code case a
    hand-typed import spreadsheet produces. Separators are stripped in SQL
    (REPLACE chain) so the prefilter returns a handful of candidates instead
    of scanning every client row in Python on each call. Client.phone is
    unique, so at most one match is ever possible."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return None
    normalized = F("phone")
    for sep in (" ", "-", "(", ")", "+", "."):
        normalized = Replace(normalized, Value(sep))
    candidates = Client.objects.annotate(_digits=normalized).filter(_digits__endswith=digits[-7:])
    suffix = digits[-9:]
    return next(
        (c for c in candidates if "".join(ch for ch in c.phone if ch.isdigit())[-9:] == suffix),
        None,
    )


def subscribe_telegram(phone: str, chat_id: int) -> Client | None:
    """A client shared their contact with the bot: link their Telegram chat so
    the bot CAN reach them. Matches by phone; returns None if no client has
    that number (the bot then can't reach them — messaging is chat_id-only).

    Deliberately does NOT set marketing_consent — CLIENT_BOTS.md §3.1: "Never
    assume consent from /start alone." A verified phone only makes a client
    reachable; consent is a separate explicit «Присылать новинки?» Да/Нет
    step (see set_marketing_consent), asked right after this."""
    client = find_client_by_phone(phone)
    if client is None:
        return None
    client.telegram_chat_id = chat_id
    client.save(update_fields=["telegram_chat_id"])
    return client


def start_handoff(client: Client, hours: int = 6) -> None:
    """WhatsApp auto-reply goes quiet for `hours` — on «менеджер», on any
    staff reply from /inbox/, or on a burst of client messages (see
    apps.wa.client_replies / apps.inbox.views). NULL/past = bot answers
    normally again."""
    client.human_handoff_until = timezone.now() + timedelta(hours=hours)
    client.save(update_fields=["human_handoff_until"])


def is_handed_off(client: Client) -> bool:
    return bool(client.human_handoff_until and client.human_handoff_until > timezone.now())


def recent_message_burst(client: Client, within_minutes: int = 2, threshold: int = 3) -> bool:
    """3+ inbound messages from this client within `within_minutes` — the
    other automatic handoff trigger (CLIENT_BOTS.md §4): a rapid-fire client
    is asking for a human, not another auto-reply."""
    cutoff = timezone.now() - timedelta(minutes=within_minutes)
    return (
        Interaction.objects.filter(
            client=client, kind=Interaction.MESSAGE, created_at__gte=cutoff
        ).count()
        >= threshold
    )


def unsubscribe_telegram(chat_id: int) -> Client | None:
    """«СТОП»/«STOP» from a subscribed client — clear consent (keep the chat_id
    so we know they were once reachable, just don't message them)."""
    client = Client.objects.filter(telegram_chat_id=chat_id).first()
    if client is None:
        return None
    client.marketing_consent = False
    client.save(update_fields=["marketing_consent"])
    return client


def lapsed_clients(days: int = 60) -> list[Client]:
    """Clients whose most recent confirmed purchase is older than `days` — the
    re-engagement segment ('bought before, but not lately'). Clients who never
    purchased are excluded; they're a colder, separate segment. Feeds both the
    /lapsed bot lookup and the campaign 'inactive for N days' audience filter."""
    from apps.sales.models import SaleOrder

    cutoff = timezone.now() - timedelta(days=days)
    last = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client__isnull=False)
        .values("client_id")
        .annotate(last=Max("confirmed_at"))
    )
    lapsed_ids = [row["client_id"] for row in last if row["last"] and row["last"] < cutoff]
    return list(
        Client.objects.filter(pk__in=lapsed_ids, is_active=True).order_by(
            "first_name", "descriptor"
        )
    )


def client_debts_by_currency(client: "Client | None" = None) -> dict[int, dict[str, Decimal]]:
    """{client_id: {currency: debt}} — a starting ClientOpeningBalance (if
    any — pre-system debt, see that model's own docstring for why it's
    neither a SaleOrder nor a Payment) plus confirmed sale totals minus
    payments, keyed by the ORDER's currency. A payment counts against the
    order it was made for by its NET applied amount (gross minus any change
    handed back, see Payment.net_applied_kgs — never the raw gross amount),
    converted into that order's currency at the rate frozen on the payment
    (net_applied_kgs ÷ order_rate) — so a USD payment on a сом order reduces
    the сом debt by what actually stayed with the shop. Three grouped
    queries total, no N+1.

    Pass `client` to scope every query to one client (an indexed lookup)
    instead of aggregating the whole business's sales/payments history —
    every single-client caller (client_debt/client_credits below, and their
    call sites) MUST pass it; leaving it unset is only for a genuine
    whole-book aggregate (total_outstanding_debt, debtors_report_rows)."""
    from decimal import ROUND_HALF_UP

    from django.db.models import DecimalField, ExpressionWrapper

    from apps.core.currency import CENTS
    from apps.sales.models import Payment, SaleOrder

    from .models import ClientOpeningBalance

    totals: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    opening_qs = ClientOpeningBalance.objects.all()
    sales_qs = SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client__isnull=False)
    payment_qs = Payment.objects.filter(client__isnull=False, order__isnull=False)
    if client is not None:
        opening_qs = opening_qs.filter(client=client)
        sales_qs = sales_qs.filter(client=client)
        payment_qs = payment_qs.filter(client=client)

    # No conversion needed — an opening balance is already stored in the
    # SAME currency it's added into, same as a SaleOrder's own `total`.
    opening_rows = opening_qs.values("client_id", "currency").annotate(t=Sum("amount"))
    for row in opening_rows:
        totals[row["client_id"]][row["currency"]] += row["t"] or Decimal("0")

    sales_rows = sales_qs.values("client_id", "currency").annotate(t=Sum("total"))
    for row in sales_rows:
        totals[row["client_id"]][row["currency"]] += row["t"] or Decimal("0")

    converted = ExpressionWrapper(
        (F("amount") * F("rate_to_kgs") - F("change_amount_kgs")) / F("order__rate_to_kgs"),
        output_field=DecimalField(max_digits=20, decimal_places=6),
    )
    payment_rows = payment_qs.values("client_id", "order__currency").annotate(t=Sum(converted))
    for row in payment_rows:
        totals[row["client_id"]][row["order__currency"]] -= row["t"] or Decimal("0")

    from django.conf import settings

    tolerance = settings.PAYMENT_ROUNDING_TOLERANCE
    result = {}
    for cid, currs in totals.items():
        quantized = {cur: amt.quantize(CENTS, rounding=ROUND_HALF_UP) for cur, amt in currs.items()}
        # A residue of at most `tolerance` сом (sub-сом currency-conversion
        # rounding) is treated as fully settled, not a lingering debt — but a
        # negative amount (client overpaid) is a real credit and stays visible.
        kept = {cur: amt for cur, amt in quantized.items() if amt > tolerance or amt < 0}
        if kept:
            result[cid] = kept
    return result


def client_statement(client: Client, date_from=None, date_to=None) -> dict[str, dict]:
    """Per-currency running-balance ledger for one client — a chronological
    merge of confirmed sales (+total) and payments (−net_applied_kgs,
    converted into the SALE's own currency at that payment's frozen rate —
    the exact same conversion client_debts_by_currency uses) with a running
    balance after each row. This is the "she pays down part of her debt,
    takes new goods on credit, one number rolls forward" statement her real
    paper ledger already is — CLAUDE.md's per-sale debt model is what the
    CODE still enforces (SaleOrder.balance, «Погасить» per sale); this is a
    read-only VIEW over that exact same data, never a second source of truth.

    Positive balance = client owes; negative = client is in credit (Аванс) —
    same sign convention as client_debts_by_currency throughout.

    Built as ONE full, unfiltered timeline per currency, then sliced by
    [date_from, date_to]: `opening` is the running balance immediately
    BEFORE the first in-range row (0 if nothing precedes it), never a
    separately tracked figure. A ClientOpeningBalance (pre-system debt)
    slots into this SAME timeline as one more entry type, at its own
    as_of_date — this is exactly why date-slicing needed no changes when it
    landed: for the unfiltered view it shows as its OWN row, described
    «Старый долг», the literal first line of her real paper statement; for
    a date-filtered view starting after as_of_date it's simply folded into
    `opening` like any other before-the-window entry, no special case.

    `closing` is the running total quantized ONCE at the very end — never
    per-row — over the exact same per-row terms client_debts_by_currency
    sums (order.total for a sale; the SAME ExpressionWrapper-computed
    `contribution` field for a payment, not a second, independently-written
    division that could round differently at the 6th decimal place). For
    the unfiltered view (date_to=None), `closing` is additionally read
    directly FROM client_debts_by_currency wherever it has an entry for
    that currency — not merely equal by construction, structurally sourced
    from it, so the two can never drift even if one function's math changes
    later without the other being updated. Below PAYMENT_ROUNDING_TOLERANCE
    (client_debts_by_currency excludes it entirely, treating it as settled)
    the walked value is kept as-is — a ledger shouldn't hide a real, if tiny,
    residue, only the debt SUMMARY badge should.

    Only currencies with in-range activity, or a non-zero balance carried
    into the range, are included in the result. Cancelled sales contribute
    no row (matching client_debts_by_currency's own CONFIRMED-only filter);
    a payment against a since-cancelled sale still appears — cancel_sale
    never touches Payment rows, so client_debts_by_currency already counts
    it too, and this must reconcile with that, not "fix" it independently."""
    from datetime import datetime, time
    from decimal import ROUND_HALF_UP

    from apps.core.currency import CENTS
    from apps.sales.models import Payment, SaleOrder

    from .models import ClientOpeningBalance

    openings = list(ClientOpeningBalance.objects.filter(client=client))
    orders = list(
        SaleOrder.objects.filter(client=client, status=SaleOrder.CONFIRMED).only(
            "id", "total", "currency", "confirmed_at"
        )
    )
    # Per-row, not a second Python-side division — see the docstring above.
    contribution = ExpressionWrapper(
        (F("amount") * F("rate_to_kgs") - F("change_amount_kgs")) / F("order__rate_to_kgs"),
        output_field=DecimalField(max_digits=20, decimal_places=6),
    )
    payments = list(
        Payment.objects.filter(client=client, order__isnull=False)
        .select_related("order")
        .annotate(contribution=contribution)
    )

    entries_by_currency: dict[str, list[dict]] = defaultdict(list)
    for ob in openings:
        # Midnight LOCAL time on as_of_date — comparable to confirmed_at/
        # created_at's own timezone-aware sort_dt, and its .date() (via
        # timezone.localtime below) reproduces as_of_date exactly, the same
        # way every other entry's "date" is derived from a real timestamp.
        ob_dt = timezone.make_aware(datetime.combine(ob.as_of_date, time.min))
        entries_by_currency[ob.currency].append(
            {
                "sort_dt": ob_dt,
                "date": ob.as_of_date,
                "kind": "opening_balance",
                "description": _("Старый долг")
                if ob.amount > 0
                else _("Остаток на начало (аванс)"),
                "sale_amount": ob.amount if ob.amount > 0 else None,
                "payment_amount": -ob.amount if ob.amount < 0 else None,
                "delta": ob.amount,
                "order": None,
            }
        )
    for o in orders:
        if o.confirmed_at is None:  # defensive — a CONFIRMED order always has one
            continue
        entries_by_currency[o.currency].append(
            {
                "sort_dt": o.confirmed_at,
                "date": timezone.localtime(o.confirmed_at).date(),
                "kind": "sale",
                "description": _("Покупка (Чек №%(n)s)") % {"n": o.pk},
                "sale_amount": o.total,
                "payment_amount": None,
                "delta": o.total,
                "order": o,
            }
        )
    # Group by (batch_id, is_reversal): every Payment ONE pay_oldest_first
    # call created shares a batch_id (see Payment.batch_id) and becomes ONE
    # ledger row — she experiences "получила 150 000" as a single event,
    # even though it's several Payment rows under the hood, one per sale it
    # covered. is_reversal is part of the key (not folded into the same
    # group as the originals it reverses) because those are two SEPARATE
    # events at two different times — voiding a batch shows as its own
    # later «Отмена оплаты» row, exactly like voiding an ordinary single
    # payment already does, never merged back into the original row. A
    # payment with no batch_id (batch_id=None) is a group of exactly one —
    # keyed on its own pk so it never accidentally merges with another
    # ungrouped payment.
    batches: dict[tuple, list] = defaultdict(list)
    for p in payments:
        key = (p.batch_id, p.reversed_payment_id is not None) if p.batch_id else ("single", p.pk)
        batches[key].append(p)

    for group in batches.values():
        first = group[0]
        order = first.order
        is_reversal = first.reversed_payment_id is not None
        total_contribution = sum((p.contribution for p in group), Decimal("0"))
        earliest = min(p.created_at for p in group)
        # "expandable to see which sales it covered" — populated only for a
        # REAL batch (2+ payments); a lone payment has nothing to expand,
        # so callers can just check truthiness rather than length.
        batch_detail = (
            [
                {
                    "order": p.order,
                    "amount": (-p.contribution if is_reversal else p.contribution).quantize(
                        Decimal("0.01")
                    ),
                }
                for p in sorted(group, key=lambda p: p.order.confirmed_at or p.created_at)
            ]
            if len(group) > 1
            else []
        )
        entries_by_currency[order.currency].append(
            {
                "sort_dt": earliest,
                "date": timezone.localtime(earliest).date(),
                "kind": "reversal" if is_reversal else "payment",
                "description": _("Отмена оплаты") if is_reversal else _("Оплата"),
                # A reversal's contribution is <= 0 (it undoes a payment), so
                # -contribution is the positive amount it ADDS back to the
                # balance — shown in the increase column, like a sale.
                "sale_amount": -total_contribution if is_reversal else None,
                "payment_amount": None if is_reversal else total_contribution,
                "delta": -total_contribution,
                "order": order if len(group) == 1 else None,
                "batch_detail": batch_detail,
            }
        )

    authoritative_closing = client_debts_by_currency(client=client).get(client.pk, {})

    result: dict[str, dict] = {}
    for currency, entries in entries_by_currency.items():
        entries.sort(key=lambda e: e["sort_dt"])
        running = Decimal("0")
        opening = Decimal("0")
        rows = []
        for e in entries:
            if date_from is not None and e["date"] < date_from:
                running += e["delta"]
                opening = running
                continue
            if date_to is not None and e["date"] > date_to:
                continue
            running += e["delta"]
            rows.append(
                {
                    "date": e["date"],
                    "kind": e["kind"],
                    "description": e["description"],
                    "sale_amount": e["sale_amount"].quantize(CENTS, rounding=ROUND_HALF_UP)
                    if e["sale_amount"] is not None
                    else None,
                    "payment_amount": e["payment_amount"].quantize(CENTS, rounding=ROUND_HALF_UP)
                    if e["payment_amount"] is not None
                    else None,
                    "balance": running.quantize(CENTS, rounding=ROUND_HALF_UP),
                    "order": e["order"],
                    "batch_detail": e.get("batch_detail") or [],
                }
            )
        closing = running.quantize(CENTS, rounding=ROUND_HALF_UP)
        if date_to is None:
            closing = authoritative_closing.get(currency, closing)

        opening = opening.quantize(CENTS, rounding=ROUND_HALF_UP)
        if not rows and opening == 0 and closing == 0:
            continue
        result[currency] = {"opening": opening, "rows": rows, "closing": closing}
    return result


def client_debt_and_credit(client: Client) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """{currency: debt}, {currency: credit} for one client, in ONE indexed
    query pair instead of two — the call site every page that shows both at
    once (client_detail) should use, instead of client_debt()+client_credits()
    back to back."""
    debts = client_debts_by_currency(client=client).get(client.pk, {})
    return (
        {cur: amt for cur, amt in debts.items() if amt > 0},
        {cur: -amt for cur, amt in debts.items() if amt < 0},
    )


def client_debt(client: Client) -> dict[str, Decimal]:
    """{currency: debt} for one client — positive balances only. Deliberately
    excludes credits (see client_credits) — this feeds debt-reminder logic,
    which must never nudge a client who's actually in credit."""
    debts = client_debts_by_currency(client=client).get(client.pk, {})
    return {cur: amt for cur, amt in debts.items() if amt > 0}


def client_credits(client: Client) -> dict[str, Decimal]:
    """{currency: credit} for one client — a negative pooled balance (paid
    more than they owe, via «В счёт долга»/«Аванс», see Payment) shown as a
    positive «Аванс» figure, never as a negative debt."""
    debts = client_debts_by_currency(client=client).get(client.pk, {})
    return {cur: -amt for cur, amt in debts.items() if amt < 0}


def client_debts_kgs() -> dict[int, Decimal]:
    """{client_id: outstanding debt, in KGS} — same frozen-value methodology
    as apps.reports.dashboard._debt() (total_kgs minus each payment's own
    net_applied_kgs, summed per order then per client), never a live-rate
    conversion of client_debts_by_currency's per-currency figures. Debt
    surfaces that need ONE KGS number (not a per-currency breakdown) must
    read this, not re-derive it — see apps.core.views._business_snapshot."""
    from apps.sales.models import Payment, SaleOrder

    orders = list(
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client__isnull=False).values(
            "id", "client_id", "total_kgs"
        )
    )
    pay_kgs = ExpressionWrapper(
        F("amount") * F("rate_to_kgs") - F("change_amount_kgs"),
        output_field=DecimalField(max_digits=20, decimal_places=6),
    )
    paid_by_order: dict[int, Decimal] = defaultdict(Decimal)
    for row in (
        Payment.objects.filter(order__isnull=False, order__client__isnull=False)
        .annotate(kgs=pay_kgs)
        .values("order_id")
        .annotate(t=Sum("kgs"))
    ):
        paid_by_order[row["order_id"]] = row["t"] or Decimal("0")

    debt_by_client: dict[int, Decimal] = defaultdict(Decimal)
    for o in orders:
        outstanding = o["total_kgs"] - paid_by_order.get(o["id"], Decimal("0"))
        if outstanding > 0:
            debt_by_client[o["client_id"]] += outstanding
    return dict(debt_by_client)


def total_outstanding_debt() -> dict[str, Decimal]:
    """{currency: total debt across all clients}."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for currs in client_debts_by_currency().values():
        for cur, amt in currs.items():
            if amt > 0:
                totals[cur] += amt
    return dict(totals)


def debtors_report_rows():
    """(client, currency, debt, last_payment_date) for every client/currency
    pair with debt > 0, highest debt first — used by the Debts sheet and the
    /debts bot command."""
    from apps.sales.models import Payment

    debts = client_debts_by_currency()
    client_ids = [cid for cid, currs in debts.items() if any(a > 0 for a in currs.values())]
    clients = {c.pk: c for c in Client.objects.filter(pk__in=client_ids)}

    last_payment = {
        row["client_id"]: row["last"]
        for row in Payment.objects.filter(client_id__in=client_ids)
        .values("client_id")
        .annotate(last=Max("created_at"))
    }

    rows = []
    for cid, currs in debts.items():
        client = clients.get(cid)
        if client is None:
            continue
        for cur, amt in currs.items():
            if amt > 0:
                rows.append((client, cur, amt, last_payment.get(cid)))
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows
