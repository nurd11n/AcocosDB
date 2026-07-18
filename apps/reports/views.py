"""Owner analytics dashboard at /dashboard/.

Owner (superuser) only — Editor/Viewer get 403 and never see the link. Desktop-
first, but stacks cleanly at 390px. Server-rendered Django + HTMX: the period
pills swap ONLY the panel (hx-get -> partial) and push ?period= so the view is
bookmarkable and survives a reload. Charts are server-rendered SVG (see
charts.py) — no chart library, no CSP exception. Aggregates come cached per
(period, catalog_version) for 5 minutes; the cache is invalidated on any sale
confirm / payment via the catalog-version signal (see apps/sales/signals.py).
"""

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from apps.inventory.cache import catalog_version

from . import charts
from .dashboard import PERIODS, dashboard_data, resolve_period

# «↑ 12% <phrase>» — names the baseline so a delta is never a bare number.
_BASELINE_PHRASE = {
    "today": "ко вчера",
    "month": "к прошлому месяцу",
    "3m": "к пред. 3 мес",
    "6m": "к пред. 6 мес",
    "year": "к прошлому году",
}
# Which direction is GOOD for each metric — colour is by MEANING, not direction.
_UP_IS_GOOD = {"revenue": True, "profit": True, "units": True, "debt": False}
_GRAN_WORD = {"hour": "часам", "day": "дням", "week": "неделям", "month": "месяцам"}


def _delta_view(delta, up_is_good, phrase):
    """Presentation for a metric delta: colour token + arrow glyph + baseline
    text. Colour never stands alone (a glyph always accompanies it)."""
    if not delta["has_baseline"]:
        return {"has": False, "token": "text-3", "text": "нет данных за прошлый период"}
    pct = delta["pct"]
    if pct == 0:
        return {"has": True, "pct": 0, "arrow": "", "token": "text-2", "text": phrase}
    up = pct > 0
    good = up if up_is_good else not up
    return {
        "has": True,
        "pct": abs(pct),
        "arrow": "↑" if up else "↓",
        "token": "paid" if good else "debt",
        "text": phrase,
    }


def _dashboard_context(period):
    data = _cached_data(period)
    metrics = data["metrics"]
    phrase = _BASELINE_PHRASE[period]
    for key in ("revenue", "profit", "units", "debt"):
        metrics[key]["delta_view"] = _delta_view(metrics[key]["delta"], _UP_IS_GOOD[key], phrase)

    # Revenue trend for the chart aria-label (accessible finding, not the SVG).
    rev_delta = metrics["revenue"]["delta"]
    gran = PERIODS[period][2]  # single source of truth for granularity
    if rev_delta["has_baseline"] and rev_delta["pct"]:
        trend = f"{'рост' if rev_delta['pct'] > 0 else 'спад'} на {abs(rev_delta['pct'])}%"
    else:
        trend = "без сравнения"
    aria = f"Выручка по {_GRAN_WORD[gran]} за {data['period_label'].lower()}, {trend}"

    return {
        **data,
        "chart": charts.area_line(data["series"]),
        "chart_aria": aria,
        "donut": charts.donut(data["methods"]),
        "channel_bars": charts.bars(data["channels"]),
        "active": "dashboard",
    }


def _cached_data(period):
    key = f"dash:{period}:v{catalog_version()}"
    data = cache.get(key)
    if data is None:
        data = dashboard_data(period)
        cache.set(key, data, 300)
    return data


def dashboard(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())
    if not request.user.is_superuser:
        raise PermissionDenied  # Editor/Viewer never reach the dashboard

    period = resolve_period(request.GET.get("period", "month"))
    context = _dashboard_context(period)
    # HTMX period swap re-renders only the panel; a normal hit renders the shell.
    template = "reports/_dashboard_panel.html" if request.headers.get("HX-Request") else "reports/dashboard.html"
    return render(request, template, context)
