"""Production-order business logic — the one path both the POS UI and
/panel/ admin go through for state transitions (CLAUDE.md: never reimplemented
in a view). An Order never deducts stock; only hand_over (via
apps.sales.services.confirm_sale) does that, exactly once.
"""

from datetime import date
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
    """items: [{"variant": ProductVariant, "quantity": int, "unit_price": Decimal}].
    Every line shares the ORDER's own currency (see OrderItem's own docstring
    for why it has no currency field of its own). Deliberately takes NO stock
    cap — ordering unproduced goods is the entire point (CLAUDE.md Part 3g);
    the create-order UI shows current stock as information only."""
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
    # Iterates .all() rather than .values_list() on purpose: values_list always
    # issues its own query, which would defeat the prefetch_related("deposits")
    # the order list relies on to stay flat instead of N+1 across 200 rows.
    for deposit in order.deposits.all():
        converted = convert(deposit.amount, deposit.currency, order.currency, today)
        if converted is not None:
            total += converted
    return total


GROUP_VARIANT = "variant"


def _due_sort_key(due):
    """Nearest deadline first, undated rows last — never compares None against
    a date (which would raise), unlike a bare tuple of the two."""
    return (due is None, due or date.max)


def production_queue() -> list[dict]:
    """«Что производить» — DERIVED, never stored (CLAUDE.md Part 3b). One row
    per VARIANT, sorted by nearest due_date first:
      need = SUM(ordered qty across open orders) − on_hand stock

    Deliberately against raw on_hand, not the reservation-adjusted `available`
    — on_hand already reflects everything produced so far (via PRODUCTION_IN
    movements written by mark_produced), so subtracting `available` too would
    double-count the very reservation this queue exists to satisfy. Rows
    where need <= 0 are kept (not hidden — she still must not sell those to a
    walk-in) and flagged `covered` for de-emphasis.

    Every row also carries a `lines` breakdown — which client ordered how
    many, due when, how much of their line is already made — so «кому это
    шить» is answerable without opening each order one by one. Used to also
    support product/client rollups (three toggles on /orders/queue/) —
    removed 2026-08 as unnecessary complexity for a single-grouping queue.
    If a rollup is ever genuinely needed again, re-derive it FROM these same
    rows (never a second source of the underlying ordered/produced/stock
    arithmetic)."""
    return _variant_rows()


