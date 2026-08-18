"""M1 (2026-08-18 audit): every revenue-shaped aggregate in apps.sales.services
and apps.reports.dashboard used to hand-repeat `status=CONFIRMED,
is_historical=False` independently — three (really more) places that had to
agree on the same exclusion rule, verified only by them currently agreeing on
today's data. apps.sales.models.SaleOrderQuerySet.confirmed_revenue /
SaleItemQuerySet.confirmed_revenue is now the ONE place that rule lives.

The test below doesn't just check today's numbers match (that was already
true before the refactor, and wouldn't catch a FUTURE regression where
someone bypasses confirmed_revenue() and hand-writes a filter again in one
function). It monkeypatches confirmed_revenue() itself to add a NEW,
hypothetical exclusion rule, and asserts every consumer's output reflects it
— if any function still had its own independent filter, patching the shared
method wouldn't touch that function's output, and this test would fail for
exactly that function.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.clients.models import Client
from apps.core.management.commands.send_daily_report import _sales_sheet
from apps.inventory.models import Category, Product, ProductVariant
from apps.inventory.services import add_movement
from apps.reports.dashboard import _calendar_days, _channels, _dead_stock, _metrics, _top_products
from apps.sales.models import SaleItem, SaleItemQuerySet, SaleOrder, SaleOrderQuerySet
from apps.sales.services import (
    revenue_last_n_days,
    revenue_last_n_days_kgs,
    sales_by_channel,
    sales_by_channel_kgs,
    today_revenue_kgs,
    today_summary,
    todays_confirmed_orders,
    units_sold_by_variant,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def variant():
    cat = Category.objects.create(name="SourceOfTruthCat")
    product = Product.objects.create(category=cat, name="SourceOfTruthDress")
    v = ProductVariant.objects.create(
        product=product,
        sku="SOT-001",
        size="M",
        color="black",
        cost_price=Decimal("1000"),
        sale_price=Decimal("2000"),
    )
    add_movement(v, "purchase_in", 50, reason="test seed")
    return v


def _confirm(variant, quantity=1, unit_price=Decimal("2000"), client=None):
    from apps.sales.services import confirm_sale

    order = SaleOrder.objects.create(client=client, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=quantity, unit_price=unit_price)
    confirm_sale(order)
    return order


@pytest.fixture
def excludable_sale(variant):
    """A perfectly normal confirmed sale, TODAY, that the patch below will
    treat as if it needed to be excluded — proving every consumer actually
    goes through the shared queryset method rather than its own filter."""
    client = Client.objects.create(first_name="ExcludableClient", phone="+996700111222")
    return _confirm(variant, quantity=3, unit_price=Decimal("2000"), client=client)


@pytest.fixture
def control_sale(variant):
    """A second confirmed sale that the patch does NOT exclude — its
    contribution must still show up everywhere, proving the patch isn't
    just breaking every query wholesale."""
    client = Client.objects.create(first_name="ControlClient", phone="+996700333444")
    return _confirm(variant, quantity=5, unit_price=Decimal("2000"), client=client)


@pytest.fixture
def patched_confirmed_revenue(monkeypatch, excludable_sale):
    """Simulates a FUTURE new exclusion rule being added to the one
    canonical place — every consumer must pick it up automatically."""
    excluded_id = excludable_sale.pk

    original_order_qs = SaleOrderQuerySet.confirmed_revenue
    original_item_qs = SaleItemQuerySet.confirmed_revenue

    def patched_order_qs(self):
        return original_order_qs(self).exclude(pk=excluded_id)

    def patched_item_qs(self):
        return original_item_qs(self).exclude(order_id=excluded_id)

    monkeypatch.setattr(SaleOrderQuerySet, "confirmed_revenue", patched_order_qs)
    monkeypatch.setattr(SaleItemQuerySet, "confirmed_revenue", patched_item_qs)
    return excludable_sale


def test_today_summary_respects_the_shared_exclusion(patched_confirmed_revenue, control_sale):
    summary = today_summary()
    # excludable_sale: 3 x 2000 = 6000, control_sale: 5 x 2000 = 10000.
    # Only control_sale's contribution may appear.
    assert summary["revenue_by_currency"]["KGS"] == Decimal("10000.00")
    assert summary["orders"] == 1


def test_today_revenue_kgs_respects_the_shared_exclusion(patched_confirmed_revenue, control_sale):
    assert today_revenue_kgs() == Decimal("10000.00")


def test_todays_confirmed_orders_respects_the_shared_exclusion(
    patched_confirmed_revenue, control_sale
):
    orders = list(todays_confirmed_orders())
    assert [o.pk for o in orders] == [control_sale.pk]


def test_revenue_last_n_days_kgs_respects_the_shared_exclusion(
    patched_confirmed_revenue, control_sale
):
    rows = revenue_last_n_days_kgs(1)
    assert rows[0]["revenue_kgs"] == Decimal("10000.00")


def test_sales_by_channel_kgs_respects_the_shared_exclusion(
    patched_confirmed_revenue, control_sale
):
    rows = sales_by_channel_kgs(1)
    assert sum(r["revenue_kgs"] for r in rows) == Decimal("10000.00")


def test_revenue_last_n_days_respects_the_shared_exclusion(patched_confirmed_revenue, control_sale):
    rows = revenue_last_n_days(1)
    assert rows[0]["by_currency"].get("KGS", Decimal("0")) == Decimal("10000.00")


def test_sales_by_channel_respects_the_shared_exclusion(patched_confirmed_revenue, control_sale):
    rows = sales_by_channel(1)
    total = sum(sum(r["by_currency"].values()) for r in rows)
    assert total == Decimal("10000.00")


def test_units_sold_by_variant_respects_the_shared_exclusion(
    patched_confirmed_revenue, control_sale, variant
):
    units = units_sold_by_variant(1)
    # excludable_sale sold 3 units, control_sale sold 5 — only 5 must count.
    assert units.get(variant.pk, 0) == 5


def test_dashboard_metrics_respects_the_shared_exclusion(patched_confirmed_revenue, control_sale):
    today = date.today()
    metrics = _metrics(today, today, today, today)
    assert metrics["revenue"]["value"] == Decimal("10000.00")


def test_dashboard_channels_respects_the_shared_exclusion(patched_confirmed_revenue, control_sale):
    today = date.today()
    rows = _channels(today, today)
    assert sum(r["value"] for r in rows) == Decimal("10000.00")


def test_dashboard_calendar_days_respects_the_shared_exclusion(
    patched_confirmed_revenue, control_sale
):
    today = date.today()
    by_day = _calendar_days(today, today)
    assert by_day[today]["revenue"] == Decimal("10000.00")
    assert by_day[today]["units"] == 5


def test_dashboard_top_products_respects_the_shared_exclusion(
    patched_confirmed_revenue, control_sale, variant
):
    today = date.today()
    rows = _top_products(today, today)
    total_units = sum(r["units"] for r in rows if r["name"] == variant.product.name)
    assert total_units == 5


def test_dashboard_dead_stock_respects_the_shared_exclusion(monkeypatch, variant):
    """Deliberately NOT using the shared patched_confirmed_revenue/control_sale
    fixtures — control_sale sells the same `variant`, which would mask the
    signal (the variant would look "recently sold" either way). Here
    `variant`'s only-ever sale is the one that gets excluded, so if the
    exclusion propagates, the variant must revert to "never sold"."""
    from apps.sales.services import confirm_sale

    order = SaleOrder.objects.create(currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("2000"))
    confirm_sale(order)

    # Old enough that, once its only sale is excluded, it's unambiguously dead.
    variant.product.created_at = timezone.now() - timedelta(days=200)
    variant.product.save(update_fields=["created_at"])

    def before():
        return {d["name"]: d["never_sold"] for d in _dead_stock(limit=50)}

    assert before().get(str(variant)) is None or before()[str(variant)] is False

    original_item_qs = SaleItemQuerySet.confirmed_revenue

    def patched_item_qs(self):
        return original_item_qs(self).exclude(order_id=order.pk)

    monkeypatch.setattr(SaleItemQuerySet, "confirmed_revenue", patched_item_qs)

    after = {d["name"]: d["never_sold"] for d in _dead_stock(limit=50)}
    assert after[str(variant)] is True


def test_daily_report_sales_sheet_respects_the_shared_exclusion(
    patched_confirmed_revenue, control_sale
):
    sheet = _sales_sheet()
    order_ids = [row[0] for row in sheet.rows]
    assert order_ids == [control_sale.pk]
    assert sheet.totals[9] == Decimal("10000.00")  # revenue_base total


def test_confirmed_revenue_excludes_historical_sales(variant):
    """Sanity check the underlying method itself, independent of the
    monkeypatch machinery above — this is the REAL rule, not a simulated one."""
    from apps.sales.services import confirm_sale

    normal = _confirm(variant, quantity=1)
    historical_order = SaleOrder.objects.create(currency="KGS")
    SaleItem.objects.create(
        order=historical_order, variant=variant, quantity=1, unit_price=Decimal("2000")
    )
    confirm_sale(historical_order, is_historical=True, historical_date=date(2023, 1, 1))

    ids = set(SaleOrder.objects.confirmed_revenue().values_list("pk", flat=True))
    assert normal.pk in ids
    assert historical_order.pk not in ids


def test_confirmed_revenue_does_not_exclude_historical_from_debt_queries(variant):
    """The flip side, equally important: is_historical must NOT be excluded
    from debt/balance queries — SaleOrderQuerySet.confirmed_revenue() is
    revenue-specific and must never be (mis)applied to a debt query. This
    just documents that the plain `objects.filter(status=CONFIRMED)` a debt
    query uses still sees a historical sale, since nothing routes debt logic
    through confirmed_revenue()."""
    from apps.sales.services import confirm_sale

    client = Client.objects.create(first_name="DebtHistClient", phone="+996700555666")
    order = SaleOrder.objects.create(client=client, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("2000"))
    confirm_sale(order, is_historical=True, historical_date=date(2023, 1, 1))

    plain_confirmed = SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client=client)
    assert order.pk in plain_confirmed.values_list("pk", flat=True)
    revenue_only = SaleOrder.objects.confirmed_revenue().filter(client=client)
    assert order.pk not in revenue_only.values_list("pk", flat=True)
