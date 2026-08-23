"""Manufacturing tracking: fabric/labor cost, contractors, production runs
with defects, and expenses — see apps/manufacturing/{models,services}.py.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.clients.models import Client
from apps.core.models import ExchangeRate
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.manufacturing.models import Contractor, ContractorTransaction, Expense, ProductionRun
from apps.manufacturing.services import (
    contractor_balance,
    contractor_statement,
    record_expense,
    record_production_run,
)
from apps.sales.models import SaleItem, SaleOrder
from apps.sales.services import confirm_sale

pytestmark = pytest.mark.django_db


@pytest.fixture
def variant():
    cat = Category.objects.create(name="Dresses")
    product = Product.objects.create(category=cat, name="Evening Dress")
    return ProductVariant.objects.create(
        product=product,
        sku="EVD-M-RED",
        size="M",
        color="red",
        cost_price=Decimal("1200.00"),
        sale_price=Decimal("3200.00"),
    )


@pytest.fixture
def contractor():
    return Contractor.objects.create(name="Швея Айгуль", contact="+996700000000")


# ---------------------------------------------------------------------------
# Part 0 — cost_price is always KGS
# ---------------------------------------------------------------------------


def test_migration_converts_only_non_kgs_variants_leaves_kgs_untouched(variant):
    """The one-time normalization (apps/inventory/migrations/
    0009_cost_price_always_kgs.py) must be a no-op for the common case —
    a KGS-currency variant's cost_price is byte-for-byte unchanged, which is
    what actually protects "existing dashboard numbers unchanged" for a
    shop that has never priced anything in a foreign currency. Also asserts
    the audit count: exactly the non-KGS variants get converted, nothing
    else."""
    import importlib

    from django.apps import apps as real_apps

    migration = importlib.import_module("apps.inventory.migrations.0009_cost_price_always_kgs")

    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    usd_variant = ProductVariant.objects.create(
        product=variant.product,
        sku="EVD-M-USD",
        size="M",
        color="blue",
        cost_price=Decimal("20.00"),  # entered as if it were "20 USD"
        sale_price=Decimal("40.00"),
        currency="USD",
    )
    kgs_cost_before = variant.cost_price

    migration.convert_foreign_cost_to_kgs(real_apps, None)

    variant.refresh_from_db()
    usd_variant.refresh_from_db()
    assert variant.cost_price == kgs_cost_before  # untouched — already KGS
    assert usd_variant.cost_price == Decimal("1740.00")  # 20 * 87.00, converted once


def test_usd_fabric_cost_produces_a_kgs_cost_price(variant, contractor):
    """A fabric purchase entered in USD must convert to KGS at the FROZEN
    rate before ever reaching cost_price — never a bare USD number treated
    as сом (apps.reports.dashboard._LINE_COGS reads cost_price as raw KGS
    with no read-time conversion)."""
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))

    record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=10,
        defect_qty=0,
        material_cost=Decimal("50"),  # USD
        labor_cost=Decimal("0"),
        currency="USD",
    )
    variant.refresh_from_db()
    # 50 USD * 87.00 rate / 10 accepted = 435.00 сом/unit — a KGS figure,
    # not the raw "50" a bare-passthrough bug would have produced.
    assert variant.cost_price == Decimal("435.00")


# ---------------------------------------------------------------------------
# Part 2 — production runs, defects, absorption, labor accrual
# ---------------------------------------------------------------------------


def test_defect_rate_is_computed_not_typed(variant, contractor):
    """success_rate is a property derived from accepted_qty/defect_qty —
    there is no field to type it directly, and it can never disagree with
    the two raw counts."""
    run = ProductionRun.objects.create(
        variant=variant, contractor=contractor, accepted_qty=17, defect_qty=3
    )
    assert run.success_rate == Decimal("17") / Decimal("20")
    assert not hasattr(run, "success_rate_raw")
    assert "success_rate" not in [f.name for f in ProductionRun._meta.get_fields()]


def test_defect_cost_is_absorbed_into_good_units_per_unit_cost(variant, contractor):
    """A batch of 20 costing 10 000 сом total that only yields 17 good units
    must cost 588.24/unit, not 500 — the 3 defective units' share of the
    cost is absorbed into the 17 that can actually be sold, never written
    off into nothing."""
    record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=17,
        defect_qty=3,
        material_cost=Decimal("8000"),
        labor_cost=Decimal("2000"),
        currency="KGS",
    )
    variant.refresh_from_db()
    assert variant.cost_price == Decimal("588.24")  # (8000+2000)/17, ROUND_HALF_UP to cents

    movement = StockMovement.objects.get(variant=variant, movement_type=StockMovement.PRODUCTION_IN)
    assert movement.cost == Decimal("588.24")


def test_accepting_writes_production_in_for_accepted_qty_only(variant, contractor):
    """Defective units never become stock — the movement's quantity is
    accepted_qty, never accepted_qty + defect_qty."""
    record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=17,
        defect_qty=3,
        material_cost=Decimal("1000"),
        currency="KGS",
    )
    movement = StockMovement.objects.get(variant=variant, movement_type=StockMovement.PRODUCTION_IN)
    assert movement.quantity == 17
    assert variant.stock == 17


def test_a_total_loss_run_writes_no_movement_but_still_records_the_cost(variant, contractor):
    """accepted_qty=0 (every unit defective): no stock arrives, so no
    PRODUCTION_IN movement and no cost_price update (nothing to divide
    into) — but the money spent is still real and recorded as an Expense,
    never silently dropped."""
    run = record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=0,
        defect_qty=5,
        material_cost=Decimal("1000"),
        currency="KGS",
    )
    assert not StockMovement.objects.filter(
        variant=variant, movement_type=StockMovement.PRODUCTION_IN
    ).exists()
    variant.refresh_from_db()
    assert variant.cost_price == Decimal("1200.00")  # unchanged from the fixture default
    assert Expense.objects.filter(production_run=run, category=Expense.FABRIC).exists()


def test_labor_accrual_moves_the_contractor_balance(variant, contractor):
    """Accepting creates the labor accrual — contractor_balance reflects it
    immediately, positive = shop owes the contractor."""
    assert contractor_balance(contractor) == {}
    record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=10,
        defect_qty=0,
        labor_cost=Decimal("3000"),
        currency="KGS",
    )
    assert contractor_balance(contractor)[contractor.pk] == {"KGS": Decimal("3000.00")}


def test_no_accrual_for_a_fully_rejected_run(variant, contractor):
    """A contractor isn't owed for units the shop rejected — accepted_qty=0
    means no accrual is created even if a labor_cost was entered."""
    record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=0,
        defect_qty=5,
        labor_cost=Decimal("1000"),
        currency="KGS",
    )
    assert contractor_balance(contractor) == {}
    assert not ContractorTransaction.objects.filter(contractor=contractor).exists()


def test_contractor_payment_reduces_balance(variant, contractor):
    record_production_run(
        variant=variant, contractor=contractor, accepted_qty=10, labor_cost=Decimal("3000")
    )
    ContractorTransaction.objects.create(
        contractor=contractor,
        kind=ContractorTransaction.PAYMENT,
        amount=Decimal("1000"),
        currency="KGS",
        rate_to_kgs=Decimal("1"),
        date=timezone.localdate(),
    )
    assert contractor_balance(contractor)[contractor.pk] == {"KGS": Decimal("2000.00")}


def test_contractor_statement_running_balance_matches_contractor_balance(variant, contractor):
    """Same reconciliation guarantee as apps.clients.services.client_statement
    vs. client_debts_by_currency: the statement's closing figure must equal
    the balance function's own number for the unfiltered view."""
    record_production_run(
        variant=variant, contractor=contractor, accepted_qty=10, labor_cost=Decimal("3000")
    )
    ContractorTransaction.objects.create(
        contractor=contractor,
        kind=ContractorTransaction.PAYMENT,
        amount=Decimal("1200"),
        currency="KGS",
        rate_to_kgs=Decimal("1"),
        date=timezone.localdate(),
    )
    statement = contractor_statement(contractor)
    assert statement["KGS"]["closing"] == contractor_balance(contractor)[contractor.pk]["KGS"]
    assert statement["KGS"]["closing"] == Decimal("1800.00")


