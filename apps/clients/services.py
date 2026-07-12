from decimal import Decimal

from django.db.models import DecimalField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from .models import Client


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
