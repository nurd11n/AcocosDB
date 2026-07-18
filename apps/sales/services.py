"""Sale confirmation is the only place stock leaves the system for a sale.

Rules enforced here:
- confirmation is atomic and locks the variants (no race between two sellers);
- stock can never go negative — the whole sale fails with a clear message;
- the order total is computed once at confirmation and stored.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import CENTS, snapshot_rate_to_base
from apps.inventory.models import ProductVariant, StockMovement
from apps.inventory.services import add_movement

from .models import Payment, SaleItem, SaleOrder


@transaction.atomic
def confirm_sale(order: SaleOrder, user=None) -> SaleOrder:
    # Lock the ORDER row itself first, not just the variants: without this, two
    # concurrent confirm attempts (e.g. a double-tap on a bad connection) can
    # both read status=draft before either commits and both write off stock.
    # This makes confirming idempotent — the second call blocks on the lock,
    # then re-reads status=confirmed and raises cleanly below. Locking via a
    # fresh fetch (not `order` itself) still serializes correctly: once this
    # transaction holds the row lock, order.save() further down — same row,
    # same transaction — needs no lock of its own.
    locked = SaleOrder.objects.select_for_update().get(pk=order.pk)
    if locked.status != SaleOrder.DRAFT:
        raise ValidationError(_("Only pending sales can be approved."))
    items = list(order.items.select_related("variant__product"))
    if not items:
        raise ValidationError(_("Cannot approve a sale with no items."))

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

    # Freeze the FX rate now, inside the atomic block, and pre-convert the total
    # to сом. Every dashboard aggregate sums total_kgs and never re-converts, so
    # changing today's rate can't retroactively move this sale's reported value.
    now = timezone.now()
    rate = snapshot_rate_to_base(order.currency, timezone.localdate())
    order.total = total
    order.rate_to_kgs = rate
    order.total_kgs = (total * rate).quantize(CENTS)
    order.status = SaleOrder.CONFIRMED
    order.confirmed_at = now
    order.save(update_fields=["total", "rate_to_kgs", "total_kgs", "status", "confirmed_at"])
    return order


@transaction.atomic
def cancel_sale(order: SaleOrder, user=None) -> SaleOrder:
    """Cancel an approved sale: return the items to stock with RETURN_IN movements."""
    if order.status != SaleOrder.CONFIRMED:
        raise ValidationError(_("Only approved sales can be cancelled."))
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


@transaction.atomic
def return_items(order: SaleOrder, returns: dict[int, int], user=None) -> SaleOrder:
    """Partial return/exchange: put the returned units back in stock and shrink
    the sale accordingly. `returns` is {sale_item_id: qty_returned}.

    A return mutates the order (line quantities + total) rather than creating a
    separate document — simplest thing that's correct for a small shop, and the
    change is captured by SaleOrder's simple_history plus the RETURN_IN ledger
    rows. Because debt is derived (total − payments), reducing the total reduces
    what the client owes automatically; a fully-returned order becomes CANCELLED.
    """
    locked = SaleOrder.objects.select_for_update().get(pk=order.pk)
    if locked.status != SaleOrder.CONFIRMED:
        raise ValidationError(_("Only approved sales can have returns."))

    cleaned = {int(k): int(v) for k, v in returns.items() if int(v) > 0}
    if not cleaned:
        raise ValidationError(_("Nothing to return."))

    items = {i.pk: i for i in order.items.select_related("variant")}
    for item_id, qty in cleaned.items():
        item = items.get(item_id)
        if item is None:
            raise ValidationError(_("Unknown item in this sale."))
        if qty > item.quantity:
            raise ValidationError(
                _("Cannot return %(qty)s of %(sku)s — only %(have)s were sold.")
                % {"qty": qty, "sku": item.variant.sku, "have": item.quantity}
            )

    for item_id, qty in cleaned.items():
        item = items[item_id]
        add_movement(
            variant=item.variant,
            movement_type=StockMovement.RETURN_IN,
            quantity=qty,
            user=user,
            reason=f"Return sale #{order.pk}",
            sale_order=order,
        )
        item.quantity -= qty
        if item.quantity == 0:
            item.delete()
        else:
            item.save(update_fields=["quantity"])

    remaining = list(order.items.all())
    if not remaining:
        order.status = SaleOrder.CANCELLED
        order.total = Decimal("0")
    else:
        order.total = sum((i.line_total for i in remaining), Decimal("0"))
    # Recompute total_kgs at the order's ORIGINAL frozen rate — a return shrinks
    # the sale but must not re-price it at a new rate.
    order.total_kgs = (order.total * order.rate_to_kgs).quantize(CENTS)
    order.save(update_fields=["total", "total_kgs", "status"])
    return order


def today_summary() -> dict:
    """Revenue broken down by currency — orders can be in different currencies,
    so there is no single "revenue" number without a display-time conversion."""
    today = timezone.localdate()
    qs = SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date=today)
    revenue_by_currency: dict[str, Decimal] = defaultdict(Decimal)
    for row in qs.values("currency").annotate(t=Sum("total")):
        revenue_by_currency[row["currency"]] += row["t"] or Decimal("0")
    items = qs.aggregate(s=Sum("items__quantity"))["s"] or 0
    return {"revenue_by_currency": dict(revenue_by_currency), "orders": qs.count(), "items": items}


def todays_confirmed_orders():
    """Today's approved sales with items + payments preloaded — used by the daily report."""
    today = timezone.localdate()
    return (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date=today)
        .select_related("client")
        .prefetch_related("items__variant", "payments")
        .order_by("confirmed_at")
    )