# ---------------------------------------------------------------------------
# Part 3 — expenses never touch revenue/received; no double-counting
# ---------------------------------------------------------------------------


def test_expenses_never_enter_revenue_profit_or_received(variant):
    """A pile of expenses across every category must not move revenue,
    profit, or «Получено» by a single сом — apps.reports.dashboard._metrics
    reads only SaleOrder/SaleItem/Payment, never Expense."""
    from apps.reports.dashboard import dashboard_data

    today = timezone.localdate()
    before = dashboard_data("month")["metrics"]

    for category in (Expense.FABRIC, Expense.TRIM, Expense.LABOR, Expense.RENT, Expense.OTHER):
        record_expense(
            date=today, category=category, amount=Decimal("50000"), currency="KGS", variant=variant
        )

    after = dashboard_data("month")["metrics"]
    assert after["revenue"]["value"] == before["revenue"]["value"]
    assert after["profit"]["value"] == before["profit"]["value"]
    assert after["received"]["value"] == before["received"]["value"]
    assert after["expected"]["value"] == before["expected"]["value"]


def test_spent_tile_sums_every_category_but_stays_separate_from_profit(variant):
    """«Потрачено» sums ALL expense categories (unlike overhead_kgs, which
    is аренда-only) — and, regardless of its own value, never feeds into
    the profit figure."""
    from apps.manufacturing.dashboard import spent_kgs
    from apps.reports.dashboard import dashboard_data

    today = timezone.localdate()
    record_expense(date=today, category=Expense.FABRIC, amount=Decimal("1000"), currency="KGS")
    record_expense(date=today, category=Expense.RENT, amount=Decimal("2000"), currency="KGS")

    assert spent_kgs(today, today) == Decimal("3000.00")

    profit_before = dashboard_data("month")["metrics"]["profit"]["value"]
    record_expense(date=today, category=Expense.OTHER, amount=Decimal("9999"), currency="KGS")
    profit_after = dashboard_data("month")["metrics"]["profit"]["value"]
    assert profit_before == profit_after


