"""Owner analytics dashboard at /dashboard/.

Owner (superuser) only — Editor/Viewer get 403 and never see the link. Desktop-
first, but stacks cleanly at 390px. Server-rendered Django + HTMX: the period
pills swap ONLY the panel (hx-get -> partial) and push ?period= so the view is
bookmarkable and survives a reload. Charts are server-rendered SVG (see
charts.py) — no chart library, no CSP exception. Aggregates come cached per
(period, catalog_version) for 5 minutes; the cache is invalidated on any sale
confirm / payment via the catalog-version signal (see apps/sales/signals.py).
"""

import calendar as _cal
import copy
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.core.currency import KGS, RUB, USD, get_rate
from apps.core.ratelimit import rate_limit
from apps.inventory.cache import catalog_version

from . import charts, export, storage
from .dashboard import (
    DASHBOARD_CSV_SHEETS,
    _windows,
    dashboard_data,
    dashboard_sheets,
    resolve_period,
)

# Display currencies the dashboard can show. Money is ALWAYS stored and computed
# in сом (KGS); USD/RUB are a view-only «≈» convenience, converted per request at
# today's NBKR rate — the cached aggregates and every export stay in сом.
_CENTS = Decimal("0.01")
# Currency shown as a text abbreviation (сом / USD / RUB), not a glyph.
_CURRENCIES = {
    KGS: {"code": KGS, "symbol": "сом", "approx": False},
    USD: {"code": USD, "symbol": "USD", "approx": True},
    RUB: {"code": RUB, "symbol": "RUB", "approx": True},
}
_CURRENCY_PILLS = [(KGS, "сом"), (USD, "USD"), (RUB, "RUB")]


def resolve_currency(cur: str) -> str:
    """Normalise the ?cur= param to a supported code, defaulting to сом."""
    cur = (cur or KGS).upper()
    return cur if cur in _CURRENCIES else KGS


def _convert_money(data: dict, rate: Decimal) -> dict:
    """A copy of the dashboard aggregates with every MONEY field divided by
    `rate` (сом per 1 unit of the target currency) — display-only. Non-money
    fields (units, margin %, client counts, days idle) are left untouched."""

    def conv(v):
        return (Decimal(v) / rate).quantize(_CENTS, rounding=ROUND_HALF_UP)

    d = copy.deepcopy(data)
    m = d["metrics"]
    m["revenue"]["value"] = conv(m["revenue"]["value"])
    m["profit"]["value"] = conv(m["profit"]["value"])
    m["discount"]["value"] = conv(m["discount"]["value"])
    m["received"]["value"] = conv(m["received"]["value"])
    m["received"]["prev_value"] = conv(m["received"]["prev_value"])
    m["expected"]["value"] = conv(m["expected"]["value"])
    m["spent"]["value"] = conv(m["spent"]["value"])
    m["net_cash"]["value"] = conv(m["net_cash"]["value"])
    m["debt"]["value"] = conv(m["debt"]["value"])
    for k in ("d0_30", "d31_60", "d60_plus"):
        m["debt"]["aging"][k] = conv(m["debt"]["aging"][k])
    for info in d["day_data"].values():
        info["revenue"] = conv(info["revenue"])
    for c in d["channels"]:
        c["value"] = conv(c["value"])
    for x in d["methods"]:
        x["value"] = conv(x["value"])
    for t in d["top_products"]:
        t["revenue"] = conv(t["revenue"])
        t["profit"] = conv(t["profit"])
    for s in d["dead_stock"]:
        s["capital"] = conv(s["capital"])
    for row in d["expenses_by_section"]:
        row["total"] = conv(row["total"])
    return d


def owner_only(view):
    """Every analytics surface (dashboard, storage, exports) is Owner-only:
    an anonymous hit bounces to /login/, an authenticated non-superuser
    (Editor/Viewer) gets a hard 403 — they never see a link to these pages and
    can't reach them by typing the URL either. An authenticated-but-not-yet-
    2FA-verified session (see apps.core.otp) bounces to /login/ too, same as
    anonymous — that session hasn't finished authenticating, it isn't denied
    outright."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        from apps.core.otp import verified

        if not verified(request.user):
            from django.contrib.auth.views import redirect_to_login

            return redirect_to_login(request.get_full_path())
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


_MONTH_RU = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]


def _cal_level(rev, max_rev):
    """Heat level 0-4 for a day's revenue (0 = no sales)."""
    if not rev or rev <= 0:
        return 0
    if not max_rev:
        return 1
    return 1 + min(3, int(float(rev) / float(max_rev) * 4))


