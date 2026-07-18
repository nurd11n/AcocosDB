"""Owner dashboard aggregates — every figure in сом, read from the FROZEN
snapshots (SaleOrder.total_kgs, Payment.amount = amount × rate_to_kgs), NEVER
converted at read time, so a change to today's rate can't move history.

Query discipline (asserted in tests, ≤12 at every period even with 5000 sales):
- current vs previous period come from ONE conditional-sum query each, not two;
- debt metric + delta + aging all come from the two debt queries (orders +
  their payment rows), computed in Python — no per-order or per-client N+1;
- dead stock is two queries (stock subquery + one grouped last-sale), no N+1.

COGS note: cost_price is treated as сом (the base currency) — that's how the
shop records what a garment cost — so profit stays snapshot-stable with no
read-time FX. Foreign-currency-costed variants (rare here) are approximate.
"""

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import (
    Case,
    DecimalField,
    F,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import (
    Coalesce,
    TruncDate,
    TruncHour,
    TruncMonth,
    TruncWeek,
)
from django.utils import timezone

from apps.inventory.models import ProductVariant, StockMovement
from apps.sales.models import Payment, SaleItem, SaleOrder

# period key -> (label, trailing days, chart granularity)
PERIODS = {
    "today": ("Сегодня", 1, "hour"),
    "month": ("Месяц", 30, "day"),
    "3m": ("3 мес", 90, "week"),
    "6m": ("6 мес", 180, "week"),
    "year": ("Год", 365, "month"),
}
DEFAULT_PERIOD = "month"
DEAD_STOCK_DAYS = 90

_MONEY = DecimalField(max_digits=18, decimal_places=2)
# сом value of a payment / sale line, from the FROZEN rate — pure DB expression.
_PAY_KGS = F("amount") * F("rate_to_kgs")
_LINE_KGS = F("unit_price") * F("quantity") * F("order__rate_to_kgs")
_LINE_COGS = F("variant__cost_price") * F("quantity")


def _cond_sum(value_expr, condition):
    """Sum(value) over rows matching `condition`, 0 when none match."""
    return Coalesce(
        Sum(Case(When(condition, then=value_expr), default=Value(0), output_field=_MONEY)),
        Value(Decimal("0")),
        output_field=_MONEY,
    )


def resolve_period(period: str) -> str:
    return period if period in PERIODS else DEFAULT_PERIOD


def _windows(period: str):
    """(start, end, prev_start, prev_end) as local dates — two adjacent
    equal-length trailing windows ending today. Fixed day-counts, so the
    comparison is exactly equal-length and month boundaries can't skew it."""
    label, days, gran = PERIODS[period]
    end = timezone.localdate()
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return start, end, prev_start, prev_end


def _delta(current: Decimal, previous: Decimal):
    """{pct, has_baseline} — % change vs the previous period. No baseline when
    the previous period was zero (never '+100%' or '+∞')."""
    if previous and previous != 0:
        return {"pct": int(round((current - previous) / previous * 100)), "has_baseline": True}
    return {"pct": None, "has_baseline": False}


def _metrics(start, end, prev_start, prev_end):
    cur = (start, end)
    prev = (prev_start, prev_end)

    # Revenue: one query, current + previous conditional sums over the union range.
    rev = SaleOrder.objects.filter(
        status=SaleOrder.CONFIRMED, confirmed_at__date__range=(prev_start, end)
    ).aggregate(
        cur=_cond_sum(F("total_kgs"), _in(cur)),
        prev=_cond_sum(F("total_kgs"), _in(prev)),
    )

    # Units + COGS: one query over the line items in the union range.
    uc = SaleItem.objects.filter(
        order__status=SaleOrder.CONFIRMED, order__confirmed_at__date__range=(prev_start, end)
    ).aggregate(
        units_cur=Coalesce(
            Sum(Case(When(_in(cur, "order__"), then=F("quantity")), default=0)), 0
        ),
        units_prev=Coalesce(
            Sum(Case(When(_in(prev, "order__"), then=F("quantity")), default=0)), 0
        ),
        cogs_cur=_cond_sum(_LINE_COGS, _in(cur, "order__")),
        cogs_prev=_cond_sum(_LINE_COGS, _in(prev, "order__")),
    )

    revenue, revenue_prev = rev["cur"], rev["prev"]
    profit = revenue - uc["cogs_cur"]
    profit_prev = revenue_prev - uc["cogs_prev"]
    margin = int(round(profit / revenue * 100)) if revenue else None

    return {
        "revenue": {"value": revenue, "delta": _delta(revenue, revenue_prev)},
        "profit": {"value": profit, "margin": margin, "delta": _delta(profit, profit_prev)},
        "units": {"value": uc["units_cur"], "delta": _delta(uc["units_cur"], uc["units_prev"])},
    }


def _in(window, prefix=""):
    return Q(**{f"{prefix}confirmed_at__date__range": window})


def _debt(prev_end):
    """Current outstanding сом + client count + aging + the previous-period-end
    figure for the delta — all from TWO queries (orders and their payment rows),
    joined in Python. Debt is a running balance, so it's computed as-of dates,
    not period-windowed."""
    today = timezone.localdate()
    orders = list(
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client__isnull=False).values(
            "id", "client_id", "total_kgs", "confirmed_at"
        )
    )
    pay_rows = list(
        Payment.objects.filter(order__isnull=False, order__client__isnull=False)
        .annotate(kgs=_PAY_KGS)
        .values("order_id", "kgs", "created_at")
    )
    paid_by_order = {}
    paid_by_order_asof = {}
    for p in pay_rows:
        paid_by_order[p["order_id"]] = paid_by_order.get(p["order_id"], Decimal("0")) + p["kgs"]
        if p["created_at"].date() <= prev_end:
            paid_by_order_asof[p["order_id"]] = (
                paid_by_order_asof.get(p["order_id"], Decimal("0")) + p["kgs"]
            )

    aging = {"d0_30": Decimal("0"), "d31_60": Decimal("0"), "d60_plus": Decimal("0")}
    debt_by_client = {}
    debt_by_client_prev = {}
    for o in orders:
        outstanding = o["total_kgs"] - paid_by_order.get(o["id"], Decimal("0"))
        if outstanding > 0:
            debt_by_client[o["client_id"]] = (
                debt_by_client.get(o["client_id"], Decimal("0")) + outstanding
            )
            age = (today - o["confirmed_at"].date()).days
            bucket = "d0_30" if age <= 30 else "d31_60" if age <= 60 else "d60_plus"
            aging[bucket] += outstanding
        if o["confirmed_at"].date() <= prev_end:
            out_prev = o["total_kgs"] - paid_by_order_asof.get(o["id"], Decimal("0"))
            if out_prev > 0:
                debt_by_client_prev[o["client_id"]] = (
                    debt_by_client_prev.get(o["client_id"], Decimal("0")) + out_prev
                )

    outstanding_total = sum(debt_by_client.values(), Decimal("0"))
    outstanding_prev = sum(debt_by_client_prev.values(), Decimal("0"))
    return {
        "value": outstanding_total,
        "clients": len(debt_by_client),
        "delta": _delta(outstanding_total, outstanding_prev),
        "aging": aging,
    }