def test_no_double_counting_between_cogs_and_expense_ledger(variant, contractor):
    """A production run's material+labor cost reaches profit EXACTLY once —
    through cost_price -> frozen SaleItem.cost_price -> COGS on the next
    sale. It must not ALSO reduce profit a second time as a raw expense
    subtraction; apps.manufacturing.dashboard.overhead_kgs (the only thing
    that reduces a profit figure directly) counts ONLY the аренда category,
    which fabric/labor/trim are structurally excluded from."""
    from apps.manufacturing.dashboard import overhead_kgs
    from apps.reports.dashboard import dashboard_data

    today = timezone.localdate()
    record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=10,
        material_cost=Decimal("5000"),
        labor_cost=Decimal("5000"),
        currency="KGS",
    )
    variant.refresh_from_db()
    assert variant.cost_price == Decimal("1000.00")  # (5000+5000)/10

    # Production-category expenses exist...
    assert Expense.objects.filter(category__in=Expense.PRODUCTION_CATEGORIES).exists()
    # ...but contribute NOTHING to overhead_kgs (аренда-only).
    assert overhead_kgs(today, today) == Decimal("0")

    order = SaleOrder.objects.create(currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)

    m = dashboard_data("month")["metrics"]
    # Profit = revenue(3200) − COGS(1000 × 1) = 2200 — the production cost
    # counted exactly once, via cost_price, never a second time as a raw
    # expense subtraction.
    assert m["profit"]["value"] == Decimal("2200.00")


def test_overhead_reduces_net_profit_but_not_gross(variant, contractor):
    """аренда is the ONLY category that reduces a profit figure directly —
    and even then, only the manufacturing dashboard's OWN "net profit after
    overhead" number, never apps.reports.dashboard's gross profit."""
    from apps.manufacturing.dashboard import manufacturing_dashboard_data
    from apps.reports.dashboard import dashboard_data

    from apps.inventory.services import add_movement

    today = timezone.localdate()
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    order = SaleOrder.objects.create(currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)

    gross = dashboard_data("month")["metrics"]["profit"]["value"]
    record_expense(date=today, category=Expense.RENT, amount=Decimal("500"), currency="KGS")

    gross_after = dashboard_data("month")["metrics"]["profit"]["value"]
    assert gross_after == gross  # gross profit untouched by overhead

    data = manufacturing_dashboard_data(today, today, gross)
    assert data["overhead"] == Decimal("500.00")
    assert data["net_profit_after_overhead"] == gross - Decimal("500.00")


