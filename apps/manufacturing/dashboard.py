"""Manufacturing dashboard aggregates — Owner-only, same tier as cost price
and profit (apps.core.permissions.can_see_costs). Two kinds of function
here:

- `spent_kgs`/`net_cash` are called from apps.reports.dashboard to add two
  tiles to the MAIN dashboard, right beside «Получено» — that function's own
  revenue/profit/received math is untouched; this module only ADDS a cash-
  out figure alongside it, never subtracts from or double-counts it.
- Everything else backs the dedicated manufacturing dashboard page
  (expenses by category, production volume, defect rate per contractor,
  real-vs-assumed cost gap, contractor balances, work-in-progress, net
  profit after overhead).
"""

from decimal import Decimal

from django.db.models import F, Sum

from .models import Expense, ProductionRun
from .services import contractor_balance


def spent_kgs(start, end) -> Decimal:
    """«Потрачено» — every Expense in the period, any category, in KGS at
    each expense's own frozen rate. Never mixed into revenue/profit/
    «Получено» — see Expense's own docstring for why."""
    return Expense.objects.filter(date__range=(start, end)).aggregate(
        t=Sum(F("amount") * F("rate_to_kgs"))
    )["t"] or Decimal("0")


def net_cash(received_kgs: Decimal, start, end) -> Decimal:
    """Получено − Потрачено — a NEW figure, never a modification of
    `received_kgs` (passed in from apps.reports.dashboard._metrics,
    untouched)."""
    return received_kgs - spent_kgs(start, end)


def overhead_kgs(start, end) -> Decimal:
    """General overhead (аренда — see Expense.OVERHEAD_CATEGORIES) only —
    NOT every expense, and never subtracted twice: this is the one place
    overhead reduces a profit figure; production-attributed expense
    categories already reached profit via cost_price -> COGS and must never
    be subtracted here too."""
    return Expense.objects.filter(
        date__range=(start, end), category__in=Expense.OVERHEAD_CATEGORIES
    ).aggregate(t=Sum(F("amount") * F("rate_to_kgs")))["t"] or Decimal("0")


def expenses_by_category(start, end) -> list[dict]:
    rows = (
        Expense.objects.filter(date__range=(start, end))
        .values("category")
        .annotate(t=Sum(F("amount") * F("rate_to_kgs")))
        .order_by("-t")
    )
    labels = dict(Expense.CATEGORY_CHOICES)
    return [
        {"category": r["category"], "label": labels[r["category"]], "total": r["t"] or Decimal("0")}
        for r in rows
    ]


def expenses_by_section(start, end) -> list[dict]:
    """The same totals as expenses_by_category, rolled up into the 3
    sections she actually scans a report by (Зарплата / Материалы /
    Прочее) — display-only, see Expense.SECTION_LABELS's own docstring for
    why the 5 real categories underneath stay distinct (аренда must never
    silently merge into прочее)."""
    totals = {key: Decimal("0") for key in Expense.SECTION_LABELS}
    for row in expenses_by_category(start, end):
        totals[Expense.category_section(row["category"])] += row["total"]
    return [
        {"section": key, "label": label, "total": totals[key]}
        for key, label in Expense.SECTION_LABELS.items()
    ]


def production_volume(start, end) -> dict:
    """Total accepted/defect units in the period + the resulting success
    rate — COMPUTED here too, same rule as ProductionRun.success_rate."""
    agg = ProductionRun.objects.filter(date__range=(start, end)).aggregate(
        accepted=Sum("accepted_qty"), defect=Sum("defect_qty")
    )
    accepted = agg["accepted"] or 0
    defect = agg["defect"] or 0
    attempted = accepted + defect
    return {
        "accepted": accepted,
        "defect": defect,
        "attempted": attempted,
        "success_rate": (Decimal(accepted) / Decimal(attempted)) if attempted else None,
    }


def defect_rate_by_contractor(start, end, limit: int = 10) -> list[dict]:
    """Worst-first — the contractor with the highest defect rate leads, so
    a quality problem is the first thing she sees, not buried in a list."""
    rows = (
        ProductionRun.objects.filter(date__range=(start, end))
        .values("contractor_id", "contractor__name")
        .annotate(accepted=Sum("accepted_qty"), defect=Sum("defect_qty"))
    )
    result = []
    for r in rows:
        accepted = r["accepted"] or 0
        defect = r["defect"] or 0
        attempted = accepted + defect
        if not attempted:
            continue
        result.append(
            {
                "contractor_id": r["contractor_id"],
                "name": r["contractor__name"],
                "accepted": accepted,
                "defect": defect,
                "attempted": attempted,
                "defect_rate": Decimal(defect) / Decimal(attempted),
            }
        )
    result.sort(key=lambda r: r["defect_rate"], reverse=True)
    return result[:limit]


