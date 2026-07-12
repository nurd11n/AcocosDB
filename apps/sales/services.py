"""Sale confirmation is the only place stock leaves the system for a sale.

Rules enforced here:
- confirmation is atomic and locks the variants (no race between two sellers);
- stock can never go negative — the whole sale fails with a clear message;
- the order total is computed once at confirmation and stored.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.inventory.models import ProductVariant, StockMovement
from apps.inventory.services import add_movement

from .models import SaleOrder


@transaction.atomic
def confirm_sale(order: SaleOrder, user=None) -> SaleOrder:
    if order.status != SaleOrder.DRAFT:
        raise ValidationError(_("Only draft sales can be confirmed."))
    items = list(order.items.select_related("variant__product"))
    if not items:
        raise ValidationError(_("Cannot confirm a sale with no items."))

    variant_ids = [i.variant_id for i in items]
    # Evaluate the locking query NOW — used lazily as a subquery it would never
    # actually acquire the row locks, reopening the two-sellers race.
    list(ProductVariant.objects.select_for_update().filter(id__in=variant_ids))
    stock = {
        row["variant_id"]: row["s"] or 0
        for row in StockMovement.objects.filter(variant_id__in=variant_ids)
        .values("variant_id")
        .annotate(s=Sum("quantity"))
    }

    total = Decimal("0")
    for item in items:
        available = stock.get(item.variant_id, 0)
        if available < item.quantity:
            raise ValidationError(
                _("Insufficient stock for %(sku)s: available %(have)s, requested %(need)s.")
                % {"sku": item.variant.sku, "have": available, "need": item.quantity}
            )
        total += item.line_total

    for item in items:
        add_movement(
            variant=item.variant,
            movement_type=StockMovement.SALE_OUT,
            quantity=item.quantity,
            user=user,
            reason=f"Sale #{order.pk}",
            sale_order=order,
        )

    order.total = total
    order.status = SaleOrder.CONFIRMED
    order.confirmed_at = timezone.now()
    order.save(update_fields=["total", "status", "confirmed_at"])
    return order


@transaction.atomic
def cancel_sale(order: SaleOrder, user=None) -> SaleOrder:
    """Cancel a confirmed sale: return the items to stock with RETURN_IN movements."""
    if order.status != SaleOrder.CONFIRMED:
        raise ValidationError(_("Only confirmed sales can be cancelled."))
    for item in order.items.select_related("variant"):
        add_movement(
            variant=item.variant,
            movement_type=StockMovement.RETURN_IN,
            quantity=item.quantity,
            user=user,
            reason=f"Cancel sale #{order.pk}",
            sale_order=order,
        )
    order.status = SaleOrder.CANCELLED
    order.save(update_fields=["status"])
    return order


def today_summary() -> dict:
    today = timezone.localdate()
    qs = SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date=today)
    revenue = qs.aggregate(s=Sum("total"))["s"] or Decimal("0")
    items = qs.aggregate(s=Sum("items__quantity"))["s"] or 0
    return {"revenue": revenue, "orders": qs.count(), "items": items}