# ---------------------------------------------------------------------------
# Historical cost_price is never rewritten
# ---------------------------------------------------------------------------


def test_historical_saleitem_cost_price_never_rewritten_by_later_production(variant, contractor):
    """A sale's frozen SaleItem.cost_price must survive any LATER production
    run that changes ProductVariant.cost_price — profit on a past sale is
    never silently rewritten by cost data that arrives afterward."""
    from apps.inventory.services import add_movement

    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    order = SaleOrder.objects.create(currency="KGS")
    item = SaleItem.objects.create(
        order=order, variant=variant, quantity=1, unit_price=Decimal("3200")
    )
    confirm_sale(order)
    item.refresh_from_db()
    original_frozen_cost = item.cost_price
    assert original_frozen_cost == Decimal("1200.00")  # the fixture's original cost_price

    # A much later production run recomputes cost_price to something else.
    record_production_run(
        variant=variant,
        contractor=contractor,
        accepted_qty=10,
        material_cost=Decimal("9000"),
        currency="KGS",
    )
    variant.refresh_from_db()
    assert variant.cost_price == Decimal("900.00")  # changed going forward

    item.refresh_from_db()
    assert item.cost_price == original_frozen_cost  # the PAST sale is untouched


# ---------------------------------------------------------------------------
# Permissions — Owner-only throughout
# ---------------------------------------------------------------------------


def test_manufacturing_pages_are_owner_only(client, django_user_model, variant, contractor):
    from apps.core.permissions import EDITOR
    from django.contrib.auth.models import Group

    editor_group, _ = Group.objects.get_or_create(name=EDITOR)
    editor = django_user_model.objects.create_user("mfg_editor", "e@e.com", "x" * 12, is_staff=True)
    editor.groups.add(editor_group)
    client.force_login(editor)

    for url in [
        "/manufacturing/",
        "/manufacturing/production/add/",
        "/manufacturing/expenses/",
        "/manufacturing/dashboard/",
        f"/manufacturing/contractors/{contractor.pk}/",
    ]:
        resp = client.get(url)
        assert resp.status_code == 403, f"Editor should be denied {url}, got {resp.status_code}"


def test_manufacturing_admin_is_owner_only(client, django_user_model, contractor):
    from apps.core.permissions import EDITOR
    from django.contrib.auth.models import Group

    editor_group, _ = Group.objects.get_or_create(name=EDITOR)
    editor = django_user_model.objects.create_user(
        "mfg_editor2", "e2@e.com", "x" * 12, is_staff=True
    )
    editor.groups.add(editor_group)
    client.force_login(editor)

    resp = client.get("/panel/manufacturing/contractor/")
    assert resp.status_code in (302, 403)  # redirected or denied, never 200


# ---------------------------------------------------------------------------
# apps.orders.services.mark_produced <-> record_production_run integration
# ---------------------------------------------------------------------------


def test_mark_produced_without_contractor_behaves_exactly_as_before(variant):
    """The plain Editor/Manager quick action («Произведено N шт», no
    contractor) must be UNCHANGED: a bare PRODUCTION_IN movement, no
    ProductionRun, no Expense, no accrual — extending mark_produced must
    never force cost/contractor data onto the simple path."""
    from apps.orders.models import Order, OrderItem
    from apps.orders.services import mark_produced

    cust = Client.objects.create(first_name="Simple", phone="+996700009991")
    order = Order.objects.create(client=cust, currency="KGS")
    item = OrderItem.objects.create(
        order=order, variant=variant, quantity=5, unit_price=Decimal("3200")
    )

    mark_produced(item, 5)

    item.refresh_from_db()
    assert item.produced_qty == 5
    assert not ProductionRun.objects.exists()
    assert not Expense.objects.exists()
    movement = StockMovement.objects.get(variant=variant, movement_type=StockMovement.PRODUCTION_IN)
    assert movement.quantity == 5
    assert movement.cost is None


