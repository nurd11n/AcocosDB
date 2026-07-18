"""Currency choices + display-only conversion helpers.

Money is entered and stored in its OWN currency on each record (ProductVariant
prices, SaleOrder total, Payment amount) — never auto-converted on save. The
manual, dated ExchangeRate rows are used ONLY to compute a converted "≈ X"
figure for display and for report totals. Stored amounts never change.
"""

import logging
from datetime import date as date_cls
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

KGS = "KGS"
USD = "USD"
RUB = "RUB"
CURRENCY_CHOICES = [
    (KGS, _("Kyrgyzstani som")),
    (USD, _("US dollar")),
    (RUB, _("Russian ruble")),
]
CURRENCY_CODES = [code for code, _label in CURRENCY_CHOICES]

CENTS = Decimal("0.01")


def _dated_rates(on_date: date_cls) -> dict:
    """{currency: effective rate} for one date, cached under rates:<date>.
    Rates change at most once a day (the fetch) plus rare owner overrides, both
    of which clear this key via a signal — so a whole day of «≈ сом» display
    conversions costs one query, not one per amount shown."""
    from django.core.cache import cache

    from .models import ExchangeRate

    key = f"rates:{on_date.isoformat()}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    rates = {}
    for code in CURRENCY_CODES:
        if code == settings.CURRENCY:
            continue
        row = (
            ExchangeRate.objects.filter(currency=code, date__lte=on_date).order_by("-date").first()
        )
        if row:
            rates[code] = row.rate
    cache.set(key, rates, 3600)
    return rates


def get_rate(currency: str, on_date: date_cls) -> Decimal | None:
    """Rate for `currency` effective on `on_date`: 1 unit of `currency` equals
    `rate` units of the base currency (settings.CURRENCY), using the most
    recent ExchangeRate row on or before that date. Base currency is always 1."""
    if currency == settings.CURRENCY:
        return Decimal("1")
    return _dated_rates(on_date).get(currency)


def snapshot_rate_to_base(currency: str, on_date: date_cls) -> Decimal:
    """The rate to FREEZE onto a sale/payment at confirm time: how many base
    (KGS) units 1 unit of `currency` is worth right now. Unlike get_rate this
    never returns None — a sale must never fail for lack of a rate:
      base currency -> 1;
      else the latest dated rate on/before on_date;
      else the most recent rate ever recorded for the currency;
      else 1.0 (logged loudly — the owner hasn't configured a rate yet).
    Once frozen, this value is NEVER recomputed, so historical figures don't
    move when today's rate changes (that's the whole point of the snapshot)."""
    if currency == settings.CURRENCY:
        return Decimal("1")
    rate = get_rate(currency, on_date)
    if rate is not None:
        return rate
    from .models import ExchangeRate

    row = ExchangeRate.objects.filter(currency=currency).order_by("-date").first()
    if row:
        return row.rate
    logger.warning("No exchange rate for %s on %s — snapshotting 1.0", currency, on_date)
    return Decimal("1")


def to_base(amount: Decimal, currency: str, on_date: date_cls) -> Decimal | None:
    """An amount stored IN `currency` -> its equivalent in the base currency
    (e.g. a report's converted-to-KGS column). None when no rate is available."""
    if amount is None:
        return None
    if currency == settings.CURRENCY:
        return Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)
    rate = get_rate(currency, on_date)
    if not rate:
        return None
    return (Decimal(amount) * rate).quantize(CENTS, rounding=ROUND_HALF_UP)


def from_base(amount: Decimal, currency: str, on_date: date_cls) -> Decimal | None:
    """A base-currency amount -> its equivalent in `currency` (e.g. the
    dashboard's "view money in X" calculator). None when no rate is available."""
    if amount is None:
        return None
    if currency == settings.CURRENCY:
        return Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)
    rate = get_rate(currency, on_date)
    if not rate:
        return None
    return (Decimal(amount) / rate).quantize(CENTS, rounding=ROUND_HALF_UP)
