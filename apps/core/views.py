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
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone


def _superuser_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _business_snapshot() -> dict:
    from apps.clients.services import clients_with_debt, total_outstanding_debt
    from apps.inventory.services import stock_totals
    from apps.sales.services import (
        pending_orders_count,
        revenue_last_n_days,
        sales_by_channel,
        today_summary,
    )

    revenue_7d = revenue_last_n_days(7)
    channels = sales_by_channel(30)
    debtors = sorted(
        (
            {"name": c.name, "phone": c.phone, "debt": c.sales_total - c.payments_total}
            for c in clients_with_debt()
            if c.sales_total - c.payments_total > 0
        ),
        key=lambda d: d["debt"],
        reverse=True,
    )[:5]

    # Pre-scale bars to a 0–100 width so the template stays logic-free.
    rev_max = max((d["revenue"] for d in revenue_7d), default=Decimal("0")) or Decimal("1")
    for d in revenue_7d:
        d["pct"] = int(d["revenue"] / rev_max * 100)
    ch_max = max((c["revenue"] for c in channels), default=Decimal("0")) or Decimal("1")
    for c in channels:
        c["pct"] = int(c["revenue"] / ch_max * 100)

    return {
        "sales_today": today_summary(),
        "stock": stock_totals(),
        "debt_total": total_outstanding_debt(),
        "pending_orders": pending_orders_count(),
        "revenue_7d": revenue_7d,
        "channels": channels,
        "debtors": debtors,
    }


@_superuser_required
def stats_view(request):
    """Business snapshot + request counters. Heavy aggregates cached 60s so a
    refresh doesn't hammer the DB."""
    days = []
    today = timezone.localdate()
    for offset in range(7):
        day = (today - timedelta(days=offset)).isoformat()
        days.append(
            {
                "day": day,
                "total": cache.get(f"reqcount:{day}:total", 0),
                "wa": cache.get(f"reqcount:{day}:wa", 0),
            }
        )

    from django.conf import settings

    snapshot = cache.get_or_set("stats:business_snapshot", _business_snapshot, 60)
    return render(
        request,
        "core/stats.html",
        {"days": days, "totals": snapshot, "currency": settings.CURRENCY},
    )


@_superuser_required
def report_download(request):
    """Download today's report (Sales/Stock/Debts) as xlsx or csv, on demand.
    Reuses the same builders as the scheduled `send_daily_report` command."""
    from apps.core.management.commands import send_daily_report as report

    fmt = request.GET.get("format", "xlsx")
    today = timezone.localdate().isoformat()

    if fmt == "csv":
        sheet = request.GET.get("sheet", "sales")
        builders = {
            "sales": report._sales_rows,
            "stock": report._stock_rows,
            "debts": report._debts_rows,
        }
        if sheet not in builders:
            raise PermissionDenied
        content = report._build_csvs()[f"acocos_{sheet}.csv"]
        resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="acocos_{sheet}_{today}.csv"'
        return resp

    content = report._build_xlsx()
    resp = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="acocos_report_{today}.xlsx"'
    return resp
