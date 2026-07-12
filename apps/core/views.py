from datetime import timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.shortcuts import render
from django.utils import timezone


@staff_member_required
def stats_view(request):
    """Request counters (from cache) + cached business totals. One page for the owner."""
    panel_section = settings.ADMIN_URL.strip("/").split("/")[0] or "root"
    days = []
    today = timezone.localdate()
    for offset in range(7):
        day = (today - timedelta(days=offset)).isoformat()
        days.append(
            {
                "day": day,
                "total": cache.get(f"reqcount:{day}:total", 0),
                "panel": cache.get(f"reqcount:{day}:{panel_section}", 0),
                "wa": cache.get(f"reqcount:{day}:wa", 0),
            }
        )

    def business_totals():
        # Imported here to keep the view importable before migrations run.
        from apps.clients.services import total_outstanding_debt
        from apps.inventory.services import stock_totals
        from apps.sales.services import today_summary

        return {
            "sales_today": today_summary(),
            "stock": stock_totals(),
            "debt_total": total_outstanding_debt(),
        }

    # Cache the heavy aggregates for 60s so refreshing the page doesn't hammer the DB.
    totals = cache.get_or_set("stats:business_totals", business_totals, 60)
    return render(request, "core/stats.html", {"days": days, "totals": totals})