def _series(start, end, gran):
    """[{label, value}] of Σ total_kgs bucketed by granularity, gaps filled with
    0 so the x-axis is complete (an empty chart reads as broken, not as empty)."""
    trunc = {
        "hour": TruncHour("confirmed_at"),
        "day": TruncDate("confirmed_at"),
        "week": TruncWeek("confirmed_at"),
        "month": TruncMonth("confirmed_at"),
    }[gran]
    rows = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date__range=(start, end))
        .annotate(bucket=trunc)
        .values("bucket")
        .annotate(v=Sum("total_kgs"))
    )
    by_bucket = {}
    for r in rows:
        b = r["bucket"]
        # TruncDate -> date; TruncWeek/Month -> aware datetime. Normalise to the
        # local date so keys line up with the generated axis below.
        key = timezone.localtime(b).date() if isinstance(b, datetime) else b
        by_bucket[key] = (r["v"] or Decimal("0")) + by_bucket.get(key, Decimal("0"))

    buckets = _bucket_axis(start, end, gran)
    return [
        {"label": lbl, "value": by_bucket.get(key, Decimal("0")), "key": key} for key, lbl in buckets
    ]


def _bucket_axis(start, end, gran):
    """Ordered (key, label) buckets spanning [start, end] at the granularity."""
    out = []
    if gran == "hour":
        for h in range(24):
            out.append((h, f"{h:02d}"))
        return out
    if gran == "day":
        d = start
        while d <= end:
            out.append((d, f"{d.day:02d}.{d.month:02d}"))
            d += timedelta(days=1)
        return out
    if gran == "week":
        d = start - timedelta(days=start.weekday())  # Monday of the start week
        while d <= end:
            out.append((d, f"{d.day:02d}.{d.month:02d}"))
            d += timedelta(days=7)
        return out
    # month
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((date(y, m, 1), calendar.month_abbr[m]))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _series_hour(end):
    """Today's series is by hour; total_kgs summed per local hour."""
    rows = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date=end)
        .annotate(bucket=TruncHour("confirmed_at"))
        .values("bucket")
        .annotate(v=Sum("total_kgs"))
    )
    by_hour = {}
    for r in rows:
        by_hour[timezone.localtime(r["bucket"]).hour] = r["v"] or Decimal("0")
    return [{"label": f"{h:02d}", "value": by_hour.get(h, Decimal("0")), "key": h} for h in range(24)]


