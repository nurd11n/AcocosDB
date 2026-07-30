"""Sale confirmation is the only place stock leaves the system for a sale.

Rules enforced here:
- confirmation is atomic and locks the variants (no race between two sellers);
- stock can never go negative — the whole sale fails with a clear message;
- the order total is computed once at confirmation and stored.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import CENTS, snapshot_rate_to_base
from apps.inventory.models import ProductVariant, StockMovement
from apps.inventory.services import add_movement, reserved_by_variant

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
    # Stock promised to an open production order (apps.orders) is off-limits
    # to any OTHER sale, walk-in included — never just a client-side cap
    # (CLAUDE.md Part 1b/1c). A handover's own SaleOrder is unaffected: the
    # orders.Order is moved to выдан (releasing its reservation) before this
    # runs, in the same transaction — see apps.orders.services.hand_over.
    reserved = reserved_by_variant(variant_ids=variant_ids)

    total = Decimal("0")
    for item in items:
        on_hand = stock.get(item.variant_id, 0)
        available = on_hand - reserved.get(item.variant_id, 0)
        if available < item.quantity:
            raise ValidationError(
                _("Insufficient stock for %(sku)s: available %(have)s, requested %(need)s.")
                % {"sku": item.variant.sku, "have": max(available, 0), "need": item.quantity}
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


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round `value` (KGS) DOWN to the nearest multiple of `step` — the
    client-facing change figure. step<=0 degrades to a plain 2-decimal floor."""
    if not step or step <= 0:
        return value.quantize(CENTS, rounding=ROUND_DOWN)
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (units * step).quantize(CENTS, rounding=ROUND_DOWN)


def _change_amount_to_kgs(amount, change_currency, payment_currency, resolved_rate):
    """Change amount (in `change_currency`) -> KGS, using ONLY this payment's
    own resolved_rate — never a second lookup. Only the till currency (KGS)
    or the payment's own currency are expressible this way; anything else
    returns None for the caller to reject ("one transaction, one rate")."""
    if change_currency == settings.CURRENCY:
        return amount.quantize(CENTS, rounding=ROUND_HALF_UP)
    if change_currency == payment_currency and resolved_rate:
        return (amount * resolved_rate).quantize(CENTS, rounding=ROUND_HALF_UP)
    return None


def balance_kgs_before_payment(order: SaleOrder) -> Decimal:
    """The order's outstanding balance in KGS, from its FROZEN total_kgs minus
    every existing payment's net_applied_kgs — valid only once the order is
    CONFIRMED (total_kgs is frozen at confirm_sale time, and a draft never has
    payments attached; see apps.pos.views for the pre-confirmation preview,
    which computes the equivalent from the draft's live item total instead,
    since total_kgs isn't meaningful before confirm)."""
    already_applied_kgs = sum((p.net_applied_kgs for p in order.payments.all()), Decimal("0"))
    return order.total_kgs - already_applied_kgs


