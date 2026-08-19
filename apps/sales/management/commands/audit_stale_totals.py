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

M2 (2026-08-18 audit): this command already worked and independently caught
a real stale total, but nothing ever ran it — a safety net nobody checks
isn't one. Now wired into scheduler.py's daily REPORT_HOUR job, and alerts
via Telegram ONLY when it finds a discrepancy — same silent-unless-something-
is-wrong pattern as send_security_digest (same module docstring reasoning:
an alert that fires every day gets muted, right when it matters most), same
TELEGRAM_STAFF_TOKEN/DRILL_CHAT_ID recipient as every other ops alert in
this project (docker/restore_drill.sh, docker/backup.sh, send_security_digest).
"""

import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.currency import CENTS
from apps.sales.models import SaleOrder

logger = logging.getLogger(__name__)


def _send_telegram_text(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
        if not resp.ok:
            logger.error(
                "Stale totals audit: Telegram rejected the send (status %s): %s",
                resp.status_code,
                resp.text,
            )
        return resp.ok
    except requests.RequestException:
        logger.exception("Stale totals audit: failed to reach Telegram")
        return False


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

        # Two SEPARATE totals, not one combined figure: item_total_diff stays
        # in each order's own currency (summed loosely across orders that may
        # not share a currency — see the caveat printed below), while
        # rate_total_diff is always KGS (that's what total_kgs IS, by
        # definition — see the field's docstring) and so is safe to sum
        # straight across every order regardless of currency. Reporting only
        # item_total_diff (as this command used to) silently showed "+0"
        # impact for a sale with a rate-only mismatch, even though real сом
        # value was at stake.
        item_total_diff = sum(
            (items_total - order.total for order, items_total in item_mismatches), Decimal("0")
        )
        rate_total_diff = sum(
            (expected_kgs - order.total_kgs for order, expected_kgs in rate_mismatches),
            Decimal("0"),
        )
        if item_mismatches:
            self.stdout.write(
                self.style.WARNING(
                    f"Net effect on line-item totals if left as-is: {item_total_diff:+} "
                    "(сом-equivalent per order's own currency, not converted/summed across "
                    "currencies here)."
                )
            )
        if rate_mismatches:
            self.stdout.write(
                self.style.WARNING(
                    f"Net effect on total_kgs if left as-is: {rate_total_diff:+} KGS "
                    "(total_kgs is always KGS, so this IS safely summed across orders)."
                )
            )

        self._alert(item_mismatches, rate_mismatches, item_total_diff, rate_total_diff)

    def _alert(self, item_mismatches, rate_mismatches, item_total_diff, rate_total_diff):
        token = settings.TELEGRAM_STAFF_TOKEN
        chat_id = settings.DRILL_CHAT_ID
        if not (token and chat_id):
            self.stdout.write(
                self.style.WARNING(
                    "Discrepancy found but TELEGRAM_STAFF_TOKEN/DRILL_CHAT_ID is not "
                    "set — alert not sent."
                )
            )
            return

        today = timezone.localdate().strftime("%d.%m.%Y")
        lines = [f"⚠️ ACOCOS — расхождение в суммах продаж ({today})"]
        if item_mismatches:
            ids = ", ".join(f"#{o.pk}" for o, _ in item_mismatches)
            lines.append(
                f"Сумма продажи не совпадает с позициями: {len(item_mismatches)} шт. ({ids})"
            )
        if rate_mismatches:
            ids = ", ".join(f"#{o.pk}" for o, _ in rate_mismatches)
            lines.append(
                f"Сумма в сомах не совпадает с курсом на момент продажи: "
                f"{len(rate_mismatches)} шт. ({ids})"
            )
        if item_mismatches:
            lines.append(f"Влияние на позиции, если не исправить: {item_total_diff:+}")
        if rate_mismatches:
            lines.append(f"Влияние на сумму в сомах, если не исправить: {rate_total_diff:+} KGS")
        lines.append("Подробности: manage.py audit_stale_totals")
        text = "\n".join(lines)

        if _send_telegram_text(token, chat_id, text):
            self.stdout.write(self.style.SUCCESS("Alert sent."))
        else:
            self.stdout.write(self.style.ERROR("Alert FAILED to send — see log."))
