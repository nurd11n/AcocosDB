from decimal import ROUND_HALF_UP, Decimal

from django import template
from django.utils.translation import gettext_lazy as _

from apps.clients.services import client_credits, client_debt
from apps.sales.models import SaleOrder

register = template.Library()

NBSP = " "

# payment_status_for() returns "unpaid"/"partial"/"paid" — the CSS classes
# and money-bar tokens use "debt" for the unpaid case (see POS-DESIGN.md's
# --debt/--partial/--paid palette), and the label is the Russian status word.
_STATUS_CSS = {SaleOrder.UNPAID: "debt", SaleOrder.PARTIAL: "partial", SaleOrder.PAID: "paid"}
_STATUS_LABEL = {
    SaleOrder.UNPAID: _("долг"),
    SaleOrder.PARTIAL: _("частично"),
    SaleOrder.PAID: _("оплачено"),
}


@register.filter(name="client_debts")
def client_debts_filter(client):
    """{{ order.client|client_debts }} -> {currency: amount}. Used by the
    client chip on the sale screen — the debt must be visible right where a
    manager decides whether to sell more on credit (see POS-DESIGN.md)."""
    return client_debt(client) if client else {}


@register.filter(name="client_credits")
def client_credits_filter(client):
    """{{ order.client|client_credits }} -> {currency: amount} — an advance
    balance («Аванс»), never mixed into the debt figure."""
    return client_credits(client) if client else {}


@register.filter(name="payment_conversion")
def payment_conversion_filter(payment):
    """'16 USD × 87.45 = 1399.20 KGS · НБКР 24.07.2026' for a converted foreign
    payment — the frozen rate must always be visible, not just stored (see
    CLAUDE.md). Empty for a payment already in the base currency (rate is
    trivially 1, nothing to show). `created_at` doubles as "as of" date for
    the rate: Payment.rate_to_kgs is frozen exactly once, at creation."""
    from django.conf import settings

    if payment.currency == settings.CURRENCY:
        return ""
    if payment.rate_source == payment.RATE_MANUAL:
        source = _("вручную")
    else:
        # Honest label either way: НБКР when it genuinely is (the pinned
        # Frankfurter provider today), whatever else it's stored as if not.
        source = payment.get_rate_source_display()
    return (
        f"{payment.amount} {payment.currency} × {payment.rate_to_kgs:.2f} = "
        f"{payment.amount_kgs} {settings.CURRENCY} · {source} {payment.created_at:%d.%m.%Y}"
    )


@register.filter(name="payment_change_display")
def payment_change_display_filter(payment):
    """'20 $ (1 749 сом) − сдача 874 сом = 874,50 сом зачтено' — gross, change,
    and net together, everywhere a payment with change appears (CLAUDE.md).
    The disposition label alone for «В счёт долга»/«Аванс» (no change given).
    Empty for a plain payment (excess_disposition == 'none')."""
    from django.conf import settings

    if payment.excess_disposition == payment.DISPOSITION_CHANGE and payment.change_amount:
        return (
            f"{payment.amount} {payment.currency} ({payment.amount_kgs} {settings.CURRENCY}) "
            f"− {_('сдача')} {payment.change_amount} {payment.change_currency} "
            f"= {payment.net_applied_kgs} {settings.CURRENCY} {_('зачтено')}"
        )
    if payment.excess_disposition in (payment.DISPOSITION_DEBT, payment.DISPOSITION_CREDIT):
        return payment.get_excess_disposition_display()
    return ""


@register.filter(name="money")
def money_filter(value, currency):
    """'3 800 сом' / '874,50 сом' — NBSP-grouped thousands, the currency
    SYMBOL (сом/₽/$, never the KGS/RUB/USD code), cents dropped when exactly
    zero (POS-DESIGN.md's money rule). Pair with the `.num` class at the call
    site for tabular-nums — this filter only formats digits."""
    if value is None or value == "":
        return ""
    from apps.core.currency import CURRENCY_SYMBOLS

    try:
        amount = Decimal(value)
    except (TypeError, ValueError):
        return value
    symbol = CURRENCY_SYMBOLS.get(currency, currency)
    sign = "-" if amount < 0 else ""
    amount = abs(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, cents = divmod(amount, 1)
    whole_str = f"{int(whole):,}".replace(",", NBSP)
    cents = int((cents * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if cents:
        return f"{sign}{whole_str},{cents:02d}{NBSP}{symbol}"
    return f"{sign}{whole_str}{NBSP}{symbol}"


@register.filter(name="currency_symbol")
def currency_symbol_filter(currency):
    """{{ "USD"|currency_symbol }} -> "$" — for a currency <select>'s option
    TEXT (the option `value` keeps the raw code the form/backend needs). A
    bare ISO code ("USD"/"KGS") reading as stray English on screen is the
    same money-formatting rule the `money` filter already enforces."""
    from apps.core.currency import CURRENCY_SYMBOLS

    return CURRENCY_SYMBOLS.get(currency, currency)


@register.filter(name="status_css")
def status_css_filter(status):
    return _STATUS_CSS.get(status, "")


@register.filter(name="status_label")
def status_label_filter(status):
    return _STATUS_LABEL.get(status, "")