def compute_change_preview(
    order: SaleOrder,
    amount: Decimal,
    currency: str,
    resolved_rate: Decimal,
    balance_kgs_before: Decimal,
    change_currency: str | None = None,
) -> dict:
    """Pure computation, no DB writes — the SINGLE source of truth for both
    the POS double-check panel (apps.pos.views, live preview) and the actual
    payment (record_payment below), so preview and confirm can never disagree.

    `resolved_rate` is the rate ALREADY chosen for this payment (NBKR or an
    Owner override) — change is converted at this exact SAME rate, never a
    second lookup ("one transaction, one rate"). `balance_kgs_before` is the
    order's outstanding balance in KGS BEFORE this payment — callers compute
    it (see balance_kgs_before_payment for a confirmed order; the draft/
    pre-confirmation preview in apps.pos.views uses the live item total
    instead, since a draft's total_kgs isn't frozen yet).

    Change is computed in KGS first: the ideal (exact) excess this ONE
    payment creates over the order's balance, rounded DOWN to
    settings.CHANGE_ROUNDING_STEP for what's physically handed back
    (change_amount_kgs); the residue (change_rounding_kgs, always >= 0) is
    reported separately, never dropped. change_amount is that rounded KGS
    figure re-expressed in `change_currency` — None if that currency isn't
    expressible via this payment's one rate (see _change_amount_to_kgs)."""
    amount = Decimal(amount)
    if currency == settings.CURRENCY:
        amount_kgs = amount.quantize(CENTS, rounding=ROUND_HALF_UP)
    else:
        amount_kgs = (amount * resolved_rate).quantize(CENTS, rounding=ROUND_HALF_UP)

    # The excess THIS payment creates, clamped to [0, amount_kgs] — change can
    # never exceed what was physically received in this one payment.
    ideal_change_kgs = min(
        max(amount_kgs - max(balance_kgs_before, Decimal("0")), Decimal("0")), amount_kgs
    )
    rounded_change_kgs = _floor_to_step(ideal_change_kgs, settings.CHANGE_ROUNDING_STEP)
    change_rounding_kgs = (ideal_change_kgs - rounded_change_kgs).quantize(
        CENTS, rounding=ROUND_HALF_UP
    )

    change_currency = change_currency or order.currency
    if change_currency == settings.CURRENCY:
        change_amount = rounded_change_kgs
    elif change_currency == currency and resolved_rate:
        change_amount = (rounded_change_kgs / resolved_rate).quantize(CENTS, rounding=ROUND_HALF_UP)
    else:
        change_amount = None  # not expressible at this payment's one rate

    return {
        "amount_kgs": amount_kgs,
        "balance_kgs_before": balance_kgs_before,
        "ideal_change_kgs": ideal_change_kgs,
        "has_excess": ideal_change_kgs > settings.PAYMENT_ROUNDING_TOLERANCE,
        "change_amount_kgs": rounded_change_kgs,
        "change_rounding_kgs": change_rounding_kgs,
        "change_currency": change_currency,
        "change_amount": change_amount,
        "net_applied_kgs": amount_kgs - rounded_change_kgs,
        "cross_currency": currency != order.currency,
    }