def _build_calendar(start, end, day_data, today):
    """Calendar-month grids spanning [start, end]. Each month is a list of cells
    (leading None pads align the 1st to its weekday, Mon-first); each real cell
    carries the day's revenue/units/orders and a heat level. Days outside the
    period are shown for context but marked out-of-range."""
    max_rev = max((info["revenue"] for info in day_data.values()), default=Decimal("0"))
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = _date(y, m, 1)
        cells = [None] * first.weekday()  # Mon=0 lead pad
        for dd in range(1, _cal.monthrange(y, m)[1] + 1):
            dt = _date(y, m, dd)
            info = day_data.get(dt)
            rev = info["revenue"] if info else Decimal("0")
            cells.append(
                {
                    "day": dd,
                    "date": dt.strftime("%d.%m.%Y"),
                    "rev": int(round(rev)),
                    "units": info["units"] if info else 0,
                    "orders": info["orders"] if info else 0,
                    "level": _cal_level(rev, max_rev),
                    "in_range": start <= dt <= end,
                    "today": dt == today,
                    "has": rev > 0,
                }
            )
        months.append({"title": f"{_MONTH_RU[m]} {y}", "cells": cells})
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def _dashboard_context(period, cur):
    data = _cached_data(period)  # сом aggregates, cached in the base currency
    today = timezone.localdate()

    # Личные расходы (apps.personal) — read here ONLY to derive the ONE
    # dashboard card below. Captured from the RAW (always-сом) `data`,
    # before the possible _convert_money reassignment right after this, and
    # NEVER written into `data` itself — dashboard_export/dashboard_sheets
    # only ever see `data`, so this stays structurally unreachable from any
    # export regardless of what happens below. See apps.personal.dashboard's
    # own module docstring for the full reasoning.
    from apps.personal.dashboard import personal_expenses_by_tag, personal_spent_kgs

    p_start, p_end, _prev_start, _prev_end = _windows(period)
    personal_spent = personal_spent_kgs(p_start, p_end)
    personal_by_tag = personal_expenses_by_tag(p_start, p_end)
    net_cash_after_personal = data["metrics"]["net_cash"]["value"] - personal_spent

    # View-only currency: convert a copy at today's rate, or fall back to сом and
    # flag it when no rate is on record for the requested currency.
    rate = Decimal("1") if cur == KGS else get_rate(cur, today)
    rate_missing = cur != KGS and not rate
    if rate_missing:
        cur = KGS
    if cur != KGS:
        data = _convert_money(data, rate)
    meta = _CURRENCIES[cur]

    months = _build_calendar(data["cal_start"], data["cal_end"], data["day_data"], today)

    return {
        **data,
        "calendar_months": months,
        "donut": charts.donut(data["methods"]),
        "channel_bars": charts.bars(data["channels"]),
        "active": "dashboard",
        # currency (view-only): symbol + whether the shown figures are approximate
        "cur": cur,
        "cur_symbol": meta["symbol"],
        "cur_approx": meta["approx"],
        "currencies": _CURRENCY_PILLS,
        "rate_date": today,
        "rate_missing": rate_missing,
        "period_sub": f"за {data['period_label'].lower()}",
        # Личные расходы card — deliberately ALWAYS сом, never following the
        # cur/cur_symbol toggle above (that toggle only ever touches `data`).
        "personal_spent": personal_spent,
        "personal_by_tag": personal_by_tag,
        "net_cash_after_personal": net_cash_after_personal,
    }


def _cached_data(period):
    key = f"dash:{period}:v{catalog_version()}"
    data = cache.get(key)
    if data is None:
        data = dashboard_data(period)
        cache.set(key, data, 300)
    return data


@owner_only
def dashboard(request):
    period = resolve_period(request.GET.get("period", "month"))
    cur = resolve_currency(request.GET.get("cur", KGS))
    context = _dashboard_context(period, cur)
    # HTMX pill swap re-renders only the panel; a normal hit renders the shell.
    template = (
        "reports/_dashboard_panel.html"
        if request.headers.get("HX-Request")
        else "reports/dashboard.html"
    )
    return render(request, template, context)


@rate_limit("report", 10, 300)
@owner_only
def dashboard_export(request):
    """Download the dashboard for the selected period as an xlsx workbook (all
    seven sheets) or one sheet as csv. Owner-only via the decorator; built from
    the SAME cached aggregates the page shows, so the file always matches."""
    period = resolve_period(request.GET.get("period", "month"))
    sheets = dashboard_sheets(_cached_data(period))
    today = timezone.localdate().isoformat()

    if request.GET.get("format") == "csv":
        key = request.GET.get("sheet", "tovary")
        name = DASHBOARD_CSV_SHEETS.get(key)
        if name is None:
            raise PermissionDenied
        content = export.to_csv_bytes(sheets[name])
        resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="dashboard_{key}_{period}_{today}.csv"'
        return resp

    content = export.to_xlsx(sheets)
    resp = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="acocos_dashboard_{period}_{today}.xlsx"'
    return resp


def _storage_data():
    """Cached per catalog version — the same version that gates the product grid
    and the sales dashboard, so any stock movement, sale, or catalog edit
    refreshes this within one bump, never stale for the 5-minute TTL otherwise."""
    key = f"storage:v{catalog_version()}"
    data = cache.get(key)
    if data is None:
        data = storage.storage_data()
        cache.set(key, data, 300)
    return data


@owner_only
def storage_view(request):
    return render(request, "reports/storage.html", {**_storage_data(), "active": "storage"})


@rate_limit("report", 10, 300)
@owner_only
def storage_export(request):
    """Download the storage report as xlsx (both sheets) or a single csv sheet.
    Owner-only via the decorator; the builders live in storage.py so the file
    and the on-screen tables are always the same numbers."""
    today = timezone.localdate().isoformat()
    if request.GET.get("format") == "csv":
        sheet = request.GET.get("sheet", "tovary")
        stub = {"tovary": "sklad_tovary", "kategorii": "sklad_kategorii"}.get(sheet)
        if stub is None:
            raise PermissionDenied
        content = storage.build_csvs()[stub]
        resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{stub}_{today}.csv"'
        return resp

    content = storage.build_xlsx()
    resp = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="acocos_sklad_{today}.xlsx"'
    return resp
