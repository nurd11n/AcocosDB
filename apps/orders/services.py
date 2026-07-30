"""Production-order business logic — the one path both the POS UI and
/panel/ admin go through for state transitions (CLAUDE.md: never reimplemented
in a view). An Order never deducts stock; only hand_over (via
apps.sales.services.confirm_sale) does that, exactly once.
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import convert, snapshot_rate_to_base
from apps.inventory.models import StockMovement
from apps.inventory.services import add_movement
from apps.sales.models import Payment, SaleItem, SaleOrder
from apps.sales.services import confirm_sale

from .models import Order, OrderItem


def create_order(
    client, items: list[dict], user=None, due_date=None, note="", currency=None
) -> Order:
    """items: [{"variant": ProductVariant, "quantity": int, "unit_price": Decimal,
    "currency": str (optional, defaults to the order's)}]. Deliberately takes
    NO stock cap — ordering unproduced goods is the entire point (CLAUDE.md
    Part 3g); the create-order UI shows current stock as information only."""
    if not items:
        raise ValidationError(_("Order must have at least one item."))
    order = Order.objects.create(
        client=client,
        currency=currency or settings.CURRENCY,
        due_date=due_date,
        note=note,
        created_by=user,
    )
    for line in items:
        OrderItem.objects.create(
            order=order,
            variant=line["variant"],
            quantity=line["quantity"],
            unit_price=line["unit_price"],
            currency=line.get("currency") or order.currency,
        )
    return order


def record_deposit(order: Order, amount: Decimal, user=None, method=Payment.CASH, currency=None):
    """A deposit (аванс) taken at order creation — reuses the SAME
    frozen-rate mechanism as a sale payment (snapshot_rate_to_base), never a
    second conversion path. Not tied to a SaleOrder yet; hand_over links it
    in later so it counts toward the eventual sale's balance automatically."""
    if amount is None or amount <= 0:
        return None
    deposit_currency = currency or order.currency
    rate = snapshot_rate_to_base(deposit_currency, timezone.localdate())
    return Payment.objects.create(
        client=order.client,
        production_order=order,
        amount=amount,
        currency=deposit_currency,
        rate_to_kgs=rate,
        method=method,
        created_by=user,
    )


def order_paid_amount(order: Order) -> Decimal:
    """Deposits taken so far, converted into the order's own currency for
    display — this is pre-sale, so there is no frozen order rate to convert
    against yet (that happens for real once hand_over links each deposit to
    the resulting SaleOrder, which uses THAT order's own frozen rate)."""
    today = timezone.localdate()
    total = Decimal("0")
    for amount, currency in order.deposits.values_list("amount", "currency"):
        converted = convert(amount, currency, order.currency, today)
        if converted is not None:
            total += converted
    return total


def production_queue() -> list[dict]:
    """«Что производить» — DERIVED, never stored (CLAUDE.md Part 3b). Rows,
    one per variant with any open demand (новый/в производстве), sorted by
    nearest due_date first:
      need = SUM(ordered qty across open orders) − on_hand stock

    Deliberately against raw on_hand, not the reservation-adjusted `available`
    — on_hand already reflects everything produced so far (via PRODUCTION_IN
    movements written by mark_produced), so subtracting `available` too would
    double-count the very reservation this queue exists to satisfy. Rows
    where need <= 0 are kept (not hidden — she still must not sell those to a
    walk-in) and flagged `covered` for de-emphasis."""
    open_items = list(
        OrderItem.objects.filter(order__status__in=[Order.NEW, Order.IN_PRODUCTION]).select_related(
            "order", "variant__product"
        )
    )
    if not open_items:
        return []

    variant_ids = {item.variant_id for item in open_items}
    on_hand = {
        row["variant_id"]: row["s"] or 0
        for row in StockMovement.objects.filter(variant_id__in=variant_ids)
        .values("variant_id")
        .annotate(s=Sum("quantity"))
    }

    by_variant: dict[int, dict] = {}
    for item in open_items:
        bucket = by_variant.setdefault(
            item.variant_id,
            {"variant": item.variant, "ordered": 0, "due_dates": [], "order_ids": set()},
        )
        bucket["ordered"] += item.quantity
        bucket["order_ids"].add(item.order_id)
        if item.order.due_date:
            bucket["due_dates"].append(item.order.due_date)

    rows = []
    for variant_id, bucket in by_variant.items():
        stock = on_hand.get(variant_id, 0)
        need = max(bucket["ordered"] - stock, 0)
        rows.append(
            {
                "variant": bucket["variant"],
                "ordered": bucket["ordered"],
                "in_stock": stock,
                "to_produce": need,
                "covered": need <= 0,
                "due_date": min(bucket["due_dates"]) if bucket["due_dates"] else None,
                "orders_count": len(bucket["order_ids"]),
            }
        )
    rows.sort(key=lambda r: (r["due_date"] is None, r["due_date"]))
    return rows


@transaction.atomic
def mark_produced(order_item: OrderItem, quantity: int, user=None) -> OrderItem:
    """«Произведено N шт»: writes a PRODUCTION_IN movement for the item's
    variant and increments produced_qty — atomic, through services.py, so it
    shows in StockMovement history like any other intake. When every line on
    the order is fully produced, the order auto-advances to готов."""
    if quantity <= 0:
        raise ValidationError(_("Quantity to produce must be positive."))
    locked = OrderItem.objects.select_for_update().get(pk=order_item.pk)
    remaining = locked.quantity - locked.produced_qty
    if quantity > remaining:
        raise ValidationError(
            _("Cannot produce %(qty)s — only %(remaining)s remain on this line.")
            % {"qty": quantity, "remaining": remaining}
        )
    add_movement(
        variant=locked.variant,
        movement_type=StockMovement.PRODUCTION_IN,
        quantity=quantity,
        user=user,
        reason=f"Order #{locked.order_id}",
    )
    locked.produced_qty = F("produced_qty") + quantity
    locked.save(update_fields=["produced_qty"])
    locked.refresh_from_db()

    order = Order.objects.select_for_update().get(pk=locked.order_id)
    if order.status in (Order.NEW, Order.IN_PRODUCTION) and order.fully_produced:
        order.status = Order.READY
        order.save(update_fields=["status"])
        # Fires exactly once, the moment the order actually becomes готов — this
        # single push kills «когда будет готово?» traffic (CLIENT_BOTS.md §3.5).
        # Deferred to after commit: mark_produced is atomic and row-locked, and
        # the push is a blocking network send — never hold the lock across it,
        # and never notify if the transaction ends up rolling back.
        from apps.inbox.services import notify_order_ready

        transaction.on_commit(lambda: notify_order_ready(order))
    elif order.status == Order.NEW:
        order.status = Order.IN_PRODUCTION
        order.save(update_fields=["status"])
    return locked


@transaction.atomic
def hand_over(order: Order, user=None) -> SaleOrder:
    """«Выдать заказ»: converts the production order into a confirmed
    SaleOrder via the EXISTING services.confirm_sale — the only place stock
    ever leaves the system, deducting it exactly once. The deposit (if any)
    carries over automatically (linked to the new SaleOrder so paid_amount
    picks it up); the remaining balance is taken through the normal payment
    panel afterward, change included. Status -> выдан; linked both ways."""
    locked = Order.objects.select_for_update().get(pk=order.pk)
    if locked.status not in (Order.NEW, Order.IN_PRODUCTION, Order.READY):
        raise ValidationError(_("Only an open order can be handed over."))
    items = list(locked.items.select_related("variant"))
    if not items:
        raise ValidationError(_("Order has no items."))

    # Release THIS order's own reservation BEFORE confirm_sale checks
    # availability, so its own promised stock doesn't block its own
    # conversion — still inside the same atomic transaction, so a failure
    # below (e.g. some OTHER order also claims this stock) rolls this back.
    locked.status = Order.DELIVERED
    locked.delivered_at = timezone.now()
    locked.save(update_fields=["status", "delivered_at"])

    sale = SaleOrder.objects.create(client=locked.client, currency=locked.currency, created_by=user)
    for item in items:
        SaleItem.objects.create(
            order=sale, variant=item.variant, quantity=item.quantity, unit_price=item.unit_price
        )
    confirm_sale(sale, user=user)

    locked.sale_order = sale
    locked.save(update_fields=["sale_order"])

    # Carry the deposit(s) over: link them to the new sale so
    # SaleOrder.paid_amount / balance include them immediately.
    locked.deposits.filter(order__isnull=True).update(order=sale)
    return sale


@transaction.atomic
def cancel_order(order: Order, user=None) -> Order:
    """Owner-only (enforced by the caller — apps.pos.views / admin), never
    Editor/Manager (CLAUDE.md Part 3h). Releases the reservation simply by
    moving out of OPEN_STATUSES; deposits are left on record, refundable by
    hand through the normal void_payment path if needed."""
    if order.status in (Order.DELIVERED, Order.CANCELLED):
        raise ValidationError(_("This order is already closed."))
    order.status = Order.CANCELLED
    order.save(update_fields=["status"])
    return order