def _channels(start, end):
    labels = dict(SaleOrder.CHANNEL_CHOICES)
    rows = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date__range=(start, end))
        .values("channel")
        .annotate(v=Sum("total_kgs"))
        .order_by("-v")
    )
    return [
        {"label": str(labels.get(r["channel"], r["channel"])), "value": r["v"] or Decimal("0")}
        for r in rows
        if r["v"]
    ]


def _methods(start, end):
    labels = dict(Payment.METHOD_CHOICES)
    rows = (
        Payment.objects.filter(
            created_at__date__range=(start, end), reversed_payment__isnull=True
        )
        .annotate(kgs=_PAY_KGS)
        .values("method")
        .annotate(v=Sum("kgs"))
        .order_by("-v")
    )
    return [
        {"label": str(labels.get(r["method"], r["method"])), "value": r["v"] or Decimal("0")}
        for r in rows
        if r["v"] and r["v"] > 0
    ]


def _top_products(start, end, limit=8):
    rows = (
        SaleItem.objects.filter(
            order__status=SaleOrder.CONFIRMED, order__confirmed_at__date__range=(start, end)
        )
        .values("variant__product__id", "variant__product__name")
        .annotate(
            revenue=Coalesce(Sum(_LINE_KGS, output_field=_MONEY), Value(Decimal("0")), output_field=_MONEY),
            units=Coalesce(Sum("quantity"), 0),
            cogs=Coalesce(Sum(_LINE_COGS, output_field=_MONEY), Value(Decimal("0")), output_field=_MONEY),
        )
        .order_by("-revenue")[:limit]
    )
    return [
        {
            "name": r["variant__product__name"],
            "revenue": r["revenue"],
            "units": r["units"],
            "profit": r["revenue"] - r["cogs"],
        }
        for r in rows
    ]


def _dead_stock(limit=8):
    """In-stock variants with no sale in 90+ days (never-sold count from the
    product's age). Two queries, no per-variant N+1."""
    today = timezone.localdate()
    stock_sq = Subquery(
        StockMovement.objects.filter(variant=OuterRef("pk"))
        .values("variant")
        .annotate(s=Sum("quantity"))
        .values("s"),
        output_field=IntegerField(),
    )
    variants = list(
        ProductVariant.objects.filter(is_active=True)
        .select_related("product")
        .annotate(_stock=Coalesce(stock_sq, 0))
        .filter(_stock__gt=0)
    )
    last_sale = {
        r["variant_id"]: r["last"]
        for r in SaleItem.objects.filter(order__status=SaleOrder.CONFIRMED)
        .values("variant_id")
        .annotate(last=Max("order__confirmed_at"))
    }
    dead = []
    for v in variants:
        sold_at = last_sale.get(v.pk)
        if sold_at:
            days_idle = (today - timezone.localtime(sold_at).date()).days
        else:
            days_idle = (today - timezone.localtime(v.product.created_at).date()).days
        if days_idle >= DEAD_STOCK_DAYS:
            dead.append(
                {
                    "name": str(v),
                    "units": v._stock,
                    "days_idle": days_idle,
                    "capital": (v.cost_price * v._stock),
                    "never_sold": sold_at is None,
                }
            )
    dead.sort(key=lambda d: d["capital"], reverse=True)
    return dead[:limit]


def dashboard_data(period: str) -> dict:
    """Everything the dashboard renders, for one period. Pure aggregation — the
    caching wrapper lives in the view so the query-budget test can measure the
    real (uncached) query count."""
    period = resolve_period(period)
    label, days, gran = PERIODS[period]
    start, end, prev_start, prev_end = _windows(period)

    metrics = _metrics(start, end, prev_start, prev_end)
    metrics["debt"] = _debt(prev_end)
    series = _series_hour(end) if gran == "hour" else _series(start, end, gran)

    return {
        "period": period,
        "period_label": label,
        "periods": [(k, v[0]) for k, v in PERIODS.items()],
        "metrics": metrics,
        "series": series,
        "channels": _channels(start, end),
        "methods": _methods(start, end),
        "top_products": _top_products(start, end),
        "dead_stock": _dead_stock(),
    }
