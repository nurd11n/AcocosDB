"""Read-only integrity check: does every CONFIRMED sale's stored total match
what its current line items actually add up to?

Two things can drift apart, independently:
- order.total vs SUM(item.line_total) — line items edited (or somehow
  inserted/removed) after confirm_sale without going back through
  services.py (the exact bug SaleItemInline's confirmed-sale lock now
  prevents going forward — this audits for any sale that went stale BEFORE
  that lock existed);
- order.total_kgs vs order.total * order.rate_to_kgs — would only drift from
  a direct field edit bypassing confirm_sale/return_items entirely (both
  total and total_kgs are admin-readonly now, so this should never fire
  going forward either).

Never writes anything — report only, so a real mismatch can be reviewed
before deciding how (or whether) to correct it.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.core.currency import CENTS
from apps.sales.models import SaleOrder


class Command(BaseCommand):
    help = "Report CONFIRMED sales whose total/total_kgs no longer match their line items."

    def handle(self, *args, **options):
        orders = SaleOrder.objects.filter(status=SaleOrder.CONFIRMED).prefetch_related("items")
        item_mismatches = []
        rate_mismatches = []

        for order in orders:
            items_total = sum((i.line_total for i in order.items.all()), Decimal("0"))
            if items_total != order.total:
                item_mismatches.append((order, items_total))

            expected_kgs = (order.total * order.rate_to_kgs).quantize(CENTS)
            if expected_kgs != order.total_kgs:
                rate_mismatches.append((order, expected_kgs))

        if not item_mismatches and not rate_mismatches:
            self.stdout.write(
                self.style.SUCCESS("No stale totals found — every confirmed sale matches.")
            )
            return

        if item_mismatches:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(item_mismatches)} confirmed sale(s) where order.total "
                    "no longer matches SUM(line items):"
                )
            )
            for order, items_total in item_mismatches:
                diff = items_total - order.total
                self.stdout.write(
                    f"  #{order.pk}: stored total={order.total} {order.currency}, "
                    f"items currently add up to={items_total} {order.currency} "
                    f"(diff {diff:+})"
                )

        if rate_mismatches:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(rate_mismatches)} confirmed sale(s) where total_kgs "
                    "no longer matches total * rate_to_kgs:"
                )
            )
            for order, expected_kgs in rate_mismatches:
                diff = expected_kgs - order.total_kgs
                self.stdout.write(
                    f"  #{order.pk}: stored total_kgs={order.total_kgs}, "
                    f"expected={expected_kgs} (diff {diff:+})"
                )

        total_diff = sum(
            (items_total - order.total for order, items_total in item_mismatches), Decimal("0")
        )
        self.stdout.write(
            self.style.WARNING(
                f"Net effect on reported revenue if left as-is: {total_diff:+} (сом-equivalent "
                "per order's own currency, not converted/summed across currencies here)."
            )
        )
