from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Max, Sum, Value
from django.db.models.functions import Replace
from django.utils import timezone

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


def subscribe_telegram(phone: str, chat_id: int) -> Client | None:
    """A client shared their contact with the bot: link their Telegram chat so
    the bot CAN reach them. Matches by phone; returns None if no client has
    that number (the bot then can't reach them — messaging is chat_id-only).

    Deliberately does NOT set marketing_consent — CLIENT_BOTS.md §3.1: "Never
    assume consent from /start alone." A verified phone only makes a client
    reachable; consent is a separate explicit «Присылать новинки?» Да/Нет
    step (see set_marketing_consent), asked right after this.

    Matching is digit-exact but format-tolerant («+996 700...» == «996700...»).
    Separators are stripped in SQL (REPLACE chain) so the prefilter returns a
    handful of candidates instead of scanning every client row in Python on
    each contact share; the Python check keeps the old exact semantics for any
    exotic character the chain doesn't cover."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return None
    normalized = F("phone")
    for sep in (" ", "-", "(", ")", "+", "."):
        normalized = Replace(normalized, Value(sep))
    candidates = Client.objects.annotate(_digits=normalized).filter(_digits__endswith=digits[-7:])
    client = next(
        (c for c in candidates if "".join(ch for ch in c.phone if ch.isdigit()) == digits),
        None,
    )
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


def client_debts_by_currency() -> dict[int, dict[str, Decimal]]:
    """{client_id: {currency: debt}} — confirmed sale totals minus payments,
    keyed by the ORDER's currency. A payment counts against the order it was
    made for by its NET applied amount (gross minus any change handed back,
    see Payment.net_applied_kgs — never the raw gross amount), converted into
    that order's currency at the rate frozen on the payment
    (net_applied_kgs ÷ order_rate) — so a USD payment on a сом order reduces
    the сом debt by what actually stayed with the shop. Two grouped queries
    total, no N+1."""
    from decimal import ROUND_HALF_UP

    from django.db.models import DecimalField, ExpressionWrapper

    from apps.core.currency import CENTS
    from apps.sales.models import Payment, SaleOrder

    totals: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    sales_rows = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client__isnull=False)
        .values("client_id", "currency")
        .annotate(t=Sum("total"))
    )
    for row in sales_rows:
        totals[row["client_id"]][row["currency"]] += row["t"] or Decimal("0")

    converted = ExpressionWrapper(
        (F("amount") * F("rate_to_kgs") - F("change_amount_kgs")) / F("order__rate_to_kgs"),
        output_field=DecimalField(max_digits=20, decimal_places=6),
    )
    payment_rows = (
        Payment.objects.filter(client__isnull=False, order__isnull=False)
        .values("client_id", "order__currency")
        .annotate(t=Sum(converted))
    )
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


def client_debt(client: Client) -> dict[str, Decimal]:
    """{currency: debt} for one client — positive balances only. Deliberately
    excludes credits (see client_credits) — this feeds debt-reminder logic,
    which must never nudge a client who's actually in credit."""
    debts = client_debts_by_currency().get(client.pk, {})
    return {cur: amt for cur, amt in debts.items() if amt > 0}


def client_credits(client: Client) -> dict[str, Decimal]:
    """{currency: credit} for one client — a negative pooled balance (paid
    more than they owe, via «В счёт долга»/«Аванс», see Payment) shown as a
    positive «Аванс» figure, never as a negative debt."""
    debts = client_debts_by_currency().get(client.pk, {})
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