def test_mark_produced_with_contractor_delegates_to_record_production_run(variant, contractor):
    """The rich Owner-only path («Запустить производство», via
    apps.manufacturing.views.production_add) passes a contractor through
    mark_produced — produced_qty counts ONLY the accepted units (defects
    never fulfil the order), and the full cost/defect/accrual machinery
    still runs, exactly once, through record_production_run."""
    from apps.orders.models import Order, OrderItem
    from apps.orders.services import mark_produced

    cust = Client.objects.create(first_name="Rich", phone="+996700009992")
    order = Order.objects.create(client=cust, currency="KGS")
    item = OrderItem.objects.create(
        order=order, variant=variant, quantity=10, unit_price=Decimal("3200")
    )

    mark_produced(
        item,
        8,
        defect_qty=2,
        contractor=contractor,
        material_cost=Decimal("4000"),
        labor_cost=Decimal("1000"),
    )

    item.refresh_from_db()
    assert item.produced_qty == 8  # defects never count toward the order
    run = ProductionRun.objects.get(order_item=item)
    assert run.accepted_qty == 8 and run.defect_qty == 2
    variant.refresh_from_db()
    assert variant.cost_price == Decimal("625.00")  # (4000+1000)/8
    assert contractor_balance(contractor)[contractor.pk] == {"KGS": Decimal("1000.00")}


# ---------------------------------------------------------------------------
# 3-section expense classification (Зарплата / Материалы / Прочее)
# ---------------------------------------------------------------------------


def test_expenses_group_into_three_sections():
    """ткань+фурнитура -> Материалы, зарплата -> Зарплата, аренда+прочее ->
    Прочее — a DISPLAY grouping; the 5 real categories underneath are
    unaffected (see test_overhead_category_stays_distinct_under_sections)."""
    from apps.manufacturing.dashboard import expenses_by_section

    today = timezone.localdate()
    record_expense(date=today, category=Expense.FABRIC, amount=Decimal("1000"), currency="KGS")
    record_expense(date=today, category=Expense.TRIM, amount=Decimal("500"), currency="KGS")
    record_expense(date=today, category=Expense.LABOR, amount=Decimal("3000"), currency="KGS")
    record_expense(date=today, category=Expense.RENT, amount=Decimal("2000"), currency="KGS")
    record_expense(date=today, category=Expense.OTHER, amount=Decimal("100"), currency="KGS")

    sections = {row["section"]: row["total"] for row in expenses_by_section(today, today)}
    assert sections[Expense.SECTION_MATERIALS] == Decimal("1500.00")  # 1000 + 500
    assert sections[Expense.SECTION_WAGES] == Decimal("3000.00")
    assert sections[Expense.SECTION_OTHER] == Decimal("2100.00")  # 2000 + 100
    # Total unchanged by the regrouping.
    assert sum(sections.values(), Decimal("0")) == Decimal("6600.00")


def test_overhead_category_stays_distinct_under_sections():
    """The 3-section grouping is display-only — аренда must still be the
    ONLY category overhead_kgs counts, even though the UI shows it merged
    with прочее under "Прочее". Confusing these two would make net-profit-
    after-overhead sometimes count прочее and sometimes not."""
    from apps.manufacturing.dashboard import overhead_kgs

    today = timezone.localdate()
    record_expense(date=today, category=Expense.RENT, amount=Decimal("2000"), currency="KGS")
    record_expense(date=today, category=Expense.OTHER, amount=Decimal("500"), currency="KGS")

    assert overhead_kgs(today, today) == Decimal("2000.00")  # RENT only, not RENT+OTHER
    assert Expense.category_section(Expense.RENT) == Expense.SECTION_OTHER
    assert Expense.category_section(Expense.OTHER) == Expense.SECTION_OTHER


