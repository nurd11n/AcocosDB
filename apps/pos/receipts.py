"""Receipt formatting and its signed, expiring public link.

CLAUDE.md's receipt format: grouped by product+size with colours nested
beneath (never a flat per-line list), delivered as a link to a read-only web
page rather than the itemised text pasted straight into WhatsApp. The token
IS the access control — signed + timestamped (django.core.signing), never a
sequential ID — so guessing or enumerating one is infeasible, and it stops
working on its own after RECEIPT_MAX_AGE with no separate revocation step.
"""

from collections import OrderedDict
from decimal import Decimal

from django.core import signing
from django.utils import timezone

from apps.sales.models import SaleOrder

RECEIPT_SALT = "pos.receipt"
RECEIPT_MAX_AGE = 7 * 24 * 60 * 60  # 7 days, per CLAUDE.md


def make_receipt_token(order: SaleOrder) -> str:
    return signing.dumps({"sale_id": order.pk}, salt=RECEIPT_SALT)


def resolve_receipt_token(token: str) -> SaleOrder | None:
    """None covers every failure mode alike — tampered signature, expired,
    or a sale that's gone/not confirmed — so the view can turn it into one
    flat 404 that never hints at which case it was."""
    try:
        data = signing.loads(token, salt=RECEIPT_SALT, max_age=RECEIPT_MAX_AGE)
    except signing.BadSignature:
        return None
    sale_id = data.get("sale_id")
    if not isinstance(sale_id, int):
        return None
    return (
        SaleOrder.objects.filter(pk=sale_id, status=SaleOrder.CONFIRMED)
        .select_related("client")
        .first()
    )


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


def receipt_context(order: SaleOrder, token: str = "", for_pdf: bool = False) -> dict:
    """Everything templates/receipts/receipt.html needs — the SAME template
    and context for both the web page and the PDF (apps.pos.public_views), so
    the two can never drift apart. Скидка/Оплачено/Остаток are OMITTED
    entirely (None) rather than shown as zero whenever they don't apply —
    many real receipts are goods-only, with NO payment recorded at all, in
    which case neither line renders (not even «Остаток», even though it
    would mathematically equal the total) — payment lines appear only once a
    real payment transaction exists to report on."""
    groups = receipt_groups(order)
    total_discount = sum((i.discount_amount for i in order.items.all()), Decimal("0"))
    paid = order.paid_amount if order.client_id else Decimal("0")
    balance = order.balance if order.client_id and paid > 0 else Decimal("0")
    return {
        "order": order,
        "token": token,
        "for_pdf": for_pdf,
        "date": timezone.localtime(order.confirmed_at).date() if order.confirmed_at else None,
        # Client-facing text uses first_name ONLY, never the staff-only
        # descriptor client.name parenthesises (see Client.name docstring).
        "client_name": order.client.first_name if order.client_id else "",
        "groups": groups,
        "total_quantity": sum(g["quantity"] for g in groups),
        "total_money": order.total,
        "currency": order.currency,
        "discount": total_discount if total_discount > 0 else None,
        "paid": paid if paid > 0 else None,
        "balance": balance if balance > 0 else None,
    }
