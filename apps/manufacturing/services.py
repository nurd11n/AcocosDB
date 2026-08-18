"""Contractor balances + production recording. Two rules drive every
function here:

1. Never touch apps.sales.Payment/SaleOrder or apps.clients — see
   models.py's module docstring for why (contractor debt runs the opposite
   direction from client debt, and an expense is not a sale).
2. cost_price is written ONLY here (via apply_production_cost), and only
   forward — a past SaleItem.cost_price snapshot is never rewritten (see
   apps.sales.models.SaleItem.cost_price's own docstring: reading a live
   cost-price edit into a past sale's profit is exactly the bug that field
   exists to prevent). This module changes what FUTURE sales freeze, never
   what a past one already did.
"""

from collections import defaultdict
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.currency import CENTS, snapshot_rate_to_base
from apps.inventory.models import StockMovement
from apps.inventory.services import add_movement

from .models import Contractor, ContractorTransaction, Expense, ProductionRun


def contractor_balance(contractor: "Contractor | None" = None) -> dict[int, dict[str, Decimal]]:
    """{contractor_id: {currency: balance}} — mirrors apps.clients.services.
    client_debts_by_currency's shape exactly, for the opposite party:
    positive = the shop owes the contractor (labor accrued, not yet paid);
    negative = the contractor owes the shop (an advance paid that hasn't
    been worked off). No opening-balance equivalent — a contractor
    relationship starts at zero, never a migrated pre-system baseline."""
    qs = ContractorTransaction.objects.all()
    if contractor is not None:
        qs = qs.filter(contractor=contractor)

    totals: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    accrual_rows = (
        qs.filter(kind=ContractorTransaction.ACCRUAL)
        .values("contractor_id", "currency")
        .annotate(t=Sum("amount"))
    )
    for row in accrual_rows:
        totals[row["contractor_id"]][row["currency"]] += row["t"] or Decimal("0")

    payment_rows = (
        qs.filter(kind=ContractorTransaction.PAYMENT)
        .values("contractor_id", "currency")
        .annotate(t=Sum("amount"))
    )
    for row in payment_rows:
        totals[row["contractor_id"]][row["currency"]] -= row["t"] or Decimal("0")

    result = {}
    for cid, currs in totals.items():
        quantized = {
            cur: amt.quantize(CENTS, rounding=ROUND_HALF_UP)
            for cur, amt in currs.items()
            if amt != 0
        }
        if quantized:
            result[cid] = quantized
    return result


def contractor_statement(contractor: Contractor, date_from=None, date_to=None) -> dict[str, dict]:
    """Per-currency running-balance ledger for one contractor — same shape
    as apps.clients.services.client_statement (opening/rows/closing per
    currency, sliced by [date_from, date_to]), so /pos/ can reuse that exact
    presentation pattern over different data. Positive balance = shop owes
    contractor, same sign convention as contractor_balance."""
    txns = list(
        ContractorTransaction.objects.filter(contractor=contractor).order_by("date", "created_at")
    )
    entries_by_currency: dict[str, list[dict]] = defaultdict(list)
    for t in txns:
        dt = timezone.make_aware(datetime.combine(t.date, time.min))
        is_accrual = t.kind == ContractorTransaction.ACCRUAL
        entries_by_currency[t.currency].append(
            {
                "sort_dt": dt,
                "date": t.date,
                "kind": t.kind,
                "description": t.note or t.get_kind_display(),
                "accrual_amount": t.amount if is_accrual else None,
                "payment_amount": t.amount if not is_accrual else None,
                "delta": t.amount if is_accrual else -t.amount,
            }
        )

    result: dict[str, dict] = {}
    for currency, entries in entries_by_currency.items():
        entries.sort(key=lambda e: e["sort_dt"])
        opening = Decimal("0")
        running = Decimal("0")
        rows = []
        for e in entries:
            if date_to is not None and e["date"] > date_to:
                continue  # outside the statement window entirely
            running += e["delta"]
            if date_from is not None and e["date"] < date_from:
                opening = running
                continue
            row = dict(e)
            row["balance"] = running.quantize(CENTS, rounding=ROUND_HALF_UP)
            rows.append(row)
        closing = running.quantize(CENTS, rounding=ROUND_HALF_UP)
        if rows or closing != 0:
            result[currency] = {
                "opening": opening.quantize(CENTS, rounding=ROUND_HALF_UP),
                "rows": rows,
                "closing": closing,
            }
    return result


def record_expense(
    *, date, category, amount: Decimal, currency, contractor=None, variant=None, note="", user=None
) -> Expense:
    """Standalone expense entry (fabric, trim, rent, misc) — freezes its own
    rate the same way a payment does. `production_run` is deliberately not
    a parameter here: that link is set ONLY by record_production_run below,
    never hand-picked in a form (same discipline as OrderItem.produced_qty)."""
    rate = snapshot_rate_to_base(currency, date)
    return Expense.objects.create(
        date=date,
        category=category,
        amount=amount,
        currency=currency,
        rate_to_kgs=rate,
        contractor=contractor,
        variant=variant,
        note=note,
        created_by=user,
    )


