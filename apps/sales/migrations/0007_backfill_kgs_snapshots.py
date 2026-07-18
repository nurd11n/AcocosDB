"""Backfill the frozen сом snapshot onto existing sales and payments.

For each row we freeze the rate from the ExchangeRate CLOSEST IN TIME to when the
row happened (confirmed_at for a sale, created_at for a payment). KGS rows are
1.0 by definition. A foreign row with no rate on record at all falls back to 1.0
— counted and logged, because that figure is a guess the owner should fix.

After this runs, every aggregate reads total_kgs / amount*rate_to_kgs and never
converts at read time, so historical charts stop moving when today's rate changes.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

logger = logging.getLogger(__name__)
CENTS = Decimal("0.01")


def _closest_rate(rates_by_currency, currency, on_date):
    """rate for `currency` from the ExchangeRate closest in time to on_date, or
    None if the currency has no rates at all."""
    rows = rates_by_currency.get(currency)
    if not rows:
        return None
    return min(rows, key=lambda r: abs((r[0] - on_date).days))[1]


def backfill(apps, schema_editor):
    SaleOrder = apps.get_model("sales", "SaleOrder")
    Payment = apps.get_model("sales", "Payment")
    ExchangeRate = apps.get_model("core", "ExchangeRate")

    # settings.CURRENCY isn't safe to trust across history; the base is KGS.
    base = "KGS"

    rates_by_currency = {}
    for row in ExchangeRate.objects.all().values_list("currency", "date", "rate"):
        rates_by_currency.setdefault(row[0], []).append((row[1], row[2]))

    sale_fallbacks = pay_fallbacks = 0

    for order in SaleOrder.objects.all().iterator():
        when = order.confirmed_at or order.created_at
        on_date = when.date() if when else None
        if order.currency == base:
            rate = Decimal("1")
        else:
            rate = _closest_rate(rates_by_currency, order.currency, on_date) if on_date else None
            if rate is None:
                rate = Decimal("1")
                sale_fallbacks += 1
        order.rate_to_kgs = rate
        order.total_kgs = (order.total * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
        order.save(update_fields=["rate_to_kgs", "total_kgs"])

    for pay in Payment.objects.all().iterator():
        when = pay.created_at
        on_date = when.date() if when else None
        if pay.currency == base:
            rate = Decimal("1")
        else:
            rate = _closest_rate(rates_by_currency, pay.currency, on_date) if on_date else None
            if rate is None:
                rate = Decimal("1")
                pay_fallbacks += 1
        pay.rate_to_kgs = rate
        pay.save(update_fields=["rate_to_kgs"])

    logger.warning(
        "Backfilled KGS snapshots: %s sale(s) and %s payment(s) used a 1.0 fallback "
        "(no exchange rate on record for their currency).",
        sale_fallbacks,
        pay_fallbacks,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0006_historicalpayment_rate_to_kgs_and_more"),
        ("core", "0004_exchangerate_source"),  # ExchangeRate must exist in the migration state
    ]

    operations = [migrations.RunPython(backfill, noop_reverse)]
