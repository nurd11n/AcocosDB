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

from datetime import timedelta
from decimal import Decimal

from django.core.files.storage import default_storage
from django.db.models import (
    Case,
    Count,
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
)
from django.utils import timezone

from apps.inventory.models import ProductVariant, StockMovement
from apps.inventory.services import cover_image_names
from apps.reports import export
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
_LINE_SUBTOTAL = F("unit_price") * F("quantity")
# Mirrors SaleItem.discount_amount in SQL — this must stay a DB expression
# (not the Python property) because it feeds a grouped .annotate()/Sum() over
# many rows, not a single loaded instance. A SaleItem's own CheckConstraints
# already guarantee discount_value can never make this go negative, so no
# extra clamping is needed here.
_LINE_DISCOUNT = Case(
    When(discount_type=SaleItem.DISCOUNT_PERCENT, then=_LINE_SUBTOTAL * F("discount_value") / 100),
    When(discount_type=SaleItem.DISCOUNT_FIXED, then=F("discount_value")),
    default=Value(Decimal("0")),
    output_field=_MONEY,
)
# "Top products by revenue" must rank by what was actually charged, not the
# sticker price — an unchanged _LINE_KGS here would overstate revenue (and
# rank) for anything sold at a discount, while the main revenue metric
# (SaleOrder.total_kgs, summed elsewhere) already correctly nets it out.
_LINE_KGS = (_LINE_SUBTOTAL - _LINE_DISCOUNT) * F("order__rate_to_kgs")
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
        units_cur=Coalesce(Sum(Case(When(_in(cur, "order__"), then=F("quantity")), default=0)), 0),
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


def _calendar_days(start, end):
    """{date: {revenue(сом), orders, units}} for every day WITH sales in the
    range — two grouped queries, no per-day N+1. The view gap-fills the calendar
    grid from the date range and looks each day up here."""
    by: dict = {}
    rev = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date__range=(start, end))
        .annotate(d=TruncDate("confirmed_at"))
        .values("d")
        .annotate(rev=Sum("total_kgs"), n=Count("id"))
    )
    for r in rev:
        by[r["d"]] = {"revenue": r["rev"] or Decimal("0"), "orders": r["n"], "units": 0}
    units = (
        SaleItem.objects.filter(
            order__status=SaleOrder.CONFIRMED, order__confirmed_at__date__range=(start, end)
        )
        .annotate(d=TruncDate("order__confirmed_at"))
        .values("d")
        .annotate(u=Sum("quantity"))
    )
    for u in units:
        by.setdefault(u["d"], {"revenue": Decimal("0"), "orders": 0, "units": 0})["units"] = (
            u["u"] or 0
        )
    return by


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
            created_at__date__range=(start, end),
            # A voided payment must not colour the method mix: drop BOTH the
            # negative reversal row (reversed_payment set) AND the original it
            # voided (reversal__isnull=True = nothing reverses it). Excluding
            # only the reversal would leave the original overstating the method.
            reversed_payment__isnull=True,
            reversal__isnull=True,
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


def _thumb_url(name):
    """Media URL for a product thumbnail/photo file name, or "" when absent —
    the template falls back to a placeholder tile."""
    return default_storage.url(name) if name else ""


