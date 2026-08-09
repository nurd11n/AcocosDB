"""Sale confirmation is the only place stock leaves the system for a sale.

Rules enforced here:
- confirmation is atomic and locks the variants (no race between two sellers);
- stock can never go negative — the whole sale fails with a clear message;
- the order total is computed once at confirmation and stored.
"""

import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import CENTS, rate_info, snapshot_rate_to_base
from apps.inventory.models import ProductVariant, StockMovement
from apps.inventory.services import add_movement, reserved_by_variant

from .models import Payment, SaleItem, SaleOrder


@transaction.atomic
def confirm_sale(
    order: SaleOrder, user=None, is_historical: bool = False, historical_date=None
) -> SaleOrder:
    """`is_historical`/`historical_date`: Owner-only backdated entry (see
    apps.pos.views.sale_confirm — the permission check lives there, this
    function trusts the caller, same pattern as record_payment's
    rate_override) for a sale that already happened before this database
    existed. She wants it itemized in «История покупок» without a fake
    stock movement and without it inflating this period's revenue. When
    True: the available-stock check is skipped entirely (an already-
    happened sale has nothing to do with TODAY's stock), NO SALE_OUT
    movement is written at all (see SaleOrder.is_historical's own
    docstring for the full list of aggregates this then excludes from),
    and confirmed_at is set from historical_date (noon Bishkek, so the
    stored UTC instant never lands on the wrong local calendar day) instead
    of now(). Everything else — total, frozen FX rate, debt/payment
    behaviour — works exactly like a normal sale, deliberately."""
    if is_historical and historical_date is None:
        raise ValidationError(_("Укажите дату исторической продажи."))

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
    if not is_historical:
        # Evaluate the locking query NOW — used lazily as a subquery it would
        # never actually acquire the row locks, reopening the two-sellers race.
        # Skipped entirely for a historical sale: it's not taking anything
        # from TODAY's warehouse, so there's nothing here to lock or check.
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

        # Check availability per VARIANT, summed across every line — never per
        # line. The same variant can legitimately appear on two lines (a production
        # order handed over via apps.orders.services.hand_over copies OrderItems
        # straight across, and nothing merges them), and a per-line check would
        # pass 6+6 against 10 available and drive stock to −2. This mirrors the
        # cart-time cap's own "across ALL lines of that same variant combined"
        # rule (CLAUDE.md Part 1a) — the guard of last resort must be at least as
        # strict as the UX cap in front of it, never weaker.
        needed: dict[int, int] = defaultdict(int)
        for item in items:
            needed[item.variant_id] += item.quantity

        skus = {i.variant_id: i.variant.sku for i in items}
        for variant_id, need in needed.items():
            on_hand = stock.get(variant_id, 0)
            available = on_hand - reserved.get(variant_id, 0)
            if available < need:
                raise ValidationError(
                    _("Insufficient stock for %(sku)s: available %(have)s, requested %(need)s.")
                    % {"sku": skus[variant_id], "have": max(available, 0), "need": need}
                )

    total = sum((item.line_total for item in items), Decimal("0"))

    for item in items:
        if not is_historical:
            add_movement(
                variant=item.variant,
                movement_type=StockMovement.SALE_OUT,
                quantity=item.quantity,
                user=user,
                reason=f"Sale #{order.pk}",
                sale_order=order,
            )
        # Freeze the cost basis NOW, same rule as the FX rate below: a cost
        # price edit next month must never rewrite this month's profit.
        item.cost_price = item.variant.cost_price
    SaleItem.objects.bulk_update(items, ["cost_price"])

    # Freeze the FX rate now, inside the atomic block, and pre-convert the total
    # to сом. Every dashboard aggregate sums total_kgs and never re-converts, so
    # changing today's rate can't retroactively move this sale's reported value.
    # Rounded UP (never down): total_kgs is what a foreign-currency order is
    # considered to OWE in KGS terms (it gates balance_kgs_before_payment /
    # change eligibility for cross-currency payments below) — rounding it down
    # would let a payment register the order "fully paid" for slightly less
    # real value than it's actually worth. A historical sale still freezes
    # TODAY's rate — there is no historical NBKR rate on record to use instead
    # (ExchangeRate keeps one current row per currency, never a dated history).
    rate = snapshot_rate_to_base(order.currency, timezone.localdate())
    order.total = total
    order.rate_to_kgs = rate
    order.total_kgs = (total * rate).quantize(CENTS, rounding=ROUND_UP)
    order.status = SaleOrder.CONFIRMED
    update_fields = ["total", "rate_to_kgs", "total_kgs", "status", "confirmed_at"]
    if is_historical:
        order.is_historical = True
        order.confirmed_at = timezone.make_aware(datetime.combine(historical_date, time(12, 0)))
        update_fields.append("is_historical")
    else:
        order.confirmed_at = timezone.now()
    order.save(update_fields=update_fields)
    return order