def record_payment(
    order: SaleOrder,
    amount: Decimal,
    user=None,
    method=Payment.CASH,
    currency=None,
    rate_override: Decimal | None = None,
    excess_disposition: str = Payment.DISPOSITION_NONE,
    change_currency: str | None = None,
    change_amount_override: Decimal | None = None,
    change_adjust_reason: str = "",
):
    """Create a payment against an order. Defaults to the order's own currency,
    but a client can pay in a different one (e.g. a KGS sale paid in USD) — it
    just won't count toward that order's own paid/balance, only the client's
    overall per-currency debt (see SaleOrder.paid_amount). Walk-ins (no client)
    carry no debt, so there's nothing to record — returns None. Non-positive
    amounts are ignored.

    `rate_override`: the Owner's hand-entered rate (booth rate differs from
    NBKR). Callers MUST have already checked user.is_superuser — this function
    trusts the caller, the actual permission gate lives in the view/admin
    layer. Stores rate_source='manual' and rate_official (the NBKR rate at
    this moment, for reconstructing the spread later).

    `excess_disposition`: what to do with money beyond the sale's balance —
    'change' hands it back (computed via compute_change_preview; a sale must
    end fully paid whenever change > 0, or this raises), 'debt'/'credit'
    apply the full amount with no change (the excess then naturally reduces
    the client's other same-currency debt through the pooled per-client
    aggregate — see apps.clients.services.client_debts_by_currency).
    `change_amount_override`: a manually adjusted change figure (in
    change_currency) — callers MUST have already checked it's within the
    allowed band (or the user is Owner); this function trusts the caller,
    same pattern as rate_override.

    A foreign-currency payment (different from the order's own currency) with
    no rate on record and no override raises instead of silently freezing a
    fallback rate of 1.0 — the view layer should already have caught this via
    apps.pos.views._payment_conversion, but a payment must never save a wrong
    conversion even if some other caller skips that check."""
    if order.client_id is None or amount is None or amount <= 0:
        return None
    payment_currency = currency or order.currency
    if rate_override is None and payment_currency != order.currency:
        from apps.core.currency import rate_info

        if rate_info(payment_currency) is None:
            raise ValidationError(
                _("Нет курса для %(c)s — оплата не сохранена. Обновите курс и повторите.")
                % {"c": payment_currency}
            )

    # Resolve the ONE rate this whole transaction (payment + any change) uses.
    if rate_override is not None:
        resolved_rate = rate_override
    elif payment_currency == settings.CURRENCY:
        resolved_rate = Decimal("1")
    else:
        resolved_rate = snapshot_rate_to_base(payment_currency, timezone.localdate())

    change_amount = Decimal("0")
    change_amount_kgs = Decimal("0")
    change_rounding_kgs = Decimal("0")
    resolved_change_currency = change_currency or order.currency

    if excess_disposition == Payment.DISPOSITION_CHANGE:
        preview = compute_change_preview(
            order,
            amount,
            payment_currency,
            resolved_rate,
            balance_kgs_before_payment(order),
            change_currency=resolved_change_currency,
        )
        if change_amount_override is not None:
            if not change_adjust_reason.strip():
                raise ValidationError(_("Укажите причину изменения суммы сдачи."))
            override_kgs = _change_amount_to_kgs(
                change_amount_override, resolved_change_currency, payment_currency, resolved_rate
            )
            if override_kgs is None:
                raise ValidationError(
                    _("Сдача в этой валюте не может быть рассчитана по курсу платежа.")
                )
            change_amount = change_amount_override
            change_amount_kgs = override_kgs
        elif preview["change_amount"] is not None:
            change_amount = preview["change_amount"]
            change_amount_kgs = preview["change_amount_kgs"]
        else:
            raise ValidationError(
                _("Сдача в этой валюте не может быть рассчитана по курсу платежа.")
            )
        # The floor-to-step residue — from the IDEAL calculation, unaffected
        # by any manual adjustment above (see Payment model docstring).
        change_rounding_kgs = preview["change_rounding_kgs"]

        amount_kgs = preview["amount_kgs"]
        net_applied_kgs = amount_kgs - change_amount_kgs
        already_applied_kgs = sum((p.net_applied_kgs for p in order.payments.all()), Decimal("0"))
        remaining_kgs = order.total_kgs - already_applied_kgs - net_applied_kgs
        if change_amount_kgs > 0 and remaining_kgs > settings.PAYMENT_ROUNDING_TOLERANCE:
            raise ValidationError(_("Нельзя выдать сдачу, пока по продаже остаётся долг."))
    elif excess_disposition not in (
        Payment.DISPOSITION_NONE,
        Payment.DISPOSITION_DEBT,
        Payment.DISPOSITION_CREDIT,
    ):
        raise ValidationError(_("Неизвестный способ учёта излишка."))

    kwargs = dict(
        client_id=order.client_id,
        order=order,
        amount=amount,
        currency=payment_currency,
        rate_to_kgs=resolved_rate,
        method=method,
        excess_disposition=excess_disposition,
        change_amount=change_amount,
        change_currency=resolved_change_currency,
        change_amount_kgs=change_amount_kgs,
        change_rounding_kgs=change_rounding_kgs,
        created_by=user,
    )
    if change_amount_override is not None:
        kwargs["note"] = _("Сдача изменена вручную: %(reason)s") % {
            "reason": change_adjust_reason.strip()
        }
    if rate_override is not None:
        kwargs["rate_source"] = Payment.RATE_MANUAL
        kwargs["rate_official"] = snapshot_rate_to_base(payment_currency, timezone.localdate())
    return Payment.objects.create(**kwargs)


def mark_fully_paid(order: SaleOrder, user=None):
    """Settle an approved order by recording a payment for its remaining balance."""
    if order.status != SaleOrder.CONFIRMED:
        raise ValidationError(_("Only approved sales can be settled."))
    return record_payment(order, order.balance, user=user)


@transaction.atomic
def void_payment(payment: Payment, user=None) -> Payment:
    """Reverse a payment with a negative-amount entry — never delete it, so the
    audit trail (and simple_history log) stays intact. Voiding an already
    reviewed payment requires a superuser.

    Mirrors EVERY money field's sign, not just amount: change_amount and
    change_amount_kgs flip too, so the reversal's net_applied_kgs is exactly
    -1 × the original's — a payment that gave change reverses to EXACTLY its
    pre-payment balance, at the SAME frozen rate, never today's. Even
    change_rounding_kgs flips, so a void doesn't double-count that day's
    till-drift report with the now-undone transaction's residue."""
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
        rate_source=payment.rate_source,
        rate_official=payment.rate_official,
        excess_disposition=payment.excess_disposition,
        change_amount=-payment.change_amount,
        change_currency=payment.change_currency,
        change_amount_kgs=-payment.change_amount_kgs,
        change_rounding_kgs=-payment.change_rounding_kgs,
        method=payment.method,
        note=_("Void of payment #%(id)s") % {"id": payment.pk},
        created_by=user,
        reversed_payment=payment,
    )
