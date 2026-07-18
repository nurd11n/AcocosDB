"""Owner statistics dashboard + authorized report downloads.

Everything here is superuser-only: `_superuser_required` returns 302→login for
anonymous users and 403 for a logged-in non-superuser, so the pages can't be
reached just by guessing the URL.
"""

from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

THEME_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def root_redirect(request):
    """/pos/ is the front door now. An unauthenticated hit here still ends up
    at /login/ — @login_required on the /pos/ views handles that redirect, so
    this view doesn't need to duplicate the auth check."""
    return redirect("/pos/")


def set_theme(request):
    """Stores the light/dark choice in a cookie (never localStorage) so the
    next request can render <html data-theme="…"> server-side — no flash of
    the wrong theme. A plain form POST, no JS required; 'auto' clears the
    cookie and falls back to the OS preference via prefers-color-scheme."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    value = request.POST.get("theme")
    next_url = request.POST.get("next", "/pos/")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = "/pos/"
    response = redirect(next_url)
    if value in ("light", "dark"):
        response.set_cookie(
            "theme", value, max_age=THEME_COOKIE_MAX_AGE, samesite="Lax", httponly=True
        )
    else:
        response.delete_cookie("theme")
    return response


def _superuser_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _normalize_to_kgs(by_currency: dict, on_date) -> Decimal:
    """Sum a {currency: amount} dict into one base-currency total, skipping any
    currency that has no exchange rate on or before `on_date` (its contribution
    is simply omitted rather than guessed at)."""
    from apps.core.currency import to_base

    total = Decimal("0")
    for cur, amt in by_currency.items():
        kgs = to_base(amt, cur, on_date)
        if kgs is not None:
            total += kgs
    return total


def _business_snapshot() -> dict:
    """Business snapshot normalized to the base currency (KGS), cached 60s.
    The view converts these base-currency numbers into whatever currency the
    user picked — see stats_view."""
    from django.db.models import Sum

    from apps.clients.services import debtors_report_rows, total_outstanding_debt
    from apps.core.currency import to_base
    from apps.inventory.models import ProductVariant
    from apps.inventory.services import stock_totals
    from apps.sales.services import (
        pending_orders_count,
        revenue_last_n_days,
        sales_by_channel,
        today_summary,
        units_sold_by_variant,
    )

    today = timezone.localdate()

    revenue_today_kgs = _normalize_to_kgs(today_summary()["revenue_by_currency"], today)

    revenue_7d = []
    for d in revenue_last_n_days(7):
        revenue_7d.append(
            {"day": d["day"], "revenue_kgs": _normalize_to_kgs(d["by_currency"], today)}
        )
    rev_max = max((d["revenue_kgs"] for d in revenue_7d), default=Decimal("0")) or Decimal("1")
    for d in revenue_7d:
        d["pct"] = int(d["revenue_kgs"] / rev_max * 100)

    channels = []
    for c in sales_by_channel(30):
        channels.append(
            {"label": c["label"], "revenue_kgs": _normalize_to_kgs(c["by_currency"], today)}
        )
    channels.sort(key=lambda c: c["revenue_kgs"], reverse=True)
    ch_max = max((c["revenue_kgs"] for c in channels), default=Decimal("0")) or Decimal("1")
    for c in channels:
        c["pct"] = int(c["revenue_kgs"] / ch_max * 100)

    debt_total_kgs = _normalize_to_kgs(total_outstanding_debt(), today)

    debtors = []
    for client, currency, debt, _last_payment in debtors_report_rows()[:5]:
        debtors.append(
            {
                "name": client.name,
                "phone": client.phone,
                "debt_kgs": to_base(debt, currency, today) or Decimal("0"),
            }
        )

    # Bestsellers vs. slow movers over 30 days — units, not money, so shown as
    # plain counts. Slow movers = still-in-stock variants that barely (or never)
    # sold, i.e. dead stock to stop reordering; bestsellers = what to restock.
    sold = units_sold_by_variant(30)
    variants = list(
        ProductVariant.objects.filter(is_active=True)
        .select_related("product")
        .annotate(_stock=Sum("movements__quantity"))
    )
    bestsellers = [
        {"name": str(v), "units": sold.get(v.pk, 0)}
        for v in sorted(variants, key=lambda v: sold.get(v.pk, 0), reverse=True)
        if sold.get(v.pk, 0) > 0
    ][:8]
    in_stock = [v for v in variants if (v._stock or 0) > 0]
    slow_movers = [
        {"name": str(v), "units": sold.get(v.pk, 0), "stock": v._stock or 0}
        for v in sorted(in_stock, key=lambda v: sold.get(v.pk, 0))
    ][:8]

    return {
        "sales_today": today_summary(),
        "stock": stock_totals(),
        "debt_total_kgs": debt_total_kgs,
        "pending_orders": pending_orders_count(),
        "revenue_today_kgs": revenue_today_kgs,
        "revenue_7d": revenue_7d,
        "channels": channels,
        "debtors": debtors,
        "bestsellers": bestsellers,
        "slow_movers": slow_movers,
    }


@_superuser_required
def stats_view(request):
    """Business snapshot + request counters, shown in a chosen currency.

    Money is normalized to the base currency internally; the currency + date
    selector (the "calculator") converts every figure for display using that
    date's manual rate. A single rate scales all amounts equally, so the bar
    proportions are unchanged — only the printed numbers convert.
    """
    from django.conf import settings
    from django.utils.dateparse import parse_date

    from apps.core.currency import CURRENCY_CODES, from_base, get_rate

    base = settings.CURRENCY
    today = timezone.localdate()

    days = []
    for offset in range(7):
        day = (today - timedelta(days=offset)).isoformat()
        days.append(
            {
                "day": day,
                "total": cache.get(f"reqcount:{day}:total", 0),
                "wa": cache.get(f"reqcount:{day}:wa", 0),
            }
        )

    snap = cache.get_or_set("stats:business_snapshot", _business_snapshot, 60)

    selected = request.GET.get("cur", base)
    if selected not in CURRENCY_CODES:
        selected = base
    on_date = parse_date(request.GET.get("on", "")) or today
    rate = get_rate(selected, on_date)
    rate_missing = selected != base and rate is None
    cur = base if rate_missing else selected  # fall back so numbers still show

    def money(amount_kgs):
        return from_base(amount_kgs, cur, on_date)

    disp = {
        "currency": cur,
        "selected": selected,
        "on_date": on_date,
        "rate": f"{rate.normalize():f}" if rate else None,  # trim trailing zeros for display
        "rate_missing": rate_missing,
        "revenue_today": money(snap["revenue_today_kgs"]),
        "debt_total": money(snap["debt_total_kgs"]),
        "revenue_7d": [
            {"day": d["day"], "pct": d["pct"], "amount": money(d["revenue_kgs"])}
            for d in snap["revenue_7d"]
        ],
        "channels": [
            {"label": c["label"], "pct": c["pct"], "amount": money(c["revenue_kgs"])}
            for c in snap["channels"]
        ],
        "debtors": [
            {"name": d["name"], "phone": d["phone"], "amount": money(d["debt_kgs"])}
            for d in snap["debtors"]
        ],
    }
    return render(
        request,
        "core/stats.html",
        {
            "days": days,
            "totals": snap,
            "disp": disp,
            "currencies": CURRENCY_CODES,
            "base_currency": base,
        },
    )


_CSV_BUILDERS = {
    "sales": ("_sales_rows", "prodazhi"),
    "stock": ("_stock_rows", "ostatok"),
    "debts": ("_debts_rows", "dolgi"),
    "unreviewed": ("_unreviewed_rows", "ne_provereno"),
}


@_superuser_required
def report_download(request):
    """Download today's report (Продажи/Остаток/Долги/Не проверено) as xlsx or
    csv, on demand. Reuses the same builders as the scheduled
    `send_daily_report` command — always in Russian, same as the scheduled one."""
    from apps.core.management.commands import send_daily_report as report

    fmt = request.GET.get("format", "xlsx")
    today = timezone.localdate().isoformat()

    if fmt == "csv":
        sheet = request.GET.get("sheet", "sales")
        if sheet not in _CSV_BUILDERS:
            raise PermissionDenied
        _builder_name, filename_stub = _CSV_BUILDERS[sheet]
        content = report._build_csvs()[f"acocos_{filename_stub}.csv"]
        resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="acocos_{filename_stub}_{today}.csv"'
        return resp

    content = report._build_xlsx()
    resp = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="acocos_report_{today}.xlsx"'
    return resp