@transaction.atomic
def cancel_sale(order: SaleOrder, user=None) -> SaleOrder:
    """Cancel an approved sale: return the items to stock with RETURN_IN movements.

    A historical sale (see SaleOrder.is_historical) never wrote a SALE_OUT
    movement in the first place — restocking it via RETURN_IN would
    phantom-add stock that was never actually removed, so that step is
    skipped for one; only its status changes."""
    # Lock first, same reason as confirm_sale: without this, two concurrent
    # cancel attempts (a double-tapped «Отменить продажу» on a bad
    # connection) could both read status=CONFIRMED before either commits,
    # and both restock the same items — doubling the RETURN_IN movements and
    # permanently inflating stock by however much the sale contained.
    locked = SaleOrder.objects.select_for_update().get(pk=order.pk)
    if locked.status != SaleOrder.CONFIRMED:
        raise ValidationError(_("Only approved sales can be cancelled."))
    if not locked.is_historical:
        for item in locked.items.select_related("variant"):
            add_movement(
                variant=item.variant,
                movement_type=StockMovement.RETURN_IN,
                quantity=item.quantity,
                user=user,
                reason=f"Cancel sale #{locked.pk}",
                sale_order=locked,
            )
    locked.status = SaleOrder.CANCELLED
    locked.save(update_fields=["status"])
    return locked


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
        # A historical sale (see SaleOrder.is_historical) never removed
        # stock, so a return from it must not add phantom stock either.
        if not locked.is_historical:
            add_movement(
                variant=item.variant,
                movement_type=StockMovement.RETURN_IN,
                quantity=qty,
                user=user,
                reason=f"Return sale #{order.pk}",
                sale_order=order,
            )
        old_qty = item.quantity
        item.quantity -= qty
        if item.quantity == 0:
            item.delete()
            continue
        fields = ["quantity"]
        # A FIXED discount was agreed against the WHOLE line, so returning part
        # of it has to shrink the discount in the same proportion — the client
        # keeps exactly the per-unit price they actually paid. Leaving it whole
        # would hand them the full discount on fewer units and, as soon as
        # discount_value exceeded the now-smaller subtotal, the
        # saleitem_discount_fixed_lte_subtotal CheckConstraint would reject the
        # save and the return would 500. A PERCENT discount needs nothing: it
        # is proportional by definition.
        if item.discount_type == SaleItem.DISCOUNT_FIXED and item.discount_value:
            scaled = (item.discount_value * item.quantity / old_qty).quantize(
                CENTS, rounding=ROUND_HALF_UP
            )
            item.discount_value = min(scaled, item.unit_price * item.quantity)
            fields.append("discount_value")
        item.save(update_fields=fields)

    remaining = list(order.items.all())
    if not remaining:
        order.status = SaleOrder.CANCELLED
        order.total = Decimal("0")
    else:
        order.total = sum((i.line_total for i in remaining), Decimal("0"))
    # Recompute total_kgs at the order's ORIGINAL frozen rate — a return shrinks
    # the sale but must not re-price it at a new rate. Rounded UP, same reason
    # as confirm_sale above.
    order.total_kgs = (order.total * order.rate_to_kgs).quantize(CENTS, rounding=ROUND_UP)
    order.save(update_fields=["total", "total_kgs", "status"])
    return order


def today_summary() -> dict:
    """Revenue broken down by currency — orders can be in different currencies,
    so there is no single "revenue" number without a display-time conversion.
    Excludes is_historical sales (see SaleOrder.is_historical) — a backdated
    entry must never inflate this or any other revenue figure."""
    today = timezone.localdate()
    qs = SaleOrder.objects.filter(
        status=SaleOrder.CONFIRMED, confirmed_at__date=today, is_historical=False
    )
    revenue_by_currency: dict[str, Decimal] = defaultdict(Decimal)
    for row in qs.values("currency").annotate(t=Sum("total")):
        revenue_by_currency[row["currency"]] += row["t"] or Decimal("0")
    items = qs.aggregate(s=Sum("items__quantity"))["s"] or 0
    return {"revenue_by_currency": dict(revenue_by_currency), "orders": qs.count(), "items": items}


