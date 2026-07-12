from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from .models import Client, Interaction


@transaction.atomic
def log_whatsapp_interaction(phone: str, text: str) -> Client:
    """CRM-linking rule: every incoming WhatsApp message matches a Client by phone,
    creating one (source=whatsapp) if none exists, and logs it as an Interaction —
    this is what makes the WhatsApp bot a CRM channel, not just a stock lookup tool.
    """
    client, _created = Client.objects.get_or_create(
        phone=phone, defaults={"name": phone, "source": Client.WHATSAPP}
    )
    Interaction.objects.create(client=client, kind=Interaction.MESSAGE, note=text)
    return client


def _decimal_subquery(qs):
    return Coalesce(
        Subquery(qs, output_field=DecimalField(max_digits=14, decimal_places=2)),
        Decimal("0"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )


def clients_with_debt():
    """Annotate every client with debt = confirmed sales total − payments total.

    Uses two subqueries (not joined Sums) so the numbers can't be duplicated by joins,
    and the whole client list stays a single SQL query.
    """
    from apps.sales.models import Payment, SaleOrder

    sales_total = (
        SaleOrder.objects.filter(client=OuterRef("pk"), status=SaleOrder.CONFIRMED)
        .values("client")
        .annotate(t=Sum("total"))
        .values("t")
    )
    payments_total = (
        Payment.objects.filter(client=OuterRef("pk"))
        .values("client")
        .annotate(t=Sum("amount"))
        .values("t")
    )
    return Client.objects.annotate(
        sales_total=_decimal_subquery(sales_total),
        payments_total=_decimal_subquery(payments_total),
    )


def client_debt(client: Client) -> Decimal:
    row = clients_with_debt().filter(pk=client.pk).first()
    if row is None:
        return Decimal("0")
    return row.sales_total - row.payments_total


def total_outstanding_debt() -> Decimal:
    total = Decimal("0")
    for c in clients_with_debt():
        total += c.sales_total - c.payments_total
    return max(total, Decimal("0"))


def debtors_report_rows():
    """(client, debt, last_payment_date) for every client with debt > 0, highest
    debt first — used by the Debts sheet in the daily report."""
    from apps.sales.models import Payment

    last_payment = (
        Payment.objects.filter(client=OuterRef("pk")).order_by("-created_at").values("created_at")
    )
    qs = clients_with_debt().annotate(last_payment_date=Subquery(last_payment[:1]))
    rows = [
        (c, c.sales_total - c.payments_total, c.last_payment_date)
        for c in qs
        if c.sales_total - c.payments_total > 0
    ]
    rows.sort(key=lambda row: row[1], reverse=True)
    return rows
