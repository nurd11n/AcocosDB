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

# Display-only symbols for the rate strip / money labels. Stored amounts and
# ISO codes are unaffected — this only decides what glyph shows next to them.
CURRENCY_SYMBOLS = {KGS: "сом", USD: "$", RUB: "₽"}

CENTS = Decimal("0.01")


def _current_rates() -> dict:
    """{currency: current rate}, cached under rates:current. There is exactly
    one ExchangeRate row per currency now; refreshing overwrites it in place and
    clears this key via a signal — so a whole session of «≈ сом» conversions
    costs one query, not one per amount shown."""
    from django.core.cache import cache

    from .models import ExchangeRate

    key = "rates:current"
    cached = cache.get(key)
    if cached is not None:
        return cached
    rates = {r.currency: r.rate for r in ExchangeRate.objects.all()}
    cache.set(key, rates, 3600)
    return rates


def get_rate(currency: str, on_date: date_cls | None = None) -> Decimal | None:
    """Current rate for `currency`: 1 unit equals `rate` units of the base
    currency. `on_date` is accepted for call-site compatibility but ignored —
    rates are current-only now (one row per currency, refreshed on demand).
    Base currency is always 1; None when no rate is on record."""
    if currency == settings.CURRENCY:
        return Decimal("1")
    return _current_rates().get(currency)


def rate_info(currency: str) -> dict | None:
    """{'rate', 'date', 'source'} for the CURRENT row on record for `currency`
    — the one canonical lookup behind the Курс card's date/age display and the
    POS risk checks, so there is exactly one place that decides "what is the
    rate right now" (never two code paths disagreeing). None when no rate has
    ever been recorded. The base currency is always rate=1, dated today, and
    never counts as stale."""
    from django.utils import timezone

    from .models import ExchangeRate

    if currency == settings.CURRENCY:
        return {"rate": Decimal("1"), "date": timezone.localdate(), "source": ExchangeRate.NBKR}
    row = ExchangeRate.objects.filter(currency=currency).first()
    if row is None:
        return None
    return {"rate": row.rate, "date": row.date, "source": row.source}


def snapshot_rate_to_base(currency: str, on_date: date_cls | None = None) -> Decimal:
    """The rate to FREEZE onto a sale/payment at confirm time: how many base
    (KGS) units 1 unit of `currency` is worth right now. Unlike get_rate this
    never returns None — a sale must never fail for lack of a rate: the current
    rate, else 1.0 (logged loudly — the owner hasn't refreshed rates yet). Once
    frozen this value is NEVER recomputed, so historical figures stay put."""
    if currency == settings.CURRENCY:
        return Decimal("1")
    rate = get_rate(currency)
    if rate is not None:
        return rate
    logger.warning("No exchange rate for %s — snapshotting 1.0", currency)
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


def convert(
    amount: Decimal, from_currency: str, to_currency: str, on_date: date_cls
) -> Decimal | None:
    """An amount in `from_currency` -> its equivalent in `to_currency`, via the
    base currency (all rates are quoted vs KGS). Powers the POS "client paid in
    USD toward a сом order" deduction: staff verify the rate by eye. Returns
    None when either leg has no rate on record."""
    if amount is None:
        return None
    if from_currency == to_currency:
        return Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)
    in_base = to_base(amount, from_currency, on_date)
    if in_base is None:
        return None
    return from_base(in_base, to_currency, on_date)
