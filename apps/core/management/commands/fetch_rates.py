"""Pull today's official FX rates from the National Bank of the Kyrgyz Republic
(https://www.nbkr.kg/XML/daily.xml) and upsert them as ExchangeRate rows so the
«≈ X сом» figures and report totals stay current without anyone typing rates by
hand.

Hard rules (see CLAUDE.md):
- A manual owner override always wins — a MANUAL row for today is never clobbered.
- On ANY failure (network down, bad XML) keep the last known rate: log loudly,
  change nothing, exit 0. A sale must never break because rates couldn't refresh.

The NBKR feed is windows-1251, uses a comma decimal separator, and quotes each
rate per <Nominal> units of the currency (Nominal is 1 for USD/RUB today, but we
divide by it anyway so a future 10/100-nominal currency stays correct).

Scheduled: the `scheduler` container runs this daily before the report.
Run manually: python manage.py fetch_rates
"""

import logging
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.currency import CURRENCY_CODES
from apps.core.models import ExchangeRate

logger = logging.getLogger(__name__)

NBKR_URL = "https://www.nbkr.kg/XML/daily.xml"


class Command(BaseCommand):
    help = "Fetch today's USD/RUB rates from NBKR and upsert ExchangeRate rows."

    def handle(self, *args, **options):
        try:
            resp = requests.get(NBKR_URL, timeout=20)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)  # respects the XML's own encoding
        except (requests.RequestException, ElementTree.ParseError) as exc:
            # Keep the last known rate — never crash the pipeline.
            logger.error("fetch_rates: could not fetch/parse NBKR feed: %s", exc)
            self.stdout.write(self.style.WARNING("NBKR unavailable — kept last known rates."))
            return

        today = timezone.localdate()
        wanted = {c for c in CURRENCY_CODES if c != "KGS"}  # base needs no rate
        updated, skipped = 0, 0

        for node in root.findall("Currency"):
            iso = node.get("ISOCode")
            if iso not in wanted:
                continue
            try:
                nominal = Decimal((node.findtext("Nominal") or "1").replace(",", "."))
                value = Decimal((node.findtext("Value") or "").replace(",", "."))
                rate = value / nominal
            except (InvalidOperation, ZeroDivisionError):
                logger.warning("fetch_rates: bad value for %s, skipping", iso)
                continue

            existing = ExchangeRate.objects.filter(currency=iso, date=today).first()
            if existing and existing.source == ExchangeRate.MANUAL:
                skipped += 1  # owner override wins — never overwrite a manual row
                continue
            ExchangeRate.objects.update_or_create(
                currency=iso,
                date=today,
                defaults={"rate": rate, "source": ExchangeRate.NBKR},
            )
            updated += 1

        msg = f"NBKR rates for {today}: {updated} updated"
        if skipped:
            msg += f", {skipped} kept (manual override)"
        self.stdout.write(self.style.SUCCESS(msg))
