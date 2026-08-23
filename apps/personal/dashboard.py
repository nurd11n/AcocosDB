"""apps.personal's ONLY dashboard aggregates — read by exactly one call site
(apps.reports.views._dashboard_context, for the ONE «Личные расходы» card on
the main dashboard) and nowhere else.

Deliberately NEVER merged into apps.reports.dashboard.dashboard_data()'s
return value: that dict is what dashboard_export/dashboard_sheets serialize
into the downloadable workbook, and what apps.reports.views._convert_money
walks for the сом/$/₽ view-currency toggle. Keeping this module's output
OUT of that dict entirely (a separate context key, added after the fact,
never spread into `data`) is what makes it structurally impossible for a
personal expense to reach a business export — not a convention someone has
to remember, a fact about which dict it's in.

That also means these figures always render в сом, deliberately NOT
following the main dashboard's own currency toggle (which only ever touches
`data`) — see templates/reports/_dashboard_panel.html's Личные расходы card.
"""

from decimal import Decimal

from django.db.models import F, Sum

from .models import PersonalExpense


def personal_spent_kgs(start, end) -> Decimal:
    """Every PersonalExpense in the period, any tag, in KGS at each row's own
    frozen rate — the personal-side mirror of
    apps.manufacturing.dashboard.spent_kgs, kept in a completely separate
    module so the two can never accidentally merge into one query."""
    return PersonalExpense.objects.filter(date__range=(start, end)).aggregate(
        t=Sum(F("amount") * F("rate_to_kgs"))
    )["t"] or Decimal("0")


def personal_expenses_by_tag(start, end) -> list[dict]:
    """Per-tag totals, biggest first — what the dashboard card actually
    displays."""
    rows = (
        PersonalExpense.objects.filter(date__range=(start, end))
        .values("tag")
        .annotate(t=Sum(F("amount") * F("rate_to_kgs")))
        .order_by("-t")
    )
    labels = dict(PersonalExpense.TAG_CHOICES)
    return [
        {"tag": r["tag"], "label": labels[r["tag"]], "total": r["t"] or Decimal("0")} for r in rows
    ]
