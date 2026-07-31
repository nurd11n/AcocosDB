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
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.currency import CURRENCY_CODES
from apps.core.models import ExchangeRate, RateChangeLog

logger = logging.getLogger(__name__)

NBKR_URL = "https://www.nbkr.kg/XML/daily.xml"


def _nbkr_proxies():
    """Route ONLY the NBKR request through settings.NBKR_PROXY when it's set
    (nbkr.kg blocks foreign/cloud IPs — see the proxy note in settings). Empty
    setting → None → a direct request, unchanged behaviour."""
    proxy = settings.NBKR_PROXY
    return {"http": proxy, "https": proxy} if proxy else None


def fetch_nbkr_rates(changed_by=None) -> tuple[int, int]:
    """Pull today's USD/RUB rates from NBKR and upsert ExchangeRate rows,
    returning (updated, skipped). A MANUAL owner override for today is never
    clobbered. Raises requests.RequestException / ElementTree.ParseError on a
    fetch/parse failure — callers (the daily command, the POS refresh button)
    decide how to surface it. On success the ExchangeRate save-signal clears
    the dated rate cache, so «≈ сом» conversions pick the new rate up at once.

    Every rate that actually CHANGES writes a RateChangeLog row (source=nbkr,
    changed_by=changed_by — None for the scheduled/system run, the acting
    user for the POS refresh button). A refresh that pulls back the same
    number is not a change and isn't logged, keeping the audit trail useful."""
    resp = requests.get(NBKR_URL, timeout=20, proxies=_nbkr_proxies())
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)  # respects the XML's own encoding

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

        old_rate = ExchangeRate.objects.filter(currency=iso).values_list("rate", flat=True).first()

        # One row per currency — overwrite it in place, stamping today as the
        # last-updated date. No dated pile-up.
        ExchangeRate.objects.update_or_create(
            currency=iso,
            defaults={"rate": rate, "date": today, "source": ExchangeRate.NBKR},
        )
        updated += 1

        if old_rate is None or old_rate != rate:
            RateChangeLog.objects.create(
                currency=iso,
                old_rate=old_rate,
                new_rate=rate,
                source=ExchangeRate.NBKR,
                changed_by=changed_by,
            )

    return updated, skipped


class Command(BaseCommand):
    help = "Fetch today's USD/RUB rates from NBKR and upsert ExchangeRate rows."

    def handle(self, *args, **options):
        try:
            updated, skipped = fetch_nbkr_rates()
        except (requests.RequestException, ElementTree.ParseError) as exc:
            # Keep the last known rate — never crash the pipeline.
            logger.error("fetch_rates: could not fetch/parse NBKR feed: %s", exc)
            self.stdout.write(self.style.WARNING("NBKR unavailable — kept last known rates."))
            return

        msg = f"NBKR rates for {timezone.localdate()}: {updated} updated"
        if skipped:
            msg += f", {skipped} kept (manual override)"
        self.stdout.write(self.style.SUCCESS(msg))
