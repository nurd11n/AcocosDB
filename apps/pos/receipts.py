"""Receipt formatting — PDF-only, generated fresh from the database on every
request. There is no web page, no signed token, no stored file: a manager
downloads the PDF and attaches it to WhatsApp herself (wa.me links cannot
attach files). Rendering this must NEVER write to the database — it is
read-only end to end, which is exactly what made the old token-link page a
non-issue for the ghost-payment class of bug, and what tests below assert
with a row-count check.

CLAUDE.md's receipt format: grouped by product+size with colours nested
beneath (never a flat per-line list).
"""

from collections import OrderedDict
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.sales.models import SaleOrder


def receipt_groups(order: SaleOrder) -> list[dict]:
    """Grouped by (product, size); one row per colour nested beneath, e.g.:

        МИМИ 2-КА   50-56   ЧЁРНЫЙ      16   1 550   24 800
                            КОРИЧНЕВЫЙ  16   1 550   24 800

    Two cart lines of the SAME variant (never merged at add-time) combine
    into one colour row here — quantity and money summed, the displayed unit
    price re-derived from that combined total, rather than showing two rows
    for what the client experiences as one line of the same item."""
    items = list(
        order.items.select_related("variant__product").order_by(
            "variant__product__name", "variant__size", "variant__color"
        )
    )
    by_variant: OrderedDict[int, dict] = OrderedDict()
    for item in items:
        bucket = by_variant.setdefault(
            item.variant_id,
            {"variant": item.variant, "quantity": 0, "line_total": Decimal("0")},
        )
        bucket["quantity"] += item.quantity
        bucket["line_total"] += item.line_total

    groups: OrderedDict[tuple, dict] = OrderedDict()
    for bucket in by_variant.values():
        variant = bucket["variant"]
        key = (variant.product_id, variant.size)
        group = groups.setdefault(
            key,
            {
                "product": variant.product,
                "size": variant.size,
                "rows": [],
                "quantity": 0,
                "line_total": Decimal("0"),
            },
        )
        unit_price = (
            (bucket["line_total"] / bucket["quantity"]).quantize(Decimal("0.01"))
            if bucket["quantity"]
            else Decimal("0")
        )
        group["rows"].append(
            {
                "color": variant.color,
                "quantity": bucket["quantity"],
                "unit_price": unit_price,
                "line_total": bucket["line_total"],
            }
        )
        group["quantity"] += bucket["quantity"]
        group["line_total"] += bucket["line_total"]

    return list(groups.values())


def receipt_context(order: SaleOrder) -> dict:
    """Everything templates/receipts/receipt.html needs to render the PDF.
    Скидка is OMITTED entirely (None) rather than shown as zero whenever it
    doesn't apply. Оплачено/Остаток only render when the sale ISN'T fully
    paid — a paid receipt shows just Итого, per the status badge already
    saying everything else needed. `status` drives that badge: 'paid' /
    'partial' / 'unpaid', the SAME classifier the rest of the app uses
    (SaleOrder.payment_status) — never a second definition of "paid" that
    could disagree with the POS screens showing this same sale."""
    groups = receipt_groups(order)
    total_discount = sum((i.discount_amount for i in order.items.all()), Decimal("0"))
    return {
        "order": order,
        "date": timezone.localtime(order.confirmed_at).date() if order.confirmed_at else None,
        # Client-facing text uses first_name ONLY, never the staff-only
        # descriptor client.name parenthesises (see Client.name docstring),
        # and never the phone number.
        "client_name": order.client.first_name if order.client_id else "",
        "groups": groups,
        "total_quantity": sum(g["quantity"] for g in groups),
        "total_money": order.total,
        "currency": order.currency,
        "discount": total_discount if total_discount > 0 else None,
        "status": order.payment_status,
        "paid": order.paid_amount,
        "balance": order.balance,
        # One contact line, never a URL — blank by default (see settings).
        "contact_line": settings.RECEIPT_CONTACT_LINE,
    }