def _top_products(start, end, limit=8):
    rows = list(
        SaleItem.objects.filter(
            order__status=SaleOrder.CONFIRMED, order__confirmed_at__date__range=(start, end)
        )
        .values("variant__product__id", "variant__product__name")
        .annotate(
            revenue=Coalesce(
                Sum(_LINE_KGS, output_field=_MONEY), Value(Decimal("0")), output_field=_MONEY
            ),
            units=Coalesce(Sum("quantity"), 0),
            cogs=Coalesce(
                Sum(_LINE_COGS, output_field=_MONEY), Value(Decimal("0")), output_field=_MONEY
            ),
        )
        .order_by("-revenue")[:limit]
    )
    # One extra bulk query for every row's cover image, not one per row — a
    # product can have many images now (see apps.inventory.services.
    # cover_image_names), and this is a grouped SaleItem aggregate, not a
    # Product queryset, so there's nothing to prefetch_related onto.
    covers = cover_image_names([r["variant__product__id"] for r in rows])
    return [
        {
            "name": r["variant__product__name"],
            "thumb": _thumb_url(covers.get(r["variant__product__id"], "")),
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
    # One bulk query for every candidate's cover image up front, keyed by
    # product id — cheaper than filtering to just the ones that turn out to
    # be dead-stock (a handful of extra ids costs nothing; a second query
    # per row would not).
    covers = cover_image_names([v.product_id for v in variants])
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
                    "thumb": _thumb_url(covers.get(v.product_id, "")),
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

    return {
        "period": period,
        "period_label": label,
        "periods": [(k, v[0]) for k, v in PERIODS.items()],
        "metrics": metrics,
        "day_data": _calendar_days(start, end),
        "cal_start": start,
        "cal_end": end,
        "channels": _channels(start, end),
        "methods": _methods(start, end),
        "top_products": _top_products(start, end),
        "dead_stock": _dead_stock(),
        # «Заказы» panels (CLAUDE.md Part 3g) — deliberately NOT touched by
        # _convert_money's view-currency toggle (it only walks the keys above)
        # since production orders are a separate concern from the revenue
        # dashboard's сом/$/₽ display switch.
        "orders_open": _orders_open_summary(),
        "queue_top": _queue_top(),
        # CLIENT_BOTS.md §1: "the one metric that governs everything" — every
        # broadcast feature is worthless below a real audience. Bots aren't
        # production-ready yet (see .env.example) — skip the queries entirely
        # while both channels are off, the template hides the panels either way.
        "telegram_reach": _telegram_reach(),
        "top_favourited": _top_favourited(),
    }


def _bots_enabled() -> bool:
    from django.conf import settings

    return settings.BOTS_ENABLED or settings.WHATSAPP_ENABLED


def _telegram_reach() -> dict:
    if not _bots_enabled():
        return {"reachable": 0, "total": 0}
    from apps.inbox.services import telegram_reach

    return telegram_reach()


def _top_favourited() -> list[dict]:
    """«Чаще всего добавляют в избранное» — free demand research for the
    dashboard (CLIENT_BOTS.md §3.6)."""
    if not _bots_enabled():
        return []
    from apps.inbox.services import top_favourited

    return [{"variant": v, "count": n} for v, n in top_favourited(limit=5)]


def _orders_open_summary() -> dict:
    """«Заказы в работе»: count + total value of open production orders
    (новый/в производстве/готов), in KGS — orders can be in different
    currencies, so a single total needs a display-time conversion, same
    principle as today_summary() for sales."""
    from apps.core.currency import to_base
    from apps.orders.models import Order

    today = timezone.localdate()
    orders = Order.objects.filter(status__in=Order.OPEN_STATUSES).prefetch_related("items")
    count = 0
    value = Decimal("0")
    for order in orders:
        count += 1
        converted = to_base(order.total, order.currency, today)
        if converted is not None:
            value += converted
    return {"count": count, "value": value}


def _queue_top(limit: int = 5) -> list[dict]:
    """Top variants by production need, for the dashboard link-out to the
    full «К производству» queue — same derived computation, just truncated."""
    from apps.orders.services import production_queue

    rows = [r for r in production_queue() if not r["covered"]]
    rows.sort(key=lambda r: r["to_produce"], reverse=True)
    return rows[:limit]


# --- Export: the same dashboard_data(), flattened into spreadsheet rows --------
# Russian headers, money as real numbers (not strings) so Excel can sum them.
# Called with the SAME cached data the page rendered from — download == screen.

# csv sheet key -> Russian sheet name (also the xlsx tab order)
DASHBOARD_CSV_SHEETS = {
    "svodka": "Сводка",
    "vyruchka": "Выручка",
    "kanaly": "Каналы",
    "oplaty": "Оплаты",
    "tovary": "Топ товаров",
    "dolgi": "Долги",
    "zalezh": "Залежавшийся",
}


def _num(value) -> float:
    return float(Decimal(value).quantize(Decimal("0.01"))) if value is not None else 0.0


def dashboard_sheets(data: dict) -> dict[str, export.Sheet]:
    m = data["metrics"]
    margin = m["profit"]["margin"]

    # Сводка is a label/value pair list rather than a table, so its "value"
    # column deliberately mixes a period label, money, and counts — it stays
    # TEXT-typed and each figure is pre-rounded, since one number format
    # can't serve all three.
    svodka = export.Sheet(
        [export.Column("Показатель"), export.Column("Значение")],
        [
            ["Период", data["period_label"]],
            ["Выручка, сом", _num(m["revenue"]["value"])],
            ["Прибыль, сом", _num(m["profit"]["value"])],
            ["Маржа, %", margin if margin is not None else ""],
            ["Продано, шт", int(m["units"]["value"])],
            ["Долг (текущий), сом", _num(m["debt"]["value"])],
            ["Должников", int(m["debt"]["clients"])],
        ],
    )

    days = sorted(data["day_data"].items())
    vyruchka = export.Sheet(
        [
            export.Column("Дата", export.DATE),
            export.Column("Выручка, сом", export.MONEY),
            export.Column("Продаж", export.INT),
            export.Column("Продано, шт", export.INT),
        ],
        [[d, _num(i["revenue"]), int(i["orders"]), int(i["units"])] for d, i in days],
        [
            "ИТОГО",
            sum(_num(i["revenue"]) for _, i in days),
            sum(int(i["orders"]) for _, i in days),
            sum(int(i["units"]) for _, i in days),
        ],
    )

    kanaly = export.Sheet(
        [export.Column("Канал"), export.Column("Выручка, сом", export.MONEY)],
        [[c["label"], _num(c["value"])] for c in data["channels"]],
        ["ИТОГО", sum(_num(c["value"]) for c in data["channels"])],
    )

    oplaty = export.Sheet(
        [export.Column("Способ оплаты"), export.Column("Сумма, сом", export.MONEY)],
        [[x["label"], _num(x["value"])] for x in data["methods"]],
        ["ИТОГО", sum(_num(x["value"]) for x in data["methods"])],
    )

    tovary = export.Sheet(
        [
            export.Column("Товар"),
            export.Column("Продано, шт", export.INT),
            export.Column("Выручка, сом", export.MONEY),
            export.Column("Прибыль, сом", export.MONEY),
        ],
        [
            [t["name"], int(t["units"]), _num(t["revenue"]), _num(t["profit"])]
            for t in data["top_products"]
        ],
    )

    aging = m["debt"]["aging"]
    dolgi = export.Sheet(
        [export.Column("Срок"), export.Column("Сумма, сом", export.MONEY)],
        [
            ["0–30 дней", _num(aging["d0_30"])],
            ["31–60 дней", _num(aging["d31_60"])],
            ["60+ дней", _num(aging["d60_plus"])],
        ],
        [
            "ВСЕГО",
            _num(aging["d0_30"]) + _num(aging["d31_60"]) + _num(aging["d60_plus"]),
        ],
    )

    zalezh = export.Sheet(
        [
            export.Column("Товар"),
            export.Column("На складе, шт", export.INT),
            export.Column("Дней без продаж", export.TEXT),
            export.Column("Заморожено, сом", export.MONEY),
        ],
        [
            [
                d["name"],
                int(d["units"]),
                "не продавалось" if d["never_sold"] else int(d["days_idle"]),
                _num(d["capital"]),
            ]
            for d in data["dead_stock"]
        ],
        ["ИТОГО", None, None, sum(_num(d["capital"]) for d in data["dead_stock"])],
    )

    return {
        "Сводка": svodka,
        "Выручка": vyruchka,
        "Каналы": kanaly,
        "Оплаты": oplaty,
        "Топ товаров": tovary,
        "Долги": dolgi,
        "Залежавшийся": zalezh,
    }