def test_expenses_list_page_shows_section_totals(client, django_user_model):
    owner = django_user_model.objects.create_superuser("sect_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    today = timezone.localdate()
    record_expense(date=today, category=Expense.LABOR, amount=Decimal("3000"), currency="KGS")

    body = client.get("/manufacturing/expenses/").content.decode()
    assert "Зарплата" in body
    assert "Материалы" in body
    assert "Прочее" in body


# ---------------------------------------------------------------------------
# apps.notes fully removed
# ---------------------------------------------------------------------------


def test_notes_app_is_fully_removed(client, django_user_model):
    from django.apps import apps as django_apps
    from django.conf import settings

    assert "apps.notes" not in settings.INSTALLED_APPS
    assert not django_apps.is_installed("apps.notes")

    owner = django_user_model.objects.create_superuser("notes-gone-owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    assert client.get("/notes/").status_code == 404
    assert "Заметки" not in client.get("/pos/").content.decode()


# ---------------------------------------------------------------------------
# Main /dashboard/ shows the same 3-section expense classification
# ---------------------------------------------------------------------------


def test_main_dashboard_shows_expense_sections_matching_spent_tile(client, django_user_model):
    """The «Расходы» panel on the MAIN dashboard (not just /manufacturing/
    dashboard/) must sum to exactly the «Потрачено» tile's own figure — two
    different renderings of the same underlying number, never allowed to
    drift apart."""
    owner = django_user_model.objects.create_superuser("maindash_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    today = timezone.localdate()
    record_expense(date=today, category=Expense.LABOR, amount=Decimal("3000"), currency="KGS")
    record_expense(date=today, category=Expense.FABRIC, amount=Decimal("1000"), currency="KGS")
    record_expense(date=today, category=Expense.RENT, amount=Decimal("500"), currency="KGS")

    from apps.reports.dashboard import dashboard_data

    data = dashboard_data("today")
    section_total = sum((row["total"] for row in data["expenses_by_section"]), Decimal("0"))
    assert section_total == data["metrics"]["spent"]["value"] == Decimal("4500.00")

    body = client.get("/dashboard/").content.decode()
    assert "Зарплата" in body
    assert "Материалы" in body
    assert "Прочее" in body


def test_main_dashboard_expense_sections_convert_with_the_currency_toggle(
    client, django_user_model
):
    """?cur=USD must convert the section totals exactly like every other
    money figure on the page — apps.reports.views._convert_money."""
    from apps.core.models import ExchangeRate
    from apps.reports.dashboard import dashboard_data
    from apps.reports.views import _convert_money

    owner = django_user_model.objects.create_superuser("convdash_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    record_expense(
        date=timezone.localdate(), category=Expense.LABOR, amount=Decimal("8700"), currency="KGS"
    )

    data = dashboard_data("today")
    converted = _convert_money(data, Decimal("87.00"))
    wages_row = next(r for r in converted["expenses_by_section"] if r["section"] == "wages")
    assert wages_row["total"] == Decimal("100.00")  # 8700 / 87


# ---------------------------------------------------------------------------
# Expense currency handling — a foreign-currency expense must never be
# silently mis-scaled, and must never be invisible in a сом-totalled list
# ---------------------------------------------------------------------------


def test_kgs_expense_is_never_multiplied_by_a_foreign_rate():
    """The base-currency case, pinned: a сом expense stores rate 1 and an
    amount_kgs equal to what was typed — even with a USD rate on record.
    «Ввёл 1000, показало намного больше» is exactly what a stray rate here
    would look like."""
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=timezone.localdate())
    e = record_expense(
        date=timezone.localdate(),
        category=Expense.RENT,
        amount=Decimal("1000"),
        currency="KGS",
    )
    assert e.rate_to_kgs == Decimal("1")
    assert e.amount_kgs == Decimal("1000")


def test_foreign_expense_converts_at_the_db_rate_and_freezes_it():
    """The rate comes from the ExchangeRate table (the same row the Курс card
    and every payment read), and is FROZEN — moving the rate afterwards must
    never restate an expense already filed."""
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=timezone.localdate())
    e = record_expense(
        date=timezone.localdate(),
        category=Expense.FABRIC,
        amount=Decimal("10"),
        currency="USD",
    )
    assert e.rate_to_kgs == Decimal("87.45")
    assert e.amount_kgs == Decimal("874.50")

    ExchangeRate.objects.filter(currency="USD").update(rate=Decimal("99.00"))
    e.refresh_from_db()
    assert e.rate_to_kgs == Decimal("87.45"), "a filed expense must not follow today's rate"
    assert e.amount_kgs == Decimal("874.50")


def test_foreign_expense_without_a_rate_saves_nothing_and_says_so(client, django_user_model):
    """Same rule record_payment already enforces: no rate on record for a
    foreign currency means save NOTHING, never fall back to a silent 1.0
    (which would file 10 $ as 10 сом — understating it ~87x, invisibly)."""
    owner = django_user_model.objects.create_superuser("norate_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    assert not ExchangeRate.objects.filter(currency="USD").exists()

    resp = client.post(
        "/manufacturing/expenses/",
        {
            "category": Expense.RENT,
            "amount": "10",
            "currency": "USD",
            "date": timezone.localdate().isoformat(),
        },
        follow=True,
    )
    assert Expense.objects.count() == 0, "nothing may be saved without a rate"
    assert "Нет курса" in resp.content.decode()


def test_expense_list_shows_the_som_equivalent_of_a_foreign_row(client, django_user_model):
    """CLAUDE.md's money rule («≈ X сом» beside every non-KGS amount) applied
    here specifically because this list MIXES currencies under сом-converted
    totals: without the ≈, a «10 $» row reads as smaller than a «1 000 сом»
    row while contributing ~9x more to every total above it."""
    owner = django_user_model.objects.create_superuser("equiv_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=timezone.localdate())
    record_expense(
        date=timezone.localdate(),
        category=Expense.FABRIC,
        amount=Decimal("10"),
        currency="USD",
    )
    record_expense(
        date=timezone.localdate(),
        category=Expense.RENT,
        amount=Decimal("1000"),
        currency="KGS",
    )

    body = client.get("/manufacturing/expenses/").content.decode()
    assert "874,50" in body, "foreign row must show its сом equivalent"
    # The KGS row gets no ≈ — it IS сом already, nothing to approximate.
    assert body.count("≈") == 1


def test_expense_form_defaults_to_som_explicitly(client, django_user_model):
    """`selected` on the KGS option, not merely first-in-list: the browser's
    own form-state restore (back/forward, bfcache, autofill) otherwise
    re-selects whatever was last used, so one $ expense silently makes $ the
    default for every expense after it."""
    owner = django_user_model.objects.create_superuser("default_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    body = client.get("/manufacturing/expenses/").content.decode()
    assert '<option value="KGS" selected>' in body


def test_saving_a_foreign_expense_confirms_both_figures(client, django_user_model):
    """The success message states what was actually filed, in both the typed
    currency and сом — a currency chosen by mistake is otherwise invisible
    until it has already inflated every total on the page."""
    owner = django_user_model.objects.create_superuser("confirm_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=timezone.localdate())
    resp = client.post(
        "/manufacturing/expenses/",
        {
            "category": Expense.FABRIC,
            "amount": "10",
            "currency": "USD",
            "date": timezone.localdate().isoformat(),
        },
        follow=True,
    )
    body = resp.content.decode()
    assert "874,50" in body and "≈" in body


def test_panel_never_asks_for_a_hand_typed_rate(client, django_user_model):
    """THE BUG BEHIND «181 000 сом showed as 15 928 000»: rate_to_kgs has no
    model default, so leaving it out of the admin made it a REQUIRED,
    hand-typed field on /panel/'s add form — and a сом amount filed with the
    сом-per-dollar rate typed in (88) is stored as amount × 88. A rate is a
    consequence, never typed (CLAUDE.md)."""
    owner = django_user_model.objects.create_superuser("panel_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    for url in (
        "/panel/manufacturing/expense/add/",
        "/panel/manufacturing/contractortransaction/add/",
    ):
        body = client.get(url).content.decode()
        assert 'name="rate_to_kgs"' not in body, f"{url} still asks for a typed rate"


def test_panel_freezes_the_rate_itself_on_add(client, django_user_model):
    """Saving from /panel/ must derive the rate the same way services.py does
    — and a сом row must come out at rate 1, never inflated."""
    owner = django_user_model.objects.create_superuser("freeze_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    today = timezone.localdate()
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=today)

    client.post(
        "/panel/manufacturing/expense/add/",
        {
            "date": today.isoformat(),
            "category": Expense.LABOR,
            "amount": "181000",
            "currency": "KGS",
            "note": "",
            "_save": "",
        },
    )
    e = Expense.objects.get(amount=Decimal("181000"))
    assert e.rate_to_kgs == Decimal("1")
    assert e.amount_kgs == Decimal("181000"), "a сом expense must never be scaled"

    client.post(
        "/panel/manufacturing/expense/add/",
        {
            "date": today.isoformat(),
            "category": Expense.FABRIC,
            "amount": "10",
            "currency": "USD",
            "note": "",
            "_save": "",
        },
    )
    usd = Expense.objects.get(amount=Decimal("10"))
    assert usd.rate_to_kgs == Decimal("87.45"), "a foreign row still converts at the DB rate"


def test_panel_edit_does_not_restate_a_filed_row(client, django_user_model):
    """Editing a note must not re-derive the rate — a filed expense never
    follows today's rate (same freeze rule as Payment.rate_to_kgs)."""
    owner = django_user_model.objects.create_superuser("edit_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    today = timezone.localdate()
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=today)
    e = record_expense(date=today, category=Expense.FABRIC, amount=Decimal("10"), currency="USD")
    ExchangeRate.objects.filter(currency="USD").update(rate=Decimal("99.00"))

    client.post(
        f"/panel/manufacturing/expense/{e.pk}/change/",
        {
            "date": today.isoformat(),
            "category": Expense.FABRIC,
            "amount": "10",
            "currency": "USD",
            "note": "edited note",
            "_save": "",
        },
    )
    e.refresh_from_db()
    assert e.note == "edited note"
    assert e.rate_to_kgs == Decimal("87.45"), "an edit must keep the original frozen rate"


def test_audit_finds_and_fixes_a_som_row_with_a_bogus_rate(capsys):
    """The corrupted rows already on record: сом with rate 88. `1 сом = 1 сом`
    makes this the one unambiguous case, so --fix may repair it; without the
    flag it only reports."""
    from django.core.management import call_command

    bad = Expense.objects.create(
        date=timezone.localdate(),
        category=Expense.LABOR,
        amount=Decimal("181000"),
        currency="KGS",
        rate_to_kgs=Decimal("88"),
    )
    assert bad.amount_kgs == Decimal("15928000")

    call_command("audit_expenses")
    out = capsys.readouterr().out
    assert f"#{bad.pk}" in out and "WRONG by definition" in out
    assert "Nothing was changed" in out
    bad.refresh_from_db()
    assert bad.rate_to_kgs == Decimal("88"), "a bare report must never modify a row"

    call_command("audit_expenses", "--fix")
    assert "FIXED" in capsys.readouterr().out
    bad.refresh_from_db()
    assert bad.rate_to_kgs == Decimal("1")
    assert bad.amount_kgs == Decimal("181000")


def test_audit_fix_never_touches_a_foreign_row(capsys):
    """--fix repairs base-currency rows ONLY. A foreign row's correct rate is
    whatever was true on its date, which this command cannot know."""
    from django.core.management import call_command

    today = timezone.localdate()
    foreign = Expense.objects.create(
        date=today,
        category=Expense.FABRIC,
        amount=Decimal("10"),
        currency="USD",
        rate_to_kgs=Decimal("1"),
    )
    call_command("audit_expenses", "--fix")
    foreign.refresh_from_db()
    assert foreign.rate_to_kgs == Decimal("1")
    assert "NOT auto-fixed" in capsys.readouterr().out


def test_audit_expenses_flags_an_unconverted_foreign_row(capsys):
    """The diagnostic behind the «показало намного больше» report: a foreign
    row filed at rate 1.0 (the old silent fallback) is understated and must
    be surfaced; a properly-converted one is listed separately for review,
    and neither is ever auto-changed."""
    from django.core.management import call_command

    today = timezone.localdate()
    bad = Expense.objects.create(
        date=today,
        category=Expense.RENT,
        amount=Decimal("10"),
        currency="USD",
        rate_to_kgs=Decimal("1"),  # the old silent fallback
    )
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=today)
    good = record_expense(date=today, category=Expense.FABRIC, amount=Decimal("5"), currency="USD")

    call_command("audit_expenses")
    out = capsys.readouterr().out
    assert f"#{bad.pk}" in out and "UNDERSTATED" in out
    assert f"#{good.pk}" in out
    assert "Nothing was changed" in out
    bad.refresh_from_db()
    assert bad.rate_to_kgs == Decimal("1"), "audit must never modify a row"


def test_audit_expenses_is_silent_when_everything_is_in_som(capsys):
    from django.core.management import call_command

    record_expense(
        date=timezone.localdate(),
        category=Expense.RENT,
        amount=Decimal("1000"),
        currency="KGS",
    )
    call_command("audit_expenses")
    assert "No mis-rated rows found" in capsys.readouterr().out
