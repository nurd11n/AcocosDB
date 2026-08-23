"""Read-only check (unless --fix): is any expense or contractor transaction on
record filed in a way that misstates what was actually spent?

Written after a real report of «ввёл сумму — показало намного больше»: 181 000
сом showing as 15 928 000 in the Расходы totals. Three independent ways a row
goes wrong, all invisible in a list that shows each row in its own currency
while every TOTAL above it is сом-converted:

1. A BASE-CURRENCY row with rate_to_kgs != 1. Arithmetically impossible —
   1 сом is 1 сом by definition — so this is unambiguous corruption, not a
   judgement call. Root cause: the /panel/ admin form used to expose
   rate_to_kgs as a required, hand-typed field, so a сом amount filed with
   the сом-per-dollar rate typed in (88) was stored as amount × 88. The admin
   now freezes the rate itself (_FrozenRateAdminMixin); rows filed BEFORE
   that are still on record and still wrong. This is the ONE case --fix will
   repair, because the correct value is not a guess: it is 1.

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
from django.db import transaction

from apps.core.currency import format_money, rate_info
from apps.manufacturing.models import ContractorTransaction, Expense

MODELS = (("Расход", Expense), ("Операция подрядчика", ContractorTransaction))


class Command(BaseCommand):
    help = "Report (or --fix) expenses whose stored rate misstates what was spent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help=(
                "Reset rate_to_kgs to 1 on BASE-CURRENCY rows only (case 1 above), "
                "where 1 is the only arithmetically possible value. Never touches a "
                "foreign-currency row."
            ),
        )

    def handle(self, *args, **options):
        base = settings.CURRENCY
        do_fix = options["fix"]
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
                if do_fix:
                    with transaction.atomic():
                        for e in corrupt:
                            e.rate_to_kgs = Decimal("1")
                            e.save(update_fields=["rate_to_kgs"])
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  FIXED: {len(corrupt)} {label} row(s) reset to rate 1."
                        )
                    )

            if unconverted:
                found_any = True
                self.stdout.write(
                    self.style.ERROR(
                        f"\n{label}: {len(unconverted)} foreign-currency row(s) filed at rate 1.0 "
                        f"— almost certainly UNDERSTATED (no rate was on record when filed). "
                        f"NOT auto-fixed — the right rate is whatever was true on that date:"
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

        if not do_fix:
            self.stdout.write(
                self.style.WARNING(
                    "\nNothing was changed. Re-run with --fix to reset ONLY the "
                    "base-currency rows above to rate 1 (the sole unambiguous case). "
                    "Foreign-currency rows must be corrected by hand in /panel/."
                )
            )
