"""Read-only check: is any expense on record filed in a way that misstates
what was actually spent?

Written after a real report of «ввёл сумму — показало намного больше». Two
independent ways that happens, both invisible in a list that shows each row
in its own currency while every TOTAL above it is сом-converted:

- a foreign-currency row filed with rate_to_kgs = 1 — the silent fallback
  snapshot_rate_to_base() used to take when no ExchangeRate row existed.
  10 $ filed as 10 сом: understated ~87x. The expenses view now refuses to
  save these (same rule record_payment already enforced), but rows filed
  BEFORE that fix are still on record and still wrong.
- a foreign-currency row that is simply unexpected — a сом amount typed
  while the currency select was left on $ (browser form-restore), which
  multiplies it into every total. Not detectable as an error by itself, so
  these are LISTED for a human to confirm, never auto-changed.

Never writes anything — report only. Correcting a row is a money decision,
made by the Owner in the panel, not by a script guessing intent.
"""

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.core.currency import format_money, rate_info
from apps.manufacturing.models import Expense


class Command(BaseCommand):
    help = "Report expenses whose stored currency/rate may misstate what was spent."

    def handle(self, *args, **options):
        base = settings.CURRENCY
        foreign = Expense.objects.exclude(currency=base).order_by("date")

        if not foreign.exists():
            self.stdout.write(
                self.style.SUCCESS(f"Every expense on record is in {base} — nothing to review.")
            )
            return

        unconverted = [e for e in foreign if e.rate_to_kgs == Decimal("1")]
        converted = [e for e in foreign if e.rate_to_kgs != Decimal("1")]

        if unconverted:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{len(unconverted)} foreign-currency expense(s) filed at rate 1.0 "
                    f"— almost certainly UNDERSTATED (no rate was on record when filed):"
                )
            )
            for e in unconverted:
                current = rate_info(e.currency)
                would_be = format_money(e.amount * current["rate"], base) if current else "?"
                self.stdout.write(
                    f"  #{e.pk} {e.date} {e.get_category_display()}: "
                    f"{format_money(e.amount, e.currency)} filed as "
                    f"{format_money(e.amount_kgs, base)} "
                    f"(at today's rate it would be {would_be})"
                )

        if converted:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(converted)} foreign-currency expense(s) filed WITH a rate. "
                    f"These are arithmetically correct — confirm each was genuinely "
                    f"spent in that currency and not сом typed with the wrong one selected:"
                )
            )
            for e in converted:
                self.stdout.write(
                    f"  #{e.pk} {e.date} {e.get_category_display()}: "
                    f"{format_money(e.amount, e.currency)} × {e.rate_to_kgs:.4f} = "
                    f"{format_money(e.amount_kgs, base)}"
                )

        self.stdout.write(
            self.style.WARNING(
                "\nNothing was changed. Correct a row in /panel/ (Расходы) — or delete "
                "and re-enter it — only after confirming with whoever filed it."
            )
        )
