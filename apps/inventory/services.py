"""All stock math lives here. Admin, bots, and sales call these functions."""

from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.utils.translation import gettext as _

from .models import ProductVariant, StockMovement


def add_movement(variant, movement_type, quantity, user=None, reason="", sale_order=None):
    """Create one ledger row. Quantity sign is normalized from the type."""
    qty = abs(int(quantity))
    if movement_type in StockMovement.OUT_TYPES:
        qty = -qty
    movement = StockMovement(
        variant=variant,
        movement_type=movement_type,
        quantity=qty,
        reason=reason,
        created_by=user,
        sale_order=sale_order,
    )
    movement.full_clean()
    movement.save()
    return movement


@transaction.atomic
def adjust_to_count(variant: ProductVariant, counted: int, user, reason: str):
    """Inventory count: write the difference as an ADJUSTMENT with a required reason."""
    if not reason:
        raise ValueError(_("An adjustment requires a reason."))
    locked = ProductVariant.objects.select_for_update().get(pk=variant.pk)
    diff = counted - locked.stock
    if diff == 0:
        return None
    movement = StockMovement(
        variant=locked,
        movement_type=StockMovement.ADJUSTMENT,
        quantity=diff,
        reason=reason,
        created_by=user,
    )
    movement.save()
    return movement


def stock_totals() -> dict:
    units = StockMovement.objects.aggregate(s=Sum("quantity"))["s"] or 0
    return {"units": units}


def low_stock_variants():
    """Variants at or below their threshold — used by the /restock bot command
    and the nightly digest. Stock is annotated and compared in SQL: one query
    regardless of catalog size (the old Python loop was one query per variant)."""
    return list(
        ProductVariant.objects.filter(is_active=True)
        .select_related("product")
        .annotate(_stock=Coalesce(Sum("movements__quantity"), 0))
        .filter(_stock__lte=F("low_stock_threshold"))
        .order_by("product__name", "size", "color")
    )


def variants_with_stock():
    """Every active variant with stock annotated in SQL — used by the daily report."""
    return (
        ProductVariant.objects.filter(is_active=True)
        .select_related("product")
        .annotate(_stock=Sum("movements__quantity"))
        .order_by("product__name", "size", "color")
    )
