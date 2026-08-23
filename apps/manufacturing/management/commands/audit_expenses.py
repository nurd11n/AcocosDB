"""Read-only check: is any expense or contractor transaction on record filed
in a way that misstates what was actually spent?

Never writes anything — report only, same as audit_stale_totals.

Written after a real report of «ввёл сумму — показало намного больше»: 181 000
сом showing as 15 928 000 in the Расходы totals. Three independent ways a row
goes wrong, all invisible in a list that shows each row in its own currency
while every TOTAL above it is сом-converted:

1. A BASE-CURRENCY row with rate_to_kgs != 1. Arithmetically impossible —
   1 сом is 1 сом by definition — so this is unambiguous corruption, not a
   judgement call. Root cause: the /panel/ admin form used to expose
   rate_to_kgs as a required, hand-typed field, so a сом amount filed with
   the сом-per-dollar rate typed in (88) was stored as amount × 88. Now
   impossible three ways over: the admin freezes the rate itself
   (_FrozenRateAdminMixin), migration 0002 repaired the rows already filed,
   and a DB CheckConstraint (expense_base_currency_rate_is_one) rejects it
   even via raw SQL. Still CHECKED here: if this ever reports a row, the
   constraint is gone and something is very wrong.

2. A foreign-currency row with rate_to_kgs == 1 — the silent fallback
   snapshot_rate_to_base() takes when no ExchangeRate row exists. 10 $ filed
   as 10 сом, understated ~87x. The expenses view now refuses to save these;
   older rows are reported, never auto-changed (the right rate is whatever
   was true on that date, which this command cannot know).

3. A foreign-currency row that is simply unexpected — a сом amount typed
   while the currency select was left on $. Arithmetically fine, so these are
   LISTED for a human to confirm, never touched.
"""

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.core.currency import format_money, rate_info
from apps.manufacturing.models import ContractorTransaction, Expense

MODELS = (("Расход", Expense), ("Операция подрядчика", ContractorTransaction))


class Command(BaseCommand):
    help = "Report expenses whose stored rate misstates what was spent (read-only)."

    def handle(self, *args, **options):
        base = settings.CURRENCY
        found_any = False

        for label, model in MODELS:
            corrupt = list(
                model.objects.filter(currency=base)
                .exclude(rate_to_kgs=Decimal("1"))
                .order_by("date")
            )
            unconverted = [
                e
                for e in model.objects.exclude(currency=base).order_by("date")
                if e.rate_to_kgs == Decimal("1")
            ]
            converted = [
                e
                for e in model.objects.exclude(currency=base).order_by("date")
                if e.rate_to_kgs != Decimal("1")
            ]

            if corrupt:
                found_any = True
                self.stdout.write(
                    self.style.ERROR(
                        f"\n{label}: {len(corrupt)} row(s) in {base} with a rate that is not 1 "
                        f"— WRONG by definition, inflating every total:"
                    )
                )
                for e in corrupt:
                    self.stdout.write(
                        f"  #{e.pk} {e.date}: {format_money(e.amount, e.currency)} "
                        f"× {e.rate_to_kgs:.4f} = {format_money(e.amount_kgs, base)} "
                        f"→ should be {format_money(e.amount, base)}"
                    )

            if unconverted:
                found_any = True
                self.stdout.write(
                    self.style.ERROR(
                        f"\n{label}: {len(unconverted)} foreign-currency row(s) filed at rate 1.0 "
                        f"— almost certainly UNDERSTATED (no rate was on record when filed). "
                        f"The right rate is whatever was true on that date, which this "
                        f"command cannot know — correct these by hand in /panel/:"
                    )
                )
                for e in unconverted:
                    current = rate_info(e.currency)
                    would_be = format_money(e.amount * current["rate"], base) if current else "?"
                    self.stdout.write(
                        f"  #{e.pk} {e.date}: {format_money(e.amount, e.currency)} filed as "
                        f"{format_money(e.amount_kgs, base)} "
                        f"(at today's rate it would be {would_be})"
                    )

            if converted:
                self.stdout.write(
                    self.style.WARNING(
                        f"\n{label}: {len(converted)} foreign-currency row(s) filed WITH a rate. "
                        f"Arithmetically correct — confirm each was genuinely spent in that "
                        f"currency, and is not сом typed with the wrong one selected:"
                    )
                )
                for e in converted:
                    self.stdout.write(
                        f"  #{e.pk} {e.date}: {format_money(e.amount, e.currency)} "
                        f"× {e.rate_to_kgs:.4f} = {format_money(e.amount_kgs, base)}"
                    )

        if not found_any:
            self.stdout.write(
                self.style.SUCCESS("No mis-rated rows found — every total reflects what was spent.")
            )
            return

        self.stdout.write(
            self.style.WARNING(
                "\nNothing was changed — this command only reports. Correct a row in "
                "/panel/ (Расходы), after confirming with whoever filed it."
            )
        )