def _variant_rows() -> list[dict]:
    """The canonical queue rows — one per variant with open demand. Two
    queries total regardless of order count (lines + one stock aggregate)."""
    open_items = list(
        OrderItem.objects.filter(order__status__in=[Order.NEW, Order.IN_PRODUCTION]).select_related(
            "order__client", "variant__product__category"
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
    today = timezone.localdate()

    by_variant: dict[int, dict] = {}
    for item in open_items:
        bucket = by_variant.setdefault(
            item.variant_id,
            {
                "variant": item.variant,
                "ordered": 0,
                "produced": 0,
                "due_dates": [],
                "order_ids": set(),
                "lines": [],
            },
        )
        bucket["ordered"] += item.quantity
        bucket["produced"] += min(item.produced_qty, item.quantity)
        bucket["order_ids"].add(item.order_id)
        if item.order.due_date:
            bucket["due_dates"].append(item.order.due_date)
        bucket["lines"].append(_line_row(item, today))

    rows = []
    for variant_id, bucket in by_variant.items():
        stock = on_hand.get(variant_id, 0)
        need = max(bucket["ordered"] - stock, 0)
        due = min(bucket["due_dates"]) if bucket["due_dates"] else None
        bucket["lines"].sort(key=lambda line: _due_sort_key(line["due_date"]))
        rows.append(
            {
                "kind": GROUP_VARIANT,
                "key": f"v{variant_id}",
                "variant": bucket["variant"],
                "product": bucket["variant"].product,
                "label": str(bucket["variant"]),
                "size_label": _size_label(bucket["variant"]),
                "ordered": bucket["ordered"],
                "produced": bucket["produced"],
                "remaining": sum(line["remaining"] for line in bucket["lines"]),
                "in_stock": stock,
                "to_produce": need,
                "covered": need <= 0,
                "due_date": due,
                "overdue": bool(due and due < today),
                "orders_count": len(bucket["order_ids"]),
                "clients_count": len({line["client"].pk for line in bucket["lines"]}),
                "lines": bucket["lines"],
            }
        )
    rows.sort(key=lambda r: _due_sort_key(r["due_date"]))
    return rows


def _size_label(variant) -> str:
    """«M / красный» — the variant WITHOUT its product name, for rows already
    grouped under that product. Falls back to the SKU so a variant with no
    size and no colour never renders as a bare « / »."""
    return " / ".join(part for part in (variant.size, variant.color) if part) or variant.sku


def _line_row(item: OrderItem, today) -> dict:
    """One order line as the queue shows it — who it's for and what's left of
    it. `remaining` is this LINE's own shortfall (quantity − produced_qty),
    which is not the same number as a variant row's stock-adjusted
    `to_produce`: stock is shared across every client waiting on that SKU."""
    due = item.order.due_date
    return {
        "order_id": item.order_id,
        "order_status": item.order.status,
        "client": item.order.client,
        "variant": item.variant,
        "size_label": _size_label(item.variant),
        "quantity": item.quantity,
        "produced": min(item.produced_qty, item.quantity),
        "remaining": item.remaining_to_produce,
        "due_date": due,
        "overdue": bool(due and due < today),
    }


def queue_summary(rows: list[dict]) -> dict:
    """Header numbers for /orders/queue/ — computed from the rows already in
    hand, so the page never re-queries just to count."""
    if not rows:
        return {"units": 0, "rows": 0, "overdue": 0, "next_due": None}
    due_dates = [r["due_date"] for r in rows if r["due_date"]]
    return {
        "units": sum(r["to_produce"] for r in rows),
        "rows": len(rows),
        "overdue": sum(1 for r in rows if r["overdue"]),
        "next_due": min(due_dates) if due_dates else None,
    }


def order_line_progress(order: Order) -> list[dict]:
    """Per-line picture for ONE order: how much of it is already made, and what
    the variant's stock looks like right now. Two grouped queries, no N+1.

    The honest split, because stock is shared and cannot be pre-allocated:
      made / quantity  — this LINE's own progress (produced_qty), unambiguous
      in_stock         — the variant's total on_hand right now, a fact
      claimed_by_others— the same SKU promised to OTHER open orders
      free_for_order   — on_hand minus those other claims: what this order
                         could actually be handed from today
    «Сшить ещё» is deliberately NOT computed here — that's the production
    queue's aggregate job (apps.orders.services.production_queue), and a
    second per-order allocation would double-count whenever two clients want
    the same SKU."""
    items = list(order.items.select_related("variant__product__category"))
    if not items:
        return []

    variant_ids = {item.variant_id for item in items}
    on_hand = {
        row["variant_id"]: row["s"] or 0
        for row in StockMovement.objects.filter(variant_id__in=variant_ids)
        .values("variant_id")
        .annotate(s=Sum("quantity"))
    }
    others = {
        row["variant_id"]: row["q"] or 0
        for row in OrderItem.objects.filter(
            variant_id__in=variant_ids, order__status__in=Order.OPEN_STATUSES
        )
        .exclude(order_id=order.pk)
        .values("variant_id")
        .annotate(q=Sum("quantity"))
    }

    rows = []
    for item in items:
        stock = on_hand.get(item.variant_id, 0)
        claimed = others.get(item.variant_id, 0)
        free = max(stock - claimed, 0)
        made = min(item.produced_qty, item.quantity)
        rows.append(
            {
                "item": item,
                "made": made,
                "remaining": item.remaining_to_produce,
                "percent": round(made * 100 / item.quantity) if item.quantity else 0,
                "in_stock": stock,
                "claimed_by_others": claimed,
                "free_for_order": free,
                # Enough on hand to hand this line over today, produced or not.
                # This mirrors exactly what confirm_sale will see at handover:
                # hand_over releases THIS order's own reservation first, so the
                # check it faces is on_hand minus what other open orders hold.
                "coverable": free >= item.quantity,
                # Shortfall for HANDOVER, which is not the production remainder
                # — a fully produced line can still be short if other open
                # orders have a prior claim on the same shared stock.
                "short_by": max(item.quantity - free, 0),
            }
        )
    return rows


def order_progress(order: Order) -> dict:
    """Whole-order production progress — the headline «X из Y принято»."""
    items = list(order.items.all())
    total = sum(i.quantity for i in items)
    made = sum(min(i.produced_qty, i.quantity) for i in items)
    return {
        "made": made,
        "total": total,
        "percent": round(made * 100 / total) if total else 0,
        "complete": bool(items) and made >= total,
    }


@transaction.atomic
def mark_produced(
    order_item: OrderItem,
    quantity: int,
    user=None,
    *,
    defect_qty: int = 0,
    contractor=None,
    material_cost=None,
    trim_cost=None,
    labor_cost=None,
    currency="KGS",
    note="",
) -> OrderItem:
    """«Произведено N шт»: writes a PRODUCTION_IN movement for the item's
    variant and increments produced_qty — atomic, through services.py, so it
    shows in StockMovement history like any other intake. When every line on
    the order is fully produced, the order auto-advances to готов.

    Two paths, chosen by whether a `contractor` is given — the plain Editor/
    Manager quick action («Произведено N шт», no cost data) never has one,
    and behaves EXACTLY as before: a bare PRODUCTION_IN movement, nothing
    else. Passing a contractor (only the Owner-only apps.manufacturing.
    views.production_add flow does, via «Запустить производство» on an
    order — see apps.orders CLAUDE.md Part 4) instead delegates the whole
    stock/cost/defect/accrual write to apps.manufacturing.services.
    record_production_run, which is the ONE place that logic lives — this
    function never re-implements defect absorption or contractor accrual
    itself, it only adds the order-specific bookkeeping (produced_qty,
    status advance) on top. `defect_qty` never counts toward produced_qty —
    a defective unit didn't fulfil the order, it still needs replacing."""
    if quantity <= 0 and defect_qty <= 0:
        raise ValidationError(_("Quantity to produce must be positive."))
    locked = OrderItem.objects.select_for_update().get(pk=order_item.pk)
    remaining = locked.quantity - locked.produced_qty
    if quantity > remaining:
        raise ValidationError(
            _("Cannot produce %(qty)s — only %(remaining)s remain on this line.")
            % {"qty": quantity, "remaining": remaining}
        )
    if contractor is not None:
        from apps.manufacturing.services import record_production_run

        record_production_run(
            variant=locked.variant,
            contractor=contractor,
            accepted_qty=quantity,
            defect_qty=defect_qty,
            material_cost=material_cost or Decimal("0"),
            trim_cost=trim_cost or Decimal("0"),
            labor_cost=labor_cost or Decimal("0"),
            currency=currency,
            order_item=locked,
            note=note,
            user=user,
        )
    elif quantity > 0:
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
def cancel_order(order: Order, user=None, reason: str = "") -> Order:
    """Owner-only (enforced by the caller — apps.pos.views / admin), never
    Editor/Manager (CLAUDE.md Part 3h). Releases the reservation simply by
    moving out of OPEN_STATUSES; deposits are left on record, refundable by
    hand through the normal void_payment path if needed.

    `reason` — a plain typed note (why), stored on cancel_reason. See
    apps.sales.services.cancel_sale's own docstring: never a classification,
    never conflated with apps.manufacturing's defect/брак tracking. Falls
    back to unset when empty — only the /pos/ UI requires a real one."""
    if order.status in (Order.DELIVERED, Order.CANCELLED):
        raise ValidationError(_("This order is already closed."))
    order.status = Order.CANCELLED
    order.cancel_reason = reason.strip() if reason else order.cancel_reason
    order.save(update_fields=["status", "cancel_reason"])
    return order
