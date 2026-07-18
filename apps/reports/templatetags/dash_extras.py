"""Dashboard number formatting.

We deliberately do NOT set the global USE_THOUSAND_SEPARATOR: it would also
localize numbers rendered into POS form value="" attributes (e.g. a payment
amount becoming "2 500,00"), which the server-side Decimal parser would then
reject — a silent money bug. So grouping is applied explicitly, only where it's
displayed, via these filters. Non-breaking spaces keep amounts from wrapping.
"""

from django import template

register = template.Library()

NBSP = " "


def _grouped(n: int) -> str:
    return f"{n:,}".replace(",", NBSP)


@register.filter
def som(value):
    """Whole сом with non-breaking-space grouping: 1 284 000 сом."""
    try:
        return f"{_grouped(round(float(value)))}{NBSP}сом"
    except (TypeError, ValueError):
        return value


@register.filter
def grp(value):
    """Grouped integer, no currency (units, capital counts)."""
    try:
        return _grouped(round(float(value)))
    except (TypeError, ValueError):
        return value


@register.filter
def raw_int(value):
    """Bare integer for JS count-up data attributes."""
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return "0"
