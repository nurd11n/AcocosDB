from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Max, Sum, Value
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


def subscribe_telegram(phone: str, chat_id: int) -> Client | None:
    """A client shared their contact with the bot: link their Telegram chat and
    opt them into broadcasts. Matches by phone; returns None if no client has
    that number (the bot then can't reach them — messaging is chat_id-only).

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
    candidates = Client.objects.annotate(_digits=normalized).filter(
        _digits__endswith=digits[-7:]
    )
    client = next(
        (c for c in candidates if "".join(ch for ch in c.phone if ch.isdigit()) == digits),
        None,
    )
    if client is None:
        return None
    client.telegram_chat_id = chat_id
    client.marketing_consent = True
    client.save(update_fields=["telegram_chat_id", "marketing_consent"])
    return client


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
        Client.objects.filter(pk__in=lapsed_ids, is_active=True).order_by("first_name", "last_name")
    )


def client_debts_by_currency() -> dict[int, dict[str, Decimal]]:
    """{client_id: {currency: debt}} — confirmed sale totals minus payments,
    computed independently per currency (a KGS payment never offsets a USD
    debt). Two grouped queries total, no N+1 regardless of client count."""
    from apps.sales.models import Payment, SaleOrder

    totals: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    sales_rows = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client__isnull=False)
        .values("client_id", "currency")
        .annotate(t=Sum("total"))
    )
    for row in sales_rows:
        totals[row["client_id"]][row["currency"]] += row["t"] or Decimal("0")

    payment_rows = (
        Payment.objects.filter(client__isnull=False)
        .values("client_id", "currency")
        .annotate(t=Sum("amount"))
    )
    for row in payment_rows:
        totals[row["client_id"]][row["currency"]] -= row["t"] or Decimal("0")

    return {
        cid: {cur: amt for cur, amt in currs.items() if amt != 0} for cid, currs in totals.items()
    }


def client_debt(client: Client) -> dict[str, Decimal]:
    """{currency: debt} for one client — positive balances only."""
    debts = client_debts_by_currency().get(client.pk, {})
    return {cur: amt for cur, amt in debts.items() if amt > 0}


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