def cost_gap_by_variant(limit: int = 15) -> list[dict]:
    """assumed (current ProductVariant.cost_price) vs real (the latest
    costed PRODUCTION_IN movement) — surfaces a manual edit that's drifted
    away from what production actually costs. Only variants with at least
    one REAL costed production run are included (nothing to compare a
    never-manufactured-through-this-system variant's assumed cost against),
    ranked by the size of the gap."""
    from apps.inventory.models import ProductVariant, StockMovement

    latest_real: dict[int, Decimal] = {}
    seen: set[int] = set()
    for m in (
        StockMovement.objects.filter(movement_type=StockMovement.PRODUCTION_IN, cost__isnull=False)
        .order_by("variant_id", "-created_at")
        .only("variant_id", "cost")
    ):
        if m.variant_id in seen:
            continue
        seen.add(m.variant_id)
        latest_real[m.variant_id] = m.cost

    if not latest_real:
        return []

    variants = ProductVariant.objects.filter(pk__in=latest_real.keys()).select_related(
        "product__category"
    )
    result = [
        {
            "variant": v,
            "assumed_cost": v.cost_price,
            "real_cost": latest_real[v.pk],
            "gap": v.cost_price - latest_real[v.pk],
        }
        for v in variants
    ]
    result.sort(key=lambda r: abs(r["gap"]), reverse=True)
    return result[:limit]


def contractor_balances_summary(limit: int = 15) -> list[dict]:
    """Active contractors with a nonzero balance — positive = shop owes
    them (see contractor_balance's own sign convention)."""
    from .models import Contractor

    balances = contractor_balance()
    if not balances:
        return []
    contractors = {c.pk: c for c in Contractor.objects.filter(pk__in=balances.keys())}
    rows = [
        {"contractor": contractors[cid], "balances": bal}
        for cid, bal in balances.items()
        if cid in contractors
    ]
    rows.sort(
        key=lambda r: sum((abs(v) for v in r["balances"].values()), Decimal("0")), reverse=True
    )
    return rows[:limit]


def work_in_progress() -> list[dict]:
    """«Что в работе»: open production-order demand (reuses apps.orders.
    services.production_queue's own arithmetic — never re-derived here,
    see CLAUDE.md's "never duplicate stock/queue logic outside services.py")
    enriched with the most recent contractor assigned to that variant and
    an ASSUMED expected cost (to_produce × current cost_price — explicitly
    an estimate, not a promise, labelled as such in the template)."""
    from django.utils import timezone

    from apps.orders.services import production_queue

    rows = [r for r in production_queue() if r["to_produce"] > 0]
    if not rows:
        return []

    variant_ids = [r["variant"].pk for r in rows]
    latest_contractor = {}
    for run in (
        ProductionRun.objects.filter(variant_id__in=variant_ids)
        .select_related("contractor")
        .order_by("variant_id", "-date", "-created_at")
    ):
        latest_contractor.setdefault(run.variant_id, run.contractor)

    today = timezone.localdate()
    result = []
    for r in rows:
        due = r["due_date"]
        result.append(
            {
                "variant": r["variant"],
                "qty": r["to_produce"],
                "contractor": latest_contractor.get(r["variant"].pk),
                "due_date": due,
                "days_remaining": (due - today).days if due else None,
                "overdue": r["overdue"],
                "expected_cost": r["to_produce"] * r["variant"].cost_price,
            }
        )
    return result


def manufacturing_dashboard_data(start, end, gross_profit_kgs: Decimal) -> dict:
    """Everything the dedicated manufacturing dashboard page renders."""
    overhead = overhead_kgs(start, end)
    return {
        "expenses_by_category": expenses_by_category(start, end),
        "expenses_by_section": expenses_by_section(start, end),
        "spent": spent_kgs(start, end),
        "production": production_volume(start, end),
        "defect_by_contractor": defect_rate_by_contractor(start, end),
        "cost_gap": cost_gap_by_variant(),
        "contractor_balances": contractor_balances_summary(),
        "wip": work_in_progress(),
        "gross_profit": gross_profit_kgs,
        "overhead": overhead,
        "net_profit_after_overhead": gross_profit_kgs - overhead,
    }
