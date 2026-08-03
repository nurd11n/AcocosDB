"""Backfill SaleItem.cost_price for rows that predate the field.

There is no true historical cost for these — the field didn't exist, so
nothing was ever frozen. The best available approximation is each line's
CURRENT ProductVariant.cost_price, same as every profit figure implicitly
used before this migration. Going forward, confirm_sale() freezes the real
value at confirm time (see apps.sales.services), so only pre-existing rows
ever take this approximate path — logged so the count is visible.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def backfill(apps, schema_editor):
    SaleItem = apps.get_model("sales", "SaleItem")

    updated = 0
    for item in SaleItem.objects.filter(cost_price__isnull=True).select_related("variant").iterator():
        item.cost_price = item.variant.cost_price
        item.save(update_fields=["cost_price"])
        updated += 1

    logger.warning(
        "Backfilled cost_price (from CURRENT variant.cost_price, an approximation "
        "for pre-existing rows) on %s sale item(s).",
        updated,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0016_saleitem_cost_price_and_more"),
    ]

    operations = [migrations.RunPython(backfill, noop_reverse)]