def todays_confirmed_orders():
    """Today's approved sales with items + payments preloaded — used by the
    daily report. Excludes is_historical (see SaleOrder.is_historical) —
    the date filter alone would already exclude a genuinely backdated entry
    in almost every case, but this stays explicit rather than relying on
    that coincidence (she COULD backdate to today by mistake)."""
    today = timezone.localdate()
    return (
        SaleOrder.objects.filter(
            status=SaleOrder.CONFIRMED, confirmed_at__date=today, is_historical=False
        )
        .select_related("client")
        .prefetch_related("items__variant", "payments")
        .order_by("confirmed_at")
    )


def today_revenue_kgs() -> Decimal:
    """Today's confirmed revenue, in KGS, summed from each order's own FROZEN
    total_kgs — never re-converted at a live rate (CLAUDE.md: 'every
    dashboard aggregate sums total_kgs and never re-converts'). This is the
    one figure /stats/ and /pos/today/ must agree with apps.reports.dashboard
    on; a same-day rate refresh must not move it. Excludes is_historical."""
    today = timezone.localdate()
    return SaleOrder.objects.filter(
        status=SaleOrder.CONFIRMED, confirmed_at__date=today, is_historical=False
    ).aggregate(s=Sum("total_kgs"))["s"] or Decimal("0")


def revenue_last_n_days_kgs(n: int = 7) -> list[dict]:
    """[{day, revenue_kgs}] for the last n days, oldest first — each day's
    figure is SUM(total_kgs) of orders confirmed that day, same frozen-value
    rule as today_revenue_kgs. Unlike revenue_last_n_days (per-currency,
    unconverted — kept for callers that want the currency breakdown), this
    is a single already-KGS number, ready to chart without a live
    conversion. Excludes is_historical."""
    today = timezone.localdate()
    start = today - timedelta(days=n - 1)
    rows = (
        SaleOrder.objects.filter(
            status=SaleOrder.CONFIRMED, confirmed_at__date__gte=start, is_historical=False
        )
        .annotate(day=TruncDate("confirmed_at"))
        .values("day")
        .annotate(revenue_kgs=Sum("total_kgs"))
    )
    by_day = {r["day"]: r["revenue_kgs"] or Decimal("0") for r in rows}
    return [
        {
            "day": start + timedelta(days=i),
            "revenue_kgs": by_day.get(start + timedelta(days=i), Decimal("0")),
        }
        for i in range(n)
    ]


def sales_by_channel_kgs(days: int = 30) -> list[dict]:
    """[{channel, label, revenue_kgs}] over the last `days` days — SUM(total_kgs)
    per channel, same frozen-value rule as today_revenue_kgs. See
    sales_by_channel for the per-currency (unconverted) variant. Excludes
    is_historical."""
    since = timezone.localdate() - timedelta(days=days - 1)
    rows = (
        SaleOrder.objects.filter(
            status=SaleOrder.CONFIRMED, confirmed_at__date__gte=since, is_historical=False
        )
        .values("channel")
        .annotate(revenue_kgs=Sum("total_kgs"))
    )
    labels = dict(SaleOrder.CHANNEL_CHOICES)
    return [
        {
            "channel": r["channel"],
            "label": str(labels.get(r["channel"], r["channel"])),
            "revenue_kgs": r["revenue_kgs"] or Decimal("0"),
        }
        for r in rows
    ]


