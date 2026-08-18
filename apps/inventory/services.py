"""All stock math lives here. Admin, bots, and sales call these functions."""

from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.utils.translation import gettext as _

from .models import ProductImage, ProductVariant, StockMovement


def reserved_by_variant(variant_ids=None) -> dict[int, int]:
    """{variant_id: reserved qty} — units promised to open production orders
    (apps.orders: новый/в производстве/готов) and therefore not sellable to a
    walk-in, whether or not they've been produced yet. A produced-but-not-
    handed-over unit still belongs to that order: производство increases
    on_hand but must NOT increase what a walk-in can buy, so reserved is the
    order's FULL line quantity, never reduced by produced_qty. A cancelled or
    delivered (выдан) order releases its reservation automatically — it's
    simply outside this status filter. Local import: apps.orders depends on
    apps.inventory, not the other way round."""
    from apps.orders.models import Order, OrderItem

    qs = OrderItem.objects.filter(order__status__in=[Order.NEW, Order.IN_PRODUCTION, Order.READY])
    if variant_ids is not None:
        qs = qs.filter(variant_id__in=variant_ids)
    rows = qs.values("variant_id").annotate(r=Sum("quantity"))
    return {row["variant_id"]: row["r"] or 0 for row in rows}


def available_by_variant(variant_ids=None) -> dict[int, int]:
    """{variant_id: available} = on_hand − reserved (CLAUDE.md Part 1b/3c) —
    the cart cap, tile badges, product search, and the sale confirmation
    guard all use THIS, never raw on_hand, so reserved stock is never sold to
    a walk-in. Two grouped queries total, no N+1."""
    qs = StockMovement.objects.all()
    if variant_ids is not None:
        qs = qs.filter(variant_id__in=variant_ids)
    on_hand = {
        row["variant_id"]: row["s"] or 0
        for row in qs.values("variant_id").annotate(s=Sum("quantity"))
    }
    reserved = reserved_by_variant(variant_ids)
    ids = set(variant_ids) if variant_ids is not None else set(on_hand) | set(reserved)
    return {vid: on_hand.get(vid, 0) - reserved.get(vid, 0) for vid in ids}


def available_for(variant) -> int:
    """Available stock for ONE variant — see available_by_variant."""
    on_hand = variant.stock
    reserved = reserved_by_variant(variant_ids=[variant.pk]).get(variant.pk, 0)
    return on_hand - reserved


def add_movement(
    variant, movement_type, quantity, user=None, reason="", sale_order=None, cost=None
):
    """Create one ledger row. Quantity sign is normalized from the type.
    `cost` (сом, per-unit) is only meaningful on PRODUCTION_IN — see
    StockMovement.cost's own docstring — and is always None for every other
    caller (sale/writeoff/adjustment/purchase/return never set it)."""
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
        cost=cost,
    )
    movement.full_clean()
    movement.save()
    if movement_type in (StockMovement.PRODUCTION_IN, StockMovement.PURCHASE_IN):
        # Back-in-stock alerts (CLIENT_BOTS.md §3.6) — a real intake, not a
        # return or a count correction, is what actually means "she made more".
        # Deferred to AFTER the surrounding transaction commits: the notify path
        # does blocking Telegram/WhatsApp sends (with a per-recipient throttle),
        # and add_movement runs inside mark_produced's atomic row-locked block —
        # firing here would hold a DB write lock across slow network I/O and, if
        # the txn rolled back, would send "back in stock!" for stock that never
        # landed. on_commit runs immediately when there's no open transaction.
        from apps.inbox.services import notify_back_in_stock

        transaction.on_commit(lambda: notify_back_in_stock(variant))
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
        .select_related("product__category")
        .annotate(_stock=Coalesce(Sum("movements__quantity"), 0))
        .filter(_stock__lte=F("low_stock_threshold"))
        .order_by("product__name", "size", "color")
    )


def variants_with_stock():
    """Every active variant with stock annotated in SQL — used by the daily report."""
    return (
        ProductVariant.objects.filter(is_active=True)
        .select_related("product__category")
        .annotate(_stock=Sum("movements__quantity"))
        .order_by("product__name", "size", "color")
    )


def cover_image_names(product_ids) -> dict[int, str]:
    """{product_id: stored file name} for each product's COVER image — the
    same first-by-position choice Product.grid_image makes, but for callers
    that already have a bag of product ids from a `.values()`/aggregate query
    rather than real Product instances (dashboard.py's top-products and
    dead-stock panels group SaleItem/StockMovement, not Product, so there's
    no queryset to prefetch_related onto). One query total regardless of how
    many ids or how many images each product has — never one query per
    product, which a naive `Product.objects.get(pk).grid_image` per row
    would be."""
    covers: dict[int, str] = {}
    rows = (
        ProductImage.objects.filter(product_id__in=product_ids)
        .order_by("product_id", "position", "id")
        .values_list("product_id", "thumbnail", "image")
    )
    for product_id, thumbnail, image in rows:
        if product_id not in covers:  # first row per product, thanks to the ordering above
            covers[product_id] = thumbnail or image
    return covers
