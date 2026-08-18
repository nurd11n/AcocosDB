"""cost_price stops sharing ProductVariant.currency and becomes always-KGS
(see the field's own docstring in apps/inventory/models.py). Existing rows
whose cost_price was entered in a foreign currency are converted ONCE here,
at TODAY's rate — the best available approximation, since a manually-typed
cost_price never recorded when it was actually incurred (no frozen rate to
recover). Logged loudly so the count is visible at deploy time, same
convention as 0017_backfill_saleitem_cost_price in apps.sales."""

import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)


def convert_foreign_cost_to_kgs(apps, schema_editor):
    from apps.core.currency import to_base
    from django.utils import timezone

    ProductVariant = apps.get_model("inventory", "ProductVariant")
    today = timezone.localdate()

    candidates = list(ProductVariant.objects.exclude(currency="KGS"))
    converted = 0
    skipped_no_rate = []
    for variant in candidates:
        new_cost = to_base(variant.cost_price, variant.currency, today)
        if new_cost is None:
            skipped_no_rate.append(variant.sku)
            continue
        variant.cost_price = new_cost
        variant.save(update_fields=["cost_price"])
        converted += 1

    logger.warning(
        "cost_price_always_kgs: found %s non-KGS-currency variant(s); "
        "converted %s to KGS at today's rate; %s skipped for lack of a "
        "recorded exchange rate (left AS-IS in their original currency's "
        "number — review these manually): %s",
        len(candidates),
        converted,
        len(skipped_no_rate),
        skipped_no_rate,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0008_alter_productimage_image"),
    ]

    operations = [
        migrations.AlterField(
            model_name="historicalproductvariant",
            name="cost_price",
            field=models.DecimalField(
                decimal_places=2, max_digits=12, verbose_name="cost price (сом)"
            ),
        ),
        migrations.AlterField(
            model_name="historicalproductvariant",
            name="currency",
            field=models.CharField(
                choices=[
                    ("KGS", "Kyrgyzstani som"),
                    ("USD", "US dollar"),
                    ("RUB", "Russian ruble"),
                ],
                default="KGS",
                help_text="Applies to the sale price. Cost price is always в сом — see its own field.",
                max_length=3,
                verbose_name="currency",
            ),
        ),
        migrations.AlterField(
            model_name="productvariant",
            name="cost_price",
            field=models.DecimalField(
                decimal_places=2, max_digits=12, verbose_name="cost price (сом)"
            ),
        ),
        migrations.AlterField(
            model_name="productvariant",
            name="currency",
            field=models.CharField(
                choices=[
                    ("KGS", "Kyrgyzstani som"),
                    ("USD", "US dollar"),
                    ("RUB", "Russian ruble"),
                ],
                default="KGS",
                help_text="Applies to the sale price. Cost price is always в сом — see its own field.",
                max_length=3,
                verbose_name="currency",
            ),
        ),
        migrations.RunPython(convert_foreign_cost_to_kgs, noop_reverse),
    ]