def revenue_last_n_days(n: int = 7) -> list[dict]:
    """[{day, by_currency: {currency: amount}}] for the last n days, oldest
    first. Excludes is_historical."""
    today = timezone.localdate()
    start = today - timedelta(days=n - 1)
    rows = (
        SaleOrder.objects.filter(
            status=SaleOrder.CONFIRMED, confirmed_at__date__gte=start, is_historical=False
        )
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
    """[{channel, label, by_currency: {currency: amount}}] over the last
    `days` days. Excludes is_historical."""
    since = timezone.localdate() - timedelta(days=days - 1)
    rows = (
        SaleOrder.objects.filter(
            status=SaleOrder.CONFIRMED, confirmed_at__date__gte=since, is_historical=False
        )
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
    conversion is involved. Excludes is_historical: a backdated entry must
    not make a discontinued item look like it's newly selling again."""
    since = timezone.localdate() - timedelta(days=days - 1)
    rows = (
        SaleItem.objects.filter(
            order__status=SaleOrder.CONFIRMED,
            order__confirmed_at__date__gte=since,
            order__is_historical=False,
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
    expressible via this payment's one rate (see _change_amount_to_kgs).

    Both foreign-currency conversions below round DOWN, always in the shop's
    favor: amount_kgs is how much debt a foreign payment forgives (rounding
    it up would credit the client for slightly more than they actually
    paid), and change_amount is foreign currency physically handed back
    (rounding it up would hand back slightly more than the computed KGS
    change is really worth) — the same "never give away more than it's
    worth" rule CHANGE_ROUNDING_STEP already applies to the KGS figure
    itself, just extended to non-KGS money."""
    amount = Decimal(amount)
    if currency == settings.CURRENCY:
        amount_kgs = amount.quantize(CENTS, rounding=ROUND_HALF_UP)
    else:
        amount_kgs = (amount * resolved_rate).quantize(CENTS, rounding=ROUND_DOWN)

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
        change_amount = (rounded_change_kgs / resolved_rate).quantize(CENTS, rounding=ROUND_DOWN)
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


@transaction.atomic
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
    batch_id=None,
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
    conversion even if some other caller skips that check.

    `batch_id`: set ONLY by pay_oldest_first, one shared uuid4() across every
    Payment a single multi-sale repayment creates — see Payment.batch_id's
    own docstring for why (batch voiding, one grouped statement row). Every
    other caller leaves it None; there is no reason for an ordinary single-
    sale payment to belong to a batch."""
    if order.client_id is None or amount is None or amount <= 0:
        return None
    # Lock the order for the rest of this call: every balance/overpayment/
    # change check below reads order.payments.all()/total_kgs, and without
    # this, two near-simultaneous payments against the same order (a
    # double-tapped debt repayment, two staff repaying the same client at
    # once) could each compute their checks against the SAME pre-payment
    # balance and both succeed — overpaying the order by however much the
    # second payment was. Re-locking a row this same transaction already
    # holds (e.g. a caller like pay_oldest_first that locks first) is a safe
    # no-op in Postgres, never a deadlock. Order.payments is a related
    # manager keyed off order.pk, so re-pointing `order` at the locked
    # instance makes every subsequent `order.payments.all()` read the
    # up-to-date set too.
    order = SaleOrder.objects.select_for_update().get(pk=order.pk)
    payment_currency = currency or order.currency
    if rate_override is None and payment_currency != order.currency:
        if rate_info(payment_currency) is None:
            raise ValidationError(
                _("Нет курса для %(c)s — оплата не сохранена. Обновите курс и повторите.")
                % {"c": payment_currency}
            )

    # Resolve the ONE rate this whole transaction (payment + any change) uses
    # — and the HONEST source label that goes with it. A payment that used
    # the currently-on-record rate must say whatever that ExchangeRate row
    # actually is (nbkr/frankfurter/manual), never a hardcoded guess: the
    # Owner may have hand-entered today's rate, and a payment against it is
    # not "NBKR" just because nobody typed an override on THIS payment.
    if rate_override is not None:
        resolved_rate = rate_override
        resolved_rate_source = Payment.RATE_MANUAL
    elif payment_currency == settings.CURRENCY:
        resolved_rate = Decimal("1")
        resolved_rate_source = Payment.RATE_NBKR  # trivial 1:1, matches rate_info's base stub
    else:
        resolved_rate = snapshot_rate_to_base(payment_currency, timezone.localdate())
        info = rate_info(payment_currency)
        resolved_rate_source = info["source"] if info else Payment.RATE_NBKR

    change_amount = Decimal("0")
    change_amount_kgs = Decimal("0")
    change_rounding_kgs = Decimal("0")
    resolved_change_currency = change_currency or order.currency

    # Computed unconditionally (cheap, pure — no DB writes) so DISPOSITION_
    # NONE can be checked against it below too, not just DISPOSITION_CHANGE.
    preview = compute_change_preview(
        order,
        amount,
        payment_currency,
        resolved_rate,
        balance_kgs_before_payment(order),
        change_currency=resolved_change_currency,
    )

    if excess_disposition == Payment.DISPOSITION_NONE:
        # THE OVERPAYMENT FORK belongs to the caller (the POS double-check
        # panel asks "сдача / в счёт долга / аванс?" before this is ever
        # called) — but that decision is made from a balance read BEFORE
        # this function's lock above, so it can go stale under concurrency
        # (two near-simultaneous payments each seeing "no excess yet, this
        # one's fine"). Re-checking here, under the lock, against the
        # authoritative current balance is what actually closes that gap —
        # "never a wrong conversion even if some other caller skips that
        # check" (see this function's docstring), extended to overpayment.
        if preview["has_excess"]:
            raise ValidationError(
                _(
                    "Платёж превышает остаток по продаже — выберите сдачу, "
                    "зачёт в счёт долга или аванс."
                )
            )
    if excess_disposition == Payment.DISPOSITION_CHANGE:
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
        rate_source=resolved_rate_source,
        method=method,
        excess_disposition=excess_disposition,
        change_amount=change_amount,
        change_currency=resolved_change_currency,
        change_amount_kgs=change_amount_kgs,
        change_rounding_kgs=change_rounding_kgs,
        created_by=user,
        batch_id=batch_id,
    )
    if change_amount_override is not None:
        kwargs["note"] = _("Сдача изменена вручную: %(reason)s") % {
            "reason": change_adjust_reason.strip()
        }
    if rate_override is not None:
        kwargs["rate_official"] = snapshot_rate_to_base(payment_currency, timezone.localdate())
    return Payment.objects.create(**kwargs)


def mark_fully_paid(order: SaleOrder, user=None):
    """Settle an approved order by recording a payment for its remaining balance."""
    if order.status != SaleOrder.CONFIRMED:
        raise ValidationError(_("Only approved sales can be settled."))
    return record_payment(order, order.balance, user=user)


def oldest_first_open_sales(client, currency: str) -> list[SaleOrder]:
    """This client's CONFIRMED sales in `currency` with a balance still owed,
    oldest first — the base list the multi-sale «Погасить долг» flow (single
    payment split across several sales) walks, for both its preview and its
    confirm step, so the two can never disagree about order."""
    return [
        o
        for o in SaleOrder.objects.filter(
            client=client, status=SaleOrder.CONFIRMED, currency=currency
        ).order_by("confirmed_at")
        if o.balance > 0
    ]


def allocate_oldest_first(client, currency: str, amount: Decimal) -> dict:
    """Pure preview (no DB writes) of how `amount` (already in `currency` —
    this never crosses a currency boundary, unlike a single sale's own
    payment panel, which is where a foreign-currency repayment belongs) would
    apply across this client's open same-currency sales, oldest first.

    Returns {"rows": [{"order", "applied", "remaining_balance", "closes"}],
    "remaining_amount": whatever is left over once every open sale in this
    currency is fully covered — > 0 means `amount` exceeds the total debt}."""
    remaining = amount
    rows = []
    for order in oldest_first_open_sales(client, currency):
        if remaining <= 0:
            break
        balance = order.balance
        applied = min(remaining, balance)
        rows.append(
            {
                "order": order,
                "applied": applied,
                "remaining_balance": balance - applied,
                "closes": (balance - applied) <= 0,
            }
        )
        remaining -= applied
    return {"rows": rows, "remaining_amount": remaining}


@transaction.atomic
def pay_oldest_first(
    client, currency: str, amount: Decimal, user=None, method=Payment.CASH
) -> list[Payment]:
    """Applies `amount` across this client's open same-currency sales, oldest
    first, one Payment per sale via the EXISTING record_payment — never a
    second payment path (same rule as every other money flow in this app).
    Raises rather than guessing what to do with money beyond the total open
    balance — repay a specific amount, or use a single sale's own payment
    panel with an explicit change/credit disposition for that case.

    Every resulting Payment shares ONE fresh batch_id — the ONE real-world
    event she'd describe as «получила 150 000», even though it lands as
    several Payment rows under the hood (see Payment.batch_id and
    void_payment_batch: this is what lets that single event be voided as
    one atomic unit later, and shown as one grouped row on her statement)."""
    allocation = allocate_oldest_first(client, currency, amount)
    if allocation["remaining_amount"] > 0:
        raise ValidationError(
            _("Сумма (%(amount)s) больше общего долга в этой валюте (%(total)s). Уменьшите сумму.")
            % {"amount": amount, "total": amount - allocation["remaining_amount"]}
        )
    batch = uuid.uuid4()
    payments = []
    for row in allocation["rows"]:
        # Re-lock and re-check each order's balance right before applying —
        # the preview above ran outside any lock, so a concurrent payment on
        # the same order (a double-tapped confirm, another manager repaying
        # the same client) could have shrunk it since. Never apply more than
        # what's still actually owed.
        locked_order = SaleOrder.objects.select_for_update().get(pk=row["order"].pk)
        applied = min(row["applied"], locked_order.balance)
        if applied <= 0:
            continue
        payment = record_payment(
            locked_order, applied, user=user, method=method, currency=currency, batch_id=batch
        )
        if payment is not None:
            payments.append(payment)
    return payments


def _create_reversal(payment: Payment, user=None) -> Payment:
    """The actual reversing-entry creation — shared by void_payment (one
    payment) and void_payment_batch (every payment in a batch, atomically).
    Callers are responsible for locking + validating `payment` FIRST (this
    function trusts them, same pattern as record_payment's rate_override).

    Mirrors EVERY money field's sign, not just amount: change_amount and
    change_amount_kgs flip too, so the reversal's net_applied_kgs is exactly
    -1 × the original's — a payment that gave change reverses to EXACTLY its
    pre-payment balance, at the SAME frozen rate, never today's. Even
    change_rounding_kgs flips, so a void doesn't double-count that day's
    till-drift report with the now-undone transaction's residue. The
    reversal inherits batch_id unchanged (None stays None for an ordinary
    payment) so a batch's reversal rows are themselves grouped the same way
    the originals were — client_statement shows a reversed batch as one
    grouped increase, not N separate ones."""
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
        batch_id=payment.batch_id,
    )


@transaction.atomic
def void_payment(payment: Payment, user=None) -> Payment:
    """Reverse a payment with a negative-amount entry — never delete it, so the
    audit trail (and simple_history log) stays intact. Voiding an already
    reviewed payment requires a superuser.

    Refuses a payment that belongs to a pay_oldest_first BATCH (batch_id
    set): voiding just one sale's share of a multi-sale repayment while
    leaving the rest standing is exactly the incoherent partial allocation
    batch voiding exists to prevent — see void_payment_batch, the ONLY path
    a batched payment can be reversed through. This is a hard guarantee, not
    a UI nicety: even a crafted admin bulk-action call lands here and gets
    refused the same way."""
    # Lock the row being voided first: two concurrent voids (a double-tapped
    # admin action) would otherwise both read "not yet voided" and each write a
    # reversal, subtracting the amount TWICE and leaving the client with a
    # phantom credit. The DB's payment_one_reversal_per_payment unique
    # constraint backs this up even for a caller that bypasses this function.
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.batch_id:
        raise ValidationError(_("This payment is part of a batch — void the whole batch instead."))
    if locked.reversed_payment_id:
        raise ValidationError(_("This payment is already a reversal — nothing to void."))
    if Payment.objects.filter(reversed_payment_id=locked.pk).exists():
        raise ValidationError(_("This payment has already been voided."))
    if locked.reviewed and not (user and user.is_superuser):
        raise ValidationError(_("Voiding a reviewed payment requires a superuser."))
    return _create_reversal(locked, user=user)


@transaction.atomic
def void_payment_batch(batch_id, user=None) -> list[Payment]:
    """Void an entire pay_oldest_first batch as ONE atomic unit — all or
    nothing, each payment reversed at its OWN frozen rate via the existing
    _create_reversal (never a second reversal implementation). The whole
    thing is one transaction: if voiding ANY payment in the batch somehow
    failed partway through, the entire batch rolls back rather than leaving
    a partial reversal — a client's balance must never reflect "some of
    that payment came back but not all of it".

    void_payment itself refuses to void a single payment that carries a
    batch_id (see its own docstring) — this function is the ONLY path a
    batched payment can be reversed through, so a partial void of a batch
    isn't just discouraged by convention, there is no code path that
    produces one."""
    if not batch_id:
        raise ValidationError(_("No batch to void."))
    # reversed_payment_id__isnull=True excludes reversal rows themselves —
    # this is "every ORIGINAL payment in the batch", never a reversal that
    # happens to share the same batch_id.
    originals = list(
        Payment.objects.select_for_update()
        .filter(batch_id=batch_id, reversed_payment_id__isnull=True)
        .order_by("pk")
    )
    if not originals:
        raise ValidationError(_("No payments found for this batch."))
    already_voided_ids = set(
        Payment.objects.filter(reversed_payment_id__in=[p.pk for p in originals]).values_list(
            "reversed_payment_id", flat=True
        )
    )
    to_void = [p for p in originals if p.pk not in already_voided_ids]
    if not to_void:
        raise ValidationError(_("This batch has already been voided."))
    for p in to_void:
        if p.reviewed and not (user and user.is_superuser):
            raise ValidationError(
                _("Batch includes an already-reviewed payment — voiding requires a superuser.")
            )
    return [_create_reversal(p, user=user) for p in to_void]