def revenue_last_n_days(n: int = 7) -> list[dict]:
    """[{day, by_currency: {currency: amount}}] for the last n days, oldest first."""
    today = timezone.localdate()
    start = today - timedelta(days=n - 1)
    rows = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date__gte=start)
        .annotate(day=TruncDate("confirmed_at"))
        .values("day", "currency")
        .annotate(revenue=Sum("total"))
    )
    by_day: dict = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        by_day[r["day"]][r["currency"]] += r["revenue"] or Decimal("0")
    return [
        {
            "day": start + timedelta(days=i),
            "by_currency": dict(by_day.get(start + timedelta(days=i), {})),
        }
        for i in range(n)
    ]


def sales_by_channel(days: int = 30) -> list[dict]:
    """[{channel, label, by_currency: {currency: amount}}] over the last `days` days."""
    since = timezone.localdate() - timedelta(days=days - 1)
    rows = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date__gte=since)
        .values("channel", "currency")
        .annotate(revenue=Sum("total"))
    )
    labels = dict(SaleOrder.CHANNEL_CHOICES)
    by_channel: dict = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        by_channel[r["channel"]][r["currency"]] += r["revenue"] or Decimal("0")
    return [
        {"channel": ch, "label": str(labels.get(ch, ch)), "by_currency": dict(currs)}
        for ch, currs in by_channel.items()
    ]


def pending_orders_count() -> int:
    return SaleOrder.objects.filter(status=SaleOrder.DRAFT).count()


def units_sold_by_variant(days: int = 30) -> dict[int, int]:
    """{variant_id: units sold} over the last `days` days from confirmed sales.
    Powers the bestseller / slow-mover view — units, not money, so no currency
    conversion is involved."""
    since = timezone.localdate() - timedelta(days=days - 1)
    rows = (
        SaleItem.objects.filter(
            order__status=SaleOrder.CONFIRMED, order__confirmed_at__date__gte=since
        )
        .values("variant_id")
        .annotate(units=Sum("quantity"))
    )
    return {r["variant_id"]: r["units"] or 0 for r in rows}


def record_payment(
    order: SaleOrder, amount: Decimal, user=None, method=Payment.CASH, currency=None
):
    """Create a payment against an order. Defaults to the order's own currency,
    but a client can pay in a different one (e.g. a KGS sale paid in USD) — it
    just won't count toward that order's own paid/balance, only the client's
    overall per-currency debt (see SaleOrder.paid_amount). Walk-ins (no client)
    carry no debt, so there's nothing to record — returns None. Non-positive
    amounts are ignored."""
    if order.client_id is None or amount is None or amount <= 0:
        return None
    return Payment.objects.create(
        client_id=order.client_id,
        order=order,
        amount=amount,
        currency=currency or order.currency,
        method=method,
        created_by=user,
    )


def mark_fully_paid(order: SaleOrder, user=None):
    """Settle an approved order by recording a payment for its remaining balance."""
    if order.status != SaleOrder.CONFIRMED:
        raise ValidationError(_("Only approved sales can be settled."))
    return record_payment(order, order.balance, user=user)


@transaction.atomic
def void_payment(payment: Payment, user=None) -> Payment:
    """Reverse a payment with a negative-amount entry — never delete it, so the
    audit trail (and simple_history log) stays intact. Voiding an already
    reviewed payment requires a superuser."""
    if payment.reversed_payment_id:
        raise ValidationError(_("This payment is already a reversal — nothing to void."))
    if payment.reviewed and not (user and user.is_superuser):
        raise ValidationError(_("Voiding a reviewed payment requires a superuser."))
    return Payment.objects.create(
        client_id=payment.client_id,
        order_id=payment.order_id,
        amount=-payment.amount,
        currency=payment.currency,
        rate_to_kgs=payment.rate_to_kgs,  # same frozen rate → cancels the original exactly
        method=payment.method,
        note=_("Void of payment #%(id)s") % {"id": payment.pk},
        created_by=user,
        reversed_payment=payment,
    )