@transaction.atomic
def record_production_run(
    *,
    variant,
    contractor: Contractor,
    accepted_qty: int,
    defect_qty: int = 0,
    material_cost: Decimal = Decimal("0"),
    trim_cost: Decimal = Decimal("0"),
    labor_cost: Decimal = Decimal("0"),
    currency="KGS",
    date=None,
    order_item=None,
    note="",
    user=None,
) -> ProductionRun:
    """Record one production event. This is the ONE place a production
    event's cost, stock, and contractor accrual all get written — called
    both by a general "record production" action and by
    apps.orders.services.mark_produced (which wraps this, then does its own
    order-specific bookkeeping: produced_qty, status advance).

    - Writes the ProductionRun row (accepted_qty/defect_qty as typed —
      success_rate is a computed property, never stored here).
    - material_cost/trim_cost are real spend REGARDLESS of accepted_qty
      (fabric was cut either way) — each becomes its own Expense row
      (category ткань/фурнитура) whenever > 0, linked to this run, so the
      money is never silently dropped even on a total-loss (accepted_qty=0)
      run. labor_cost becomes a Expense(category=зарплата) row on the SAME
      condition (real spend regardless of outcome)...
    - ...but the labor ACCRUAL on the contractor's balance (a
      ContractorTransaction) is written ONLY when accepted_qty > 0 AND
      labor_cost > 0 — "accepting creates the accrual": a contractor isn't
      owed for units the shop rejected.
    - Defective units write NO StockMovement (nothing sellable resulted).
      Accepted units DO — quantity=accepted_qty — carrying `cost` = the
      ABSORBED per-unit figure: (material+trim+labor) ÷ accepted_qty. This
      is what makes defect cost "real and not vanish" per CLAUDE.md: a
      batch of 20 that cost 10 000 сом total but only yielded 17 good units
      costs 588.24/unit, not 500 — the 3 defective units' share of the
      fabric+labor is absorbed into the 17 that sold, not written off into
      nothing. Skipped entirely when total cost is 0 (no cost info was
      given) or accepted_qty is 0 (nothing to divide into) — cost_price is
      left untouched rather than being zeroed out by a costless run.
    - That absorbed figure also becomes the new ProductVariant.cost_price —
      what confirm_sale freezes onto the NEXT sale. Past SaleItem.cost_price
      snapshots are never touched (see this module's own docstring)."""
    if accepted_qty < 0 or defect_qty < 0:
        raise ValueError("accepted_qty/defect_qty must be non-negative.")
    if accepted_qty == 0 and defect_qty == 0:
        raise ValueError("A production run must record at least one unit (accepted or defect).")

    run_date = date or timezone.localdate()
    rate = snapshot_rate_to_base(currency, run_date)

    run = ProductionRun.objects.create(
        variant=variant,
        contractor=contractor,
        accepted_qty=accepted_qty,
        defect_qty=defect_qty,
        date=run_date,
        note=note,
        order_item=order_item,
        created_by=user,
    )

    def _expense(category, amount):
        if amount and amount > 0:
            Expense.objects.create(
                date=run_date,
                category=category,
                amount=amount,
                currency=currency,
                rate_to_kgs=rate,
                contractor=contractor,
                variant=variant,
                production_run=run,
                note=note,
                created_by=user,
            )
            return amount * rate
        return Decimal("0")

    total_cost_kgs = Decimal("0")
    total_cost_kgs += _expense(Expense.FABRIC, material_cost)
    total_cost_kgs += _expense(Expense.TRIM, trim_cost)
    total_cost_kgs += _expense(Expense.LABOR, labor_cost)

    if accepted_qty > 0:
        absorbed_unit_cost = None
        if total_cost_kgs > 0:
            absorbed_unit_cost = (total_cost_kgs / accepted_qty).quantize(
                CENTS, rounding=ROUND_HALF_UP
            )
        add_movement(
            variant=variant,
            movement_type=StockMovement.PRODUCTION_IN,
            quantity=accepted_qty,
            user=user,
            reason=f"Партия производства #{run.pk} — {contractor.name}",
            cost=absorbed_unit_cost,
        )
        if absorbed_unit_cost is not None:
            variant.cost_price = absorbed_unit_cost
            variant.save(update_fields=["cost_price"])

    if accepted_qty > 0 and labor_cost and labor_cost > 0:
        ContractorTransaction.objects.create(
            contractor=contractor,
            kind=ContractorTransaction.ACCRUAL,
            amount=labor_cost,
            currency=currency,
            rate_to_kgs=rate,
            date=run_date,
            note=note or f"Производство: {variant} × {accepted_qty}",
            production_run=run,
            created_by=user,
        )

    return run
